# J2：LLM 成本/复杂度路由
# 简单请求（寒暄/应答）→ 本地小模型（快、零成本）；复杂/工具/长文本 → 云端大模型
# 用法：config.yaml 中 LLM 下新建 type: routing 的配置，selected_module.LLM 指向它
from __future__ import annotations

import re

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

# 简单请求判定：超过该字数直接走云端
DEFAULT_SIMPLE_MAX_CHARS = 20

# 简单请求模式（命中走本地）：寒暄、应答、身份确认等无需复杂推理
DEFAULT_SIMPLE_PATTERNS = "|".join(
    [
        # 问候
        r"^(你好|您好|哈喽|嗨|hi|hello|喂)(呀|啊|啦|了)?[~～，,。.！!？? ]*$",
        r"^(早上好|中午好|下午好|晚上好|早安|午安|晚安)[~～，,。.！!？? ]*$",
        # 带称呼的问候（AI 名字动态拼接，见 _name_greeting_pattern）
        r"^(你好呀|嗨你)[好呀啊]*[~～，,。.！!？? ]*$",
        # 应答语气词
        r"^(嗯+|哦+|噢+|啊+|哈+|哎)[~～。.！!]*$",
        # 应答确认
        r"^(好的|好吧|可以|行|对|是的|不是|没错|没问题|收到|明白|知道|知道了|好嘞|好呀|好)[呀啊啦嘛哈~～]*[。.~～！!？?]*$",
        # 致谢/告别
        r"^(谢谢|多谢|感谢|辛苦了|麻烦你了?|再见|拜拜|告辞|晚安啦)[呀啊啦~～]*[。.~～！!]*$",
        # 身份/能力确认（无需工具、无实时信息）
        r"^(你是谁|你叫什么(名字)?|你是什么|你的名字|自我介绍(一下)?|你能干什么|你能做什么|你会什么|你会干(什么|嘛))[\s?？。.！!]*$",
        # 在线确认
        r"^(在吗|在不在|在的|在呢|睡了(吗|没)?|醒了(吗)?|听得到(吗)?|听得见(吗)?)[\s?？]*$",
        # 轻娱乐（本地可答）
        r"^(讲个笑话|说个笑话|来个笑话|唱个歌|唱首歌|给我唱首歌)[吧~～]?$",
    ]
)

# 复杂请求模式（命中强制走云端）：推理/解释/家务操作工具/长句
DEFAULT_COMPLEX_PATTERN = (
    r"为什么|怎么回事|怎么样|怎么办|怎么弄|如何|怎样|解释|分析|总结|概括|介绍|比较|区别|"
    r"帮我|帮忙|麻烦你|请你|"
    # 家居操作（控制类不能出错，走云端大模型）
    r"打开|关掉|关闭|开灯|关灯|亮度|调高|调低|音量|"
    r"天气|温度|湿度|提醒|闹钟|定时|播放|暂停|切歌|下一首|"
    r"空调|窗帘|灯光|电视|扫地机器人|门锁|摄像头|"
    r"新闻|搜索|查(一)?下|查查|计算|翻译|写(一)?(篇|个|首)|"
    r"讲个故事|说个故事"
)


# 口语归一化：ASR 转写常带前缀称呼（你/我/你给我）或句尾语气词，去后再匹配简单模式
STRIP_PREFIX_RE = re.compile(r"^(你给我|给我|请|你们|咱们|你|我)+")
STRIP_SUFFIX_RE = re.compile(r"(了|呀|啊|吧|哦|啦|哈|呢|嘛)+$")


def _name_greeting_pattern() -> str:
    """生成"你好<AI名字>"形式的问候模式。

    AI 名字取自后台设置（core.utils.assistant_identity），后台改名后自动同步；
    避免在正则里写死具体名字导致改名后问候词失效。

    [2026-10-06] 原来取不到名字时回退到写死「伊娃」的正则。助手名因部署而异，
    兜底不该念别人的名字 —— 取不到就返回空串（不匹配任何问候，让它走正常
    LLM 链路，比误认成唤醒词更安全）。
    """
    try:
        from core.utils.assistant_identity import get_name

        name = (get_name() or "").strip()
    except Exception:
        name = ""
    if not name:
        return r"^(?!)"  # 永不匹配
    esc = re.escape(name)
    return rf"^(你好{esc}|{esc}你好)[好呀啊]*[~～，,。.！!？? ]*$"


