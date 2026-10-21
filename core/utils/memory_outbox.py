# -*- coding: UTF-8 -*-
"""记忆抽取待办队列（2026-10-13 WP-3）

为什么要落盘而不是内存缓冲（实测确认的问题）：
- 原 `_extract_now()` **先清空** `_session_msgs[source]` 再调 `mem.add()`，
  502 / 超时 / 无效 JSON 时这批消息**永久丢失**（近 6 个日志文件里 43 次 502、
  13 次 Context size exceeded、15 次 Model unloaded，全部只留一行 ERROR）。
- `MAX_BACKLOG=24` 溢出时 `del bucket[:...]` 直接丢最旧的消息。
- 进程重启时内存缓冲全丢。

设计：SQLite outbox（WAL），状态机
    pending → in_flight → done
    in_flight → pending（退避重试，tries+1）
    tries > MAX_TRIES → dead（人工可在管理台查看/重放/丢弃）
幂等：msg_id 唯一键。同一轮对话被重复提交（实时保存 + 关闭收尾）只会入库一次，
不再产生重复记忆（现网 160 条里有 28 条完全重复，主因就是重复提交）。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

logger = setup_logging()
TAG = __name__

_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 重试退避（秒）：LM Studio 卸载模型 / 502 期间不要高频重试放大故障
BACKOFF_SECONDS = (30.0, 120.0, 600.0, 3600.0)
MAX_TRIES = 5
# 待办保留期：超过就作废（避免攒了几个月的闲聊一次性砸给模型）
PENDING_TTL_SECONDS = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_outbox (
    msg_id        TEXT PRIMARY KEY,
    scope         TEXT NOT NULL,
    speaker       TEXT,
    speaker_known INTEGER NOT NULL DEFAULT 0,
    device_id     TEXT,
    session_id    TEXT,
    text          TEXT NOT NULL,
    assistant_ctx TEXT,
    ts            REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    tries         INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON memory_outbox(status, next_retry_at);
"""


def _db_path() -> Path:
    return _DB_DIR / "memory_outbox.db"


def make_msg_id(device_id: str, session_id: str, turn: Any, text: str) -> str:
    """稳定幂等键：同一轮同一句话无论提交几次都只有一个 msg_id。"""
    raw = f"{device_id}|{session_id}|{turn}|{text}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


