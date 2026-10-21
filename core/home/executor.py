"""执行器：前置检查 → 下发 → 后置校验

「HA 返回 200」不等于「设备动了」。历史教训（2026-10-13）：4 盏离线射灯被「控制」，
系统报「已控制 4 个设备」，主人说「没反应」。所以：
1. 下发前先看设备此刻的状态：离线/未接入直接如实说，不假装执行
2. 已经是目标状态就不重复下发，如实说「本来就是开着的」
3. 下发后观察状态变化：看到到位才报成功；一直没变就说「发了指令但没看到变化」
   （窗帘/空调这类运动设备只要进入「正在开/关」就算受理，不等它走完）

状态来源：有实时订阅时直接读注册表（HA 推的 state_changed），不额外打 HTTP；
没有订阅时退化为按需 GET /api/states/<id>。
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from core.home.actions import ServiceCall, Unsupported, plan
from core.home.config import get_ha_config
from core.home.registry import STATELESS_DOMAINS, Entity, HomeRegistry

VERIFY_TIMEOUT = 3.0
VERIFY_INTERVAL = 0.25
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6)
_MAX_PARALLEL = 8


@dataclass
class ExecResult:
    entity: Entity
    # ok 已生效 | already 本来就是 | offline 离线 | not_connected 未接入 |
    # unconfirmed 指令已发但没看到变化 | failed HA 报错 | unsupported 不支持 | blocked 安全拦截
    status: str
    action: str = ""
    value: Any = None
    detail: str = ""


def snapshot_of(st: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """控制前的状态快照（用于「恢复刚才的样子」）"""
    if not isinstance(st, dict):
        return {}
    a = st.get("attributes") or {}
    snap = {"state": str(st.get("state", "")).lower()}
    for k in ("brightness", "color_temp_kelvin", "current_position", "temperature",
              "hvac_mode", "percentage", "volume_level"):
        if a.get(k) is not None:
            snap[k] = a[k]
    return snap


def restore_call(ent: Entity, snap: Dict[str, Any]) -> Optional[ServiceCall]:
    """按快照还原；不能安全还原的（例如开锁）返回 None"""
    if not snap:
        return None
    s = str(snap.get("state") or "")
    d = ent.domain
    if d == "light":
        if s == "off":
            r = plan(ent, "turn_off")
        elif snap.get("brightness") is not None:
            r = plan(ent, "set_brightness", round(float(snap["brightness"]) / 255 * 100))
        else:
            r = plan(ent, "turn_on")
    elif d == "cover":
        if snap.get("current_position") is not None:
            r = plan(ent, "set_position", snap["current_position"])
        else:
            r = plan(ent, "open" if s in ("open", "opening") else "close")
    elif d in ("switch", "fan", "input_boolean", "humidifier"):
        r = plan(ent, "turn_on" if s == "on" else "turn_off")
    elif d == "climate":
        if s == "off":
            r = plan(ent, "turn_off")
        elif snap.get("temperature") is not None:
            r = plan(ent, "set_temperature", snap["temperature"])
            if isinstance(r, ServiceCall) and s not in ("", "unknown", "unavailable"):
                r.data["hvac_mode"] = s          # 从「关」恢复要带上模式，只设温度可能仍是关着
        else:
            r = plan(ent, "set_hvac_mode", s)
    elif d == "lock" and s == "locked":
        r = plan(ent, "lock")       # 只还原「上锁」，绝不自动开锁
    else:
        return None
    return r if isinstance(r, ServiceCall) else None


async def _read_state(registry: HomeRegistry, ent: Entity) -> Dict[str, Any]:
    """尽量新的状态对象：订阅在线就读注册表，否则打一次 REST"""
    if registry.live:
        e = registry.get(ent.entity_id)
        if e is not None:
            return {"state": e.state, "attributes": e.attrs}
    live = await registry.live_state(ent.entity_id)
    if live is None:
        e = registry.get(ent.entity_id)
        return {"state": e.state if e else "unknown",
                "attributes": (e.attrs if e else {})}
    return live


async def _post_service(session: aiohttp.ClientSession, call: ServiceCall):
    base, token = get_ha_config()
    async with session.post(f"{base}/api/services/{call.domain}/{call.service}",
                            headers={"Authorization": f"Bearer {token}"},
                            json=call.data, timeout=_HTTP_TIMEOUT) as r:
        if r.status in (200, 201):
            return True, ""
        body = (await r.text())[:160]
        return False, f"HTTP {r.status} {body}"


async def execute_one(registry: HomeRegistry, session: aiohttp.ClientSession, ent: Entity,
                      call: ServiceCall, sem: asyncio.Semaphore,
                      snapshots: Optional[Dict[str, Dict[str, Any]]] = None) -> ExecResult:
    async with sem:
        st = await _read_state(registry, ent)
        cur = str(st.get("state", "")).lower()
        if cur == "unknown" and ent.domain not in STATELESS_DOMAINS:
            return ExecResult(ent, "not_connected", call.action, call.value)
        if cur in ("unavailable", "unreachable"):
            return ExecResult(ent, "offline", call.action, call.value)
        if snapshots is not None:
            snapshots[ent.entity_id] = snapshot_of(st)
        if call.idempotent and call.expect and call.expect(st) is True:
            return ExecResult(ent, "already", call.action, call.value)

        try:
            ok, err = await _post_service(session, call)
        except Exception as e:
            return ExecResult(ent, "failed", call.action, call.value, detail=str(e)[:100])
        if not ok:
            return ExecResult(ent, "failed", call.action, call.value, detail=err)
        if call.expect is None:
            return ExecResult(ent, "ok", call.action, call.value)

        # 后置校验：运动类设备进「正在…」也算受理
        waited = 0.0
        st2 = st
        while waited <= VERIFY_TIMEOUT:
            await asyncio.sleep(VERIFY_INTERVAL)
            waited += VERIFY_INTERVAL
            st2 = await _read_state(registry, ent)
            if cur == str(st2.get("state", "")).lower() and snapshot_of(st) == snapshot_of(st2):
                continue
            if call.expect(st2) is not False:
                return ExecResult(ent, "ok", call.action, call.value)
            return ExecResult(ent, "unconfirmed", call.action, call.value)
        # 一直没变化：若目标本来就满足（例如亮度已经是那个值）算成功，别报「没看到变化」
        if call.expect(st2) is True:
            return ExecResult(ent, "ok", call.action, call.value)
        return ExecResult(ent, "unconfirmed", call.action, call.value)


async def execute(registry: HomeRegistry, calls: List[tuple],
                  snapshots: Optional[Dict[str, Dict[str, Any]]] = None) -> List[ExecResult]:
    """calls = [(entity, ServiceCall)]，并发执行，单个失败不影响其他"""
    if not calls:
        return []
    sem = asyncio.Semaphore(_MAX_PARALLEL)
    async with aiohttp.ClientSession(trust_env=False) as session:
        return list(await asyncio.gather(
            *[execute_one(registry, session, e, c, sem, snapshots) for e, c in calls]))