class LLMProvider(LLMProviderBase):
    """双模型路由：按请求复杂度选择本地小模型或云端大模型。

    配置示例：
        routing-hybrid:
            type: routing
            cloud:
                type: openai
                base_url: https://coding.dashscope.aliyuncs.com/v1
                api_key: ${DASHSCOPE_CODING_KEY}
                model_name: qwen3.6-plus
            local:
                type: openai
                base_url: http://127.0.0.1:1234/v1
                api_key: lm-studio
                model_name: qwen3-4b-instruct-2507
            mode: classify | delegate
        classify = 按复杂度自动分流（默认，兼容现有行为）
        delegate  = 始终用本地模型，cloud_tools 中的工具由
                    UnifiedToolHandler 拦截后委托给云端模型处理
    """

    def __init__(self, config):
        from core.utils.llm import create_instance

        cloud_cfg = dict(config.get("cloud") or {})
        local_cfg = dict(config.get("local") or {})
        if not cloud_cfg:
            raise ValueError("LLM 路由(routing)配置缺少 cloud 子配置")
        if not local_cfg:
            raise ValueError("LLM 路由(routing)配置缺少 local 子配置")

        self.cloud = create_instance(cloud_cfg.get("type", "openai"), cloud_cfg)
        self.local = create_instance(local_cfg.get("type", "openai"), local_cfg)

        self.mode = (config.get("mode") or "classify").lower()

        self.simple_max_chars = int(config.get("simple_max_chars", DEFAULT_SIMPLE_MAX_CHARS))
        simple_patterns = config.get("simple_patterns") or DEFAULT_SIMPLE_PATTERNS
        if isinstance(simple_patterns, list):
            simple_patterns = "|".join(str(p).strip() for p in simple_patterns if str(p).strip())
        # 动态追加"你好<AI名字>"问候（名字取自后台设置，改名后自动同步）
        simple_patterns = f"{simple_patterns}|{_name_greeting_pattern()}"
        self._simple_re = re.compile(simple_patterns)
        self._complex_re = re.compile(config.get("complex_pattern") or DEFAULT_COMPLEX_PATTERN)

        self.tool_calls_provider = (config.get("tool_calls_provider") or "auto").lower()
        self.fallback_to_cloud = bool(config.get("fallback_to_cloud", True))

        logger.bind(tag=TAG).info(
            f"LLM 路由初始化完成: mode={self.mode}, "
            f"local={getattr(self.local, 'model_name', '?')}@{getattr(self.local, 'base_url', '?')}, "
            f"cloud={getattr(self.cloud, 'model_name', '?')}@{getattr(self.cloud, 'base_url', '?')}, "
            f"tool_calls_provider={self.tool_calls_provider}, fallback_to_cloud={self.fallback_to_cloud}"
        )

    @staticmethod
    def _last_user_text(dialogue):
        """取最后一条真实用户消息（跳过 chat_engine 注入的工具提示临时消息）"""
        if not dialogue:
            return ""
        for msg in reversed(dialogue):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content") or ""
            if "当前可用工具" in content or "仅当用户请求" in content:
                continue
            return content.strip()
        return ""

    def classify(self, text):
        """返回 (级别, 原因)，级别: local | cloud"""
        if not text:
            return "cloud", "空消息"
        if len(text) > self.simple_max_chars:
            return "cloud", f"文本较长({len(text)}字)"
        if self._complex_re.search(text):
            return "cloud", "含复杂/操作意图词"
        normalized = STRIP_SUFFIX_RE.sub("", STRIP_PREFIX_RE.sub("", text.strip()).strip())
        if self._simple_re.match(text) or self._simple_re.match(normalized):
            return "local", "寒暄/应答"
        return "cloud", "默认云端"

    def _select(self, dialogue, with_functions):
        # delegate 模式：始终用本地模型，复杂工具由 handle_llm_function_call 拦截后委托云端
        if self.mode == "delegate":
            return self.local
        text = self._last_user_text(dialogue)
        level, reason = self.classify(text)
        if with_functions:
            # 工具请求路由策略：cloud=全走云端 | local=全走本地 | auto=按复杂度
            if self.tool_calls_provider == "cloud":
                level = "cloud"
            elif self.tool_calls_provider == "local":
                level = "local"
        provider = self.local if level == "local" else self.cloud
        logger.bind(tag=TAG).info(
            f"[LLM路由] -> {level} ({reason}) | 工具:{'是' if with_functions else '否'} | "
            f"模型:{provider.model_name} | 查询:{text[:40]}"
        )
        return provider

    def _call(self, provider, session_id, dialogue, with_functions, functions, kwargs):
        if with_functions:
            return provider.response_with_functions(session_id, dialogue, functions=functions)
        return provider.response(session_id, dialogue, **kwargs)

    def _stream(self, session_id, dialogue, with_functions, functions, kwargs):
        provider = self._select(dialogue, with_functions)
        gen = self._call(provider, session_id, dialogue, with_functions, functions, kwargs)
        try:
            first = next(gen)
        except Exception as e:
            # 本地首 token 失败 → 回退云端重试（首个 token 未发出，无重复内容风险）
            if provider is self.cloud or not self.fallback_to_cloud:
                raise
            logger.bind(tag=TAG).warning(f"[LLM路由] 本地模型请求失败({e})，回退云端")
            gen = self._call(self.cloud, session_id, dialogue, with_functions, functions, kwargs)
            first = next(gen)
        yield first
        yield from gen

    def response(self, session_id, dialogue, **kwargs):
        yield from self._stream(session_id, dialogue, with_functions=False, functions=None, kwargs=kwargs)

    def response_with_functions(self, session_id, dialogue, functions=None):
        yield from self._stream(session_id, dialogue, with_functions=True, functions=functions, kwargs={})
