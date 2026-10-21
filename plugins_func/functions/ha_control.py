"""
HomeAssistant 智能家居控制插件 —— `core/home` 引擎的适配层（2026-10-13 重构）

这一层现在只做三件事：
    1. 把大模型给的指令交给 `core.home` 引擎：语义寻址（房间/楼层/类型/别名/拼音）、
       动作规划、**前置可用性检查 + 后置状态校验**、安全分级、结果说人话；
    2. 兼容老调用方的入参形状（`entities` / `query` / `summary`），它们照样能用；
    3. 继续对外提供一批历史符号，别处还在用（**改名或删除前必须先 grep 全项目**）：
           HASS_BASE_URL、HASS_ACCESS_TOKEN、HEADERS、_HTTP_TIMEOUT、
           ALLOWED_ENTITIES、_ALLOWED_SET、_ENTITY_NAMES、
           sync_allowed_entities、_ensure_allowed_entities、_check_entity、_friendly、
           _short、_STATE_CN、_MEDIA_TYPE_CN、_DOMAIN_CN、_spoken_state、
           _media_device_desc、spoken_summary、ha_get_state、ha_summary、
           _resolve_service、ha_turn_on、ha_turn_off、apply_entities、
           _summarize_entities、apply_scene、legacy_op_to_call、apply_device_catalog

为什么工具描述里不再让大模型填 entity_id（主人 2026-10-13 批准的重构）：
    实测大模型会编造实体（light.living_room、camera.main_entrance 都是它编的），
    也会把 entity_id 念出来（「已打开 media_player.web_chrome_on_windows」）。
    现在只让它说「哪个房间、什么设备、做什么」，由引擎在真实清单里找。

设备边界（2026-09-17 起）：由 HA「暴露给语音助手(conversation)」的实体清单决定。
配置来源（2026-10-13 起）：统一走 `core/home/config.py`（sqlite system_config →
环境变量回退），**源码里不再有任何硬编码令牌**。
"""
import asyncio
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

import aiohttp

from plugins_func.register import FunctionItem, ToolType, Action, ActionResponse, all_function_registry

from core.home import actions, engine, guard, speech
from core.home.config import get_ha_config
from core.home.executor import ExecResult, execute
from core.home.registry import Entity, get_registry
from core.home.resolver import Command

# 状态口语化文案（主人 2026-10-12 实测校准过，实现已迁到 core/home/speech.py，
# 这里按旧名字再导出，老代码和老单测都按这些名字 import）
from core.home.speech import (
    DOMAIN_CN as _DOMAIN_CN,
    _MEDIA_TYPE_CN,
    _STATE_CN,
    media_device_desc as _media_device_desc,
    spoken_state as _spoken_state,
    spoken_summary,
)


# ==================== HA 配置（唯一来源：core/home/config.py） ====================
def _get_ha_config() -> Dict[str, str]:
    """读 HA 地址与令牌；读不到返回空串（调用时会给出人话提示，不会抛到用户脸上）"""
    try:
        base, token = get_ha_config(force=True)
        return {"base_url": base, "token": token}
    except Exception as e:
        print(f"[HA控制] 读取 HA 配置失败: {e}")
        return {"base_url": "", "token": ""}


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


def _cfg() -> tuple:
    """动态取配置（30 秒缓存），不要依赖 import 时冻结的模块级常量"""
    cfg = _get_ha_config()
    return cfg["base_url"], cfg["token"]


def _cfg_base() -> str:
    return _cfg()[0]


def _headers() -> Dict[str, str]:
    """动态 headers，同时把模块级 HEADERS 原地刷新（照顾 from-import 的老调用方）"""
    base, token = _cfg()
    HEADERS["Authorization"] = f"Bearer {token}"
    HEADERS["Content-Type"] = "application/json"
    return dict(HEADERS)


def _not_configured() -> Optional[str]:
    base, token = _cfg()
    if not base or not token:
        return "还没有配置 Home Assistant 的地址或令牌，请在管理台里填好再试"
    return None


