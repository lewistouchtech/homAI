"""心情场景：用户说出心情/氛围 → 直接调（不征求确认）→ 一句自然的话带过 → 之后按语境微调/保存

设计（2026-10-13 主人纠正后的版本）：
- 用户累了说「来个放松的氛围」，不该先念一大串方案再等确认；管家直接把灯调柔和、放轻音乐，
  只说一句自然的话。调得不合适，用户自己会说「再亮一点」，接着调就行。
- 对用户说的话由大模型自己写（`say`）：口语、体贴，不报设备名、不报数字和状态。
- 引擎只负责「调」：寻址、剔除离线/不支持的、**开锁/开门这类要确认的操作一律不放进场景**，
  真的去调（有后置校验、可「撤销」）。局部设备离线不用念出来——没人关心；只有一个都没调成才说一句。
- 要不要问「存成固定场景」看语境：大模型觉得用户心情不错才给 `save_ask`；用户累了/低落就不问；
  用户主动说「把这个存成场景」随时可以存（`save_applied`），存的是**当前实际状态**（含用户后来调过的），
  而且存的是人话指令（名字+房间），不是 entity_id。
"""
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.logger import setup_logging
from core.home import actions, guard, scenes, speech
from core.home.registry import Entity, HomeRegistry, get_registry
from core.home.resolver import Command, resolve
from core.home.session import ScenePlan, get_session_store

TAG = __name__
logger = setup_logging()

_MEDIA_WORDS = ("音箱", "音响", "播放", "音乐", "喇叭", "media_player")
_music_player: Optional[Callable[[Any, str], bool]] = None     # 仅预置场景沿用（心情场景的音乐由大模型另调音乐工具）
_on_saved: Optional[Callable[[], None]] = None

SAVE_CONFIRM = frozenset(("保存", "保存吧", "存吧", "存下来", "存一下", "要", "要的", "要保存", "好的",
                          "好", "可以", "行", "嗯", "是的", "对", "存", "好啊", "好呀"))
SAVE_CANCEL = frozenset(("不用", "不用了", "不要", "不存", "不保存", "不需要", "算了", "不了", "取消"))

MAX_GUESS_DEVICES = 6
DEFAULT_SAY = "好的，我来给你调一下。"
NOTHING_SAY = "这个我一时帮不上忙，你再跟我说说想要什么感觉？"
NO_RESPONSE_SAY = "家里的设备好像没反应，可能是断线了。"
# 给大模型看的内部说明（不是念给用户的）：灯光等没调成时，让它自己说一句自然的话，别报设备名和技术细节
LLM_FAILED_HINT = ("（内部情况：家里的灯光、窗帘这些设备现在都联系不上，没能调成。请你用一句自然、简短、体贴的话告诉用户，"
                   "不要提设备名、编号、离线之类的技术说法；合适的话可以顺手放点轻音乐陪他。）")
LLM_NOTHING_HINT = ("（内部情况：家里没有可以按这个心情调的设备。请你用一句自然、简短的话告诉用户，"
                    "不要提设备名和技术细节；如果合适，可以顺手放点音乐。）")


def set_music_player(fn: Optional[Callable[[Any, str], bool]]) -> None:
    global _music_player
    _music_player = fn


def set_on_saved(fn: Optional[Callable[[], None]]) -> None:
    global _on_saved
    _on_saved = fn


def _domain(cmd: Command) -> str:
    from core.home.resolver import _split_domain
    return _split_domain(cmd)[0]


