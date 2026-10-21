"""web_read 插件 —— 联网读取网页正文（本地抓取 + trafilatura 提取）

与 web_search 的分工：
  - web_search：全网"搜索"，返回标题 + 摘要列表
  - web_read  ：给定网址"读取正文"，返回网页/文章的完整 Markdown 文本

实现方式：httpx 抓取 HTML + trafilatura 本地提取正文，
不依赖任何境外服务（如 Jina Reader），因此在"国内可直连"的网络环境下即可用。
"""

import asyncio
import re

import httpx
import trafilatura
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

WEB_READ_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_read",
        "description": (
            "联网读取指定网页的正文内容。当用户给出一个网址（链接），"
            "或需要了解某个网页、文章、帖子的具体内容时使用。"
            "返回该网页正文的 Markdown 文本。"
            "注意：这是「读取正文」，不是「搜索」；"
            "若用户只是想搜索信息、并不知道具体网址，请改用 web_search。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读取的网页地址，例如 https://example.com/article",
                }
            },
            "required": ["url"],
        },
    },
}

# 正文过长会挤占 LLM 上下文，做截断（约 4000 字符）
_MAX_CONTENT_CHARS = 4000
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _normalize_url(url: str) -> str:
    """补全协议头、去空白。"""
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


async def _fetch_and_extract(url: str) -> str:
    """抓取网页并提取正文（trafilatura 是同步的，放线程池跑，避免阻塞事件循环）。"""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    text = await asyncio.to_thread(
        trafilatura.extract,
        html,
        output_format="markdown",
        include_links=False,
        include_images=False,
    )
    return text or ""


@register_function("web_read", WEB_READ_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_read(conn: "ConnectionHandler", url: str = None):
    logger.bind(tag=TAG).info(f"web_read 被调用 | url={url}")
    url = _normalize_url(url)
    if not url:
        return ActionResponse(Action.REQLLM, "请提供要读取的网页地址。", None)
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        return ActionResponse(
            Action.REQLLM, "网页地址格式不对，请以 http:// 或 https:// 开头。", None
        )

    try:
        content = await _fetch_and_extract(url)
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error(f"web_read 超时: {url}")
        return ActionResponse(
            Action.REQLLM,
            "读取网页超时，该网站可能访问较慢或当前网络不可达，请稍后重试。",
            None,
        )
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"web_read HTTP {e.response.status_code}: {url}")
        code = e.response.status_code
        if code in (401, 403):
            msg = "该网站拒绝访问（可能有反爬或需要登录），无法读取正文。"
        elif code == 404:
            msg = "网页不存在（404），请检查网址是否正确。"
        else:
            msg = f"读取网页失败（HTTP {code}）。"
        return ActionResponse(Action.REQLLM, msg, None)
    except Exception as e:
        logger.bind(tag=TAG).error(f"web_read 异常: {type(e).__name__}: {e}")
        return ActionResponse(
            Action.REQLLM, "读取网页时出现异常，请稍后重试。", None
        )

    if not content.strip():
        logger.bind(tag=TAG).info(f"web_read 未提取到正文: {url}")
        return ActionResponse(
            Action.REQLLM,
            "没能从该网页提取到正文，可能是视频/图片页、动态渲染页或需要登录。",
            None,
        )

    truncated = len(content) > _MAX_CONTENT_CHARS
    if truncated:
        content = content[:_MAX_CONTENT_CHARS]

    header = f"【网页正文】{url}\n"
    footer = "\n（正文过长已截断，如需更多内容请追问）" if truncated else ""
    result_text = header + content + footer
    logger.bind(tag=TAG).info(
        f"web_read 提取完成 | url={url} 长度={len(content)} 截断={truncated}"
    )
    return ActionResponse(Action.REQLLM, result_text, None)
