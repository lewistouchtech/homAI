# -*- coding: utf-8 -*-
"""开发数据采集（旁路统计，不影响主链路）

设计原则（2026-09-26 主人确认）：
    这是一个**额外的、只为了统计用的需求**，因此**绝不修改主对话链路**。
    所有采集均为「最优努力」——任何异常都只记日志，绝不向上抛出，
    保证采集失败时主流程完全不受影响。

数据来源全部是**已有的、旁路的**：
    - dialogue_history（已有表，对话历史）→ 派生 interaction_log
    - security_listener 的 Frigate 事件 → event_log
    - 显式反馈（API）→ feedback_log

三张表用途：
    interaction_log  每次交互一条（用户说了什么、调了哪个工具、延迟、是否被打断）
    event_log        安防/家居事件（人/脸/车牌/音频/逗留/在场）
    feedback_log     显式与隐式反馈（点赞/点踩/重说/打断/沉默）

供开发端做数据驱动优化：
    - 找 LLM 理解错的样本 → 补提示词/工具描述
    - 找 ASR 常错的词 → 补热词/换模型
    - 找延迟瓶颈 → 优化链路
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# 写锁：sqlite 在多线程下并发写会报 database is locked，用单锁串行化（采集是低频操作）
_write_lock = threading.Lock()

# 单条文本长度上限，避免超长内容把库撑爆
_MAX_TEXT = 2000

# 初始化只做一次
_initialized = False
_init_lock = threading.Lock()


def init_tables() -> bool:
    """建三张采集表（幂等）。返回是否成功。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return True
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS interaction_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT,
                        device_id TEXT,
                        member_id INTEGER,
                        user_text TEXT,
                        ai_text TEXT,
                        tool_called TEXT,
                        tool_args TEXT,
                        tool_result TEXT,
                        latency_ms INTEGER,
                        interrupted INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS event_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT,
                        camera_id TEXT,
                        member_id INTEGER,
                        label TEXT,
                        plate TEXT,
                        greeting_triggered INTEGER DEFAULT 0,
                        greeting_text TEXT,
                        extra TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS feedback_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        interaction_id INTEGER,
                        type TEXT,
                        detail TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # ── 2026-10-18 主人批准 P8：运行观察新增三张表 ──
                # observation_metrics：高频事件的**聚合**（每 10 分钟一行），
                #   如「被静默丢弃的语音 47 条、疑似唤醒失败 3 条」+ 少量样本。
                #   不逐条落库是为了避免一天上千行把库撑爆（声纹丢弃日均 ~2000）。
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS observation_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        window_start REAL,
                        window_end REAL,
                        counters TEXT,
                        samples TEXT,
                        gauges TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # turn_log：每轮对话一行（耗时 / 是否调工具 / 是否被打断）
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS turn_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT,
                        session_id TEXT,
                        user_head TEXT,
                        turn_ms INTEGER,
                        reply_chars INTEGER,
                        with_tool INTEGER DEFAULT 0,
                        aborted INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # tool_call_log：每次工具调用一行（工具名 / 成败 / 耗时 / 错误）
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS tool_call_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name TEXT,
                        device_id TEXT,
                        ok INTEGER DEFAULT 1,
                        latency_ms INTEGER,
                        error TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # 常用查询索引
                cur.execute("CREATE INDEX IF NOT EXISTS idx_interaction_created "
                            "ON interaction_log(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_event_created "
                            "ON event_log(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_event_plate "
                            "ON event_log(plate)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_interaction "
                            "ON feedback_log(interaction_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_created "
                            "ON observation_metrics(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_turn_created "
                            "ON turn_log(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_toolcall_created "
                            "ON tool_call_log(created_at)")
                conn.commit()
                _initialized = True
                logger.bind(tag=TAG).info("[数据采集] 采集表已就绪（interaction/event/feedback/observation_metrics/turn/tool_call）")
                return True
            finally:
                conn.close()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[数据采集] 建表失败（不影响主流程）: {e}")
            return False


def _clip(text: Any, limit: int = _MAX_TEXT) -> str:
    """截断过长文本，避免库膨胀。"""
    if text is None:
        return ""
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…[截断]"


