"""结果 → 人话。播报里永远不出现 entity_id、英文字段名、HTTP 状态码、Python 报错。"""
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from core.home.registry import Entity

DOMAIN_CN = {
    "light": "灯", "cover": "窗帘", "climate": "空调", "fan": "风扇", "lock": "门锁",
    "media_player": "播放设备", "switch": "开关", "vacuum": "扫地机", "humidifier": "加湿器",
    "scene": "场景", "script": "脚本", "input_boolean": "开关", "automation": "自动化",
    "sensor": "传感器", "binary_sensor": "传感器",
}
UNIT = {"light": "盏", "climate": "台", "fan": "台", "lock": "把", "vacuum": "台", "humidifier": "台"}

_EID_RE = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")


def scrub(text: str) -> str:
    """兜底：万一哪里漏进了 entity_id，替换成泛称，绝不念出去"""
    return _EID_RE.sub("这个设备", str(text or ""))


def label(ent: Entity, with_area: bool = False) -> str:
    """设备的人话名字；没有中文友好名时退化成「区域+类型」，不用 entity_id"""
    name = (ent.name or "").strip()
    if not name or "." in name or name == ent.entity_id:
        name = DOMAIN_CN.get(ent.domain, "设备")
        if ent.area:
            name = f"{ent.area}的{name}"
        return name
    if with_area and ent.area and ent.area not in name:
        return f"{ent.area}的{name}"
    return name


def _num(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 1))
    except (TypeError, ValueError):
        return str(v)


def phrase(action: str, value, name: str, domain: str = "") -> str:
    """成功后的一句话，如「已打开射灯一」"""
    if action == "turn_on":
        return f"已打开{name}"
    if action == "turn_off":
        return f"已关闭{name}"
    if action == "open":
        return f"已打开{name}" if domain != "lock" else f"已打开{name}"
    if action == "close":
        return f"已关闭{name}"
    if action == "stop":
        return f"{name}已停下"
    if action == "set_position":
        return f"{name}已调到开合度百分之{_num(value)}"
    if action == "set_brightness":
        return f"{name}已调到亮度百分之{_num(value)}"
    if action == "adjust_brightness":
        return f"{name}已调亮" if (value is None or float(value) >= 0) else f"{name}已调暗"
    if action == "brightness_up":
        return f"{name}已调亮"
    if action == "brightness_down":
        return f"{name}已调暗"
    if action == "set_color_temp":
        return f"{name}色温已调好"
    if action == "lock":
        return f"已锁上{name}"
    if action == "unlock":
        return f"已打开{name}"
    if action == "set_temperature":
        return f"{name}已设为{_num(value)}度"
    if action == "adjust_temperature":
        return f"{name}已调整"
    if action == "temperature_up":
        return f"{name}温度已调高"
    if action == "temperature_down":
        return f"{name}温度已调低"
    if action == "set_hvac_mode":
        return f"{name}已切到{_HVAC_NAME.get(str(value), str(value))}模式"
    if action == "set_fan":
        return f"{name}风速已调好"
    if action == "set_volume":
        return f"{name}音量已调到百分之{_num(value)}"
    if action == "adjust_volume":
        return f"已调大{name}的音量" if (value is None or float(value) >= 0) else f"已调小{name}的音量"
    if action == "volume_up":
        return f"已调大{name}的音量"
    if action == "volume_down":
        return f"已调小{name}的音量"
    if action == "play":
        return f"{name}继续播放"
    if action == "pause":
        return f"已暂停{name}"
    if action in ("next", "previous"):
        return f"{name}已切歌"
    if action == "mute":
        return f"{name}已静音"
    if action == "unmute":
        return f"{name}已取消静音"
    if action == "activate":
        return f"已启动{name}"
    if action == "start":
        return f"{name}开始工作了"
    if action == "return_home":
        return f"{name}回去充电了"
    if action == "toggle":
        return f"已切换{name}"
    return f"已处理{name}"


