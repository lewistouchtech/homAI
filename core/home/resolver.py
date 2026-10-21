"""语义寻址：把「射灯一 / 客厅的灯 / 二楼窗帘」解析成具体实体

借鉴 HA Assist 的寻址模型（name / area / floor / domain / device_class），
但在本地完成：大模型只说人话里的名字，不再凭空编 entity_id。
容错：别名、拼音（ASR 把「射灯一」听成「射灯仪」仍能命中）、数字写法（1/一/01）。
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from pypinyin import lazy_pinyin

from core.home.guard import is_door
from core.home.registry import Entity, HomeRegistry

# 中文类别词 → (domain, device_class)
DOMAIN_WORDS: Dict[str, tuple] = {
    "灯": ("light", ""), "灯光": ("light", ""), "射灯": ("light", ""), "吊灯": ("light", ""),
    "台灯": ("light", ""), "筒灯": ("light", ""), "灯带": ("light", ""), "壁灯": ("light", ""),
    "窗帘": ("cover", "curtain"), "帘子": ("cover", "curtain"), "纱帘": ("cover", "curtain"),
    "卷帘": ("cover", "shade"), "百叶窗": ("cover", "blind"), "车库门": ("cover", "garage"),
    "空调": ("climate", ""), "风扇": ("fan", ""), "电风扇": ("fan", ""), "新风": ("fan", ""),
    "门锁": ("lock", ""), "锁": ("lock", ""), "智能锁": ("lock", ""),
    "音箱": ("media_player", ""), "电视": ("media_player", "tv"), "音响": ("media_player", ""),
    "开关": ("switch", ""), "插座": ("switch", ""),
    "扫地机": ("vacuum", ""), "扫地机器人": ("vacuum", ""), "加湿器": ("humidifier", ""),
    "场景": ("scene", ""), "脚本": ("script", ""),
}
_DOMAINS = {"light", "cover", "climate", "fan", "lock", "media_player", "switch", "vacuum",
            "humidifier", "scene", "script", "input_boolean", "automation", "sensor",
            "binary_sensor", "valve", "alarm_control_panel", "water_heater"}
_WHOLE_HOUSE = ("全屋", "整个家", "全家", "整屋", "所有房间", "whole house", "家里")
_CN_NUM = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
           "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_PUNCT = re.compile(r"[\s_\-·.,，。！？!?、:：;；'\"“”‘’()（）]+")


def norm(text: str) -> str:
    """规整：去空白标点、小写、中文数字→阿拉伯、去前导零、楼→层、去「的」"""
    s = str(text or "").strip().lower()
    s = _PUNCT.sub("", s)
    s = "".join(_CN_NUM.get(c, c) for c in s)
    s = re.sub(r"(?<!\d)0+(\d)", r"\1", s)
    s = s.replace("楼", "层").replace("的", "").replace("边", "侧")
    return s


_DIGIT_CN = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五",
             "6": "六", "7": "七", "8": "八", "9": "九"}


def pinyin_key(text: str) -> str:
    """拼音键：数字先还原成汉字再转拼音，让「射灯1」「射灯一」「射灯仪」同键"""
    s = norm(text)
    if not re.search(r"[\u4e00-\u9fff]", s):
        return s
    return "".join(lazy_pinyin("".join(_DIGIT_CN.get(c, c) for c in s)))


def _text_score(query: str, cand: str) -> float:
    q, c = norm(query), norm(cand)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    qp, cp = pinyin_key(query), pinyin_key(cand)
    if qp and qp == cp:
        return 0.95
    if len(q) >= 2 and (q in c or c in q) and min(len(q), len(c)) >= 2:
        return 0.8
    if len(qp) >= 4 and (qp in cp or cp in qp) and min(len(qp), len(cp)) >= 4:
        return 0.72
    if len(qp) >= 4 and len(cp) >= 4:
        ratio = SequenceMatcher(None, qp, cp).ratio()
        if ratio >= 0.85:
            return ratio * 0.7
    return 0.0


def name_score(query: str, ent: Entity) -> float:
    return max([_text_score(query, n) for n in [ent.name, *ent.aliases] if n] or [0.0])


def area_match(query: str, ent: Entity) -> bool:
    return any(_text_score(query, a) >= 0.72 for a in [ent.area, *ent.area_aliases] if a)


def floor_match(query: str, ent: Entity) -> bool:
    return bool(ent.floor) and _text_score(query, ent.floor) >= 0.72


@dataclass
class Command:
    action: str = ""
    name: str = ""
    area: str = ""
    floor: str = ""
    domain: str = ""
    device_class: str = ""
    value: object = None
    entity_id: str = ""         # 兼容旧调用：已知 entity_id 直接寻址
    ref: str = ""               # "last" = 上一次控制的设备
    all: bool = False           # 「所有/全部」

    @classmethod
    def from_dict(cls, d: dict) -> "Command":
        d = d or {}
        s = lambda k: str(d.get(k) or "").strip()
        return cls(action=s("action").lower(), name=s("name"), area=s("area"), floor=s("floor"),
                   domain=s("domain").lower(), device_class=s("device_class").lower(),
                   value=d.get("value"), entity_id=s("entity_id"), ref=s("ref").lower(),
                   all=bool(d.get("all")))


@dataclass
class Resolution:
    status: str                              # ok | none | ambiguous
    entities: List[Entity] = field(default_factory=list)
    options: List[str] = field(default_factory=list)   # ambiguous 时给用户选的名字
    reason: str = ""                         # none 时的原因：no_domain / no_area / no_name / no_target
    hint: str = ""                           # none 时的人话线索（如「有灯的是总经理办公室」）
    used_default_area: str = ""              # 悄悄用了终端所在房间
    ask_area: bool = False                   # 需要追问「哪个房间」


def _split_domain(cmd: Command):
    """从 name/domain 里归整出 (domain, device_class, name)"""
    domain, dclass, name = cmd.domain, cmd.device_class, cmd.name
    if domain and domain not in _DOMAINS:
        w = DOMAIN_WORDS.get(domain)
        domain, dclass = (w[0], dclass or w[1]) if w else ("", dclass)
    if name:
        w = DOMAIN_WORDS.get(norm(name)) or DOMAIN_WORDS.get(name)
        if w and (not domain or domain == w[0]):
            domain, dclass, name = w[0], dclass or w[1], ""
    return domain, dclass, name


def resolve(registry: HomeRegistry, cmd: Command, default_area: str = "",
            last_ids: Optional[List[str]] = None, for_query: bool = False) -> Resolution:
    ents = registry.all()
    if cmd.ref == "last":
        got = [registry.get(e) for e in (last_ids or []) if registry.get(e)]
        return Resolution("ok", got) if got else Resolution("none", reason="no_last")
    if cmd.entity_id:
        e = registry.get(cmd.entity_id)
        return Resolution("ok", [e]) if e else Resolution("none", reason="no_target")

    domain, dclass, name = _split_domain(cmd)
    pool = [e for e in ents if for_query or e.domain not in ("sensor", "binary_sensor")]
    if domain:
        pool = [e for e in pool if e.domain == domain]
        if not pool:
            return Resolution("none", reason="no_domain")
    if dclass:
        by_class = [e for e in pool if e.device_class == dclass
                    or (dclass == "curtain" and e.device_class in ("curtain", "blind", "shade", ""))]
        # 没有命中类型时兜底全部 cover，但门（车库门/大门）不算窗帘
        pool = by_class or [e for e in pool if not is_door(e)] or pool

    whole = norm(cmd.area) in {norm(w) for w in _WHOLE_HOUSE}
    area_q = "" if whole else cmd.area
    used_default = ""
    if area_q:
        narrowed = [e for e in pool if area_match(area_q, e)]
        if not narrowed:
            where = sorted({e.area for e in pool if e.area})
            return Resolution("none", reason="no_area",
                              hint="、".join(where[:3]))
        pool = narrowed
    if cmd.floor:
        narrowed = [e for e in pool if floor_match(cmd.floor, e)]
        if not narrowed:
            return Resolution("none", reason="no_area")
        pool = narrowed

    if name:
        if default_area and not area_q and not cmd.floor:
            local = [e for e in pool if area_match(default_area, e)]
            scored_local = [e for e in local if name_score(name, e) >= 0.6]
            if scored_local:
                pool, used_default = local, default_area
        scored = [(name_score(name, e), e) for e in pool]
        scored = [(s, e) for s, e in scored if s >= 0.6]
        if not scored:
            return Resolution("none", reason="no_name")
        top = max(s for s, _ in scored)
        best = [e for s, e in scored if s >= top - 0.05]
        if len(best) > 1 and not cmd.all:
            # 同名多个：离线的不参与反问（只剩一个在线就是它）；全离线才照实反问
            online = [e for e in best if e.online]
            if len(online) == 1:
                return Resolution("ok", online, used_default_area=used_default)
            if len(online) > 1:
                best = online
            return Resolution("ambiguous", best, options=[_label(e) for e in best[:3]])
        return Resolution("ok", best, used_default_area=used_default)

    # 没有名字：按 区域+类型 取一组
    group = [e for e in pool if not e.placeholder]
    if not group:
        return Resolution("none", reason="no_target")
    if default_area and not area_q and not cmd.floor and not whole and not cmd.all:
        local = [e for e in group if area_match(default_area, e)]
        if local:
            return Resolution("ok", local, used_default_area=default_area)
    if not area_q and not cmd.floor and not whole and not cmd.all and domain:
        rooms = sorted({e.area for e in group if e.area})
        if len(rooms) > 1 and len(group) > 1:
            return Resolution("ambiguous", [], options=rooms[:3], ask_area=True)
    return Resolution("ok", group)


def _label(e: Entity) -> str:
    return f"{e.area}的{e.name}" if e.area and e.area not in e.name else e.name
