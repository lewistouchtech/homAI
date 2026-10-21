"""飞书事件长连接监听器（HomeBrain 用）

核心设计：
  - 用 lark-oapi 的 WebSocket 长连接模式（`lark.ws.Client`）接收事件
    • 无需公网 URL / ngrok —— 机器人主动连到飞书云
    • SDK 内部自动重连
  - MVP 范围：只处理主人（白名单）私聊的纯文本消息
  - 绕过 ChatEngine（强绑 WebSocket+TTS），直接调 LLM
  - 每个 open_id 维护独立对话历史（内存，重启清零）

线程模型：
  - SDK 的事件回调在 SDK 自己的线程里
  - 用 `asyncio.run_coroutine_threadsafe` 把任务派回主 asyncio loop
  - LLM 调用是同步 generator，用 `loop.run_in_executor` 避免阻塞

不在本文件范围（后续立项）：
  - 群消息 / @机器人 / 富文本 / 图片 / 文件 / 语音
  - 工具调用（function_call）
  - 记忆集成（OpenViking 注入）
  - L1 快速响应层
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Optional

from config.logger import setup_logging
from core.utils.feishu_client import FeishuClient
from core.utils.assistant_identity import get_name, get_full_prompt

logger = setup_logging()
TAG = "FeishuListener"


def _cache_p2p_chat_id(sender_id: str, chat_id: str) -> None:
    """缓存单聊 chat_id 供 read_feishu_messages 工具查消息。

    飞书 chats 列表接口不返回 p2p 会话，只能在收到消息时把 chat_id 记下来。
    失败不影响主流程（只记 debug 日志）。
    """
    try:
        p = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "data", ".lark_chat_cache.json"))
        cache = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cache = json.load(f) or {}
        if cache.get(sender_id) != chat_id:
            cache[sender_id] = chat_id
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _flatten_post(content: dict) -> tuple[str, list[str]]:
    """递归解析 post 富文本 → (纯文本, 链接列表)。

    飞书 post 结构：{"title": "...", "content": [[{"tag":"text","text":...},
    {"tag":"a","text":...,"href":...}], ...]}，不同端版本字段略有差异，统一递归抓。
    """
    texts: list[str] = []
    urls: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            href = node.get("href") or node.get("url")
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                urls.append(href)
                if node.get("text"):
                    texts.append(str(node["text"]))
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.strip():
            texts.append(node)

    walk(content)
    from core.utils.feishu_inbox import extract_urls  # 已有去重的链接提取
    for u in extract_urls(" ".join(texts)):
        if u not in urls:
            urls.append(u)
    return " ".join(texts), urls


# 注意：lark_oapi 不在模块顶部 import —— SDK 的 client.py 在模块顶部
# `loop = asyncio.get_event_loop()` 抓主 loop 缓存，会污染主线程。
# 延迟到 executor 线程里再 import，让模块级 loop 绑到线程自己的 loop。
# 类型注解靠 `from __future__ import annotations` 转字符串，不需要实际导入。

# 人设统一由后台「AI身份设定」提供（core/utils/assistant_identity.py）。
# 生效 prompt 与语音链路共用一份，后台改动后自动同步，不再硬编码。
FEISHU_STYLE_SUFFIX = "不要使用列表或 markdown 格式（飞书纯文本消息）。"


def get_system_prompt() -> str:
    """飞书端 system prompt：复用后台配置的生效 prompt，缺失时回退到最小人设。"""
    prompt = get_full_prompt()
    if prompt:
        return f"{prompt}\n{FEISHU_STYLE_SUFFIX}"
    return (
        f"你是{get_name()}，homAi 家庭 AI 管家。说话简短自然，像家人聊天。"
        f"{FEISHU_STYLE_SUFFIX}"
    )


# ── loop 引用（SDK 回调线程需要这个来派任务回 asyncio）──────────────
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """app.py 启动时调用，保存主 loop 引用。"""
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop:
    if _main_loop is None:
        raise RuntimeError("飞书 listener 的 main loop 未设置（app.py 启动时漏调 set_main_loop）")
    return _main_loop


# ── 主类 ──────────────────────────────────────────────────────────
class FeishuListener:
    """飞书长连接事件监听器。"""

    def __init__(
        self,
        config: dict,
        client: FeishuClient,
        ws_server,  # 持有 ws_server 引用，动态查 llm/asr/tts —— 后台热切换才生效
        whitelist_open_ids: set[str],
    ):
        self.config = config
        self.client = client
        self.ws_server = ws_server
        self.whitelist = whitelist_open_ids
        feishu_cfg = config.get("feishu", {}) or {}
        self.dedup_ttl = int(feishu_cfg.get("dedup_ttl", 300))
        self.max_history = int(feishu_cfg.get("max_history_turns", 10)) * 2  # 一轮 = user + assistant

        self._seen: OrderedDict[str, float] = OrderedDict()  # message_id → ts
        self._dialogues: dict[str, list[dict]] = {}           # open_id → dialogue history
        self._ws_client: Optional[lark.ws.Client] = None  # type: ignore  # noqa: F821 (late import)

        # 2026-10-11 健康录入管道：注入发送/抽取回调（主动问询巡检共用）
        try:
            from core.utils import health_inbox

            async def _health_send(oid: str, msg: str) -> None:
                await self.client.send_text(oid, msg)

            async def _health_extract(prompt: str) -> str:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None, self._invoke_llm_sync, "health-extract",
                    [{"role": "user", "content": prompt}])

            health_inbox.register(_health_send, _health_extract)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"健康录入管道注册失败（不影响飞书主功能）: {e}")

    # ── 动态访问 ws_server 的模块（支持后台热切换）────────────────
    @property
    def llm(self):
        return getattr(self.ws_server, "_llm", None)

    @property
    def asr(self):
        return getattr(self.ws_server, "_asr", None)

    @property
    def vllm(self):
        return getattr(self.ws_server, "_vllm", None)

    def _create_vllm(self):
        """根据最新配置动态创建 VLLM 实例（视觉模型不常驻，每次调用时创建）。

        配置来源：优先 data/.config.yaml（后台 UI 刚保存/切换的），
        失败则回退启动时的 self.config。
        """
        import os
        import yaml
        from core.utils.vllm import create_instance

        config = self.config
        try:
            server_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            custom_path = os.path.join(server_root, "data", ".config.yaml")
            if os.path.exists(custom_path):
                with open(custom_path, "r", encoding="utf-8") as f:
                    runtime_cfg = yaml.safe_load(f) or {}
                rt_selected = runtime_cfg.get("selected_module") or {}
                if rt_selected.get("VLLM"):
                    config = dict(config)
                    config["selected_module"] = {
                        **(config.get("selected_module") or {}),
                        **rt_selected,
                    }
                    config["VLLM"] = {
                        **(config.get("VLLM") or {}),
                        **(runtime_cfg.get("VLLM") or {}),
                    }
        except Exception as e:
            logger.bind(tag=TAG).warning(f"读取运行时配置失败，用启动配置: {e}")

        select = (config.get("selected_module") or {}).get("VLLM")
        if not select:
            return None
        cfg = (config.get("VLLM") or {}).get(select)
        if not isinstance(cfg, dict):
            return None
        vllm_type = cfg.get("type", select)
        try:
            return create_instance(vllm_type, cfg)
        except Exception as e:
            logger.bind(tag=TAG).error(f"创建视觉模型实例失败: {e}")
            return None

    def _get_tts_config(self) -> dict:
        """从运行时的活跃配置读 TTS（支持后台热切换）。

        优先级：data/.config.yaml（后台 UI 写入）> config.yaml（启动时的默认值）
        返回: {"type": "moss_nano"|"alibl_stream"|..., "api_url": "...", ...}
        """
        import os
        import yaml

        # 1. 尝试读 data/.config.yaml（后台修改后的生效配置）
        active_cfg = {}
        dot_config_path = os.path.join(
            os.path.dirname(__file__) or ".", "..", "..", "data", ".config.yaml"
        )
        if os.path.exists(dot_config_path):
            try:
                with open(dot_config_path) as f:
                    active_cfg = yaml.safe_load(f) or {}
            except Exception as e:
                logger.bind(tag=TAG).warning(f"读 data/.config.yaml 失败: {e}，回退到启动配置")

        # 2. 合并：active_cfg 覆盖 self.config
        tts_section = active_cfg.get("TTS") or self.config.get("TTS") or {}
        selected = (active_cfg.get("selected_module") or self.config.get("selected_module") or {}).get("TTS")

        # 3. 找到选中的 TTS 配置块
        if not selected:
            # 没选就用 MossNano（默认）
            tts_cfg = tts_section.get("MossNano") or {}
            selected = "MossNano"
        else:
            tts_cfg = tts_section.get(selected) or {}

        # 4. 根据 type 返回统一的调用参数
        tts_type = tts_cfg.get("type", "")
        if tts_type == "moss_nano":
            # 音色读后台 tts_config（provider=MossNano 才认），与设备播报同源。
            # 兜底不用 "eva" —— 8086 没这个音色名，会被静默变成默认男声 zh_1。
            return {
                "type": "moss_nano",
                "api_url": tts_cfg.get("api_url", "http://127.0.0.1:8086/api/tts"),
                "voice": _backend_moss_voice() or None,
                "speed": float(tts_cfg.get("speed", 1.0)),
                "volume": float(tts_cfg.get("volume", 5.0)),
            }
        else:
            # 其他 TTS 类型暂不支持语音回复，退回文本
            logger.bind(tag=TAG).warning(f"TTS 类型 {tts_type} 暂不支持飞书语音回复，将用文本")
            return {"type": tts_type or "unknown", "supported": False}

# ── lifecycle ─────────────────────────────────────────────────
    async def start(self) -> None:
        """在 executor 线程里跑阻塞的 cli.start()。

        SDK 的 client.py 在模块顶部 `loop = asyncio.get_event_loop()` 抓了
        主 loop 缓存住，start() 直接用这个缓存 —— 所以必须在延迟 import SDK
        **之前**给线程建独立 loop，让模块级 loop 抓到线程 loop 而不是主 loop。
        """
        main_loop = asyncio.get_event_loop()
        logger.bind(tag=TAG).info(
            f"飞书长连接启动（app_id={self.client.app_id}，白名单 {len(self.whitelist)} 人）"
        )

        def _run_sdk_in_fresh_loop() -> None:
            # 1. 给线程建独立 loop（必须在 import SDK 之前）
            fresh = asyncio.new_event_loop()
            asyncio.set_event_loop(fresh)
            try:
                # 2. 延迟 import SDK —— 模块级 `loop` 会绑到 fresh
                import lark_oapi as lark  # noqa: PLC0415
                from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401

                # 3. SDK 日志桥接到 loguru（在 import 后设置，"Lark" logger 才存在）
                class _LarkLoguruBridge(logging.Handler):
                    def emit(self, record):
                        try:
                            msg = self.format(record)
                            lvl = record.levelno
                            if lvl >= logging.ERROR:
                                logger.bind(tag="LarkSDK").error(msg)
                            elif lvl >= logging.WARNING:
                                logger.bind(tag="LarkSDK").warning(msg)
                            else:
                                logger.bind(tag="LarkSDK").info(msg)
                        except Exception:
                            pass
                _lark_logger = logging.getLogger("Lark")
                if not any(isinstance(h, _LarkLoguruBridge) for h in _lark_logger.handlers):
                    _b = _LarkLoguruBridge()
                    _b.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
                    _lark_logger.addHandler(_b)
                    _lark_logger.setLevel(logging.INFO)
                    _lark_logger.propagate = False

                event_handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(self._on_message)
                    .build()
                )
                self._ws_client = lark.ws.Client(
                    app_id=self.client.app_id,
                    app_secret=self.client.app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.INFO,
                    domain=lark.FEISHU_DOMAIN,
                    auto_reconnect=True,
                )
                logger.bind(tag=TAG).info("飞书 SDK 在线程里初始化，开始连接…")
                self._ws_client.start()  # blocks, uses fresh loop
            except Exception as e:
                logger.bind(tag=TAG).error(f"飞书 SDK 异常: {type(e).__name__}: {e}")
            finally:
                try:
                    fresh.close()
                except Exception:
                    pass

        # daemon 线程里跑，进程退出时自然结束
        await main_loop.run_in_executor(None, _run_sdk_in_fresh_loop)

    async def stop(self) -> None:
        """SDK 没有显式 stop 接口 —— 让 executor 线程随进程退出。"""
        logger.bind(tag=TAG).info("飞书长连接停止")

    # ── SDK 回调（在 SDK 线程里跑）──────────────────────────────
    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """收到消息事件。SDK 回调在 SDK 线程，需要派回主 loop。"""
        try:
            event = data.event
            sender_id = event.sender.sender_id.open_id
            msg = event.message
            msg_id = msg.message_id

            # 1. 白名单
            if sender_id not in self.whitelist:
                logger.bind(tag=TAG).warning(f"拒绝非白名单用户: {sender_id[:10]}…")
                return

            # 2. 去重
            if self._is_duplicate(msg_id):
                logger.bind(tag=TAG).debug(f"重复消息已忽略: {msg_id[:12]}…")
                return

            # 3. MVP：只处理 p2p
            if msg.chat_type != "p2p":
                logger.bind(tag=TAG).info(f"忽略非 p2p 消息: chat_type={msg.chat_type}")
                return

            # 3.5 2026-09-21：缓存单聊 chat_id（read_feishu_messages 工具用）
            _cache_p2p_chat_id(sender_id, getattr(msg, "chat_id", "") or "")

            msg_type = msg.message_type

            # 4. 文本消息
            if msg_type == "text":
                try:
                    content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                    text = (content.get("text") or "").strip()
                except json.JSONDecodeError:
                    logger.bind(tag=TAG).warning(f"消息 content 解析失败: {msg.content[:100]}")
                    return
                if not text:
                    return
                logger.bind(tag=TAG).info(f"入站[文本]: {sender_id[:10]}… → {text[:40]}…")
                asyncio.run_coroutine_threadsafe(
                    self._handle_text(sender_id, text, reply_voice=False),
                    get_main_loop(),
                )
                return

            # 5. 语音消息
            if msg_type == "audio":
                try:
                    content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                    file_key = content.get("file_key")
                    duration_ms = int(content.get("duration") or 0)
                except (json.JSONDecodeError, ValueError):
                    logger.bind(tag=TAG).warning(f"语音消息 content 解析失败: {msg.content[:100]}")
                    return
                if not file_key:
                    return
                logger.bind(tag=TAG).info(
                    f"入站[语音]: {sender_id[:10]}… file_key={file_key[:12]}… duration={duration_ms}ms"
                )
                asyncio.run_coroutine_threadsafe(
                    self._handle_audio(sender_id, msg_id, file_key, duration_ms),
                    get_main_loop(),
                )
                return

            # 6. 图片消息
            if msg_type == "image":
                try:
                    content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                    image_key = content.get("image_key")
                except json.JSONDecodeError:
                    logger.bind(tag=TAG).warning(f"图片消息 content 解析失败: {msg.content[:100]}")
                    return
                if not image_key:
                    return
                logger.bind(tag=TAG).info(f"入站[图片]: {sender_id[:10]}… image_key={image_key[:16]}…")
                asyncio.run_coroutine_threadsafe(
                    self._handle_image(sender_id, msg_id, image_key),
                    get_main_loop(),
                )
                return

            # 7. 文件消息（2026-09-29）→ anydoc 转 Markdown → 「我的收藏」库
            if msg_type == "file":
                try:
                    content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                    file_key = content.get("file_key")
                    file_name = content.get("file_name") or "未命名文件"
                except json.JSONDecodeError:
                    logger.bind(tag=TAG).warning(f"文件消息 content 解析失败: {msg.content[:100]}")
                    return
                if not file_key:
                    return
                logger.bind(tag=TAG).info(
                    f"入站[文件]: {sender_id[:10]}… name={file_name} key={file_key[:12]}…"
                )
                asyncio.run_coroutine_threadsafe(
                    self._handle_file(sender_id, msg_id, file_key, file_name),
                    get_main_loop(),
                )
                return

            # 8. 富文本（post，转发网页卡片常是这种）→ 提取链接走收藏
            if msg_type == "post":
                try:
                    content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                    post_text, post_urls = _flatten_post(content)
                except json.JSONDecodeError:
                    logger.bind(tag=TAG).warning(f"post 消息 content 解析失败: {msg.content[:100]}")
                    return
                if post_urls:
                    logger.bind(tag=TAG).info(
                        f"入站[富文本链接]: {sender_id[:10]}… {len(post_urls)} 个"
                    )
                    asyncio.run_coroutine_threadsafe(
                        self._handle_links(sender_id, post_text, post_urls),
                        get_main_loop(),
                    )
                    return
                if post_text.strip():
                    asyncio.run_coroutine_threadsafe(
                        self._handle_text(sender_id, post_text.strip(), reply_voice=False),
                        get_main_loop(),
                    )
                    return
                return

            # 9. 兜底：尝试从任意类型的 content 里提取链接（share/card 等分享卡片）
            try:
                content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                # 递归提取所有 href/url 字段 + 文本里的链接
                fallback_urls = []
                def _extract_urls_recursive(node):
                    if isinstance(node, dict):
                        for k in ("href", "url", "link"):
                            v = node.get(k)
                            if isinstance(v, str) and v.startswith(("http://", "https://")):
                                fallback_urls.append(v)
                        for v in node.values():
                            _extract_urls_recursive(v)
                    elif isinstance(node, list):
                        for v in node:
                            _extract_urls_recursive(v)
                    elif isinstance(node, str):
                        from core.utils.feishu_inbox import extract_urls
                        for u in extract_urls(node):
                            if u not in fallback_urls:
                                fallback_urls.append(u)
                _extract_urls_recursive(content)
                if fallback_urls:
                    logger.bind(tag=TAG).info(f"入站[兜底链接类型={msg_type}]: {sender_id[:10]}… {len(fallback_urls)} 个")
                    asyncio.run_coroutine_threadsafe(
                        self._handle_links(sender_id, "", fallback_urls),
                        get_main_loop(),
                    )
                    return
            except Exception:
                pass

            # 10. 其他类型（video 等）—— 暂回提示
            asyncio.run_coroutine_threadsafe(
                self.client.send_text(sender_id, "（这类消息暂时不支持，可以发文本/语音/图片/文件/链接）"),
                get_main_loop(),
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"_on_message 异常: {e}")

    # ── 主处理 ────────────────────────────────────────────────────
    async def _handle_text(self, open_id: str, text: str, reply_voice: bool = False) -> None:
        try:
            from core.utils import feishu_inbox as inbox

            # 0. 2026-10-11 健康录入管道/问询闭环：命中健康意图先走结构化抽取
            # （确认卡/问询回复/关键词预筛都不命中时返回 False，落到原流程）
            from core.utils import health_inbox
            if await health_inbox.maybe_handle(self.config, open_id, text, self.client):
                return

            # 1. 批量反问等待期：短确认词 → 把暂存的链接入库
            if inbox.guard.pending and re.match(
                r"^(是|对|要|存|收|好|可以|确认|嗯|行|yes|ok)", text.strip(), re.IGNORECASE
            ):
                urls = inbox.guard.take_pending()
                receipts = [await inbox.handle_link(self.config, u) for u in urls]
                await self.client.send_text(open_id, "\n".join(r for r in receipts if r) or "已收藏")
                return

            # 2. 文本带链接 → 收藏入库（主人习惯：转发即收藏，不走 LLM 对话）
            urls = inbox.extract_urls(text)
            if urls:
                await self._handle_links(open_id, text, urls)
                return

            reply = await self._call_llm(open_id, text)
            if reply_voice:
                await self._send_voice_reply(open_id, reply)
            else:
                await self.client.send_text(open_id, reply)
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理消息异常: {type(e).__name__}: {e}")
            try:
                await self.client.send_text(open_id, f"（处理出错：{type(e).__name__}）")
            except Exception:
                pass

    # ── 收藏入库：链接 ─────────────────────────────────────────────
    async def _handle_links(self, open_id: str, text: str, urls: list) -> None:
        """文本/富文本里的链接 → RAGflow「我的收藏」；短时大量涌入先反问。"""
        from core.utils import feishu_inbox as inbox

        async def _send(msg: str) -> None:
            await self.client.send_text(open_id, msg)

        try:
            _ok, receipts = await inbox.guard.offer(self.config, urls, _send)
            body = "\n".join(r for r in receipts if r)
            if body:
                await self.client.send_text(open_id, body)
        except Exception as e:
            logger.bind(tag=TAG).error(f"链接收藏异常: {type(e).__name__}: {e}")
            try:
                await self.client.send_text(open_id, f"（收藏出错：{type(e).__name__}）")
            except Exception:
                pass

    # ── 收藏入库：文件 ─────────────────────────────────────────────
    async def _handle_file(self, open_id: str, message_id: str,
                           file_key: str, file_name: str) -> None:
        """文件消息：下载 → anydoc 转 Markdown → 入库回执。"""
        from core.utils import feishu_inbox as inbox

        data = await self.client.download_message_resource(message_id, file_key, "file")
        if not data:
            await self.client.send_text(open_id, "（文件下载失败）")
            return
        receipt = await inbox.handle_file(self.config, data, file_name)
        await self.client.send_text(open_id, receipt)

    async def _handle_audio(
        self, open_id: str, message_id: str, file_key: str, duration_ms: int
    ) -> None:
        """语音消息：下载 → ASR → LLM → 语音回复。"""
        # 1. 下载音频（飞书的资源下载 API type 参数只支持 image/file，语音用 file）
        opus_data = await self.client.download_message_resource(message_id, file_key, "file")
        if not opus_data:
            await self.client.send_text(open_id, "（语音下载失败）")
            return

        # 2. opus → PCM 16kHz mono float32 样本
        pcm_samples = await self._opus_to_pcm_samples(opus_data)
        if not pcm_samples:
            await self.client.send_text(open_id, "（语音转码失败）")
            return

        # 3. ASR
        asr = self.asr
        if asr is None or not hasattr(asr, "_recognize_samples"):
            await self.client.send_text(open_id, "（ASR 模块不可用）")
            return
        try:
            text = await asyncio.get_event_loop().run_in_executor(
                None, asr._recognize_samples, pcm_samples
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"ASR 异常: {e}")
            await self.client.send_text(open_id, f"（语音识别出错：{type(e).__name__}）")
            return

        text = (text or "").strip()
        if not text:
            await self.client.send_text(open_id, "（没听清，再说一次？）")
            return

        logger.bind(tag=TAG).info(f"语音识别结果: {text[:40]}…")

        # 4. LLM + 语音回复
        await self._handle_text(open_id, text, reply_voice=True)

    # ── 图片消息处理 ──────────────────────────────────────────────
    async def _handle_image(self, open_id: str, message_id: str, image_key: str) -> None:
        """图片消息：下载 → VLLM 视觉模型 → 文字回复。"""
        import base64

        # 1. 下载图片（飞书资源下载 API type=image）
        image_data = await self.client.download_message_resource(message_id, image_key, "image")
        if not image_data:
            await self.client.send_text(open_id, "（图片下载失败）")
            return

        # 2. 转 base64
        b64_image = base64.b64encode(image_data).decode("utf-8")
        logger.bind(tag=TAG).info(f"图片已下载 → base64 {len(b64_image)} 字符")

        # 3. 调 VLLM 视觉模型（每次动态创建实例，后台切换模型后即时生效）
        vllm = self._create_vllm()
        if vllm is None or not hasattr(vllm, "response"):
            await self.client.send_text(open_id, "（视觉模型不可用，请在后台「模型配置 → 视觉模型」配置并选用模型）")
            return

        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None, vllm.response, "请描述这张图片", b64_image
            )
            reply = (reply or "").strip() or "（伊娃没看懂这张图）"
            logger.bind(tag=TAG).info(f"视觉模型回复: {reply[:40]}…")
            await self.client.send_text(open_id, reply)
        except Exception as e:
            logger.bind(tag=TAG).error(f"视觉模型调用异常: {e}")
            await self.client.send_text(open_id, f"（图片识别出错：{type(e).__name__}）")

    # ── 语音回复：text → TTS → 上传 → 发 audio 消息 ──────────────
    async def _send_voice_reply(self, open_id: str, text: str) -> None:
        import os
        import tempfile
        import aiohttp

        tts_cfg = self._get_tts_config()
        
        # 检查 TTS 类型是否支持
        if not tts_cfg.get("supported", True):
            logger.bind(tag=TAG).info(f"TTS 类型不支持语音回复，用文本: {tts_cfg.get('type')}")
            await self.client.send_text(open_id, text)
            return
        
        api_url = tts_cfg["api_url"]

        tmp_wav = None
        tmp_opus = None
        try:
            # 1. 直接调 MossNano HTTP API 生成 WAV
            tmp_wav = tempfile.mktemp(suffix=".wav", prefix="feishu_tts_")
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    api_url,
                    json={
                        "text": text,
                        "voice": tts_cfg["voice"],
                        "speed": tts_cfg["speed"],
                        "volume": tts_cfg["volume"],
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        raise RuntimeError(f"TTS HTTP {r.status}: {body[:100]}")
                    audio_data = await r.read()
            with open(tmp_wav, "wb") as f:
                f.write(audio_data)
            if os.path.getsize(tmp_wav) == 0:
                raise RuntimeError("TTS 输出文件为空")
            logger.bind(tag=TAG).info(f"TTS 生成 {len(audio_data)} 字节 → {tmp_wav}")

            # 2. WAV → opus
            tmp_opus = tmp_wav.replace(".wav", ".opus")
            await self._wav_to_opus(tmp_wav, tmp_opus)
            if not os.path.exists(tmp_opus) or os.path.getsize(tmp_opus) == 0:
                raise RuntimeError("opus 转码失败")

            # 3. 估算时长（粗估）
            duration_ms = int(os.path.getsize(tmp_opus) / 3000 * 1000)

            # 4. 上传 + 发送
            file_key = await self.client.upload_audio(tmp_opus, duration_ms)
            if not file_key:
                raise RuntimeError("上传失败")
            if not await self.client.send_audio(open_id, file_key):
                raise RuntimeError("发送失败")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"语音回复失败，退回文本: {e}")
            await self.client.send_text(open_id, text)
        finally:
            for p in (tmp_wav, tmp_opus):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    # ── 音频转码工具 ──────────────────────────────────────────────
    async def _opus_to_pcm_samples(self, opus_data: bytes) -> list:
        """opus 字节 → PCM 16kHz mono float32 样本列表（给 ASR 用）。"""
        import asyncio
        import subprocess
        import struct
        import tempfile

        tmp = tempfile.mktemp(suffix=".opus")
        try:
            with open(tmp, "wb") as f:
                f.write(opus_data)
            # ffmpeg → raw PCM s16le 16kHz mono
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", tmp,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.bind(tag=TAG).error(f"opus→pcm 失败: {stderr.decode()[:200]}")
                return []
            # s16le → float samples (-1.0..1.0)
            n_samples = len(stdout) // 2
            samples = struct.unpack(f"<{n_samples}h", stdout)
            return [s / 32768.0 for s in samples]
        finally:
            try:
                import os
                os.unlink(tmp)
            except OSError:
                pass

    async def _wav_to_opus(self, wav_path: str, opus_path: str) -> None:
        """WAV 文件 → opus 文件（飞书语音消息格式）。"""
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", wav_path,
            "-c:a", "libopus", "-b:a", "24k",
            "-vbr", "on", "-compression_level", "10",
            opus_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"wav→opus 失败: {stderr.decode()[:200]}")

    # ── LLM 调用 ─────────────────────────────────────────────────
    async def _call_llm(self, open_id: str, user_text: str) -> str:
        """把对话历史 + 用户消息喂给 LLM，收集流式响应为完整字符串。"""
        history = self._dialogues.setdefault(open_id, [])
        history.append({"role": "user", "content": user_text})
        # 截断保留最近 N 条（user + assistant 各算一条）
        if len(history) > self.max_history:
            history[:] = history[-self.max_history:]

        # 完整 dialogue：系统人设 + 历史
        dialogue = [{"role": "system", "content": get_system_prompt()}] + history

        # LLM 是同步 generator，放 executor 跑避免阻塞 asyncio loop
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None, self._invoke_llm_sync, open_id, dialogue
        )

        history.append({"role": "assistant", "content": reply})
        return reply

    def _invoke_llm_sync(self, session_id: str, dialogue: list[dict]) -> str:
        """同步调 LLM，收集 generator 输出。在 executor 里跑。"""
        chunks: list[str] = []
        gen = self.llm.response(session_id, dialogue)
        for item in gen:
            # routing-hybrid yield 的可能是 str 或 dict（含 content 字段）
            if isinstance(item, dict):
                piece = item.get("content") or ""
            elif isinstance(item, (list, tuple)) and item:
                piece = item[0] if item[0] is not None else ""
            else:
                piece = item or ""
            if piece:
                chunks.append(piece)
        return "".join(chunks).strip() or "（伊娃走神了，没想出来）"

    # ── 去重 ──────────────────────────────────────────────────────
    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        # 清理过期（FIFO）
        while self._seen:
            oldest_key, oldest_ts = next(iter(self._seen.items()))
            if oldest_ts < now - self.dedup_ttl:
                self._seen.popitem(last=False)
            else:
                break
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


def _backend_moss_voice():
    """读后台主音色（devices.db/tts_config，provider=MossNano 时才认）。

    与 core/providers/tts/moss_nano.py:_get_current_voice 和安防播报同源，
    保证飞书语音回复与家里设备播报音色一致。读不到返回 None。
    """
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "devices.db")
        conn = sqlite3.connect(os.path.abspath(db_path), timeout=2)
        try:
            rows = conn.execute(
                "SELECT config_key, config_value FROM tts_config "
                "WHERE config_key IN ('voice', 'provider')"
            ).fetchall()
        finally:
            conn.close()
        cfg = {str(k): ("" if v is None else str(v)) for k, v in rows}
        if cfg.get("provider", "").strip() == "MossNano":
            return cfg.get("voice", "").strip() or None
        return None
    except Exception:
        return None