_HVAC_NAME = {"cool": "制冷", "heat": "制热", "auto": "自动", "dry": "除湿",
              "fan_only": "送风", "heat_cool": "冷暖自动"}

_ALREADY = {
    "turn_on": "开着", "turn_off": "关着", "open": "开着", "close": "关着",
    "lock": "锁着", "unlock": "开着",
}


def already_text(action: str, name: str) -> str:
    return f"{name}本来就是{_ALREADY.get(action, '这个状态')}的"


def _group_label(ents: List[Entity], action: str, value, domain: str) -> str:
    """多个同类设备合并说：「客厅的 3 盏灯」"""
    areas = {e.area for e in ents if e.area}
    cn = DOMAIN_CN.get(domain, "设备")
    unit = UNIT.get(domain, "个")
    prefix = f"{next(iter(areas))}的" if len(areas) == 1 and all(e.area for e in ents) else ""
    return f"{prefix}{len(ents)}{unit}{cn}"


def summarize_results(results: list) -> str:
    """results: executor.ExecResult 列表。同动作的成功项合并，异常项逐类说明。"""
    ok = OrderedDict()           # (action, value, domain) → [ent]
    already, offline, noconn, unconf, failed, unsup, blocked = [], [], [], [], [], OrderedDict(), []
    for r in results:
        e = r.entity
        if r.status == "ok":
            ok.setdefault((r.action, _hash(r.value), e.domain), []).append((e, r.value))
        elif r.status == "already":
            already.append(r)
        elif r.status == "offline":
            offline.append(e)
        elif r.status == "not_connected":
            noconn.append(e)
        elif r.status == "unconfirmed":
            unconf.append(e)
        elif r.status == "failed":
            failed.append(e)
        elif r.status == "unsupported":
            unsup.setdefault(r.detail, []).append(e)
        elif r.status == "blocked":
            blocked.append(r)

    # 同一设备被多条指令提到（亮度+色温都发给离线的灯）只说一次
    offline, noconn, unconf, failed = (_uniq(x) for x in (offline, noconn, unconf, failed))
    for k, v in unsup.items():
        unsup[k] = _uniq(v)

    parts: List[str] = []
    for (action, _h, domain), items in ok.items():
        ents = [e for e, _ in items]
        value = items[0][1]
        name = label(ents[0]) if len(ents) == 1 else _group_label(ents, action, value, domain)
        parts.append(phrase(action, value, name, domain))
    if already:
        if len(already) == 1:
            parts.append(already_text(already[0].action, label(already[0].entity)))
        else:
            parts.append(f"{len(already)}个设备本来就是这个状态")
    if offline:
        parts.append(_offline_text(offline))
    if noconn:
        parts.append(f"{_names(noconn)}还没接入系统，控制不了")
    if unconf:
        parts.append(f"已经给{_names(unconf)}发了指令，但还没看到它有变化，你确认下")
    if failed:
        parts.append(f"{_names(failed)}没控制成功，设备没响应")
    for reason, ents in unsup.items():
        parts.append(f"{_names(ents)}，{reason}")
    for r in blocked:
        parts.append(r.detail)
    text = "，".join(parts) if parts else "没有可执行的操作"
    return scrub(text)


def result_reason(r) -> str:
    """单条执行结果的人话原因（老接口的 failed 字典要用，不能是 HTTP 400 这种）"""
    st = getattr(r, "status", "")
    detail = str(getattr(r, "detail", "") or "").strip()
    if st == "already":
        return already_text(getattr(r, "action", ""), label(r.entity))
    if st == "offline":
        return "离线了，控制不了"
    if st == "not_connected":
        return "还没接入系统，控制不了"
    if st == "unconfirmed":
        return "指令发出去了，但没看到它有变化"
    if st == "unsupported":
        return detail or "这个操作它不支持"
    if st == "blocked":
        return detail or "这个操作被安全策略拦下了"
    return detail or "设备没响应"


