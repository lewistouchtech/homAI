"""家居引擎：唯一执行入口（语音直通、LLM 工具、场景、批量都走这里）

流程：寻址（说人话的名字）→ 安全分级 → 动作表 → 执行校验 → 人话播报。
大模型不再写 entity_id，也不再自己判断设备在不在线。
"""
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.home import actions, guard, speech
from core.home.executor import ExecResult, execute, restore_call
from core.home.registry import Entity, HomeRegistry, get_registry
from core.home.resolver import Command, Resolution, resolve
from core.home.session import get_session_store

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "data", "devices.db")
_ROOM_TTL = 60.0
_room_cache: Dict[str, Tuple[float, str]] = {}

# 口头确认/取消：必须是「整句就是确认词」。
# 绝不能用子串匹配：等确认期间说「把灯打开」「今天天气好」都带「开/好」，会把门锁开掉（2026-10-13 实测）。
_CONFIRM_WORDS = frozenset((
    "确认", "确定", "是的", "是", "是啊", "对", "对的", "好", "好的", "好吧", "行", "可以", "开吧",
    "开门", "开锁", "解锁", "嗯", "嗯嗯", "确认开锁", "确认开门", "确定开锁", "确定开门", "yes", "ok", "okay",
))
_CANCEL_WORDS = frozenset((
    "不用", "不用了", "算了", "取消", "别", "不要", "不开", "别开", "不需要", "不确认", "先不开", "不了",
))
_FILLERS = re.compile(r"[\s，。！？!?、,.~～…]+")
_TAIL = re.compile(r"[啊呀哦噢啦吧了哈]+$")


def _norm_reply(text: str) -> str:
    t = _FILLERS.sub("", str(text or "")).lower()
    return t


def _reply_kind(text: str) -> str:
    """'confirm' | 'cancel' | ''（和待确认无关的话）"""
    t = _norm_reply(text)
    if not t or len(t) > 8:
        return ""
    for cand in (t, _TAIL.sub("", t)):
        if cand in _CANCEL_WORDS:
            return "cancel"
        if cand in _CONFIRM_WORDS:
            return "confirm"
    return ""


@dataclass
class EngineResult:
    spoken: str
    ok: bool = True
    ask: bool = False                 # 需要用户回答（反问/待确认）
    entity_ids: List[str] = field(default_factory=list)
    statuses: List[str] = field(default_factory=list)
    hint_llm: bool = False            # True = spoken 不是念给用户的，而是给大模型的内部情况说明，让它自己用人话说


def device_area(device_id: str) -> str:
    """终端所在房间名（devices.room_id → rooms.room_name），拿不到返回空串"""
    if not device_id:
        return ""
    hit = _room_cache.get(device_id)
    if hit and time.time() - hit[0] < _ROOM_TTL:
        return hit[1]
    name = ""
    conn = None
    try:
        conn = sqlite3.connect(_DB)
        row = conn.execute(
            "SELECT r.room_name FROM devices d LEFT JOIN rooms r ON d.room_id = r.id "
            "WHERE d.device_id = ?", (device_id,)).fetchone()
        name = str(row[0] or "") if row else ""
    except Exception:
        name = ""
    finally:
        if conn:
            conn.close()
    _room_cache[device_id] = (time.time(), name)
    return name


def _speaker(conn) -> str:
    return str(getattr(conn, "current_speaker", "") or "").strip()


def _device_id(conn) -> str:
    return str(getattr(conn, "device_id", "") or "")


# ==================== 状态播报 ====================

_ON_STATES = {"light": {"on"}, "switch": {"on"}, "fan": {"on"}, "input_boolean": {"on"},
              "humidifier": {"on"}, "vacuum": {"cleaning", "returning"},
              "cover": {"open", "opening"}, "media_player": {"playing", "paused"},
              "lock": {"locked"}}
_CLIMATE_ON = {"heat", "cool", "heat_cool", "auto", "dry", "fan_only"}


def _is_on(e: Entity) -> bool:
    s = str(e.state).lower()
    if e.domain == "climate":
        return s in _CLIMATE_ON
    return s in _ON_STATES.get(e.domain, {"on"})