class MemoryOutbox:
    """记忆抽取待办队列。所有方法都吞异常（记忆不可用不能拖垮对话）。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _db_path()
        self._ensured = False

    # ------------------------------------------------------------ 基础设施

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            if not self._ensured:
                conn.executescript(_SCHEMA)
                self._ensured = True
            return conn
        except Exception as e:
            logger.bind(tag=TAG).error(f"记忆待办队列打不开（{self.db_path}）: {e}")
            return None

    def restore_in_flight(self) -> int:
        """进程启动/重启：把上次没跑完的 in_flight 恢复成 pending（崩溃恢复）。"""
        conn = self._connect()
        if conn is None:
            return 0
        try:
            cur = conn.execute(
                "UPDATE memory_outbox SET status='pending', next_retry_at=0 "
                "WHERE status='in_flight'"
            )
            conn.commit()
            n = cur.rowcount or 0
            if n:
                logger.bind(tag=TAG).info(f"记忆待办队列恢复 {n} 条未完成任务")
            return n
        except Exception as e:
            logger.bind(tag=TAG).warning(f"恢复未完成任务失败: {e}")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------ 入队

    def enqueue(
        self,
        *,
        msg_id: str,
        scope: str,
        text: str,
        speaker: Optional[str] = None,
        speaker_known: bool = False,
        device_id: Optional[str] = None,
        session_id: Optional[str] = None,
        assistant_ctx: Optional[str] = None,
    ) -> bool:
        """入队一条待抽取的用户消息。重复 msg_id 直接忽略（幂等）。"""
        if not text or not text.strip():
            return False
        conn = self._connect()
        if conn is None:
            return False
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_outbox "
                "(msg_id, scope, speaker, speaker_known, device_id, session_id, "
                " text, assistant_ctx, ts, status, tries, next_retry_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',0,0)",
                (
                    msg_id, scope, speaker, 1 if speaker_known else 0,
                    device_id, session_id, text.strip(), assistant_ctx, time.time(),
                ),
            )
            conn.commit()
            return conn.total_changes > 0
        except Exception as e:
            logger.bind(tag=TAG).warning(f"记忆入队失败: {e}")
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------ 取任务

    def claim_batch(self, limit: int = 8, max_chars: int = 700) -> List[Dict[str, Any]]:
        """取出到期任务并标记 in_flight。按 ts 升序（先说的话先抽）。

        同时按「条数 + 合计字数」限量：LM Studio 的 llama-server 是 ctx 8192 /
        parallel 4，每槽只有 2048 token，抽取提示词约 1.3K，塞太多必 400。
        """
        conn = self._connect()
        if conn is None:
            return []
        try:
            now = time.time()
            cur = conn.execute(
                "SELECT msg_id, scope, speaker, speaker_known, device_id, session_id, "
                "       text, assistant_ctx, ts, tries FROM memory_outbox "
                "WHERE status='pending' AND next_retry_at <= ? AND ts > ? "
                "ORDER BY ts ASC LIMIT ?",
                (now, now - PENDING_TTL_SECONDS, int(limit)),
            )
            rows = cur.fetchall()
            picked: List[Dict[str, Any]] = []
            chars = 0
            for r in rows:
                t = r[6] or ""
                if picked and chars + len(t) > max_chars:
                    break
                picked.append({
                    "msg_id": r[0], "scope": r[1], "speaker": r[2],
                    "speaker_known": bool(r[3]), "device_id": r[4],
                    "session_id": r[5], "text": t, "assistant_ctx": r[7],
                    "ts": r[8], "tries": r[9],
                })
                chars += len(t)
            if picked:
                ids = [p["msg_id"] for p in picked]
                conn.executemany(
                    "UPDATE memory_outbox SET status='in_flight' WHERE msg_id=?",
                    [(i,) for i in ids],
                )
                conn.commit()
            return picked
        except Exception as e:
            logger.bind(tag=TAG).warning(f"取记忆待办失败: {e}")
            return []
        finally:
            conn.close()

    def mark_done(self, msg_id: str, created: int = 0, updated: int = 0) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                "UPDATE memory_outbox SET status='done', last_error=NULL, "
                "created_count=?, updated_count=? WHERE msg_id=?",
                (int(created), int(updated), msg_id),
            )
            conn.commit()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"标记完成失败: {e}")
        finally:
            conn.close()

    def mark_failed(self, msg_id: str, error: str, tries: int) -> str:
        """失败：退避重试；超过 MAX_TRIES 进 dead。返回最终状态。"""
        conn = self._connect()
        if conn is None:
            return "unknown"
        try:
            next_tries = int(tries) + 1
            if next_tries >= MAX_TRIES:
                status = "dead"
                delay = 0.0
            else:
                status = "pending"
                idx = min(next_tries - 1, len(BACKOFF_SECONDS) - 1)
                delay = BACKOFF_SECONDS[idx]
            conn.execute(
                "UPDATE memory_outbox SET status=?, tries=?, next_retry_at=?, "
                "last_error=? WHERE msg_id=?",
                (status, next_tries, time.time() + delay, str(error)[:400], msg_id),
            )
            conn.commit()
            if status == "dead":
                logger.bind(tag=TAG).error(
                    f"记忆抽取进入死信（重试 {next_tries} 次仍失败）: {str(error)[:200]}"
                )
            return status
        except Exception as e:
            logger.bind(tag=TAG).warning(f"标记失败状态出错: {e}")
            return "unknown"
        finally:
            conn.close()

    # ------------------------------------------------------------ 观测

    def counts(self) -> Dict[str, int]:
        conn = self._connect()
        if conn is None:
            return {}
        try:
            out: Dict[str, int] = {}
            for status, n in conn.execute(
                "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status"
            ):
                out[str(status)] = int(n)
            row = conn.execute(
                "SELECT COALESCE(SUM(created_count),0), COALESCE(SUM(updated_count),0) "
                "FROM memory_outbox"
            ).fetchone()
            out["created_memories"] = int(row[0] or 0)
            out["updated_memories"] = int(row[1] or 0)
            return out
        except Exception as e:
            logger.bind(tag=TAG).warning(f"统计待办队列失败: {e}")
            return {}
        finally:
            conn.close()

    def list_dead(self, limit: int = 50) -> List[Dict[str, Any]]:
        """死信列表（管理台人工查看/重放/丢弃）。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT msg_id, scope, speaker, text, tries, last_error, ts "
                "FROM memory_outbox WHERE status='dead' ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [
                {"msg_id": r[0], "scope": r[1], "speaker": r[2], "text": r[3],
                 "tries": r[4], "last_error": r[5], "ts": r[6]}
                for r in rows
            ]
        except Exception as e:
            logger.bind(tag=TAG).warning(f"读死信失败: {e}")
            return []
        finally:
            conn.close()

    def replay(self, msg_id: str) -> bool:
        """把死信重新放回 pending（人工重放）。"""
        conn = self._connect()
        if conn is None:
            return False
        try:
            conn.execute(
                "UPDATE memory_outbox SET status='pending', tries=0, next_retry_at=0, "
                "last_error=NULL WHERE msg_id=?",
                (msg_id,),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"重放死信失败: {e}")
            return False
        finally:
            conn.close()

    def purge_done(self, older_than_seconds: float = 30 * 24 * 3600) -> int:
        """清理已完成的历史记录（只清 done，dead 永远保留供人工处理）。"""
        conn = self._connect()
        if conn is None:
            return 0
        try:
            cur = conn.execute(
                "DELETE FROM memory_outbox WHERE status='done' AND ts < ?",
                (time.time() - older_than_seconds,),
            )
            conn.commit()
            return cur.rowcount or 0
        except Exception as e:
            logger.bind(tag=TAG).warning(f"清理已完成记录失败: {e}")
            return 0
        finally:
            conn.close()