def _uniq(ents: List[Entity]) -> List[Entity]:
    seen, out = set(), []
    for e in ents:
        if e.entity_id not in seen:
            seen.add(e.entity_id)
            out.append(e)
    return out


def _hash(v):
    try:
        return str(v)
    except Exception:
        return ""


def _names(ents: List[Entity]) -> str:
    if len(ents) <= 3:
        return "、".join(label(e) for e in ents)
    return f"{len(ents)}个设备"


def _offline_text(ents: List[Entity]) -> str:
    if len(ents) == 1:
        return f"{label(ents[0], with_area=True)}现在离线了，控制不了"
    domains = {e.domain for e in ents}
    areas = {e.area for e in ents}
    if len(domains) == 1 and len(areas) == 1 and ents[0].area:
        d = ents[0].domain
        return f"{ents[0].area}的{len(ents)}{UNIT.get(d, '个')}{DOMAIN_CN.get(d, '设备')}现在都离线了，控制不了"
    return f"{_names(ents)}现在离线了，控制不了"


# ---------- 寻址失败 / 反问 ----------
_NOUN = {"light": "灯", "cover": "窗帘", "climate": "空调", "fan": "风扇", "lock": "门锁",
         "media_player": "音箱", "switch": "开关", "vacuum": "扫地机", "humidifier": "加湿器",
         "scene": "场景", "script": "脚本", "input_boolean": "开关"}


def none_text(res, cmd, domain: str = "") -> str:
    noun = _NOUN.get(domain, "设备")
    if res.reason == "no_last":
        return "我不记得刚才控制过什么，你告诉我是哪个设备"
    if res.reason == "no_domain":
        return f"家里没有{noun}"
    if res.reason == "no_area":
        where = f"{cmd.area or cmd.floor}没有{noun}"
        return f"{where}，有{noun}的是{res.hint}" if res.hint else where
    if res.reason == "no_name":
        return f"没找到叫「{cmd.name}」的{noun if domain else '设备'}"
    return f"没找到可以控制的{noun}"


def ask_text(res, noun: str) -> str:
    if res.ask_area:
        return f"好几个房间都有{noun}（{'、'.join(res.options[:3])}），要哪个房间的？"
    opts = res.options[:3]
    if len(opts) == 2:
        return f"是{opts[0]}还是{opts[1]}？"
    return "是" + "、".join(opts[:-1]) + f"还是{opts[-1]}？"


# ==================== 单个设备的状态口语（从 ha_control 迁入，行为保持不变） ====================
# 这些文案是主人实测校准过的（2026-10-12：「办公室 空闲」像数据库字段 → 改「现在没在用」），
# 迁进引擎时**一字不改**，只把「查不到名字就返回 entity_id」改成返回泛称。

_STATE_CN = {
    "on": "已打开", "off": "已关闭",
    "open": "已打开", "opening": "正在打开", "closed": "已关闭", "closing": "正在关闭",
    "playing": "正在播放", "paused": "已暂停", "idle": "现在没在用", "standby": "待机",
    "unavailable": "离线", "unknown": "状态未知",
    "locked": "已上锁", "unlocked": "已解锁",
    "home": "在家", "not_home": "不在家",
    "cleaning": "正在清扫", "docked": "已回充", "returning": "正在回充",
}

_MEDIA_TYPE_CN = {
    "homepod": "HomePod", "speaker": "音箱", "cast": "投屏设备", "tv": "电视",
    "receiver": "功放", "soundbar": "回音壁",
}


def media_device_desc(attrs: Dict[str, Any]) -> str:
    """media_player 是什么设备（音箱/HomePod/电视…），拿不到返回空串"""
    mt = str((attrs or {}).get("mass_player_type") or "").strip().lower()
    if mt in _MEDIA_TYPE_CN:
        return _MEDIA_TYPE_CN[mt]
    if str((attrs or {}).get("device_class") or "").lower() == "speaker":
        return "音箱"
    return ""


