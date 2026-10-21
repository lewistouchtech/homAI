"""搜索引擎配置 API

为 HomeBrain 控制台提供搜索引擎管理页面。数据源与 `plugins.web_search` 插件
共用同一份配置（`plugins.web_search`），避免两套配置漂移。

HomeBrain 是独立程序，搜索引擎配置全部存在项目内（data/.config.yaml），
部署到新电脑只需项目目录 + 配 API Key，零外部依赖。
搜索引擎调用走 HTTP 直连 REST API（与 web_search.py 同方式），不依赖
任何外部 CLI / MCP server / 全局 Python 包。

当前支持引擎（与 web_search.py 的 ENGINES 对齐）：
    metaso / tavily / brave / serper / jina / duckduckgo / searxng

路由：
    GET  /api/config/search-providers    → 列出所有搜索引擎及状态（api_key 脱敏）
    POST /api/config/search-providers    → 更新配置（selected / api_key 等）
    POST /api/config/search-test         → 测试连通性（HTTP 直连 REST API）
"""
import asyncio
import json
from pathlib import Path

import httpx
import yaml
from aiohttp import web

from config.logger import setup_logging
from config.config_loader import load_yaml_expanded

logger = setup_logging()
TAG = "SearchConfigAPI"

SERVER_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = SERVER_ROOT / "data" / ".config.yaml"

