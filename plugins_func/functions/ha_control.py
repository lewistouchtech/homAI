# -*- coding: utf-8 -*-
"""Home Assistant 智能家居控制插件（社区版）

社区版提供**标准 Home Assistant REST API 集成**：

- 读取 HA 地址与令牌（管理台配置 → sqlite / 环境变量）
- 实体状态查询与设备控制（light / switch / cover / climate / media_player / lock …）
- 供其他插件复用的公共接口（`HASS_BASE_URL` / `HEADERS` / `_friendly` / `_short` …）

> 想用**中文语义寻址**（房间 / 楼层 / 设备类型 / 别名 / 拼音 → 实体）、
> 场景编排、语音化设备调用这些深度能力，请使用商业版。
> 本文件是社区版的独立实现，不依赖任何商业模块。

标准协议参考：https://developers.home-assistant.io/docs/api/rest/
"""
import asyncio
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# ==================== 配置 ====================

_DOMAIN_CN = {
    "light": "灯",
    "switch": "开关",
    "cover": "窗帘",
    "climate": "空调",
    "fan": "风扇",
    "media_player": "播放器",
    "lock": "门锁",
    "sensor": "传感器",
    "binary_sensor": "传感器",
    "person": "人",
    "device_tracker": "设备",
    "scene": "场景",
    "script": "脚本",
}

_ON_STATES = {"on", "open", "opening", "playing", "paused", "locked", "home", "heat", "cool"}
_OFF_STATES = {"off", "closed", "closing", "idle", "standby", "unlocked", "away", "auto"}
_SKIP_STATES = {"unknown", "none", "", "unavailable"}

# 设备类型 → HA 服务（标准化控制）
_SERVICE_BY_DOMAIN = {
    "light": ("light", "turn_on", "turn_off"),
    "switch": ("switch", "turn_on", "turn_off"),
    "fan": ("fan", "turn_on", "turn_off"),
    "cover": ("cover", "open_cover", "close_cover"),
    "climate": ("climate", "set_temperature", "turn_off"),
    "media_player": ("media_player", "media_play", "media_stop"),
    "lock": ("lock", "lock", "unlock"),
    "input_boolean": ("input_boolean", "turn_on", "turn_off"),
    "input_select": ("input_select", "select_option", ""),
}


def _db_path() -> str:
    """sqlite 路径（与项目其他模块一致）"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "data", "devices.db")


def _get_ha_config() -> Dict[str, str]:
    """读 HA 地址与令牌；读不到返回空串（调用时给提示，不抛异常）"""
    base_url = ""
    token = ""
    # 1) 环境变量
    base_url = (os.environ.get("HASS_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("HASS_TOKEN") or "").strip()
    # 2) sqlite system_config（管理台写入）
    if not base_url or not token:
        try:
            conn = sqlite3.connect(_db_path(), timeout=3)
            cur = conn.cursor()
            for key, val in cur.execute(
                "SELECT key, value FROM system_config WHERE key IN ('ha_host','ha_port','ha_token')"
            ):
                if key == "ha_host" and val:
                    base_url = base_url or f"http://{val}:8123"
                elif key == "ha_port" and val and not base_url:
                    base_url = f"http://localhost:{val}"
                elif key == "ha_token" and val:
                    token = token or str(val)
            conn.close()
        except Exception:
            pass
    return {"base_url": base_url, "token": token}


_ha_config = _get_ha_config()
HASS_BASE_URL: str = _ha_config["base_url"]
HASS_ACCESS_TOKEN: str = _ha_config["token"]

# HEADERS 是 dict，别处（ha_music 等）以 from-import 持有同一个对象 →
# 这里**原地刷新**，配置变化后老调用方也能拿到新令牌
HEADERS: Dict[str, str] = {
    "Authorization": f"Bearer {HASS_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)


def _cfg() -> Tuple[str, str]:
    """动态取配置，不要依赖 import 时冻结的模块级常量"""
    cfg = _get_ha_config()
    return cfg["base_url"], cfg["token"]


def _cfg_base() -> str:
    return _cfg()[0]


def _headers() -> Dict[str, str]:
    """动态 headers，同时把模块级 HEADERS 原地刷新"""
    base, token = _cfg()
    HEADERS["Authorization"] = f"Bearer {token}"
    HEADERS["Content-Type"] = "application/json"
    return dict(HEADERS)


def _not_configured() -> Optional[str]:
    base, token = _cfg()
    if not base or not token:
        return "还没有配置 Home Assistant 的地址或令牌，请在管理台里填好再试"
    return None


# ==================== 实体 ====================

_ENTITY_NAMES: Dict[str, str] = {}


async def _fetch_states(session) -> List[Dict[str, Any]]:
    base, _ = _cfg()
    if not base:
        return []
    try:
        async with session.get(f"{base}/api/states", headers=_headers(),
                               timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception:
        return []


async def _load_entities() -> None:
    """刷新实体名映射（friendly name → entity_id）"""
    base, _ = _cfg()
    if not base:
        return
    try:
        async with aiohttp.ClientSession() as session:
            states = await _fetch_states(session)
        for ent in states:
            eid = ent.get("entity_id")
            attrs = ent.get("attributes") or {}
            name = attrs.get("friendly_name") or ""
            if eid and name:
                _ENTITY_NAMES[eid] = name
    except Exception:
        pass


def _friendly(entity_id: str) -> str:
    """友好名。**绝不返回 entity_id** —— 它是拼音，念出来主人听不懂"""
    name = _ENTITY_NAMES.get(entity_id) or ""
    if not name:
        domain = str(entity_id).split(".")[0]
        name = _DOMAIN_CN.get(domain) or "这个设备"
    return name


def _short(text: Any, limit: int = 200) -> str:
    """截断，避免超长文本被 TTS 念个没完"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _is_on_state(domain: str, state: Any) -> bool:
    s = str(state or "").lower()
    if s in _SKIP_STATES:
        return False
    if domain in ("lock",):
        return s == "locked"
    return s in _ON_STATES


