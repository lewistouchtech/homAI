"""动作表：把「动作 + 设备类型」翻译成 HA 服务调用

规矩：
- 每个 domain 只接受它真有的动作，不认识的动作返回 Unsupported（带人话原因），绝不乱发。
- 门锁只认 lock / unlock，**不认 turn_on / turn_off**（HA 的 turn_off 对锁 = 开锁，一句「关一下」不能把门打开）。
- 「停」只对窗帘/媒体/扫地机有效，不再被当成「关」。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Union

from core.home.registry import Entity


@dataclass
class ServiceCall:
    domain: str
    service: str
    data: Dict[str, Any] = field(default_factory=dict)
    action: str = ""
    value: Any = None
    # 状态对象 → True 已到位 / False 没到位 / None 无法判断
    expect: Optional[Callable[[Dict[str, Any]], Optional[bool]]] = None
    idempotent: bool = False        # 已经是目标状态时可以不发


@dataclass
class Unsupported:
    reason: str


ACTION_ALIASES = {
    "on": "turn_on", "off": "turn_off", "turn_on": "turn_on", "turn_off": "turn_off",
    "open": "open", "opened": "open", "close": "close", "closed": "close",
    "stop": "stop", "pause": "pause", "play": "play", "resume": "play",
    "lock": "lock", "unlock": "unlock",
    "toggle": "toggle", "next": "next", "previous": "previous", "prev": "previous",
    "set_brightness": "set_brightness", "adjust_brightness": "adjust_brightness",
    # ⚠️ 方向必须保留在动作名里：原来 brightness_up/down 都映射成 adjust_brightness，
    # 而 adjust_brightness 在没有 value 时默认 +30 → 「调暗一点」会把灯调亮（2026-10-13 修）
    "brightness_up": "brightness_up", "brightness_down": "brightness_down",
    "set_color_temp": "set_color_temp", "set_position": "set_position",
    "set_temperature": "set_temperature", "adjust_temperature": "adjust_temperature",
    "temperature_up": "temperature_up", "temperature_down": "temperature_down",
    "set_hvac_mode": "set_hvac_mode", "set_fan": "set_fan",
    "set_volume": "set_volume", "adjust_volume": "adjust_volume",
    "volume_up": "volume_up", "volume_down": "volume_down", "mute": "mute", "unmute": "unmute",
    "activate": "activate", "start": "start", "return_home": "return_home",
    "set_humidity": "set_humidity", "undo": "undo",
    # 中文动作词：大模型/判定模型偶尔直接吐中文，这里兜住（2026-10-13）
    "打开": "turn_on", "开": "turn_on", "开启": "turn_on", "关掉": "turn_off",
    "关闭": "turn_off", "关": "turn_off", "拉开": "open", "升起": "open",
    "合上": "close", "拉上": "close", "停下": "stop", "暂停": "pause",
    "播放": "play", "继续播放": "play", "上锁": "lock", "锁门": "lock",
    "开锁": "unlock", "解锁": "unlock", "调亮": "brightness_up",
    "亮一点": "brightness_up", "亮一些": "brightness_up", "调暗": "brightness_down",
    "暗一点": "brightness_down", "暗一些": "brightness_down",
    "大点声": "volume_up", "大声一点": "volume_up", "小点声": "volume_down",
    "小声一点": "volume_down", "静音": "mute",
    "取消静音": "unmute", "下一首": "next", "上一首": "previous",
    "撤销": "undo", "恢复原样": "undo", "刚才的恢复": "undo",
    "调高": "temperature_up", "高一点": "temperature_up",
    "调低": "temperature_down", "低一点": "temperature_down",
}

_HVAC_CN = {"制冷": "cool", "冷风": "cool", "制热": "heat", "暖风": "heat", "自动": "auto",
            "除湿": "dry", "送风": "fan_only", "通风": "fan_only", "关闭": "off", "关": "off",
            "cool": "cool", "heat": "heat", "auto": "auto", "dry": "dry", "fan_only": "fan_only",
            "off": "off", "heat_cool": "heat_cool"}
_FAN_LEVELS = {"低": 33, "小": 33, "low": 33, "中": 66, "mid": 66, "medium": 66,
               "高": 100, "大": 100, "high": 100, "最大": 100}
_KELVIN = {"暖": 2700, "暖光": 2700, "warm": 2700, "中性": 4000, "自然": 4000, "neutral": 4000,
           "冷": 6000, "冷光": 6000, "cool": 6000, "白": 5000}


def normalize_action(action: str) -> str:
    return ACTION_ALIASES.get(str(action or "").strip().lower(), "")


def _num(value, default=None):
    try:
        return float(str(value).strip().rstrip("%度°"))
    except (TypeError, ValueError):
        return default


def _state(st) -> str:
    return str((st or {}).get("state", "")).lower()


def _attr(st, k):
    return ((st or {}).get("attributes") or {}).get(k)


def plan(ent: Entity, action: str, value: Any = None) -> Union[ServiceCall, Unsupported]:
    """为一个实体生成服务调用；不支持就给原因"""
    fn = _PLANNERS.get(ent.domain)
    if fn is None:
        return Unsupported("这类设备我暂时还不会控制")
    return fn(ent, action, value)


def _call(domain, service, ent, action, value=None, expect=None, idempotent=False, **data):
    return ServiceCall(domain, service, {"entity_id": ent.entity_id, **data},
                       action=action, value=value, expect=expect, idempotent=idempotent)


def _light(ent, a, v):
    modes = ent.attrs.get("supported_color_modes") or []
    dimmable = not modes or any(m != "onoff" for m in modes)
    on = lambda st: _state(st) == "on"
    off = lambda st: _state(st) == "off"
    if a == "turn_on":
        return _call("light", "turn_on", ent, a, expect=on, idempotent=True)
    if a == "turn_off":
        return _call("light", "turn_off", ent, a, expect=off, idempotent=True)
    if a == "toggle":
        return _call("light", "toggle", ent, a)
    if a in ("set_brightness", "adjust_brightness"):
        if not dimmable:
            return Unsupported("只能开关，不能调亮度")
        pct = _num(v)
        if pct is None and a == "adjust_brightness":
            pct = 30
        if pct is None:
            return Unsupported("没说要调到多亮")
        if a == "set_brightness":
            pct = max(0, min(100, pct))
            if pct == 0:
                return _call("light", "turn_off", ent, "turn_off", expect=off)
            return _call("light", "turn_on", ent, a, pct, expect=on, brightness_pct=int(pct))
        return _call("light", "turn_on", ent, a, pct, expect=on,
                     brightness_step_pct=int(max(-100, min(100, pct))))
    if a in ("brightness_up", "brightness_down"):
        # 方向只由动作名决定，绝不让「调暗一点」变成调亮
        if not dimmable:
            return Unsupported("只能开关，不能调亮度")
        step = abs(_num(v, 30) or 30)
        step = step if a == "brightness_up" else -step
        return _call("light", "turn_on", ent, a, step, expect=on,
                     brightness_step_pct=int(max(-100, min(100, step))))
    if a == "set_color_temp":
        if modes and "color_temp" not in modes:
            return Unsupported("不支持调色温")
        k = int(_num(v)) if _num(v) else _KELVIN.get(str(v).strip().lower())
        if not k:
            return Unsupported("没说要调到多少色温")
        return _call("light", "turn_on", ent, a, k, expect=on, color_temp_kelvin=int(k))
    return Unsupported("灯不支持这个操作")


def _onoff(domain):
    def f(ent, a, v):
        on = lambda st: _state(st) == "on"
        off = lambda st: _state(st) == "off"
        if a == "turn_on":
            return _call(domain, "turn_on", ent, a, expect=on, idempotent=True)
        if a == "turn_off":
            return _call(domain, "turn_off", ent, a, expect=off, idempotent=True)
        if a == "toggle":
            return _call(domain, "toggle", ent, a)
        if domain == "fan" and a == "set_fan":
            lvl = _FAN_LEVELS.get(str(v).strip().lower(), _num(v))
            if lvl is None:
                return _call("fan", "turn_on", ent, a, expect=on)
            return _call("fan", "set_percentage", ent, a, lvl, expect=on, percentage=int(lvl))
        if domain == "humidifier" and a == "set_humidity":
            h = _num(v)
            if h is None:
                return Unsupported("没说要调到多少湿度")
            return _call("humidifier", "set_humidity", ent, a, h, humidity=int(h))
        return Unsupported("不支持这个操作")
    return f


def _cover(ent, a, v):
    if a in ("open", "turn_on"):
        return _call("cover", "open_cover", ent, "open",
                     expect=lambda st: _state(st) in ("open", "opening"), idempotent=True)
    if a in ("close", "turn_off"):
        return _call("cover", "close_cover", ent, "close",
                     expect=lambda st: _state(st) in ("closed", "closing"), idempotent=True)
    if a == "stop":
        return _call("cover", "stop_cover", ent, a)
    if a == "set_position":
        p = _num(v)
        if p is None:
            return Unsupported("没说要开到多少")
        p = int(max(0, min(100, p)))
        return _call("cover", "set_cover_position", ent, a, p, position=p)
    return Unsupported("不支持这个操作")


def _climate(ent, a, v):
    if a == "turn_on":
        return _call("climate", "turn_on", ent, a, expect=lambda st: _state(st) != "off", idempotent=True)
    if a == "turn_off":
        return _call("climate", "turn_off", ent, a, expect=lambda st: _state(st) == "off", idempotent=True)
    if a == "set_temperature":
        t = _num(v)
        if t is None:
            return Unsupported("没说要调到多少度")
        lo, hi = ent.attrs.get("min_temp"), ent.attrs.get("max_temp")
        if lo is not None and hi is not None and not (float(lo) <= t <= float(hi)):
            return Unsupported(f"温度只能在 {int(float(lo))} 到 {int(float(hi))} 度之间")

        def _ok(st):
            cur = _attr(st, "temperature")
            return None if cur is None else abs(float(cur) - t) < 0.6
        return _call("climate", "set_temperature", ent, a, t, expect=_ok, idempotent=True, temperature=t)
    if a == "adjust_temperature":
        delta = _num(v, 1.0)
        cur = _num(ent.attrs.get("temperature"))
        if cur is None:
            return Unsupported("现在没有设定温度，没法在原来的基础上调")
        return plan(ent, "set_temperature", cur + delta)
    if a in ("temperature_up", "temperature_down"):
        # 方向写死在动作名里，避免「调低一点」被当成调高
        delta = abs(_num(v, 1.0) or 1.0)
        return plan(ent, "adjust_temperature", delta if a == "temperature_up" else -delta)
    if a == "set_hvac_mode":
        m = _HVAC_CN.get(str(v).strip().lower())
        modes = ent.attrs.get("hvac_modes") or []
        if not m or (modes and m not in modes):
            return Unsupported("不支持这个模式")
        if m == "off":
            return plan(ent, "turn_off")
        return _call("climate", "set_hvac_mode", ent, a, m,
                     expect=lambda st: _state(st) == m, hvac_mode=m)
    if a == "set_fan":
        modes = ent.attrs.get("fan_modes") or []
        want = str(v).strip().lower()
        key = {"低": "low", "小": "low", "中": "medium", "高": "high", "大": "high", "自动": "auto"}.get(want, want)
        wants = {want, key, "mid" if key == "medium" else key}
        pick = next((m for m in modes if m.lower() in wants), None)
        if pick is None:
            return Unsupported("这台空调没有这个风速")
        return _call("climate", "set_fan_mode", ent, a, pick, fan_mode=pick)
    return Unsupported("空调不支持这个操作")


def _lock(ent, a, v):
    if a == "lock":
        return _call("lock", "lock", ent, a,
                     expect=lambda st: _state(st) in ("locked", "locking"), idempotent=True)
    if a in ("unlock", "open"):
        return _call("lock", "unlock", ent, "unlock",
                     expect=lambda st: _state(st) in ("unlocked", "unlocking", "open", "opening"),
                     idempotent=True)
    if a in ("turn_on", "turn_off"):
        return Unsupported("门锁请明确说「上锁」或「开锁」")
    return Unsupported("门锁不支持这个操作")


def _media(ent, a, v):
    st_in = lambda *xs: (lambda st: _state(st) in xs)
    table = {
        "turn_on": ("turn_on", None), "turn_off": ("turn_off", st_in("off", "standby", "idle")),
        "play": ("media_play", st_in("playing", "buffering")),
        "pause": ("media_pause", st_in("paused", "idle")),
        "stop": ("media_stop", None), "next": ("media_next_track", None),
        "previous": ("media_previous_track", None),
    }
    if a in table:
        svc, exp = table[a]
        return _call("media_player", svc, ent, a, expect=exp)
    if a == "set_volume":
        p = _num(v)
        if p is None:
            return Unsupported("没说要调到多大声")
        return _call("media_player", "volume_set", ent, a, p, volume_level=max(0.0, min(1.0, p / 100)))
    if a in ("adjust_volume", "volume_up", "volume_down"):
        # 方向优先看动作名；adjust_volume 才看 value 的正负
        up = (a == "volume_up") if a != "adjust_volume" else (_num(v, 10) or 10) >= 0
        return _call("media_player", "volume_up" if up else "volume_down", ent, a, v)
    if a in ("mute", "unmute"):
        return _call("media_player", "volume_mute", ent, a, is_volume_muted=(a == "mute"))
    return Unsupported("不支持这个操作")


def _activate(domain, service):
    def f(ent, a, v):
        if a in ("activate", "turn_on", "start"):
            return _call(domain, service, ent, "activate")
        return Unsupported("只能启动，不能做别的")
    return f


def _vacuum(ent, a, v):
    if a in ("start", "turn_on", "activate"):
        return _call("vacuum", "start", ent, "start")
    if a in ("stop", "pause"):
        return _call("vacuum", "pause" if a == "pause" else "stop", ent, a)
    if a in ("return_home", "turn_off"):
        return _call("vacuum", "return_to_base", ent, "return_home")
    return Unsupported("不支持这个操作")


_PLANNERS: Dict[str, Callable] = {
    "light": _light, "switch": _onoff("switch"), "input_boolean": _onoff("input_boolean"),
    "fan": _onoff("fan"), "humidifier": _onoff("humidifier"),
    "cover": _cover, "climate": _climate, "lock": _lock, "media_player": _media,
    "scene": _activate("scene", "turn_on"), "script": _activate("script", "turn_on"),
    "automation": _activate("automation", "trigger"), "button": _activate("button", "press"),
    "vacuum": _vacuum,
}