def record_interaction(
    device_id: str = "",
    session_id: str = "",
    user_text: str = "",
    ai_text: str = "",
    member_id: Optional[int] = None,
    tool_called: Optional[str] = None,
    tool_args: Any = None,
    tool_result: Any = None,
    latency_ms: Optional[int] = None,
    interrupted: bool = False,
) -> Optional[int]:
    """记录一次交互。返回 interaction_id（失败返回 None）。**绝不抛异常。**"""
    try:
        init_tables()
        if isinstance(tool_args, (dict, list)):
            tool_args = json.dumps(tool_args, ensure_ascii=False)
        if isinstance(tool_result, (dict, list)):
            tool_result = json.dumps(tool_result, ensure_ascii=False)
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO interaction_log "
                    "(session_id, device_id, member_id, user_text, ai_text, "
                    " tool_called, tool_args, tool_result, latency_ms, interrupted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id or "", device_id or "", member_id,
                     _clip(user_text), _clip(ai_text),
                     tool_called, _clip(tool_args), _clip(tool_result),
                     latency_ms, 1 if interrupted else 0),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录交互失败（忽略）: {e}")
        return None


def record_event(
    event_type: str,
    camera_id: str = "",
    member_id: Optional[int] = None,
    label: str = "",
    plate: str = "",
    greeting_triggered: bool = False,
    greeting_text: str = "",
    extra: Any = None,
) -> Optional[int]:
    """记录一条安防/家居事件。**绝不抛异常。**

    event_type 取值建议：person / face / plate / audio / dwell / presence / unknown_person
    """
    try:
        init_tables()
        if isinstance(extra, (dict, list)):
            extra = json.dumps(extra, ensure_ascii=False)
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO event_log "
                    "(event_type, camera_id, member_id, label, plate, "
                    " greeting_triggered, greeting_text, extra) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_type, camera_id or "", member_id, label or "",
                     plate or "", 1 if greeting_triggered else 0,
                     _clip(greeting_text), _clip(extra)),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录事件失败（忽略）: {e}")
        return None


def record_feedback(
    interaction_id: Optional[int],
    type: str,
    detail: str = "",
) -> Optional[int]:
    """记录反馈。**绝不抛异常。**

    type 取值建议：thumbs_up / thumbs_down / rephrase / interrupt / silence
    """
    try:
        init_tables()
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO feedback_log (interaction_id, type, detail) "
                    "VALUES (?, ?, ?)",
                    (interaction_id, type, _clip(detail)),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录反馈失败（忽略）: {e}")
        return None


# ==================== 运行观察：聚合指标 / 轮次 / 工具调用（2026-10-18 P5~P8） ====================

def record_metrics(
    window_start: float,
    window_end: float,
    counters: Any = None,
    samples: Any = None,
    gauges: Any = None,
) -> Optional[int]:
    """写一行聚合指标（由 core/utils/observation.py 每 10 分钟调用）。**绝不抛异常。**"""
    try:
        init_tables()
        if isinstance(counters, (dict, list)):
            counters = json.dumps(counters, ensure_ascii=False)
        if isinstance(samples, (dict, list)):
            samples = json.dumps(samples, ensure_ascii=False)
        if isinstance(gauges, (dict, list)):
            gauges = json.dumps(gauges, ensure_ascii=False)
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO observation_metrics "
                    "(window_start, window_end, counters, samples, gauges) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (float(window_start), float(window_end),
                     _clip(counters), _clip(samples), _clip(gauges)),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录聚合指标失败（忽略）: {e}")
        return None


def record_turn(
    device_id: str = "",
    session_id: str = "",
    user_head: str = "",
    turn_ms: Optional[int] = None,
    reply_chars: int = 0,
    with_tool: bool = False,
    aborted: bool = False,
) -> Optional[int]:
    """记一轮对话的结果。**绝不抛异常。**"""
    try:
        init_tables()
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO turn_log "
                    "(device_id, session_id, user_head, turn_ms, reply_chars, "
                    " with_tool, aborted) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (device_id or "", session_id or "", _clip(user_head, 80),
                     turn_ms, int(reply_chars or 0),
                     1 if with_tool else 0, 1 if aborted else 0),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录轮次失败（忽略）: {e}")
        return None


def record_tool_call(
    tool_name: str,
    device_id: str = "",
    ok: bool = True,
    latency_ms: Optional[int] = None,
    error: str = "",
) -> Optional[int]:
    """记一次工具调用。**绝不抛异常。**"""
    try:
        init_tables()
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO tool_call_log "
                    "(tool_name, device_id, ok, latency_ms, error) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (tool_name or "", device_id or "", 1 if ok else 0,
                     latency_ms, _clip(error, 300)),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[数据采集] 记录工具调用失败（忽略）: {e}")
        return None


