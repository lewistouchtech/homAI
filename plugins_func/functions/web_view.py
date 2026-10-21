"""web_view 插件 —— 在有屏终端中央舞台打开网页；不能嵌的回落系统浏览器

[2026-10-07] 主人定案：有屏终端说「查什么」时，应该像高德地图那样
把网页打开给人看，同时由 web_search_read 读全文给 AI。

实现（仿 show_map）：
  1. 预检目标网站是否允许被 iframe 嵌入（X-Frame-Options / CSP frame-ancestors）；
  2. 能嵌 → 广播 web_view 给看板，前端在中央舞台用 iframe 显示；
  3. 不能嵌（百度/微博/豆瓣等约一半站点会拒绝）或没有看板在线
     → 回落 mac_browser 用系统浏览器打开。
"""

import json
import re

import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.api.config_api import get_websocket_server_instance
from plugins_func.functions.show_map import _is_screen_ws
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

WEB_VIEW_NOT_SHOWN = "【网页未显示】"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

WEB_VIEW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_view",
        "description": (
            "在家庭看板的中央窗口打开一个网页给人看。"
            "当用户想「打开/看看」某个网页，或你刚搜索到一个值得展示的链接时使用。"
            "注意：只能传完整网址（http/https 开头）；如果用户只是要了解内容、"
            "并没有明确要求「打开网页看」，应优先用 web_search_read。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要打开的网页地址（http/https 开头）",
                },
                "title": {
                    "type": "string",
                    "description": "可选，窗口标题",
                },
            },
            "required": ["url"],
        },
    },
}


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


async def _can_iframe(url: str) -> bool:
    """预检目标网站是否允许被 iframe 嵌入。

    只看响应头，不下载正文（用 stream 提前关闭）。
    探测失败时保守返回 False —— 宁可回落到系统浏览器，也不要前端白屏。
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            async with client.stream("GET", url) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
    except Exception as e:
        logger.bind(tag=TAG).info(f"iframe 预检失败 {url}: {type(e).__name__}")
        return False

    xfo = (headers.get("x-frame-options") or "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return False

    csp = headers.get("content-security-policy") or ""
    m = re.search(r"frame-ancestors([^;]*)", csp, re.I)
    if m:
        allow = m.group(1).strip()
        # 只有显式允许任意来源（*）才认为可嵌；'self' / 白名单域名都不行
        if "*" not in allow:
            return False
    return True


async def _broadcast_web_view(url: str, title: str) -> int:
    """广播 web_view 给所有连接，返回「送达的有屏终端数」。"""
    server = get_websocket_server_instance()
    if server is None:
        return 0
    async with server.connections_lock:
        conns = (
            list(server.connections)
            if hasattr(server, "connections")
            else list(server.active_connections)
        )
    payload = json.dumps(
        {"type": "web_view", "action": "open", "url": url, "title": title},
        ensure_ascii=False,
    )
    sent_screen = 0
    for ws in conns:
        if not _is_screen_ws(ws):
            continue
        try:
            await ws.send(payload)
            sent_screen += 1
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[网页] 看板广播失败：{type(e).__name__}: {e}")
    logger.bind(tag=TAG).info(f"[网页] web_view 广播完成：看板送达 {sent_screen} 个")
    return sent_screen


async def _fallback_browser(conn, url: str) -> ActionResponse:
    """回落到系统浏览器打开（复用 mac_browser）。"""
    try:
        from plugins_func.functions.mac_control import mac_browser

        return await mac_browser(conn, url=url)
    except Exception as e:
        logger.bind(tag=TAG).error(f"[网页] 回落系统浏览器失败：{type(e).__name__}: {e}")
        return ActionResponse(
            Action.REQLLM,
            f"{WEB_VIEW_NOT_SHOWN}网页没能在看板或电脑浏览器上打开（{type(e).__name__}）。"
            f"请如实告知用户没有打开成功，不要谎称已打开。",
            None,
        )


@register_function("web_view", WEB_VIEW_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_view(conn: "ConnectionHandler", url: str = None, title: str = ""):
    logger.bind(tag=TAG).info(f"web_view 被调用 | url={url} title={title}")
    url = _normalize_url(url)
    if not url:
        return ActionResponse(Action.REQLLM, "请提供要打开的网页地址。", None)
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        return ActionResponse(
            Action.REQLLM, "网页地址格式不对，请以 http:// 或 https:// 开头。", None
        )

    can_embed = await _can_iframe(url)
    if can_embed:
        sent_screen = await _broadcast_web_view(url, title or url)
        if sent_screen > 0:
            return ActionResponse(
                Action.REQLLM,
                f"已在家庭看板的中央窗口打开网页：{url}。"
                f"请简短告诉用户「已经打开了」，不要念网址、也不要复述网页内容。",
                None,
            )
        logger.bind(tag=TAG).info("[网页] 无看板在线，回落到系统浏览器")
    else:
        logger.bind(tag=TAG).info(f"[网页] {url} 不允许 iframe 嵌入，回落到系统浏览器")

    return await _fallback_browser(conn, url)
