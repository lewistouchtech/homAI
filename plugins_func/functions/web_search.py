import httpx
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

_DEFAULT_DESCRIPTION = (
    "联网搜索工具。当用户明确需要联网搜索问题时使用此工具。"
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                }
            },
            "required": ["query"],
        },
    },
}

# 支持的搜索引擎（provider 取值 -> 是否需要 api_key）
ENGINES = {
    "metaso": {"name": "秘塔搜索", "need_key": True},
    "tavily": {"name": "Tavily", "need_key": True},
    "brave": {"name": "Brave Search API", "need_key": True},
    "serper": {"name": "Serper (Google结果)", "need_key": True},
    "jina": {"name": "Jina AI 搜索", "need_key": True},
    "duckduckgo": {"name": "DuckDuckGo (免费无需Key)", "need_key": False},
    "searxng": {"name": "SearXNG (自托管/自定义)", "need_key": False},
}


def _format_lines(header: str, items: list) -> str:
    lines = [header]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        lines.append(f"{i}. 标题：{title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
    return "\n".join(lines)


# [2026-10-07] 每个 _search_* 统一返回 dict：
#     {"items": [{"title","snippet","url"}, ...], "answer": str}
# 这样 web_search（只格式化输出）与 web_search_read（取 url 深读全文）
# 可以共用同一份搜索逻辑，避免两套引擎实现漂移。
# 空结果返回 {"items": [], "answer": ""}，由调用方决定提示语。


async def _search_metaso(api_key: str, query: str, max_results: int) -> dict:
    """调用秘塔搜索API（国内可直接访问）"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
    data = response.json()

    webpages = data.get("webpages", [])
    items = [
        {
            "title": w.get("title", "无标题"),
            "snippet": w.get("summary", ""),
            "url": w.get("link") or w.get("url") or "",
        }
        for w in webpages[:max_results]
    ]
    return {"items": items, "answer": ""}


async def _search_tavily(api_key: str, query: str, max_results: int) -> dict:
    """调用Tavily搜索API（为AI Agent设计，免费档1000次/月）"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
    data = response.json()

    results = data.get("results", [])
    items = [
        {
            "title": r.get("title", "无标题"),
            "snippet": r.get("content", ""),
            "url": r.get("url", ""),
        }
        for r in results[:max_results]
    ]
    return {"items": items, "answer": data.get("answer", "") or ""}


async def _search_brave(api_key: str, query: str, max_results: int) -> dict:
    """Brave Search API（免费档2000次/月，需信用卡注册）"""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": max_results, "search_lang": "zh-hans"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
    data = response.json()

    results = (data.get("web") or {}).get("results", [])
    items = [
        {
            "title": r.get("title", "无标题"),
            "snippet": r.get("description", ""),
            "url": r.get("url", ""),
        }
        for r in results[:max_results]
    ]
    return {"items": items, "answer": ""}


async def _search_serper(api_key: str, query: str, max_results: int) -> dict:
    """Serper.dev（Google 搜索结果，注册送100次，之后$1/1000次）"""
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": max_results, "hl": "zh-cn"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
    data = response.json()

    results = data.get("organic", [])
    items = [
        {
            "title": r.get("title", "无标题"),
            "snippet": r.get("snippet", ""),
            "url": r.get("link", ""),
        }
        for r in results[:max_results]
    ]
    return {"items": items, "answer": ""}


async def _search_jina(api_key: str, query: str, max_results: int) -> dict:
    """Jina AI 搜索（s.jina.ai，返回整理好的Markdown，免费档有额度）"""
    import urllib.parse

    url = f"https://s.jina.ai/{urllib.parse.quote(query)}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0)) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    data = response.json()

    results = data.get("data") or []
    items = [
        {
            "title": r.get("title", "无标题"),
            "snippet": r.get("description", ""),
            "url": r.get("url", ""),
        }
        for r in results[:max_results]
    ]
    return {"items": items, "answer": ""}


async def _search_duckduckgo(api_key: str, query: str, max_results: int) -> dict:
    """DuckDuckGo 官方无Key接口 Instant Answers（国内网络可能无法访问）"""
    url = "https://api.duckduckgo.com/"
    params = {
        "q": query,
        "format": "json",
        "no_html": 1,
        "skip_disambig": 1,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
    data = response.json()

    items = []
    abstract = data.get("AbstractText") or data.get("Definition", "")
    if abstract:
        items.append(
            {
                "title": data.get("Heading", query),
                "snippet": abstract,
                "url": data.get("AbstractURL", "") or "",
            }
        )
    for topic in (data.get("RelatedTopics") or [])[: max_results - len(items)]:
        if "Text" in topic:
            items.append(
                {
                    "title": topic.get("Text", "")[:40],
                    "snippet": topic.get("Text", ""),
                    "url": topic.get("FirstURL", ""),
                }
            )
        elif "Topics" in topic:
            for t in topic["Topics"][:2]:
                if "Text" in t:
                    items.append(
                        {
                            "title": t.get("Text", "")[:40],
                            "snippet": t.get("Text", ""),
                            "url": t.get("FirstURL", ""),
                        }
                    )
    return {"items": items[:max_results], "answer": ""}


async def _search_searxng(base_url: str, query: str, max_results: int) -> dict:
    """SearXNG 自托管实例（base_url 必填，格式如 http://YOUR_SEARXNG_HOST:8888）"""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/search"
    params = {"q": query, "format": "json", "language": "zh-CN"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
    data = response.json()

    results = data.get("results", [])
    items = [
        {
            "title": r.get("title", "无标题"),
            "snippet": r.get("content", ""),
            "url": r.get("url", ""),
        }
        for r in results[:max_results]
    ]
    return {"items": items, "answer": ""}


SEARCH_FUNCS = {
    "metaso": _search_metaso,
    "tavily": _search_tavily,
    "brave": _search_brave,
    "serper": _search_serper,
    "jina": _search_jina,
    "duckduckgo": _search_duckduckgo,
}


def _resolve_engine(web_search_config: dict):
    """解析 provider/api_key，返回 (provider, api_key, engine, error_msg)。"""
    provider = str(web_search_config.get("provider", "")).lower().strip()
    api_key = web_search_config.get("api_key", "")
    # 非 ASCII 的 key 必是占位符/乱码（如"你的Tavily API密钥"），按未配置处理，
    # 否则 httpx 拼 Authorization 头时会抛 UnicodeEncodeError
    if api_key and not api_key.isascii():
        api_key = ""
    if not provider or provider not in ENGINES:
        names = " / ".join(f"{k}({v['name']})" for k, v in ENGINES.items())
        return (
            provider,
            api_key,
            None,
            f"联网搜索未选择引擎。可用引擎：{names}。请在后台插件配置中选择。",
        )
    engine = ENGINES[provider]
    if engine["need_key"] and not api_key:
        return provider, api_key, engine, f"{engine['name']} 需要 API Key，请在后台插件配置中填写。"
    return provider, api_key, engine, ""


async def raw_search(web_search_config: dict, query: str, max_results: int) -> dict:
    """执行搜索，返回 {"items": [{"title","snippet","url"}], "answer": str, "error": str}。

    供 web_search（只格式化输出）与 web_search_read（取 url 深读全文）共用。
    异常（超时/HTTP 错误）由调用方捕获。
    """
    provider, api_key, engine, err = _resolve_engine(web_search_config)
    if err:
        return {"items": [], "answer": "", "error": err}

    if provider == "searxng":
        base_url = web_search_config.get("base_url", "")
        if not base_url:
            return {
                "items": [],
                "answer": "",
                "error": "SearXNG 引擎需要配置 base_url（自托管实例地址），请在后台插件配置中填写。",
            }
        res = await _search_searxng(base_url, query, max_results)
    else:
        res = await SEARCH_FUNCS[provider](api_key, query, max_results)

    res.setdefault("error", "")
    return res


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search 被调用 | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    max_results = int(web_search_config.get("max_results", 3))

    try:
        res = await raw_search(web_search_config, query, max_results)
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("联网搜索请求超时")
        return ActionResponse(
            Action.REQLLM,
            "联网搜索请求超时，可能是该引擎在当前网络环境不可达，请换其他引擎。",
            None,
        )
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"联网搜索请求失败: {e.response.status_code}")
        return ActionResponse(
            Action.REQLLM,
            f"联网搜索请求失败(HTTP {e.response.status_code})，请检查引擎选择和 API Key 是否有效。",
            None,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"联网搜索异常: {e}")
        return ActionResponse(Action.REQLLM, "联网搜索出现异常，请稍后重试。", None)

    if res.get("error"):
        return ActionResponse(Action.REQLLM, res["error"], None)

    items = res.get("items") or []
    if not items:
        return ActionResponse(Action.REQLLM, "未找到相关搜索结果。", None)

    header = "【联网搜索结果】"
    answer = res.get("answer") or ""
    if answer:
        header += f"\n总结：{answer}"
    result_text = _format_lines(header, items)
    logger.bind(tag=TAG).info(f"搜索结果组装完成:\n{result_text}")
    return ActionResponse(Action.REQLLM, result_text, None)