def _clean_cmd(cmd: Command, action: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {"action": action}
    for k in ("name", "area", "floor", "domain", "device_class"):
        v = getattr(cmd, k)
        if v:
            d[k] = v
    if cmd.value is not None:
        d["value"] = cmd.value
    if cmd.all:
        d["all"] = True
    return d


def _guess_domain(cmd: Command) -> str:
    """大模型常把「客厅的灯」「客厅窗帘」当设备名写进 name：从里面认出设备类型"""
    from core.home.resolver import DOMAIN_WORDS, _split_domain
    d = _split_domain(cmd)[0]
    if d:
        return d
    text = cmd.name or ""
    for w in sorted(DOMAIN_WORDS, key=len, reverse=True):
        if w in text:
            return DOMAIN_WORDS[w][0]
    return ""


def _fallback_group(reg: HomeRegistry, cmd: Command, area: str):
    """大模型写的房间/名字家里对不上（实测它会编出家里没有的「客厅」）时，按设备类型找：
    优先说话终端所在房间的，其次整个家（不超过 6 个在线的才动，免得把全屋都改了）。"""
    domain = _guess_domain(cmd)
    if not domain:
        return None
    base = Command(action=cmd.action, domain=domain, device_class=cmd.device_class, value=cmd.value,
                   area=cmd.area, floor=cmd.floor)          # 大模型给的房间若家里确有，要用上
    res = resolve(reg, base, default_area=area)
    if res.status == "none" and (cmd.area or cmd.floor):    # 房间是它编的：去掉房间再按类型找
        base = Command(action=cmd.action, domain=domain, device_class=cmd.device_class, value=cmd.value)
        res = resolve(reg, base, default_area=area)
    if res.status == "ambiguous" and res.ask_area:
        res = resolve(reg, Command(action=cmd.action, domain=domain, device_class=cmd.device_class,
                                   value=cmd.value, all=True), default_area=area)
    if res.status != "ok":
        return None
    # 不管有没有分配房间，凭猜测一次最多动 6 个在线设备（有终端所在房间的另算）：
    # 0.231 实测 34 盏灯没分配房间，不设上限会把全屋/展板灯一起调了
    if res.used_default_area or (res.status == "ok" and (base.area or base.floor)):
        return res                      # 终端所在房间 / 大模型给的房间家里确有：是有依据的，不算猜
    live = [e for e in res.entities if e.online and not e.placeholder]
    return res if 0 < len(live) <= MAX_GUESS_DEVICES else None


def _split_music(commands: List[dict]) -> Tuple[List[dict], str]:
    """大模型有时把音乐写进某条 command（带 music 字段/对音箱 play）：拎出来当音乐提示，不当设备控制"""
    keep, hint = [], ""
    for raw in commands or []:
        if isinstance(raw, dict):
            text = f"{raw.get('name', '')}{raw.get('domain', '')}"
            if raw.get("music") or (any(w in text for w in _MEDIA_WORDS) and str(raw.get("action", "")).lower() in ("play", "播放", "")):
                hint = hint or str(raw.get("music") or "").strip() or "轻音乐"
                continue
        keep.append(raw)
    return keep, hint


def _prepare(reg: HomeRegistry, conn, commands: List[dict], area: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把大模型给的指令落到具体设备：返回 (能做的项, 做不了的原因)。不控制任何设备。"""
    items: List[Dict[str, Any]] = []
    notes: List[str] = []
    seen = set()
    for raw in commands or []:
        cmd = raw if isinstance(raw, Command) else Command.from_dict(raw)
        action = actions.normalize_action(cmd.action) if cmd.action else "turn_on"
        if not action or action == "undo":
            notes.append("有一项操作我不认识")
            continue
        res = resolve(reg, cmd, default_area=area)
        if res.status != "ok":
            alt = _fallback_group(reg, cmd, area)
            if alt is not None and alt.status == "ok":
                res = alt
        if res.status == "none":
            notes.append(speech.none_text(res, cmd, _domain(cmd)))
            continue
        if res.status == "ambiguous":
            notes.append("有一项不确定指的是哪个设备")
            continue
        ok_ents: List[Entity] = []
        for ent in res.entities:
            if not ent.online or ent.placeholder:
                notes.append(f"{speech.label(ent)}离线")
                continue
            call = actions.plan(ent, action, cmd.value)
            if isinstance(call, actions.Unsupported):
                notes.append(f"{speech.label(ent)}{call.reason}")
                continue
            if guard.classify(ent, call, conn) != guard.FREE:
                notes.append(f"{speech.label(ent)}这类操作要单独确认，不放进场景")
                continue
            if (ent.entity_id, action) in seen:
                continue
            seen.add((ent.entity_id, action))
            ok_ents.append(ent)
        if ok_ents:
            items.append({"cmd": _clean_cmd(cmd, action), "entity_ids": [e.entity_id for e in ok_ents],
                          "action": action, "value": cmd.value})
    return items, list(dict.fromkeys(notes))


async def apply_mood(conn, name: str, commands: List[dict], say: str = "", save_ask: str = "",
                     music_hint: str = "", mood: str = "", registry: Optional[HomeRegistry] = None):
    """直接按心情调家里的设备，返回要对用户说的一句话。"""
    from core.home import engine
    reg = registry or get_registry()
    await reg.ensure_ready()
    did = str(getattr(conn, "device_id", "") or "")
    store = get_session_store()
    title = (str(name or "").strip() or str(mood or "").strip() or "这个")[:12]
    commands, found_music = _split_music(commands)
    music_hint = str(music_hint or "").strip() or found_music
    items, notes = _prepare(reg, conn, commands, engine.device_area(did))
    say = str(say or "").strip() or DEFAULT_SAY
    logger.bind(tag=TAG).info(
        f"[心情场景] 「{title}」收到 {len(commands)} 条指令 → 能调 {sum(len(i['entity_ids']) for i in items)} 个设备"
        f"{'，音乐提示=' + music_hint if music_hint else ''}；没调的原因: {notes or '无'}")

    if not items:
        # 一个设备都没调到：不能让用户听到「给你调柔和了」而实际什么都没变。
        # 交给大模型用人话说（它也可以顺手放音乐），不在这里念机器话
        return engine.EngineResult(LLM_FAILED_HINT if notes else LLM_NOTHING_HINT, ok=False, hint_llm=True)

    cmds = [{"action": it["action"], "entity_id": eid, "value": it["value"]}
            for it in items for eid in it["entity_ids"]]
    res = await engine.run(conn, commands=cmds, registry=reg)
    good = any(s in ("ok", "already") for s in res.statuses)
    if not good:
        logger.bind(tag=TAG).info(f"[心情场景] 「{title}」指令都没生效: statuses={res.statuses}")
        return engine.EngineResult(LLM_FAILED_HINT, ok=False, hint_llm=True)

    ids = [e for it in items for e in it["entity_ids"]]
    kind = "save" if str(save_ask or "").strip() else "applied"
    store.set_scene(did, ScenePlan(time.time(), title, str(mood or ""), items,
                                   music_hint, "", kind))
    text = say if kind == "applied" else f"{say} {str(save_ask).strip()}"
    return engine.EngineResult(text, ok=True, entity_ids=ids, statuses=list(res.statuses))


def _capture(ent: Entity) -> List[Dict[str, Any]]:
    """按设备**当前**状态生成指令：用户后来说「再亮一点」调过的也算进去"""
    # eid 是存档内部用的定位：设备改了名也找得到；设备被删/换了再退回按名字+房间找
    base: Dict[str, Any] = {"name": ent.name, "domain": ent.domain, "eid": ent.entity_id}
    if ent.area:
        base["area"] = ent.area
    s, a = str(ent.state).lower(), ent.attrs or {}
    if not ent.online or ent.placeholder:
        return []
    if ent.domain == "light":
        if s != "on":
            return [{**base, "action": "turn_off"}]
        out = []
        b = a.get("brightness")
        out.append({**base, "action": "set_brightness", "value": round(float(b) / 255 * 100)} if b is not None
                   else {**base, "action": "turn_on"})
        k = a.get("color_temp_kelvin")
        if k and "color_temp" in (a.get("supported_color_modes") or []) and a.get("color_mode") in (None, "color_temp"):
            out.append({**base, "action": "set_color_temp", "value": int(k)})
        return out
    if ent.domain == "cover":
        if a.get("current_position") is not None:
            return [{**base, "action": "set_position", "value": int(a["current_position"])}]
        return [{**base, "action": "open" if s in ("open", "opening") else "close"}]
    if ent.domain == "climate":
        if s == "off":
            return [{**base, "action": "turn_off"}]
        return [{**base, "action": "set_temperature", "value": a["temperature"]}] if a.get("temperature") is not None else []
    if ent.domain in ("switch", "fan", "input_boolean", "humidifier"):
        return [{**base, "action": "turn_on" if s == "on" else "turn_off"}]
    return []


def _save(reg: HomeRegistry, plan: ScenePlan, name: str) -> Optional[str]:
    cmds: List[Dict[str, Any]] = []
    for eid in dict.fromkeys(e for it in plan.items for e in it["entity_ids"]):
        ent = reg.get(eid)
        if ent is not None:
            cmds.extend(_capture(ent))
    if not cmds:
        cmds = [it["cmd"] for it in plan.items]          # 拿不到当前状态就退回原来的方案
    name = (name or plan.name or "我的场景").strip()[:12]
    cfg = {"name": name, "description": plan.mood or f"{name}（按心情组合）", "commands": cmds,
           "music_hint": plan.music, "created": datetime.now().isoformat(timespec="seconds")}
    if not scenes.save_custom(name, cfg):
        return None
    if _on_saved:
        try:
            _on_saved()
        except Exception:
            pass
    return name


async def save_applied(conn, name: str = "", registry: Optional[HomeRegistry] = None):
    """把刚调好的氛围（当前实际状态）存成固定场景。用户主动要求时随时可存。"""
    from core.home.engine import EngineResult
    reg = registry or get_registry()
    await reg.ensure_ready()
    did = str(getattr(conn, "device_id", "") or "")
    plan = get_session_store().scene(did)
    if plan is None:
        return EngineResult("我还没给你调过氛围呢，想要什么感觉跟我说说", ok=False)
    saved = _save(reg, plan, name)
    if saved is None:
        return EngineResult("这次没存成功，等会儿再试试", ok=False)
    get_session_store().drop_scene(did)
    return EngineResult(f"好，存好了，以后说「{saved}」我就直接调")


def reply_kind(text: str, plan: ScenePlan) -> str:
    """对「要不要存成固定场景」的回答：'confirm' | 'cancel' | ''（无关的话）"""
    from core.home import engine
    t = engine._norm_reply(text)
    if not t or len(t) > 8:
        return ""
    for cand in (t, engine._TAIL.sub("", t)):
        if cand in SAVE_CANCEL:
            return "cancel"
        if cand in SAVE_CONFIRM:
            return "confirm"
    return ""


async def handle_reply(conn, text: str, registry: Optional[HomeRegistry] = None):
    """只处理「刚问过要不要存」的回答；无关的话返回 None，原样放行。"""
    from core.home.engine import EngineResult
    did = str(getattr(conn, "device_id", "") or "")
    store = get_session_store()
    plan = store.scene(did)
    if plan is None or plan.kind != "save":
        return None
    kind = reply_kind(text, plan)
    if not kind:
        return None
    if kind == "cancel":
        plan.kind = "applied"             # 不存了，但用户之后想存仍然可以主动说
        return EngineResult("好的")
    return await save_applied(conn, "", registry)


def _relocate(reg: HomeRegistry, commands: List[dict]) -> List[dict]:
    """已保存的指令优先按存档时记下的设备定位；设备已不在了就退回名字+房间"""
    out = []
    for c in commands or []:
        c = dict(c)
        eid = c.pop("eid", "")
        if eid and reg.get(eid) is not None:
            c = {"action": c.get("action"), "entity_id": eid, "value": c.get("value")}
        out.append(c)
    return out


async def run_commands_scene(conn, display_name: str, commands: List[dict], music: str = "",
                             registry: Optional[HomeRegistry] = None) -> Tuple[str, List[str]]:
    """执行预置/已保存场景：说一句人话，不念设备状况（没人关心哪盏灯怎么了）。
    只有一个设备都没调成时才说一句，免得用户干等。"""
    from core.home import engine
    reg = registry or get_registry()
    commands = _relocate(reg, commands)
    res = await engine.run(conn, commands=commands, registry=registry) if commands else None
    statuses = list(res.statuses) if res else []
    good = any(s in ("ok", "already") for s in statuses)
    played = False
    music = str(music or "").strip()
    if music and music.lower() not in ("stop", "none") and _music_player is not None:
        try:
            played = bool(_music_player(conn, music))
        except Exception as e:
            print(f"[AI场景] 播放音乐失败: {e}")
    if good or played or res is None:
        text = f"好了，{display_name}给你调好了"
    else:
        text = NO_RESPONSE_SAY
    return text, statuses