def _summary_text(ents: List[Entity]) -> str:
    live = [e for e in ents if e.online]
    offline = [e for e in ents if not e.online and not e.placeholder]
    parts: List[str] = []
    by_domain: Dict[str, List[Entity]] = {}
    for e in live:
        by_domain.setdefault(e.domain, []).append(e)
    for d in ("light", "cover", "climate", "switch", "fan", "media_player", "lock",
              "vacuum", "humidifier"):
        group = by_domain.get(d)
        if not group:
            continue
        on = [e for e in group if _is_on(e)]
        cn = speech.DOMAIN_CN.get(d, "设备")
        unit = speech.UNIT.get(d, "个")
        if d == "lock":
            parts.append(f"{len(on)}/{len(group)} 把门锁已上锁" if on else f"{len(group)} 把门锁都没上锁")
        elif d == "cover":
            parts.append(f"{len(on)}/{len(group)} 个窗帘开着" if on else f"{len(group)} 个窗帘都关着")
        elif not on:
            parts.append(f"{len(group)} {unit}{cn}都关着")
        elif len(on) == len(group):
            parts.append(f"{len(group)} {unit}{cn}全开着")
        else:
            parts.append(f"{cn}开着 {len(on)}/{len(group)}")
    text = ("家里现在：" + "，".join(parts) + "。") if parts else "家里没有查到在线的可控设备。"
    if offline:
        text += f"另有 {len(offline)} 个设备离线。"
    return speech.scrub(text)


def _state_text(e: Entity) -> str:
    name = speech.label(e, with_area=True)
    s = str(e.state).lower()
    if e.domain == "sensor":
        unit = e.attrs.get("unit_of_measurement") or ""
        return f"{name} {e.state}{unit}"
    if not e.online:
        return f"{name} 离线了"
    if e.domain == "light":
        if s == "on":
            b = e.attrs.get("brightness")
            if b is not None:
                return f"{name}开着，亮度百分之{int(round(float(b) / 255 * 100))}"
            return f"{name}开着"
        return f"{name}关着"
    if e.domain == "cover":
        pos = e.attrs.get("current_position")
        if pos is not None:
            return f"{name}开合度百分之{int(float(pos))}"
        return f"{name}{'开着' if s in ('open', 'opening') else '关着'}"
    if e.domain == "climate":
        t = e.attrs.get("temperature")
        mode = e.attrs.get("hvac_mode") or s
        cn = speech._HVAC_NAME.get(str(mode), str(mode))
        return f"{name}{cn}，目标温度{t}度" if t is not None else f"{name}{cn}"
    if e.domain == "lock":
        return f"{name}{'锁着' if s in ('locked', 'locking') else '没锁'}"
    if e.domain == "media_player":
        title = e.attrs.get("media_title")
        vol = e.attrs.get("volume_level")
        base = {"playing": "正在播放", "paused": "已暂停"}.get(s, "现在没在播放")
        extra = f"，放的是{title}" if title else ""
        extra += f"，音量百分之{int(round(float(vol) * 100))}" if vol is not None else ""
        return f"{name}{base}{extra}"
    return speech.scrub(f"{name}，状态是{s}")


# ==================== 主入口 ====================