async def ha_get_state(entity_id: str) -> Dict[str, Any]:
    """查一个实体的实时状态，返回 HA 原生结构；失败返回 {"error": ...}"""
    base, _ = _cfg()
    if not base:
        return {"error": "not_configured"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/states/{entity_id}",
                                   headers=_headers(), timeout=_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def ha_call_service(domain: str, service: str, data: Optional[Dict] = None) -> Dict[str, Any]:
    """调用 HA 服务"""
    base, _ = _cfg()
    if not base:
        return {"error": "not_configured"}
    payload = {"entity_id": (data or {}).get("entity_id")}
    payload.update({k: v for k, v in (data or {}).items() if k != "entity_id"})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/api/services/{domain}/{service}",
                                    json=payload, headers=_headers(),
                                    timeout=_HTTP_TIMEOUT) as resp:
                if resp.status in (200, 201):
                    return {"success": True}
                return {"error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _resolve_action(domain: str, action: str) -> Tuple[str, str]:
    """把「开/关/播放/停止」映射成 (service_domain, service)"""
    mapping = _SERVICE_BY_DOMAIN.get(domain, (domain, "turn_on", "turn_off"))
    act = (action or "").strip().lower()
    if act in ("开", "打开", "on", "open", "打开"):
        return mapping[0], mapping[1]
    if act in ("关", "关闭", "off", "close", "关闭"):
        return mapping[0], mapping[2] or "turn_off"
    return mapping[0], mapping[1]


async def ha_turn_on(entity_id: str, **kwargs) -> Dict[str, Any]:
    """打开设备（灯 / 开关 / 风扇 / 播放器…）"""
    domain = entity_id.split(".")[0] if "." in entity_id else "switch"
    svc_domain, svc = _resolve_action(domain, "开")
    return await ha_call_service(svc_domain, svc, {"entity_id": entity_id, **kwargs})


async def ha_turn_off(entity_id: str, **kwargs) -> Dict[str, Any]:
    """关闭设备"""
    domain = entity_id.split(".")[0] if "." in entity_id else "switch"
    svc_domain, svc = _resolve_action(domain, "关")
    return await ha_call_service(svc_domain, svc, {"entity_id": entity_id, **kwargs})


# ==================== 工具入口 ====================

HA_CONTROL_FUNC_DESC = {
    "type": "function",
    "function": {
        "name": "ha_control",
        "description": (
            "控制 Home Assistant 里的智能家居设备：开灯、关灯、开关窗帘、"
            "调空调温度、暂停音乐、锁门等。也可以查询设备当前状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "description": "要执行的操作列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "description": "设备实体 ID，如 light.living_room"},
                            "action": {"type": "string", "description": "操作：开 / 关 / 播放 / 停止 / 状态"},
                            "params": {"type": "object", "description": "额外参数，如空调温度"},
                        },
                        "required": ["entity_id", "action"],
                    },
                },
            },
            "required": ["commands"],
        },
    },
}


async def ha_control_async(commands: List[Dict[str, Any]]) -> str:
    """执行一组设备操作，返回人话播报文本"""
    if not commands:
        return "没有要执行的设备操作"
    base, _ = _cfg()
    if not base:
        return "还没有配置 Home Assistant 的地址或令牌，请在管理台里填好再试"

    await _load_entities()
    done: List[str] = []
    failed: List[str] = []
    async with aiohttp.ClientSession() as session:
        for cmd in commands:
            eid = str(cmd.get("entity_id") or "").strip()
            action = str(cmd.get("action") or "").strip()
            params = cmd.get("params") or {}
            if not eid:
                failed.append("有个设备没说是哪个")
                continue
            domain = eid.split(".")[0] if "." in eid else "switch"
            label = _friendly(eid)
            if action in ("状态", "查询", "state", "status"):
                st = await ha_get_state(eid)
                if "error" in st:
                    failed.append(f"{label}查不到状态")
                else:
                    done.append(f"{label}现在是{st.get('state')}")
                continue
            svc_domain, svc = _resolve_action(domain, action)
            res = await ha_call_service(svc_domain, svc, {"entity_id": eid, **params})
            if res.get("success"):
                done.append(f"{label}已{'打开' if svc.endswith('turn_on') else '调整'}")
            else:
                failed.append(f"{label}操作失败")

    parts = []
    if done:
        parts.append("，".join(done))
    if failed:
        parts.append("；".join(failed) + "失败了")
    return "。".join(parts) if parts else "没有执行任何操作"


# 供其他插件以 from-import 方式复用（保持与完整版一致的接口名）
ha_control_func = HA_CONTROL_FUNC_DESC