# ==================== 可控设备边界 ====================
# 由 HA「暴露给语音助手」的清单决定，这里只是它的镜像，供老调用方查名字/做校验
ALLOWED_ENTITIES: List[str] = []
_ALLOWED_SET: set = set()
_ENTITY_NAMES: Dict[str, str] = {}


async def sync_allowed_entities(force: bool = False) -> int:
    """刷新可控设备清单（现在是刷新引擎的实体注册表，再镜像一份到这里）

    返回同步到的实体数量；拉取失败时沿用旧值，绝不把清单清空。
    """
    try:
        reg = get_registry()
        await reg.ensure_ready(force=force)
        entities = reg.all()
        if not entities:
            print("[HA控制] 未取到 HA 暴露实体，沿用现有清单")
            return len(ALLOWED_ENTITIES)

        # 原地刷新（别处可能持有同一个 list 对象）
        ALLOWED_ENTITIES[:] = [e.entity_id for e in entities]
        _ALLOWED_SET.clear()
        _ALLOWED_SET.update(ALLOWED_ENTITIES)
        _ENTITY_NAMES.clear()
        for e in entities:
            _ENTITY_NAMES[e.entity_id] = speech.label(e)

        apply_device_catalog(reg)
        print(f"[HA控制] 设备清单已刷新: {len(ALLOWED_ENTITIES)} 个实体")
        return len(ALLOWED_ENTITIES)
    except Exception as e:
        print(f"[HA控制] 同步设备清单失败: {e}")
        return len(ALLOWED_ENTITIES)


async def _ensure_allowed_entities() -> None:
    """清单为空时补拉一次（失败只打日志，交给后面的检查给人话提示）"""
    reg = get_registry()
    if reg.all():
        return
    try:
        await sync_allowed_entities()
    except Exception as e:
        print(f"[HA控制] 补拉设备清单失败: {e}")


def _check_entity(entity_id: str) -> Optional[str]:
    """实体在不在可控清单里；不在返回错误说明（给 API/日志看，不是给 TTS 念的）"""
    if entity_id in _ALLOWED_SET or get_registry().get(entity_id):
        return None
    return f"设备 {entity_id} 不在允许控制列表中（仅限 HA 暴露给语音助手的设备）"


def _friendly(entity_id: str) -> str:
    """友好名。**绝不返回 entity_id** —— 它是拼音，念出来主人听不懂"""
    name = _ENTITY_NAMES.get(entity_id) or ""
    if not name or "." in name:
        ent = get_registry().get(entity_id)
        name = speech.label(ent) if ent else ""
    if not name:
        domain = str(entity_id).split(".")[0]
        name = _DOMAIN_CN.get(domain) or "这个设备"
    return name