async def run(conn=None, commands: Optional[List[dict]] = None, query: Optional[Any] = None,
              summary: bool = False, registry: Optional[HomeRegistry] = None) -> EngineResult:
    reg = registry or get_registry()
    # 状态查询必须新鲜：主人问「灯还开着吗」，答 30 秒前的缓存就很尴尬。
    # 控制指令走缓存即可（订阅了 state_changed，缓存本来就是活的）。
    await reg.ensure_ready(force=bool(summary or query))
    did = _device_id(conn)
    area = device_area(did)

    if summary:
        ents = [e for e in reg.all() if not e.placeholder]
        return EngineResult(_summary_text(ents), entity_ids=[e.entity_id for e in ents])

    if query:
        return await _run_query(reg, query, area, did)

    cmds = [c if isinstance(c, Command) else Command.from_dict(c) for c in (commands or [])]
    if not cmds:
        return EngineResult("请告诉我要控制哪个设备", ok=False)

    store = get_session_store()
    sp = store.scene(did)
    if sp is not None and sp.kind == "save":
        sp.kind, sp.ts = "applied", time.time()     # 用户转头调别的了：「要不要存」那个问题作废，免得后面一句「好的」对错事
    results: List[ExecResult] = []
    asks: List[str] = []
    planned: List[Tuple[Entity, Any]] = []
    snapshots: Dict[str, Dict[str, Any]] = {}
    pending_ids: List[str] = []
    pending_action = ""
    pending_value = None
    undone = False

    for cmd in cmds:
        action = actions.normalize_action(cmd.action)
        if cmd.action and not action:
            asks.append("我不认识这个操作")
            continue
        if cmd.action == "undo" or cmd.ref == "undo":
            got, notes = await _undo(reg, store, did, conn)
            undone = undone or bool(got)
            results.extend(got)
            asks.extend(notes)
            if not got and not notes:
                asks.append("刚才的操作没法恢复")
            continue
        if not action:
            action = "turn_on"
        res = resolve(reg, cmd, default_area=area, last_ids=_last_ids(store, did))
        if res.status == "none":
            asks.append(speech.none_text(res, cmd, _domain_of(cmd)))
            continue
        if res.status == "ambiguous":
            noun = speech._NOUN.get(_domain_of(cmd), "设备")
            asks.append(speech.ask_text(res, noun))
            continue
        plans = [(ent, actions.plan(ent, action, cmd.value)) for ent in res.entities]
        any_ok = any(not isinstance(c, actions.Unsupported) for _, c in plans)
        for ent, call in plans:
            if isinstance(call, actions.Unsupported):
                if len(plans) > 1 and any_ok:
                    continue      # 一组设备里个别不支持这个动作（如「再亮一点」时的窗帘）：悄悄跳过，不念出来
                results.append(ExecResult(ent, "unsupported", action, cmd.value, detail=call.reason))
                continue
            level = guard.classify(ent, call, conn) if conn is not None else guard.FREE
            if level == guard.DENY:
                results.append(ExecResult(ent, "blocked", action, cmd.value,
                                          detail=f"{speech.label(ent)}不允许这样操作"))
                continue
            if level == guard.CONFIRM:
                allowed, reason, _ = guard.check_speaker(conn) if conn is not None else (True, "", "")
                if not allowed:
                    results.append(ExecResult(ent, "blocked", action, cmd.value, detail=reason))
                    continue
                got = store.take_pending(did, [e.entity_id for e in res.entities], action)
                if got is None:
                    pending_ids = [e.entity_id for e in res.entities]
                    pending_action = action
                    pending_value = cmd.value
                    asks.append(_confirm_text(ent, action))
                    continue
            planned.append((ent, call))

    if planned:
        results.extend(await execute(reg, planned, snapshots=snapshots))

    spoken = speech.summarize_results(results) if results else ""
    if asks:
        spoken = "；".join([p for p in (spoken, *asks) if p])
    if not spoken:
        return EngineResult("没找到可以控制的设备", ok=False)

    if results:
        ids = [r.entity.entity_id for r in results]
        store.remember(did, ids, results[0].action, snapshots)
        if undone:
            store.drop_snapshots(did)        # 撤销过的不能再撤销一次
    if pending_ids:
        store.set_pending(did, pending_ids, pending_action, pending_value, _speaker(conn))
    return EngineResult(spoken, ok=bool(results) or not asks, ask=bool(asks) and not results,
                        entity_ids=[r.entity.entity_id for r in results],
                        statuses=[r.status for r in results])


def _confirm_text(ent: Entity, action: str) -> str:
    what = {"unlock": "开锁"}.get(action, "打开")
    return f"要{what}{speech.label(ent, with_area=True)}吗？你说「确认」我就开"


def _last_ids(store, did) -> List[str]:
    lc = store.last(did)
    return list(lc.entity_ids) if lc else []


def _domain_of(cmd: Command) -> str:
    from core.home.resolver import _split_domain
    return _split_domain(cmd)[0]


async def _run_query(reg: HomeRegistry, query: Any, area: str, did: str = "") -> EngineResult:
    store = get_session_store()
    items = query if isinstance(query, (list, tuple)) else [query]
    parts: List[str] = []
    ids: List[str] = []
    for q in items:
        if isinstance(q, dict):
            cmd = Command.from_dict(q)
        elif isinstance(q, str) and "." in q and q.split(".", 1)[0].isalpha():
            cmd = Command(entity_id=q)
        else:
            cmd = Command(name=str(q))
        res = resolve(reg, cmd, default_area=area, last_ids=_last_ids(store, did), for_query=True)
        if res.status == "none":
            parts.append(speech.none_text(res, cmd, _domain_of(cmd)))
            continue
        if res.status == "ambiguous":
            parts.append(speech.ask_text(res, speech._NOUN.get(_domain_of(cmd), "设备")))
            continue
        for ent in res.entities[:6]:
            await reg.live_state(ent.entity_id) if not reg.live else None
            parts.append(_state_text(ent))
            ids.append(ent.entity_id)
    return EngineResult(speech.scrub("；".join(parts)) if parts else "没查到设备状态",
                        ok=bool(parts), entity_ids=ids)


