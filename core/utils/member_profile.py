# 成员档案注入：声纹识别出成员后，把基本信息/家庭关系/健康数据拼成文本
# 注入 LLM system prompt（<member_profile> 标签），让伊娃针对性交流
# 任何失败返回 None，不影响对话主链
import json
import sqlite3
from pathlib import Path

# 数据库路径与 family_api 同构
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 输出字段（按给 LLM 的可读性排序，跳过 id/avatar/时间戳/声纹向量）
_PROFILE_FIELDS = [
    ("name", "姓名"), ("nickname", "昵称（伊娃对他的称呼）"),
    ("relation", "家庭关系"), ("relation_detail", "血缘路径"),
    ("age", "年龄"), ("life_stage", "人生阶段"), ("gender", "性别"), ("birthday", "生日"),
    ("height", "身高cm"), ("weight", "体重kg"), ("occupation", "职业"),
    ("position", "职位/身份"), ("phone", "电话"), ("health_status", "健康状态"),
    ("notes", "备注"),
]

# onenet_health_data data_json 内常见键 → 可读名
_VITAL_KEYS = {
    "blood_pressure": "血压", "high": "收缩压", "low": "舒张压",
    "body_temperature": "体温", "temperature": "体温", "temp": "体温",
    "blood_oxygen": "血氧", "spo2": "血氧", "pr": "心率", "heart_rate": "心率",
    "glucose": "血糖", "weight": "体重",
}


def _latest_vitals_text(limit=3):
    """家庭健康设备最新体征（onenet_health_data 最新 N 条，家人共享设备）。"""
    db_path = _DB_DIR / "health_data.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            rows = conn.execute(
                "SELECT device_type, measurement_time, data_json "
                "FROM onenet_health_data ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        parts = []
        for device_type, mtime, data_json in rows:
            try:
                data = json.loads(data_json)
            except Exception:
                continue
            items = []
            for k, label in _VITAL_KEYS.items():
                if k in data:
                    items.append(f"{label}{data[k]}")
            if items:
                parts.append(f"{mtime}：" + "，".join(items))
        return "\n".join(parts)
    except Exception:
        return ""


def get_member_profile_text(name):
    """按姓名或昵称查成员完整档案，返回给 LLM 的文本；查无/异常返回 None。

    [2026-10-05] 追加「常去地点」段。目的是让 LLM 听到「我上班了」时知道
    对应哪个地点、从而去调 plan_commute，而不是反问「你公司在哪」。
    这里只列地点的**名称/别名/出行方式**，不列完整地址 —— 完整地址在
    <family_places> 里（全员可见），避免同一份地址在 prompt 里出现两遍。
    """
    if not name:
        return None
    db_path = _DB_DIR / "devices.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM family_members WHERE name=? OR nickname=? LIMIT 1",
                (name, name)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        lines = []
        keys = row.keys()
        for key, label in _PROFILE_FIELDS:
            val = row[key] if key in keys else None
            if val is not None and str(val).strip() != "":
                lines.append(f"{label}：{val}")
        vitals = _latest_vitals_text()
        if vitals:
            lines.append("家庭健康设备最新数据：\n" + vitals)

        # [2026-10-05] 常去地点：让「我上班了」「我上学了」能对上具体地点
        try:
            from core.utils.place_manager import get_places_for_prompt
            places_text = get_places_for_prompt(row["id"], str(db_path))
            if places_text:
                lines.append("常去地点（说「上班」「上学」时对应下面这些）：\n" + places_text)
        except Exception:
            pass

        return "\n".join(lines) if lines else None
    except Exception:
        return None


def get_family_places_text():
    """家庭地点总览（全员可见），注入 <family_places>。

    [2026-10-05] 为什么必须有这一段、且不能只靠个人档案：
        1. 小花朵问「妈妈上班要多久」时妈妈没说话，妈妈的个人档案根本没被注入；
        2. 声纹万一没识别出说话人，个人档案是空的，但家庭地点总览应该照样在，
           LLM 至少能回答「小花朵的学校在 xxx，你知道怎么去吗」，
           而不是连家在哪都不知道。
    任何异常返回空串（不注入空标签）。
    """
    try:
        from core.utils.place_manager import get_family_places_overview
        return get_family_places_overview(str(_DB_DIR / "devices.db"))
    except Exception:
        return ""
