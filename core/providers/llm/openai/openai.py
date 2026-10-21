import httpx
import json
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class _LLMTurnSuperseded(Exception):
    """本轮已被更新的轮次接管 —— 主动断开旧流时抛出。

    与「LLM 真的出错」区分开：上层看到这个异常应该安静退出，不要念错误话术
    （主人要听的是新那轮的回复，不是这句「伊娃有点忙」）。
    """


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model") or config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        # 优化超时设置：默认60秒，避免长时间等待
        timeout = config.get("timeout", 60)
        self.timeout = int(timeout) if timeout else 60
        # 连接超时设置为10秒
        self.connect_timeout = 10

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        # qwen3.x 等 hybrid 模型的思考开关：false 可大幅降低首 token 延迟并稳定工具调用
        # 仅在配置显式给出时下发（None/缺省则不传，避免不兼容端点报错）
        self.enable_thinking = config.get("enable_thinking")
        # 2026-10-06 火山方舟「深度思考」开关。带该标签的模型（doubao-seed-2.0-* 等）
        # **默认开启思考**，实测会把 80 个 token 全花在推理上（finish_reason=length、
        # content 为空），既慢又容易让下游拿不到正文。
        # 配置项 thinking_type: disabled|enabled|auto（显式优先，缺省见 _build_extra_body）
        self.thinking_type = config.get("thinking_type")
        # 关键路径留观测点：确认思考到底开没关（改配置后看日志一眼可知）
        try:
            logger.bind(tag=TAG).info(
                f"[LLM] 思考开关: {self._build_extra_body() or '未设置'}（model={self.model_name}）"
            )
        except Exception:
            pass

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        # 使用优化的超时配置：连接超时10秒，读取超时60秒
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout)
        )

    # 可能被服务端硬性限制的采样参数（不同模型端点规则不同，切换模型时需自愈）
    _RESTRICTABLE_PARAMS = (
        "temperature",
        "top_p",
        "max_tokens",
        "frequency_penalty",
        "presence_penalty",
        "stream_options",
    )

    def _build_extra_body(self):
        """构造思考开关的 extra_body。

        2026-10-06：火山方舟带「深度思考」标签的模型（doubao-seed-2.0-* 等）**默认开启思考**。
        实测副作用：max_tokens 被推理吃光（finish_reason=length）、message 里只有
        reasoning_content 而没有 content，下游取正文会 KeyError。对家庭助手这种
        「短回答 + 工具调用」场景，思考只有坏处，因此**对方舟端点默认关闭**。

        优先级（前者显式配置时覆盖后者）：
          1. thinking_type: disabled|enabled|auto  → 火山方舟格式 {"thinking":{"type":...}}
          2. enable_thinking: true|false           → dashscope/qwen 格式 {"enable_thinking":...}
          3. 未配置且端点是火山方舟            → 默认关闭
          4. 端点是 Ollama（11434）              → {"reasoning_effort":"none"}
        """
        try:
            if self.thinking_type:
                return {"thinking": {"type": str(self.thinking_type)}}
            if self.enable_thinking is not None:
                return {"enable_thinking": self.enable_thinking}
            if self.base_url and "volces.com" in str(self.base_url):
                return {"thinking": {"type": "disabled"}}
            # Ollama 本地端点：GGUF/safetensors 格式的 Qwen3 默认开启思考，
            # 必须传 reasoning_effort=none 才能关闭（实测 2026-10-11）
            if self.base_url and "11434" in str(self.base_url):
                return {"reasoning_effort": "none"}
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[LLM] 思考开关构造失败，按不传处理: {e!r}")
        return None

    def _create_with_param_fallback(self, request_params: dict):
        """发起 chat.completions.create；若采样参数不被当前模型接受则自动修正重试一次。

        背景：后台随时切换云端模型时，各端点对 temperature/top_p 等参数限制不一致
        （例：kimi-for-coding 要求 temperature 只能为 1，火山/豆包部分模型会吃掉 429 额度）。
        旧参数沿用会导致 400 直接被拒，对话链兜底成"伊娃有点忙"。
        此处按错误信息降级：能按提示修正就修正，否则移除该参数，保证换模型即用。
        """
        try:
            return self.client.chat.completions.create(**request_params)
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            error_str = str(e)
            # 只处理参数不合法这类 400，其余错误（401/429/500 等）原样抛出
            if status_code != 400 and "invalid_request_error" not in error_str:
                raise

            target = None
            for key in self._RESTRICTABLE_PARAMS:
                if key in request_params and key in error_str:
                    target = key
                    break
            if target is None:
                raise

            old_value = request_params[target]
            if "only 1 is allowed" in error_str:
                request_params[target] = 1
                action = f"改为 {target}=1"
            else:
                request_params.pop(target, None)
                action = f"移除 {target}"

            logger.bind(tag=TAG).warning(
                f"[LLM参数自愈] 模型 {self.model_name} 拒绝参数 "
                f"{target}={old_value}（{error_str[:160]}），已{action}后重试"
            )
            try:
                return self.client.chat.completions.create(**request_params)
            except Exception as retry_error:
                logger.bind(tag=TAG).error(
                    f"[LLM参数自愈] 重试仍失败: {retry_error}"
                )
                raise

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def response(self, session_id, dialogue, **kwargs):
        import time
        dialogue = self.normalize_dialogue(dialogue)

        # 打印发送给 LLM 的对话大小（用于调试）
        total_chars = sum(len(str(m.get('content', ''))) for m in dialogue)
        logger.bind(tag=TAG).info(f"[LLM请求] 发送 {len(dialogue)} 条消息，总字符数: {total_chars}")

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
        }
        
        # 性能监控：记录请求开始时间
        request_start_time = time.time()
        first_token_logged = False
        
        # 延迟监控：LLM 请求开始
        # 注意：这里无法直接访问 conn，通过 session_id 获取监控器
        from core.utils.latency_monitor import get_monitor
        monitor = get_monitor(session_id)
        monitor.mark('llm_request_start', messages=len(dialogue), chars=total_chars)

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 思考开关（走 extra_body 下发的厂商扩展参数）
        _extra_body = self._build_extra_body()
        if _extra_body:
            request_params["extra_body"] = _extra_body

        try:
            responses = self._create_with_param_fallback(request_params)
        except Exception as e:
            error_str = str(e)
            logger.bind(tag=TAG).error(f"LLM API 调用失败: {error_str}")
            raise

        is_active = True
        in_thinking = False  # 标记是否在思考过程中
        _yielded_chars = 0   # 【加固 2026-09-12】跟踪流式输出总字符数
        for chunk in responses:
            try:
                # 检查是否有错误信息（LM Studio 可能通过流返回错误）
                if hasattr(chunk, 'error'):
                    error_msg = str(chunk.error)
                    logger.bind(tag=TAG).error(f"LLM 流式响应错误: {error_msg}")
                    raise Exception(error_msg)

                delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                content = getattr(delta, "content", "") if delta else ""
            except IndexError:
                content = ""
            
            # 处理 <think> 标签：过滤掉思考过程
            if content:
                if "<think>" in content:
                    in_thinking = True
                    content = content.split("<think>")[0]  # 保留 <think> 之前的内容
                if "</think>" in content:
                    in_thinking = False
                    content = content.split("</think>")[-1]  # 保留 </think> 之后的内容
                
                # 如果在思考过程中，跳过输出
                if in_thinking:
                    continue
            
            # 性能监控：记录首 token 延迟
            if not first_token_logged and content:
                first_token_logged = True
                first_token_latency_ms = (time.time() - request_start_time) * 1000
                logger.bind(tag=TAG).info(f"[LLM性能] 首token延迟: {first_token_latency_ms:.0f}ms, 模型: {self.model_name}")
                
                # 延迟监控：LLM 首 token
                monitor.mark('llm_first_token', latency_ms=first_token_latency_ms)
            
            if content:
                if "actories include: /models/common.py" in content:
                    # LM Studio 错误消息，抛出异常
                    logger.bind(tag=TAG).error(f"LLM 内容包含错误: {content}")
                    raise Exception(f"Context size exceeded: {content}")
                if "/models/common.py" in content or "Context size" in content:
                    # LM Studio 错误消息，抛出异常
                    logger.bind(tag=TAG).error(f"LLM 内容包含错误: {content}")
                    raise Exception(f"Context size exceeded: {content}")
                if "base_logprob" in content or "Error Data" in content:
                    # 其他 LM Studio 错误格式
                    logger.bind(tag=TAG).error(f"LLM 内容包含错误: {content}")
                    raise Exception(f"LLM error: {content}")
                if "<|start|>" in content:
                    is_active = False
                    content = content.split("<|start|>")[0]
                if "<|end|>" in content:
                    is_active = True
                    content = content.split("<|end|>")[-1]
                if is_active:
                    _yielded_chars += len(content)
                    yield content

        # 【加固 2026-09-12】流结束零内容兜底警告：
        # LM Studio 配置缺 /v1 时返回 HTTP 200 + JSON 错误体，SDK 流解析器
        # 把它当成非 SSE 响应 → 0 个 chunk → 对话链收到空回复 → TTS 报
        # "Please ensure input text is valid"。此处显式警告，让日志一眼看出。
        if _yielded_chars == 0:
            logger.bind(tag=TAG).warning(
                f"[LLM加固] 流结束但零字符输出 model={self.model_name} "
                f"base_url={self.base_url} — 可能是端点返回了非 SSE 的 200 JSON，"
                f"检查 base_url 是否缺 /v1 后缀、模型名是否正确"
            )

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        import time
        dialogue = self.normalize_dialogue(dialogue)

        # 打印发送给 LLM 的对话大小（用于调试）
        total_chars = sum(len(str(m.get('content', ''))) for m in dialogue)
        logger.bind(tag=TAG).info(f"[LLM请求] 发送 {len(dialogue)} 条消息，总字符数: {total_chars}, 函数数量: {len(functions) if functions else 0}")

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
            # 2026-10-11 观测：要末尾 usage 块才能看到提示词缓存命中（cached_tokens）。
            # 端点不支持时由 _create_with_param_fallback 自动移除该参数重试。
            "stream_options": {"include_usage": True},
        }
        
        # 性能监控：记录请求开始时间
        request_start_time = time.time()
        first_token_logged = False
        
        # 延迟监控：LLM 请求开始
        from core.utils.latency_monitor import get_monitor
        monitor = get_monitor(session_id)
        monitor.mark('llm_request_start', messages=len(dialogue), chars=total_chars, functions=len(functions) if functions else 0)
        # 2026-10-11 观测：首条 system 前 200 字（定位改名/人设注入问题）
        try:
            _sys0 = next((m for m in dialogue if m.get('role') == 'system'), None)
            if _sys0:
                logger.bind(tag=TAG).info(
                    f"[LLM请求] system 前200字: {str(_sys0.get('content',''))[:200]}"
                )
        except Exception:
            pass

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 思考开关（走 extra_body 下发的厂商扩展参数）
        _extra_body = self._build_extra_body()
        if _extra_body:
            request_params["extra_body"] = _extra_body

        try:
            stream = self._create_with_param_fallback(request_params)
        except Exception as e:
            error_str = str(e)
            logger.bind(tag=TAG).error(f"LLM API 调用失败: {error_str}")
            raise

        in_thinking = False  # 标记是否在思考过程中
        _yielded_chars_fn = 0  # 【加固 2026-09-12】
        _yielded_tool_fn = 0   # 2026-09-28：纯 tool_calls 响应（content 为空）是合法的，
                               # 原警告只看 content 字符，误报并掩盖了真正的工具调用。
        # [2026-10-12 根治「线程池被占满」] 外部取消探针。
        # 背景：abort 只置 client_abort + 清音频队列，**停不掉已经发出去的 LLM 请求**。
        # 于是旧 chat() 线程仍卡在 `for chunk in stream:` 等云端返回（豆包首 token
        # 1~3s），期间主人连说几句就每人占一个 worker，5 个 worker 全满 →
        # 之后所有轮次在 ThreadPoolExecutor 里无限排队（症状：只有 ASR 字幕、
        # 没 LLM 输出、没 TTS，刷新网页也没用）。
        # 修法：每轮 chat 开始时由 receiveAudioHandle 埋一个 abort_probe
        # （判据 = 当前线程对应的轮次序号已不是最新 → 说明有更新的轮次接手）。
        # 这里每个 chunk 检查一次，一旦判定过期立刻断开流并退出，
        # **worker 立即释放**，新轮不再需要等旧轮跑完。
        _abort_probe = getattr(self, "abort_probe", None)
        _stale_checked = False
        for chunk in stream:
            if _abort_probe is not None and not _stale_checked:
                try:
                    if _abort_probe():
                        logger.bind(tag=TAG).info(
                            "[LLM中断] 检测到新一轮已接管，主动断开旧流（释放执行线程）"
                        )
                        try:
                            stream.close()
                            stream.response.close()
                        except Exception:
                            pass
                        raise _LLMTurnSuperseded()
                except _LLMTurnSuperseded:
                    raise
                except Exception:
                    pass
                _stale_checked = True
            # 检查是否有错误信息
            if hasattr(chunk, 'error'):
                error_msg = str(chunk.error)
                logger.bind(tag=TAG).error(f"LLM 流式响应错误: {error_msg}")
                raise Exception(error_msg)

            if getattr(chunk, "choices", None):
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", "")
                tool_calls = getattr(delta, "tool_calls", None)
                
                # 处理 <think> 标签：过滤掉思考过程
                if content:
                    if "<think>" in content:
                        in_thinking = True
                        content = content.split("<think>")[0]  # 保留 <think> 之前的内容
                    if "</think>" in content:
                        in_thinking = False
                        content = content.split("</think>")[-1]  # 保留 </think> 之后的内容
                    
                    # 如果在思考过程中，跳过输出
                    if in_thinking:
                        content = ""
                
                # 性能监控：记录首 token 延迟
                if not first_token_logged and (content or tool_calls):
                    first_token_logged = True
                    first_token_latency_ms = (time.time() - request_start_time) * 1000
                    logger.bind(tag=TAG).info(f"[LLM性能] 首token延迟: {first_token_latency_ms:.0f}ms, 模型: {self.model_name}, 函数调用: {tool_calls is not None}")
                    
                    # 延迟监控：LLM 首 token
                    monitor.mark('llm_first_token', latency_ms=first_token_latency_ms)
                # 检查内容中的错误消息
                if content and ("Context size" in content or "/models/common.py" in content or "Error Data" in content):
                    logger.bind(tag=TAG).error(f"LLM 内容包含错误: {content}")
                    raise Exception(f"Context size exceeded: {content}")
                _yielded_chars_fn += len(content or "")
                if tool_calls:
                    _yielded_tool_fn += 1
                yield content, tool_calls
            elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                usage_info = getattr(chunk, "usage", None)
                _details = getattr(usage_info, "prompt_tokens_details", None)
                _cached = getattr(_details, "cached_tokens", None) if _details else None
                logger.bind(tag=TAG).info(
                    f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                    f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                    f"共计 {getattr(usage_info, 'total_tokens', '未知')}，"
                    f"缓存命中 {_cached if _cached is not None else '未知'}"
                )

        # 【加固 2026-09-12】流结束零内容兜底警告（含工具调用路径）
        if _yielded_chars_fn == 0 and _yielded_tool_fn == 0:
            # 调试：落盘最后一次请求体，用于定位"零输出"根因（2026-09-28）
            try:
                with open("/tmp/llm_zero_request.json", "w", encoding="utf-8") as _df:
                    json.dump(request_params, _df, ensure_ascii=False, indent=1)
            except Exception:
                pass
            logger.bind(tag=TAG).warning(
                f"[LLM加固-functions] 流结束但零字符输出 model={self.model_name} "
                f"base_url={self.base_url} — 检查端点/模型名/鉴权"
            )
