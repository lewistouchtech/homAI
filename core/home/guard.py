"""安全分级：free 直接做 / confirm 先口头确认（+声纹成员权限）/ deny 不让做

主人裁定（2026-10-13）：门锁可以「口头确认 + 声纹」开，无屏设备也可以。
- 开锁、开车库门/大门：confirm。上锁、关门：free（让家更安全的操作不设卡）。
- confirm 动作只有「刚问过、用户确认后」才执行：必须存在一致的待确认记录，模型自己写 confirm=true 无效。
- 声纹：启用声纹时，没认出是谁 → 拒绝；认出是小孩 → 拒绝；认出是家人 → 通过。
  没启用声纹（没有 voiceprint_provider）→ 只靠口头确认。
"""
import sqlite3
import os
from typing import Any, Optional, Tuple

from core.home.actions import ServiceCall
from core.home.registry import Entity

FREE, CONFIRM, DENY = "free", "confirm", "deny"
_DB = os.path.join(os.path.dirname(__file__), "..", "..", "data", "devices.db")
_CHILD_RELATIONS = ("儿子", "女儿", "孙子", "孙女", "外孙", "外孙女", "孩子", "小孩")
_CHILD_STAGE = ("child", "kid", "infant", "toddler", "儿童", "婴幼儿", "幼儿", "学龄", "小学", "幼儿园", "婴儿")
_DOOR_CLASSES = ("garage", "door", "gate")
_DOOR_NAME_WORDS = ("车库门", "车库大门", "大门", "卷帘门", "入户门", "garage", "gate")
# 只有家里人能做开锁/开门这类操作；命中拒绝词一律不行（「女朋友」「同事的孩子」这类别误放行）
_FAMILY_WORDS = ("本人", "主人", "家人", "亲", "父", "母", "爸", "妈", "夫", "妻", "老公", "老婆", "配偶",
                 "爱人", "儿", "女", "爷", "奶", "公", "婆", "兄", "弟", "姐", "妹", "孙")
_OUTSIDER_WORDS = ("同事", "朋友", "同学", "客人", "访客", "保姆", "阿姨", "邻居", "租客", "员工", "司机", "快递")


def _guard_cfg(conn) -> dict:
    try:
        return ((conn.config.get("plugins") or {}).get("home_assistant") or {}).get("guard") or {}
    except Exception:
        return {}


def classify(ent: Entity, call: ServiceCall, conn=None) -> str:
    cfg = _guard_cfg(conn) if conn is not None else {}
    if ent.entity_id in (cfg.get("deny_entities") or []):
        return DENY
    if ent.entity_id in (cfg.get("confirm_entities") or []) or ent.domain in (cfg.get("confirm_domains") or []):
        return CONFIRM
    if ent.domain == "lock" and call.service in ("unlock", "open"):
        return CONFIRM
    if ent.domain == "cover" and is_door(ent) and _opens_door(call):
        return CONFIRM
    return FREE


def is_door(ent: Entity) -> bool:
    """车库门/大门：看类型，也看名字（很多车库门没标 device_class）"""
    if ent.device_class in _DOOR_CLASSES:
        return True
    text = f"{ent.name}{ent.entity_id}".lower()
    return any(w in text for w in _DOOR_NAME_WORDS)


def _opens_door(call: ServiceCall) -> bool:
    """除了「关」「停」，其余对门的动作（开、开到某个位置、切换）都算开门"""
    if call.service in ("close_cover", "stop_cover"):
        return False
    if call.service == "set_cover_position":
        try:
            return float((call.data or {}).get("position") or 0) > 0
        except (TypeError, ValueError):
            return True
    return True


def _member(speaker: str) -> Optional[dict]:
    conn = None
    try:
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name, relation, age, life_stage FROM family_members WHERE name = ? OR nickname = ? LIMIT 1",
            (speaker, speaker)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def check_speaker(conn) -> Tuple[bool, str, str]:
    """返回 (是否允许, 拒绝原因（人话）, 说话人名字)"""
    cfg = _guard_cfg(conn)
    speaker = str(getattr(conn, "current_speaker", "") or "").strip()
    vp_on = getattr(conn, "voiceprint_provider", None) is not None
    require = cfg.get("require_voiceprint", True)
    if not speaker:
        if vp_on and require:
            return False, "我没认出是谁在说话，这个操作需要认出是家里人才行", ""
        return True, "", ""
    m = _member(speaker)
    if m is None:
        return (True, "", speaker) if not (vp_on and require) else (False, f"没有在家庭成员里找到{speaker}，这个操作我不能替他做", speaker)
    age = m.get("age")
    stage = str(m.get("life_stage") or "").lower()
    rel = str(m.get("relation") or "")
    if not _is_family(rel, cfg):
        return False, "这个操作只有家里人才能做", speaker
    is_child = (isinstance(age, (int, float)) and age < 14) or any(k in stage for k in _CHILD_STAGE) \
        or (any(k in rel for k in _CHILD_RELATIONS) and isinstance(age, (int, float)) and age < 16)
    if is_child:
        return False, "这个操作小朋友不能做，需要大人来说", speaker
    return True, "", speaker


def _is_family(relation: str, cfg: dict) -> bool:
    allow = cfg.get("allow_relations")
    if allow:
        return any(str(w) and str(w) in relation for w in allow)
    if not relation or any(w in relation for w in _OUTSIDER_WORDS):
        return False
    return any(w in relation for w in _FAMILY_WORDS)