def _short(text: Any, limit: int = 200) -> str:
    """截断，避免超长文本被 TTS 念个没完"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ==================== 单个实体状态查询 ====================
async def ha_get_state(entity_id: str) -> Dict[str, Any]:
    """查一个实体的实时状态，返回 HA 原生结构（含 state / attributes）

    失败返回 {"error": ...}，形状与重构前一致（调用方按 error 键判断）。
    """
    await _ensure_allowed_entities()
    blocked = _check_entity(entity_id)
    if blocked:
        return {"error": blocked}

    reg = get_registry()
    st = await reg.live_state(entity_id)
    if st is None:
        ent = reg.get(entity_id)
        if ent:                     # 网络不通但缓存里有 → 用缓存，别报错
            return {"entity_id": entity_id, "state": ent.state, "attributes": ent.attrs}
        return {"error": f"查询 {_friendly(entity_id)} 失败，现在拿不到它的状态"}
    return st


# ==================== 全屋聚合 ====================
_ON_STATES = {"on", "open", "opening", "opening", "playing", "paused", "locked",
              "cleaning", "returning", "heat", "cool", "heat_cool", "auto", "dry",
              "fan_only", "home", "locked", "unlocked"}
_CLIMATE_ON = {"heat", "cool", "heat_cool", "auto", "dry", "fan_only"}
_SKIP_STATES = {"unknown", "none", ""}


def _is_on_state(domain: str, state: str) -> bool:
    s = str(state or "").lower()
    if domain == "climate":
        return s in _CLIMATE_ON
    if domain == "lock":
        return s == "locked"
    if domain == "cover":
        return s in ("open", "opening")
    if domain == "media_player":
        return s == "playing"
    if domain == "vacuum":
        return s in ("cleaning", "returning")
    return s == "on"


async def ha_summary(domains: Optional[List[str]] = None, fresh: bool = True) -> Dict[str, Any]:
    """全屋聚合：每个 domain 开了几个 / 一共几个，外加离线数

    数据源改为引擎的注册表（WS 订阅 + 30 秒兜底刷新），不再每次 GET /api/states。
    返回形状与重构前一致：{domain: {"on": n, "total": m}, "unavailable": k}
    """
    await _ensure_allowed_entities()
    reg = get_registry()
    try:
        await reg.ensure_ready(force=bool(fresh))
    except Exception as e:
        print(f"[HA控制] 刷新全屋状态失败: {e}")

    entities = reg.all()
    if not entities:
        return {"error": _not_configured() or "现在拿不到家里的设备状态，稍后再试"}

    want = {str(d).strip().lower() for d in (domains or []) if str(d).strip()}
    agg: Dict[str, Dict[str, int]] = {}
    unavailable = 0
    for e in entities:
        if want and e.domain not in want:
            continue
        state = str(e.state or "").lower()
        if state in ("unavailable", "unreachable"):
            unavailable += 1
            continue
        if state in _SKIP_STATES:
            continue
        b = agg.setdefault(e.domain, {"on": 0, "total": 0})
        b["total"] += 1
        if _is_on_state(e.domain, state):
            b["on"] += 1

    out: Dict[str, Any] = {d: agg[d] for d in sorted(agg)}
    out["unavailable"] = unavailable
    return out


# ==================== 直连服务调用（供管理台 API / 音乐插件用） ====================
def _resolve_service(entity_id: str, kind: str, params: Dict[str, Any]) -> tuple:
    """把「开 / 关」翻译成 HA REST 服务地址与负载

    只给 `core/api/smart_home_api.py`、`ha_music.py` 这类**直连**调用方用；
    语音链路已经走 core.home 引擎（带可用性检查与结果校验），不经过这里。
    """
    domain = entity_id.split(".")[0]
    params = params or {}
    data: Dict[str, Any] = {"entity_id": entity_id}

    if kind == "off":
        # 窗帘没有 turn_off，门锁没有 turn_off：按各自 domain 的正确服务走
        service = {"cover": "close_cover", "lock": "lock", "scene": "turn_off"}.get(domain, "turn_off")
    else:
        service = {"cover": "open_cover", "lock": "unlock"}.get(domain, "turn_on")
        if domain == "cover" and params.get("position") is not None:
            service = "set_cover_position"
            data["position"] = params["position"]
        for key in ("brightness", "brightness_pct", "brightness_step_pct",
                    "color_temp_kelvin", "color_temp", "temperature", "hvac_mode",
                    "fan_mode", "volume_level", "transition"):
            if params.get(key) is not None:
                data[key] = params[key]
        if domain != "cover" and params.get("position") is not None:
            data["position"] = params["position"]

    return f"{_cfg_base()}/api/services/{domain}/{service}", data


async def _call_service(entity_id: str, kind: str, params: Dict[str, Any]) -> Dict[str, Any]:
    missing = _not_configured()
    if missing:
        return {"error": missing}
    url, data = _resolve_service(entity_id, kind, params)
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(url, headers=_headers(), json=data) as resp:
                body = await resp.text()
                if resp.status not in (200, 201):
                    return {"error": f"HTTP {resp.status} {body[:160]}"}
                try:
                    return {"result": json.loads(body)}
                except Exception:
                    return {"result": body[:160]}
    except asyncio.TimeoutError:
        return {"error": "Home Assistant 没有响应（超时）"}
    except Exception as e:
        return {"error": _short(f"调用 Home Assistant 失败: {e}", 160)}


async def ha_turn_on(entity_id: str, **kwargs) -> Dict[str, Any]:
    return await _call_service(entity_id, "on", kwargs)


async def ha_turn_off(entity_id: str, **kwargs) -> Dict[str, Any]:
    return await _call_service(entity_id, "off", kwargs)


# ==================== 老入参形状 → 引擎指令 ====================
_CLOSE_WORDS = ("off", "close", "closed", "turn_off", "关闭", "关")
_OPEN_WORDS = ("on", "open", "opened", "turn_on", "打开", "开")
_STOP_WORDS = ("stop", "stop_cover", "pause", "停下", "停")


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _spec_to_action(spec: Any) -> tuple:
    """把老 spec（"on" / {"state":"on","brightness_pct":80}）翻译成 (canonical_action, value)

    修掉两个老坑：
      - `stop` 以前被塞进 _CLOSE_WORDS，窗帘「停一下」变成「关窗帘」；
      - 门锁以前 state=lock 会走 lock/turn_off（HA 根本没这个服务，必失败）。
    """
    if isinstance(spec, dict):
        raw_state = str(spec.get("state") or "").strip().lower()
    else:
        raw_state = str(spec or "").strip().lower()
        spec = {}

    if raw_state in _STOP_WORDS:
        return ("pause" if raw_state == "pause" else "stop"), None
    if raw_state in ("lock", "locked", "上锁"):
        return "lock", None
    if raw_state in ("unlock", "unlocked", "开锁"):
        return "unlock", None
    if raw_state == "toggle":
        return "toggle", None
    if raw_state in ("play", "播放"):
        return "play", None
    if raw_state in ("next", "下一首"):
        return "next", None
    if raw_state in ("previous", "上一首"):
        return "previous", None

    # 数值优先：说了数值就按数值动作走（引擎会转成正确的 HA 服务）
    if spec.get("brightness_step_pct") is not None:
        step = _num(spec["brightness_step_pct"], 0) or 0
        return ("brightness_up" if step >= 0 else "brightness_down"), abs(step)
    if spec.get("brightness_pct") is not None:
        return "set_brightness", _num(spec["brightness_pct"])
    if spec.get("brightness") is not None:          # HA 原生 0-255
        b = _num(spec["brightness"])
        return "set_brightness", (None if b is None else round(b / 255 * 100))
    if spec.get("color_temp_kelvin") is not None:
        return "set_color_temp", _num(spec["color_temp_kelvin"])
    if spec.get("color_temp") is not None:          # mired → kelvin
        m = _num(spec["color_temp"])
        return "set_color_temp", (None if not m else round(1000000 / m))
    if spec.get("position") is not None or spec.get("current_position") is not None:
        p = _num(spec.get("position", spec.get("current_position")))
        return "set_position", p
    if spec.get("temperature") is not None:
        return "set_temperature", _num(spec["temperature"])
    if spec.get("hvac_mode"):
        return "set_hvac_mode", str(spec["hvac_mode"])
    if spec.get("fan_mode"):
        return "set_fan", str(spec["fan_mode"])
    if spec.get("volume_pct") is not None:
        return "set_volume", _num(spec["volume_pct"])
    if spec.get("volume_level") is not None:
        v = _num(spec["volume_level"])
        return "set_volume", (None if v is None else round(v * 100))

    if raw_state in _CLOSE_WORDS:
        return "turn_off", None
    if raw_state in _OPEN_WORDS or raw_state in ("open", "opened"):
        return "open" if raw_state in ("open", "opened") else "turn_on", None
    return "turn_on", None


def _spec_to_command(entity_id: str, spec: Any) -> Command:
    action, value = _spec_to_action(spec)
    domain = entity_id.split(".")[0]
    # 非门锁设备收到 lock/unlock（老代码把「关」写成 lock）→ 退回开/关
    if domain != "lock" and action in ("lock", "unlock"):
        action = "turn_off" if action == "lock" else "turn_on"
    return Command(action=action, entity_id=entity_id, value=value)


async def apply_entities(entities: Dict[str, Any], *, trusted: bool = False,
                         conn=None) -> Dict[str, Any]:
    """按 entity_id 精确控制（老接口，形状不变）

    trusted=False（默认）：来自大模型/语音直通等「不可信」来源，必须过安全分级——
        只要有一项需要确认（开锁、开车库门）或被禁止，**整批都不执行**并返回 error，
        由调用方降级到引擎的口头确认流程。2026-10-13 实测过：这条路径原本能直接开锁。
    trusted=True：配置里写死的自动化（车牌预欢迎等），由主人事先配置，沿用原行为。

    入参：{"light.xxx": "on"} 或 {"light.xxx": {"state":"on","brightness_pct":80}}
    返回：{"ok": n, "total": m, "failed": {entity_id: 人话原因},
           "results": [ExecResult], "snapshots": {...}, "spoken": "..."}

    与重构前的区别（主人 2026-10-13 批准的止血项）：
      - **离线设备不再假成功**：unavailable → offline，unknown → not_connected，如实播报；
      - **控制后回读状态校验**：状态没变成期望值 → unconfirmed（「按了，但没确认到位」）；
      - 失败原因说人话，不再是 `HTTP 400 ...`。
    """
    if not isinstance(entities, dict) or not entities:
        return {"error": 'entities 不能为空，格式：{"light.xxx": "on" 或 {"state":"on","brightness_pct":80}}'}

    missing = _not_configured()
    if missing:
        return {"error": missing}

    await _ensure_allowed_entities()
    blocked = [eid for eid in entities if _check_entity(eid)]
    if blocked:
        return {"error": "以下设备不在允许控制列表中（仅限 HA 暴露给语音助手的设备）："
                         + "，".join(blocked)}

    reg = get_registry()
    try:
        await reg.ensure_ready()
    except Exception as e:
        print(f"[HA控制] 刷新设备状态失败: {e}")

    planned: List[tuple] = []
    results: List[Any] = []
    snapshots: Dict[str, Dict[str, Any]] = {}
    gated: List[str] = []
    for eid, spec in entities.items():
        ent = reg.get(eid)
        if ent is None:                     # 清单里有但注册表没细节 → 兜个最小实体
            ent = Entity(entity_id=eid, name=_friendly(eid))
        cmd = _spec_to_command(eid, spec)
        call = actions.plan(ent, cmd.action, cmd.value)
        if isinstance(call, actions.Unsupported):      # plan 返回的是对象，不是异常
            results.append(ExecResult(entity=ent, status="unsupported", action=cmd.action,
                                      value=cmd.value, detail=call.reason))
            continue
        if not trusted and guard.classify(ent, call, conn) != guard.FREE:
            gated.append(speech.label(ent, with_area=True))
        planned.append((ent, call))

    if gated:
        return {"error": "「" + "、".join(gated[:3]) + "」这类操作我要先跟你确认，"
                         "请直接对我说，比如「开锁」「打开车库门」，我会问你「确认吗」"}

    if planned:
        results.extend(await execute(reg, planned, snapshots))

    failed: Dict[str, str] = {}
    ok = 0
    for r in results:
        if r.status == "ok":
            ok += 1
        elif r.status in ("already",):
            ok += 1                         # 已经在目标状态，算成功（不重复动作）
        else:
            failed[r.entity.entity_id] = speech.result_reason(r) or r.detail or "没成功"

    spoken = speech.summarize_results(results)
    out = {
        "ok": ok,
        "total": len(entities),
        "failed": failed,
        "results": results,
        "snapshots": snapshots,
        "spoken": spoken,
    }
    print(f"[HA控制] apply_entities ok={ok}/{len(entities)} failed={list(failed)}")
    return out


def _summarize_entities(entities: Dict[str, Any], res: Dict[str, Any]) -> str:
    """把控制结果压成一句中文口语（可直接播报）

    优先用引擎给的 ExecResult（能说清「离线 / 没确认到位 / 已经是这个状态」）；
    拿不到 results 时（比如单测直接塞了个朴素 dict）退回按 ok/total 播报。
    """
    if isinstance(res, dict) and res.get("results"):
        return speech.summarize_results(res["results"])

    ok = int((res or {}).get("ok", 0) or 0)
    total = int((res or {}).get("total", len(entities or {})) or 0)
    failed = (res or {}).get("failed") or {}
    if failed:
        detail = "；".join(f"{_friendly(e)} 没成功" for e in failed)
        return f"{ok}/{total} 个设备已控制，{detail}"
    if total == 1:
        eid = next(iter(entities or {}), "")
        return f"已控制{_friendly(eid)}"
    return f"已控制 {total} 个设备"


async def apply_scene(entities: Dict[str, Any], transition: Optional[float] = None) -> Dict[str, Any]:
    """一次性把多个实体设成目标状态（HA scene.apply）

    实测限制：窗帘只认 state=open/closed，传 current_position 会被忽略。
    保留给 run_ai_scene / batch_device_control 这类批处理用；语音链路走引擎。
    """
    missing = _not_configured()
    if missing:
        return {"error": missing}
    if not isinstance(entities, dict) or not entities:
        return {"error": "entities 不能为空"}

    payload: Dict[str, Any] = {"entities": entities}
    if transition is not None:
        try:
            payload["transition"] = float(transition)
        except (TypeError, ValueError):
            pass

    url = f"{_cfg_base()}/api/services/scene/apply"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(url, headers=_headers(), json=payload) as resp:
                body = await resp.text()
                if resp.status not in (200, 201):
                    return {"error": f"HTTP {resp.status} {body[:160]}"}
                return {"ok": len(entities), "total": len(entities), "failed": {}}
    except asyncio.TimeoutError:
        return {"error": "Home Assistant 没有响应（超时）"}
    except Exception as e:
        return {"error": _short(f"场景执行失败: {e}", 160)}


def legacy_op_to_call(entity_id: str, action: str, **kwargs) -> Dict[str, Any]:
    """把老的 (entity_id, action, **kw) 三元组翻译成 ha_control 的入参

    供 `run_ai_scene.py` / `batch_device_control.py` 沿用，形状不变。
    """
    act = str(action or "").strip().lower()
    if act in ("get_state", "query", "state"):
        return {"query": entity_id}

    spec: Dict[str, Any] = {"state": "off" if act in _CLOSE_WORDS else "on"}
    if kwargs.get("brightness") is not None:      # HA 原生 0-255 → 百分比
        b = _num(kwargs["brightness"])
        if b is not None:
            spec["brightness_pct"] = round(b / 255 * 100)
    if kwargs.get("brightness_pct") is not None:
        spec["brightness_pct"] = kwargs["brightness_pct"]
    if kwargs.get("brightness_step_pct") is not None:
        spec["brightness_step_pct"] = kwargs["brightness_step_pct"]
    if kwargs.get("color_temp_kelvin") is not None:
        spec["color_temp_kelvin"] = kwargs["color_temp_kelvin"]
    if kwargs.get("color_temp") is not None:
        spec["color_temp_kelvin"] = kwargs["color_temp"]
    if kwargs.get("position") is not None:
        spec["position"] = kwargs["position"]
    if kwargs.get("temperature") is not None:
        spec["temperature"] = kwargs["temperature"]
    return {"entities": {entity_id: spec}}


# ==================== 工具入口 ====================
async def ha_control(conn, commands: List[Dict[str, Any]] = None,
                     entities: Dict[str, Any] = None, query=None,
                     summary: bool = False):
    """家居控制 / 查询的唯一入口（交给 core.home 引擎）

    参数:
    - commands: **推荐**。[{"action":"turn_on","name":"射灯一","area":"总经理办公室"}]
                只写中文设备名 / 房间 / 类型，引擎自己去真实清单里找。
    - entities: 老形状，按 entity_id 精确控制（代码仍支持，但不再推荐给大模型）。
    - query:    要查询的设备，中文名或 entity_id，字符串或列表。
    - summary:  True 时做全屋聚合，返回「家里现在：灯开着 2/4……」一句话。

    返回: ActionResponse（一句简短中文口语，不含 entity_id、不含 HA 原始 JSON）
    """
    print(f"[HA控制] commands={commands}, entities={list((entities or {}).keys())}, "
          f"query={query}, summary={summary}")

    try:
        if commands or query or summary or not entities:
            result = await engine.run(conn=conn, commands=commands, query=query,
                                      summary=bool(summary))
        else:                                   # 只有 entities：不要引擎那句「请告诉我要控制哪个设备」
            result = engine.EngineResult("")

        if entities:
            res = await apply_entities(entities, conn=conn)
            if "error" in res:
                if not result.spoken:
                    return ActionResponse(action=Action.ERROR, response=_short(res["error"]))
                extra = _short(res["error"], 80)
            else:
                extra = _summarize_entities(entities, res)
            result_spoken = "；".join([p for p in (result.spoken, extra) if p])
        else:
            result_spoken = result.spoken

        if not result_spoken:
            return ActionResponse(
                action=Action.ERROR,
                response="请说要控制哪个设备（例如「把客厅的灯打开」），或要查什么状态",
            )

        print(f"[HA控制] 播报: {result_spoken}")
        return ActionResponse(action=Action.RESPONSE, response=_short(result_spoken, 300))

    except Exception as e:
        print(f"[HA控制] 失败: {e}")
        return ActionResponse(action=Action.ERROR, response=_short(f"家居控制失败: {e}"))


# ==================== 工具描述 ====================
_ACTION_ENUM = [
    "on", "off", "open", "close", "stop", "toggle",
    "set_brightness", "adjust_brightness", "brightness_up", "brightness_down",
    "set_color_temp", "set_position",
    "set_temperature", "adjust_temperature", "temperature_up", "temperature_down",
    "set_hvac_mode", "set_fan",
    "set_volume", "volume_up", "volume_down", "mute", "unmute",
    "play", "pause", "next", "previous",
    "lock", "unlock", "activate", "start", "return_home", "undo",
]

ha_control_func = FunctionItem(
    name="ha_control",
    description={
        "type": "function",
        "function": {
            "name": "ha_control",
            "description": (
                "控制或查询家里的智能设备（灯、窗帘、空调、风扇、门锁、音箱、开关、扫地机、场景）。"
                "⚠️ 用户说「开灯」「关灯」「开空调」「关窗帘」「调亮一点」「锁门」等家居指令时必须用本工具，"
                "不要用 mac_app。"
                "**只写中文设备名、房间名和设备类型，绝对不要写 entity_id**"
                "（形如 light.xxx、media_player.yyy 的英文标识一律不要出现，写错就找不到设备）。"
                "找不到确切名字时，只填 domain（+area），由系统在真实清单里匹配。"
                "控制多个设备就在 commands 里写多项。"
                "查询某个设备状态用 query；问「家里什么状态」「有几盏灯开着」这类总览用 summary=true。"
                "开门锁这类操作系统会先跟用户确认，不需要你替用户决定。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "description": "要执行的操作，一项一个动作。控制多个设备就写多项。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": _ACTION_ENUM,
                                    "description": (
                                        "动作。on/off=开关，open/close=窗帘等开合，stop=停下（窗帘停在中途），"
                                        "set_brightness=调到某个亮度（value 0-100），"
                                        "brightness_up/brightness_down=调亮一点/调暗一点（value 可省，默认 30），"
                                        "set_color_temp=色温（value: 暖/中性/冷 或 2700-6500），"
                                        "set_position=窗帘开合度（value 0-100），"
                                        "set_temperature=空调调到几度（value 数字），"
                                        "temperature_up/temperature_down=调高/调低一度，"
                                        "set_hvac_mode=空调模式（value: 制冷/制热/送风/除湿/自动），"
                                        "set_volume=音量（value 0-100），volume_up/volume_down=大点声/小点声，"
                                        "play/pause/next/previous=播放控制，"
                                        "lock/unlock=上锁/开锁（门锁必须明确说哪个，不要用 on/off），"
                                        "activate=启动场景，undo=恢复上一次操作前的样子。"
                                    ),
                                },
                                "name": {
                                    "type": "string",
                                    "description": (
                                        "设备中文名（用户嘴里的那个名字，如「射灯一」「左侧窗帘」「办公室音箱」）。"
                                        "**不要写 entity_id**。不确定名字就留空，只填 domain/area。"
                                        "家里现有的设备见下方清单（离线设备也在里面，但控制不了）："
                                    ),
                                },
                                "area": {
                                    "type": "string",
                                    "description": "房间名（如「客厅」「总经理办公室」）。用户说了房间就填，"
                                                   "没说不填；用户说「这里」「这个房间」时也不用填，"
                                                   "系统会用说话人所在终端的房间。",
                                },
                                "floor": {
                                    "type": "string",
                                    "description": "楼层（如「二层」「三楼」）。用户说了「二楼所有灯」才填。",
                                },
                                "domain": {
                                    "type": "string",
                                    "enum": ["light", "cover", "climate", "fan", "lock",
                                             "media_player", "switch", "vacuum", "humidifier",
                                             "scene", "script"],
                                    "description": "设备类型。用户只说「灯」「窗帘」「空调」这种类别时填这个，"
                                                   "比如「把客厅的灯都关掉」= domain=light + area=客厅 + action=off。",
                                },
                                "device_class": {
                                    "type": "string",
                                    "description": "更细的类型，仅必要时填：curtain(布帘)/shade(卷帘)/"
                                                   "blind(百叶窗)/garage(车库门)/speaker(音箱)/tv(电视)。",
                                },
                                "value": {
                                    "description": "动作需要的数值或选项：亮度/音量/开合度用 0-100 的数字，"
                                                   "温度用数字，色温可写「暖」「冷」或 2700-6500，"
                                                   "空调模式写「制冷」「制热」等。不需要数值就不填。",
                                },
                            },
                            "required": ["action"],
                        },
                    },
                    "query": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要查询状态的设备中文名（如 [\"射灯一\"]，也可写房间+类型如 [\"客厅的灯\"]）。"
                                       "不要写 entity_id。",
                    },
                    "summary": {
                        "type": "boolean",
                        "description": (
                            "全屋总览。true 时忽略 commands/query，返回「家里现在：灯开着 2/4，窗帘都关着，"
                            "另有 N 个设备离线」这样一句话。适用于「家里什么状态」「有几盏灯开着」"
                            "「全屋设备总览」这类不指向具体设备的问题。查某个具体设备时不要用它，用 query。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    func=ha_control,
    type=ToolType.IOT_CTL,
)


def apply_device_catalog(devices=None) -> None:
    """把「家里现在有哪些设备」的紧凑摘要刷进工具描述（原地改 dict，引用共享）

    2026-10-13 起不再把 entity_id 清单塞给大模型（它会照着编 id、还会把 id 念出来），
    改成「房间 → 类型×数量」的摘要 + 设备名清单，让它用中文名字说话。

    devices 可以是：None（用引擎注册表）/ HomeRegistry / [{"entity_id","name","state"}…]
    """
    try:
        entities: List[Entity] = []
        if devices is None or hasattr(devices, "all"):
            reg = devices if hasattr(devices, "all") else get_registry()
            entities = list(reg.all())
        elif isinstance(devices, (list, tuple)):
            for d in devices:
                if not isinstance(d, dict):
                    continue
                eid = str(d.get("entity_id") or "")
                if not eid:
                    continue
                entities.append(Entity(entity_id=eid, name=str(d.get("name") or ""),
                                       state=str(d.get("state") or "unknown"),
                                       attrs=d.get("attributes") or {}))
        if not entities:
            return

        text = engine.catalog_summary(entities=entities)
        if not text:
            return
        props = ha_control_func.description["function"]["parameters"]["properties"]
        name_desc = props["commands"]["items"]["properties"]["name"]["description"]
        head = name_desc.split("家里现有的设备")[0]
        props["commands"]["items"]["properties"]["name"]["description"] = head + "家里现有的设备：" + text
    except Exception as e:
        print(f"[HA控制] 刷新工具描述失败: {e}")


all_function_registry["ha_control"] = ha_control_func
print("[HA控制] 函数已注册: ha_control（core/home 引擎适配层，语义寻址 + 结果校验 + 说人话）")