def _registry_name(entity_id: str) -> str:
    """从注册表拿友好名（同步、不打网络）；拿不到返回空串"""
    try:
        from core.home.registry import get_registry
        ent = get_registry().get(entity_id)
        if ent and ent.name and "." not in ent.name:
            return ent.name
    except Exception:
        pass
    return ""


def spoken_state(entity_id: str, state_obj: Dict[str, Any]) -> str:
    """把 HA 状态对象压成一句简短中文口语（可直接播报，不含 JSON / entity_id）"""
    attrs = (state_obj or {}).get("attributes") or {} if isinstance(state_obj, dict) else {}
    name = str(attrs.get("friendly_name") or "").strip() or _registry_name(entity_id)
    if not isinstance(state_obj, dict):
        return name or "这个"
    state = str(state_obj.get("state", "unknown"))
    domain = str(entity_id).split(".")[0]

    if domain == "sensor":
        unit = attrs.get("unit_of_measurement") or ""
        return f"{name or '传感器'} {state}{unit}"

    parts = [_STATE_CN.get(state, state)]
    try:
        if domain == "light" and state == "on":
            b = attrs.get("brightness")
            if isinstance(b, (int, float)):
                parts.append(f"亮度百分之{int(round(b / 255 * 100))}")
        elif domain == "cover":
            pos = attrs.get("current_position")
            if isinstance(pos, (int, float)) and state not in ("opening", "closing"):
                parts.append(f"开合度百分之{int(pos)}")
        elif domain == "media_player":
            if attrs.get("media_title"):
                parts.append(str(attrs["media_title"]))
            vol = attrs.get("volume_level")
            if isinstance(vol, (int, float)):
                parts.append(f"音量百分之{int(round(vol * 100))}")
        elif domain == "climate":
            t = attrs.get("temperature")
            if t is not None:
                parts.append(f"目标温度{t}度")
    except Exception:
        pass
    if state not in _STATE_CN:
        parts[0] = f"状态是{parts[0]}"

    if domain == "media_player":
        who = media_device_desc(attrs)
        if who:
            if "." in name or not name:
                return f"{who}，" + "，".join(parts)
            return f"{name}的{who}，" + "，".join(parts)
    if "." in name or not name:
        name = "这个"          # 禁止把 entity_id（拼音）读给用户
    return f"{name}，" + "，".join(parts)


def spoken_summary(summary: Dict[str, Any]) -> str:
    """把聚合结果压成一句简短中文口语（可直接播报，不含 JSON）"""
    if not isinstance(summary, dict) or summary.get("error"):
        return str((summary or {}).get("error") or "查询失败")[:160]

    parts: List[str] = []
    for domain in ("light", "cover", "climate", "switch", "fan",
                   "media_player", "lock", "vacuum", "humidifier"):
        b = summary.get(domain)
        if not isinstance(b, dict) or not b.get("total"):
            continue
        cn = DOMAIN_CN.get(domain, domain)
        on, total = b["on"], b["total"]
        if domain == "lock":
            parts.append(f"门锁 {on}/{total} 已上锁" if on else f"{total} 个门锁未上锁")
        elif domain == "cover":
            parts.append(f"窗帘 {on}/{total} 开着" if on else f"{total} 个窗帘都关着")
        elif on == 0:
            parts.append(f"{total} 个{cn}都关着")
        elif on == total:
            parts.append(f"{total} 个{cn}全开着")
        else:
            parts.append(f"{cn}开着 {on}/{total}")

    text = ("家里现在：" + "，".join(parts) + "。") if parts else "家里没有查到在线的可控设备。"
    if summary.get("unavailable"):
        text += f"另有 {summary['unavailable']} 个设备离线。"
    return text[:300]
