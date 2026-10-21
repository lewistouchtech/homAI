# -*- coding: utf-8 -*-
"""车牌 ↔ 车主绑定（支持"一车多人"）

为什么要这张表（2026-09-26 主人设计）：
    车牌识别只能回答"哪台车回来了"，回答不了"谁回来了"。
    家里只有一台车、好几个人都会开时，**绝不能仅凭车牌就认定是谁**，
    否则会把"妈妈开车回来"播报成"主人回来了"。

    因此本表提供的是**候选集**而非唯一答案：
        车牌 → [可能的驾驶员列表]
    真实身份由 identity_fusion（人脸/声纹）在时间窗内确认后才有结论。

车牌归一化：
    Frigate LPR / OCR 出来的字符串形态不稳定（大小写、空格、中点、全角），
    统一归一化后再比对，避免"京A·12345"与"京A12345"被当成两台车。
"""
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# 读写都用同一把锁：本模块调用频率极低（车牌事件级别），串行化最省心，
# 也避免 MQTT 回调线程与 HTTP 协程并发写触发 database is locked
_lock = threading.Lock()

_initialized = False

# 归一化时要剔除的分隔符：中点、间隔号、空格、连字符、点号
_SEP_RE = re.compile(r"[\s·・.\-_/|,，、]+")
# 全角字母数字 → 半角
_FULLWIDTH_OFFSET = 0xFEE0


def normalize_plate(raw: Any) -> str:
    """车牌归一化：去分隔符、全角转半角、字母大写。

    >>> normalize_plate("京a·12345")
    '京A12345'
    >>> normalize_plate(" 京A-12345 ")
    '京A12345'
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # 全角转半角（保留全角空格 U+3000 的处理）
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= _FULLWIDTH_OFFSET
        out.append(chr(code))
    s = "".join(out)
    s = _SEP_RE.sub("", s)
    return s.upper()


def init_table() -> bool:
    """建 plate_bindings 表（幂等）。"""
    global _initialized
    with _lock:
        if _initialized:
            return True
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS plate_bindings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plate TEXT NOT NULL,
                        member_id INTEGER,
                        is_primary INTEGER DEFAULT 0,
                        label TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(plate, member_id)
                    )
                ''')
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plate_bindings_plate "
                             "ON plate_bindings(plate)")
                conn.commit()
                _initialized = True
                return True
            finally:
                conn.close()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[车牌绑定] 建表失败: {e}")
            return False


def bind(plate: str, member_id: Optional[int],
         is_primary: bool = False, label: str = "") -> Dict[str, Any]:
    """绑定车牌到成员。同一 (plate, member_id) 重复绑定视为更新。

    设 is_primary=True 时会把该车牌下其他成员的主驾标记清掉（一台车只有一个主驾）。
    """
    plate_n = normalize_plate(plate)
    if not plate_n:
        return {"success": False, "message": "车牌号为空"}
    init_table()
    try:
        with _lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                if is_primary:
                    cur.execute("UPDATE plate_bindings SET is_primary=0 WHERE plate=?",
                                (plate_n,))
                cur.execute(
                    "INSERT INTO plate_bindings (plate, member_id, is_primary, label) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(plate, member_id) DO UPDATE SET "
                    "is_primary=excluded.is_primary, label=excluded.label",
                    (plate_n, member_id, 1 if is_primary else 0, label or ""),
                )
                conn.commit()
                return {"success": True, "plate": plate_n, "member_id": member_id,
                        "is_primary": bool(is_primary)}
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[车牌绑定] 绑定失败: {e}")
        return {"success": False, "message": str(e)}


def unbind(plate: str, member_id: Optional[int] = None) -> Dict[str, Any]:
    """解绑。member_id 为 None 时解绑该车牌的全部绑定。"""
    plate_n = normalize_plate(plate)
    if not plate_n:
        return {"success": False, "message": "车牌号为空"}
    init_table()
    try:
        with _lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                if member_id is None:
                    cur.execute("DELETE FROM plate_bindings WHERE plate=?", (plate_n,))
                else:
                    cur.execute("DELETE FROM plate_bindings WHERE plate=? AND member_id=?",
                                (plate_n, member_id))
                conn.commit()
                return {"success": True, "removed": cur.rowcount}
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[车牌绑定] 解绑失败: {e}")
        return {"success": False, "message": str(e)}


def get_candidates(plate: str) -> List[Dict[str, Any]]:
    """车牌 → 候选驾驶员列表（含成员昵称/姓名，主驾排前面）。

    返回空列表表示"这台车没绑定过" → 应视为访客车，不要猜是谁。
    """
    plate_n = normalize_plate(plate)
    if not plate_n:
        return []
    init_table()
    try:
        with _lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                # LEFT JOIN：允许 member_id 为空或成员已被删除的绑定仍能返回
                cur.execute(
                    "SELECT b.plate, b.member_id, b.is_primary, b.label, "
                    "       m.name, m.nickname, m.relation "
                    "FROM plate_bindings b "
                    "LEFT JOIN family_members m ON m.id = b.member_id "
                    "WHERE b.plate=? "
                    "ORDER BY b.is_primary DESC, b.id ASC",
                    (plate_n,),
                )
                rows = cur.fetchall()
                out = []
                for r in rows:
                    name = r["name"] or ""
                    out.append({
                        "plate": r["plate"],
                        "member_id": r["member_id"],
                        "name": name,
                        # 称呼优先用昵称（与 security_listener 的 _member_profile 口径一致）
                        "call_name": (r["nickname"] or name),
                        "relation": r["relation"] or "",
                        "is_primary": bool(r["is_primary"]),
                        "label": r["label"] or "",
                    })
                return out
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[车牌绑定] 查候选失败: {e}")
        return []


def is_known_plate(plate: str) -> bool:
    """该车牌是否已登记（区分"家人车"与"访客车"）。"""
    return len(get_candidates(plate)) > 0


def list_all() -> List[Dict[str, Any]]:
    """列出全部绑定（后台页面用）。"""
    init_table()
    try:
        with _lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT b.id, b.plate, b.member_id, b.is_primary, b.label, "
                    "       b.created_at, m.name, m.nickname "
                    "FROM plate_bindings b "
                    "LEFT JOIN family_members m ON m.id = b.member_id "
                    "ORDER BY b.plate ASC, b.is_primary DESC, b.id ASC")
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[车牌绑定] 列表失败: {e}")
        return []


def plates_of_member(member_id: int) -> List[str]:
    """某成员绑定的所有车牌。"""
    init_table()
    try:
        with _lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("SELECT plate FROM plate_bindings WHERE member_id=? "
                            "ORDER BY is_primary DESC, id ASC", (member_id,))
                return [r[0] for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[车牌绑定] 查成员车牌失败: {e}")
        return []