# 搜索引擎元数据（与 web_search.py 的 ENGINES 对齐）
PROVIDERS_META = {
    "metaso": {
        "name": "秘塔搜索",
        "description": "国内 AI 搜索，中文效果好",
        "need_key": True,
        "api_key_prefix": "ms-",
        "config_url": "https://metaso.cn/openapi",
        "features": ["search"],
    },
    "tavily": {
        "name": "Tavily",
        "description": "实时网页搜索、内容提取、网站爬取、深度研究（Agent 友好）",
        "need_key": True,
        "api_key_prefix": "tvly-",
        "config_url": "https://tavily.com",
        "features": ["search", "extract", "map", "crawl", "research"],
    },
    "brave": {
        "name": "Brave Search API",
        "description": "隐私优先的通用搜索 API",
        "need_key": True,
        "api_key_prefix": "BSA",
        "config_url": "https://brave.com/search/api/",
        "features": ["search"],
    },
    "serper": {
        "name": "Serper (Google 结果)",
        "description": "返回 Google 搜索结果的 API 封装",
        "need_key": True,
        "api_key_prefix": "",
        "config_url": "https://serper.dev",
        "features": ["search"],
    },
    "jina": {
        "name": "Jina AI 搜索",
        "description": "面向 LLM 优化的搜索结果 API",
        "need_key": True,
        "api_key_prefix": "jina_",
        "config_url": "https://jina.ai",
        "features": ["search"],
    },
    "duckduckgo": {
        "name": "DuckDuckGo",
        "description": "免费、无需 API Key 的隐私搜索",
        "need_key": False,
        "api_key_prefix": "",
        "config_url": "https://duckduckgo.com",
        "features": ["search"],
    },
    "searxng": {
        "name": "SearXNG (自托管)",
        "description": "自托管的聚合搜索引擎，需要填写 base_url",
        "need_key": False,
        "api_key_prefix": "",
        "config_url": "https://docs.searxng.org",
        "features": ["search"],
    },
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            return load_yaml_expanded(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载 data/.config.yaml 失败: {e}")
    return {}


def _save_config(cfg: dict) -> None:
    """整体写回 data/.config.yaml。

    ⚠️ 2026-10-05 修一个会造成密钥泄露的组合缺陷：
    本模块的 _load_config() 走的是**带插值**的读取（拿到的 api_key 是 .env 里的真值），
    而这里直接 yaml.safe_dump 整体写回 —— 于是真值被写进 data/.config.yaml。
    那个文件是 git 跟踪的，等于把密钥重新塞进版本历史。

    现在写回前统一做一次归集：明文密钥 → .env，本文件只留 ${} 引用。
    规则与其他写入点一致（见 core/utils/secret_store.py）。

    另注：safe_dump 会丢掉文件里的全部注释，这是整体重写的固有代价；
    本模块只改 plugins.web_search 一个节点，若后续要保留注释需改为行级写回。
    """
    from core.utils.secret_store import sweep_dict_secrets
    cfg = sweep_dict_secrets(cfg)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _get_web_search_config(cfg: dict) -> dict:
    """从 plugins.web_search 取配置（与 web_search 插件共享同一份）"""
    plugins = cfg.get("plugins", {})
    ws = plugins.get("web_search", {})
    return {
        "provider": ws.get("provider", "duckduckgo"),
        "api_key": ws.get("api_key", ""),
        "base_url": ws.get("base_url", ""),
        "max_results": int(ws.get("max_results", 3)),
    }


def _set_web_search_config(cfg: dict, updates: dict) -> dict:
    """写回 plugins.web_search"""
    plugins = cfg.setdefault("plugins", {})
    ws = plugins.setdefault("web_search", {})
    ws.update(updates)
    return cfg


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:4] + "***"
    return key[:8] + "****" + key[-2:]


def _json_response(data, status=200):
    return web.json_response(data, status=status)


async def get_search_providers(request):
    """列出所有搜索引擎及状态"""
    try:
        cfg = _load_config()
        ws = _get_web_search_config(cfg)
        current = ws.get("provider", "duckduckgo")
        providers = []
        for pid, meta in PROVIDERS_META.items():
            providers.append({
                "id": pid,
                "name": meta["name"],
                "description": meta["description"],
                "need_key": meta["need_key"],
                "api_key_prefix": meta["api_key_prefix"],
                "config_url": meta["config_url"],
                "features": meta["features"],
                "selected": pid == current,
                "api_key_masked": _mask_key(ws.get("api_key", "")) if pid == current and ws.get("api_key") else "",
                "api_key_set": bool(ws.get("api_key")) if pid == current else False,
                "base_url": ws.get("base_url", "") if pid == current else "",
            })
        return _json_response({
            "success": True,
            "selected": current,
            "max_results": ws.get("max_results", 3),
            "providers": providers,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"get_search_providers 失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def save_search_providers(request):
    """更新搜索引擎配置

    body 字段：
      - selected: str            → 选中的 provider id（写入 plugins.web_search.provider）
      - api_key: str             → 选中的 provider 的 API key
      - base_url: str            → SearXNG 等的 base_url
      - max_results: int         → 默认返回条数
    """
    try:
        body = await request.json()
    except Exception:
        return _json_response({"success": False, "error": "无效的 JSON 请求体"}, status=400)

    cfg = _load_config()
    updates = {}

    if "selected" in body:
        sel = body["selected"]
        if sel not in PROVIDERS_META:
            return _json_response({"success": False, "error": f"不支持的搜索引擎: {sel}"}, status=400)
        updates["provider"] = sel

    if "api_key" in body:
        key = body["api_key"].strip()
        selected_id = body.get("selected") or _get_web_search_config(cfg).get("provider")
        prefix = PROVIDERS_META.get(selected_id, {}).get("api_key_prefix", "")
        if key and prefix and not key.startswith(prefix):
            return _json_response({
                "success": False,
                "error": f"{PROVIDERS_META[selected_id]['name']} API key 应以 {prefix} 开头",
            }, status=400)
        updates["api_key"] = key

    if "base_url" in body:
        updates["base_url"] = body["base_url"].strip()

    if "max_results" in body:
        try:
            updates["max_results"] = int(body["max_results"])
        except (TypeError, ValueError):
            return _json_response({"success": False, "error": "max_results 必须是整数"}, status=400)

    _set_web_search_config(cfg, updates)
    try:
        _save_config(cfg)
    except Exception as e:
        logger.bind(tag=TAG).error(f"写入 data/.config.yaml 失败: {e}")
        return _json_response({"success": False, "error": f"写入配置失败: {e}"}, status=500)

    return _json_response({"success": True, "message": "搜索引擎配置已保存"})


async def test_search_provider(request):
    """测试指定搜索引擎的连通性（HTTP 直连 REST API，零外部依赖）"""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"success": False, "error": "无效的 JSON 请求体"}, status=400)

    provider_id = body.get("provider_id")
    cfg = _load_config()
    ws = _get_web_search_config(cfg)
    if not provider_id:
        provider_id = ws.get("provider")
    if provider_id not in PROVIDERS_META:
        return _json_response({"success": False, "error": f"不支持的搜索引擎: {provider_id}"}, status=400)

    api_key = ws.get("api_key", "")
    need_key = PROVIDERS_META[provider_id]["need_key"]
    if need_key and not api_key:
        return _json_response({
            "success": False,
            "error": f"{PROVIDERS_META[provider_id]['name']} 需要 API Key，请先配置",
        })

    if provider_id == "tavily":
        return await _test_tavily(api_key)
    if provider_id == "duckduckgo":
        return await _test_duckduckgo()
    return _json_response({
        "success": True,
        "message": f"{PROVIDERS_META[provider_id]['name']} 的在线连通测试尚未实现，保存后请让 LLM 实际调用 web_search 工具验证",
    })


async def _test_tavily(api_key: str):
    """HTTP 直连 Tavily REST API（与 web_search.py 同方式，零外部依赖）"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": "homAi connectivity test",
                    "max_results": 1,
                    "include_answer": False,
                },
            )
            if resp.status_code == 401:
                return _json_response({"success": False, "error": "API Key 无效（401）"})
            if resp.status_code == 429:
                return _json_response({"success": False, "error": "配额已用完或限流（429）"})
            if resp.status_code != 200:
                return _json_response({
                    "success": False,
                    "error": f"Tavily 返回 HTTP {resp.status_code}: {resp.text[:200]}",
                })
            data = resp.json()
            results = data.get("results", [])
            return _json_response({
                "success": True,
                "message": f"Tavily 连通，返回 {len(results)} 条结果",
                "sample": {
                    "title": (results[0].get("title") or "")[:100] if results else "(无结果)",
                    "url": results[0].get("url", "") if results else "",
                },
            })
    except httpx.TimeoutException:
        return _json_response({"success": False, "error": "连接超时（30s），可能是网络问题"})
    except Exception as e:
        return _json_response({"success": False, "error": f"测试执行异常: {e}"})


async def _test_duckduckgo():
    """DuckDuckGo 不需要 key，直接调 web_search 插件的搜索逻辑做冒烟"""
    try:
        from plugins_func.functions.web_search import _search_duckduckgo
        result = await _search_duckduckgo("", "homAi", 1)
        return _json_response({
            "success": True,
            "message": "DuckDuckGo 连通，返回结果",
            "sample": {"snippet": (result or "")[:200]},
        })
    except Exception as e:
        return _json_response({"success": False, "error": f"DuckDuckGo 测试失败: {e}"})


def register_routes(app):
    app.router.add_get("/api/config/search-providers", get_search_providers)
    app.router.add_post("/api/config/search-providers", save_search_providers)
    app.router.add_post("/api/config/search-test", test_search_provider)