async def _undo(reg: HomeRegistry, store, did, conn=None) -> Tuple[List[ExecResult], List[str]]:
    """还原上一次操作。需要确认的还原（重新打开车库门等）不自动做，改为提醒用户亲口说。"""
    lc = store.last(did)
    if not lc or not lc.snapshots:
        return [], []
    calls: List[Tuple[Entity, Any]] = []
    notes: List[str] = []
    for eid, snap in lc.snapshots.items():
        ent = reg.get(eid)
        if ent is None:
            continue
        call = restore_call(ent, snap)
        if call is None:
            continue
        if guard.classify(ent, call, conn) != guard.FREE:
            notes.append(f"{speech.label(ent, with_area=True)}要重新打开的话需要你确认，请直接跟我说")
            continue
        calls.append((ent, call))
    if not calls:
        return [], notes
    return await execute(reg, calls), notes


# ==================== 口头确认（「确认」「算了」） ====================

async def confirm_or_cancel(conn, text: str, registry: Optional[HomeRegistry] = None) -> Optional[EngineResult]:
    """有待确认动作时，用户的「确认/算了」在这里落地。没有待确认动作返回 None（不干扰别的流程）。"""
    did = _device_id(conn)
    store = get_session_store()
    sp = store.scene(did)
    if sp is not None and sp.kind == "save":
        from core.home import scene_plan
        got = await scene_plan.handle_reply(conn, text, registry)
        if got is not None:
            return got
    pend = store._pending.get(did)
    if not pend or time.time() - pend.ts > 60.0:
        store._pending.pop(did, None)
        return None
    kind = _reply_kind(text)
    if kind == "cancel":
        store._pending.pop(did, None)
        return EngineResult("好的，不动它", ask=False)
    if kind != "confirm":
        return None                     # 和待确认无关：原样放行，待确认继续等（60 秒过期）
    if pend.speaker and _speaker(conn) != pend.speaker:
        return EngineResult("这个操作要提出的人自己确认才行", ok=False)
    reg = registry or get_registry()
    await reg.ensure_ready()
    allowed, reason, _ = guard.check_speaker(conn) if conn is not None else (True, "", "")
    if not allowed:
        store._pending.pop(did, None)
        return EngineResult(reason, ok=False)
    ents = [reg.get(e) for e in pend.entity_ids if reg.get(e)]
    if not ents:
        store._pending.pop(did, None)
        return EngineResult("设备找不到了", ok=False)
    calls = []
    for ent in ents:
        call = actions.plan(ent, pend.action, pend.value)
        if isinstance(call, actions.Unsupported):
            continue
        if guard.classify(ent, call, conn) != guard.CONFIRM:
            continue
        calls.append((ent, call))
    store._pending.pop(did, None)
    if not calls:
        return EngineResult("这个操作我不能做", ok=False)
    snapshots: Dict[str, Dict[str, Any]] = {}
    results = await execute(reg, calls, snapshots=snapshots)
    store.remember(did, [r.entity.entity_id for r in results], pend.action, snapshots)
    return EngineResult(speech.summarize_results(results), entity_ids=[r.entity.entity_id for r in results],
                        statuses=[r.status for r in results])


def has_pending(conn) -> bool:
    did = _device_id(conn)
    store = get_session_store()
    pend = store._pending.get(did)
    sp = store.scene(did)
    return bool((pend and time.time() - pend.ts <= 60.0) or (sp is not None and sp.kind == "save"))


# ==================== 提示词用的设备摘要 ====================

def catalog_summary(registry: Optional[HomeRegistry] = None, max_rooms: int = 12,
                    entities: Optional[List[Entity]] = None) -> str:
    """「房间 → 类型×数量」紧凑摘要，替代把全表设备塞进提示词

    entities 可直接传实体列表（老调用方手上是 dict 清单时用），否则用注册表。
    """
    reg = registry or get_registry()
    src = list(entities) if entities else reg.all()
    ents = [e for e in src if not e.placeholder and e.domain in actions._PLANNERS]
    rooms: Dict[str, Dict[str, int]] = {}
    for e in ents:
        rooms.setdefault(e.area or "未分配房间", {}).setdefault(e.domain, 0)
        rooms[e.area or "未分配房间"][e.domain] += 1
    lines = []
    for room, doms in list(rooms.items())[:max_rooms]:
        detail = " ".join(f"{speech.DOMAIN_CN.get(d, d)}{n}" for d, n in sorted(doms.items()))
        lines.append(f"{room}：{detail}")
    offline = len([e for e in src if not e.online and not e.placeholder])
    text = "；".join(lines)
    if offline:
        text += f"。另有{offline}个设备当前离线（离线设备控制不了，要如实告知）"
    return text