# ==================== 旁路派生：从 dialogue_history 同步 ====================

def sync_from_dialogue_history(limit: int = 200) -> int:
    """把 dialogue_history 里**尚未同步**的记录派生到 interaction_log。

    这是"旁路采集"的核心：**不修改 chat_engine**，而是定期把已有的对话历史
    搬一份到 interaction_log，供后续分析。返回本次新增条数。

    幂等：通过 (device_id, user_text, ai_text, 时间) 去重，避免重复搬。
    """
    added = 0
    try:
        init_tables()
        with _write_lock:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            try:
                cur = conn.cursor()
                # 取最近 N 条原始对话
                cur.execute(
                    "SELECT device_id, session_id, user_text, ai_text, created_at "
                    "FROM dialogue_history ORDER BY id DESC LIMIT ?", (int(limit),)
                )
                rows = cur.fetchall()
                for device_id, session_id, user_text, ai_text, created_at in rows:
                    # 去重：同一设备 + 同一时间点 + 同样文本 视为已同步
                    cur.execute(
                        "SELECT 1 FROM interaction_log "
                        "WHERE device_id=? AND user_text=? AND ai_text=? "
                        "AND created_at=? LIMIT 1",
                        (device_id or "", user_text or "", ai_text or "",
                         created_at),
                    )
                    if cur.fetchone():
                        continue
                    # [2026-10-18 P8] 回填轮次/工具/打断：从 turn_log 与 tool_call_log
                    # 里找 ±20 秒内同一设备的记录。dialog_history 与这两张表都用
                    # SQLite CURRENT_TIMESTAMP（UTC），可直接比。
                    latency_ms, interrupted = None, 0
                    tool_called = None
                    try:
                        cur.execute(
                            "SELECT turn_ms, aborted FROM turn_log "
                            "WHERE device_id=? AND "
                            "abs(strftime('%s', created_at) - strftime('%s', ?)) <= 20 "
                            "ORDER BY id DESC LIMIT 1",
                            (device_id or "", created_at),
                        )
                        _t = cur.fetchone()
                        if _t:
                            latency_ms = _t[0]
                            interrupted = int(_t[1] or 0)
                        cur.execute(
                            "SELECT group_concat(DISTINCT tool_name) FROM tool_call_log "
                            "WHERE device_id=? AND "
                            "abs(strftime('%s', created_at) - strftime('%s', ?)) <= 20",
                            (device_id or "", created_at),
                        )
                        _tool = cur.fetchone()
                        if _tool and _tool[0]:
                            tool_called = _tool[0]
                    except Exception:
                        pass
                    cur.execute(
                        "INSERT INTO interaction_log "
                        "(session_id, device_id, user_text, ai_text, created_at, "
                        " latency_ms, interrupted, tool_called) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (session_id or "", device_id or "",
                         _clip(user_text), _clip(ai_text), created_at,
                         latency_ms, interrupted, tool_called),
                    )
                    added += 1
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[数据采集] 同步对话历史失败（忽略）: {e}")
    return added


def query_summary(days: int = 7) -> Dict[str, Any]:
    """产出最近 N 天的汇总，供开发端分析。"""
    out: Dict[str, Any] = {"interactions": 0, "events": 0, "feedback": 0,
                           "tool_usage": {}, "feedback_types": {}}
    try:
        init_tables()
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        try:
            cur = conn.cursor()
            since = f"datetime('now', '-{int(days)} days')"
            cur.execute(f"SELECT COUNT(*) FROM interaction_log WHERE created_at >= {since}")
            out["interactions"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM event_log WHERE created_at >= {since}")
            out["events"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM feedback_log WHERE created_at >= {since}")
            out["feedback"] = cur.fetchone()[0]
            cur.execute(f"SELECT tool_called, COUNT(*) FROM interaction_log "
                        f"WHERE created_at >= {since} AND tool_called IS NOT NULL "
                        f"GROUP BY tool_called ORDER BY 2 DESC LIMIT 20")
            out["tool_usage"] = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(f"SELECT type, COUNT(*) FROM feedback_log "
                        f"WHERE created_at >= {since} GROUP BY type ORDER BY 2 DESC")
            out["feedback_types"] = {r[0]: r[1] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[数据采集] 汇总失败（忽略）: {e}")
    return out
