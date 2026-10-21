# -*- coding: UTF-8 -*-
"""短期记忆续接（L1）—— 2026-10-13 WP-5

要解决的两个真实现象：

① **5 轮之外的内容彻底消失**
   `chat_engine.chat()` 每轮开头调 `trim_history(max_turns=5)`，被截掉的旧轮次
   直接丢弃。用户聊到第 8 轮时说「刚才我说的那个地方」，模型已经看不到了。

② **刷新页面 / 待机唤醒后就失忆**
   新连接的 `dialogue` 是空的。而 `dialogue_history` 表里其实**已经有 10807 行**
   按 device_id 记录的真实对话（user_text / ai_text / created_at），
   却一直**只写不读**（读方只有 data_collector、conversation_summary 和管理台展示）。

设计（不加新表、不调 LLM、不碰稳定模块的热路径）：
- **滚动上下文**：`trim_history` 截掉的轮次不丢，压成「用户说… / 伊娃答…」的
  紧凑文本挂在本连接上，注入 `<recent_context>`（system prompt 段，
  **不塞回 dialogue 消息列表** —— 塞回去会破坏 tool_calls 的配对结构）。
- **跨连接续接**：新连接建立时从 `dialogue_history` 取本设备最近 N 轮
  （默认 TTL 24 小时）注入同一段 `<recent_context>`。
- 按主人 2026-10-13 裁决 D4，被截掉的轮次同时落 `conversation_summaries` 表
  （该表已存在但 topic 全空、valuable 全 0，是半成品；现在给它真实内容）。

⚠️ 全部函数吞异常返回空值：短期上下文属于增强项，任何失败都不能影响对话主链。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

logger = setup_logging()
TAG = __name__

_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 跨连接续接：取最近几轮 / 只看多久以内的
# 2026-10-13 主人要求：短期记忆至少保留一天（原 2 小时），条数 6→8
RECALL_MAX_ROUNDS = 8
RECALL_TTL_SECONDS = 24 * 3600
# 单条文本截断（注入的是「线索」，不是全文；全文在 dialogue_history 里）
RECALL_USER_CHARS = 80
RECALL_AI_CHARS = 60
# 滚动上下文：最多记几轮、整段最多多少字
ROLLING_MAX_ROUNDS = 8
ROLLING_MAX_CHARS = 700

_HEADER = (
    "以下是本次对话更早之前的内容摘要（已被截断出上下文窗口，仅供理解指代与延续话题）。"
    "**不要主动复述或总结这些内容**，只有在用户明确提到「刚才/之前/上次说的」时才引用。"
)


def _plain(text: Any) -> str:
    """语音链路的用户话是 {"speaker":"李威","content":"…"} 的 JSON，注入前取出 content。"""
    t = str(text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                return data["content"].strip()
        except (ValueError, TypeError):
            pass
    return t


def _assistant_label() -> str:
    """助手称呼取后台配置（不得硬编码人名），取不到兜底「助手」。"""
    try:
        from core.utils.assistant_identity import safe_name

        return safe_name() or "助手"
    except Exception:
        return "助手"


def _db_path() -> Path:
    return _DB_DIR / "devices.db"


def _connect() -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(str(_db_path()), timeout=3)
        return conn
    except Exception as e:
        logger.bind(tag=TAG).warning(f"打开 devices.db 失败，短期续接跳过: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 一、跨连接续接：从 dialogue_history 回灌
# ══════════════════════════════════════════════════════════════════

def build_recent_context(
    device_id: Optional[str],
    *,
    exclude_session_id: Optional[str] = None,
    max_rounds: int = RECALL_MAX_ROUNDS,
    ttl_seconds: float = RECALL_TTL_SECONDS,
) -> str:
    """新连接建立时，取本设备最近的对话轮次拼成 `<recent_context>` 内容。

    - 只取 `ttl_seconds` 以内的（隔了一天的旧对话不该当作「刚才」）
    - 排除**当前** session_id（那是本次连接自己刚说的话，已经在 dialogue 里）
    - 拿不到就返回空串
    """
    if not device_id:
        return ""
    conn = _connect()
    if conn is None:
        return ""
    try:
        sql = (
            "SELECT user_text, ai_text, datetime(created_at, 'localtime') FROM dialogue_history "
            "WHERE device_id = ? AND created_at >= datetime('now', ?) "
        )
        params: List[Any] = [device_id, f"-{int(ttl_seconds)} seconds"]
        if exclude_session_id:
            sql += "AND (session_id IS NULL OR session_id != ?) "
            params.append(exclude_session_id)
        sql += "ORDER BY id DESC LIMIT ?"
        params.append(int(max_rounds))
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return ""
        rows.reverse()  # 改成时间正序
        lines = []
        for user_text, ai_text, created_at in rows:
            u = _plain(user_text).replace("\n", " ")[:RECALL_USER_CHARS]
            a = str(ai_text or "").strip().replace("\n", " ")[:RECALL_AI_CHARS]
            if not u and not a:
                continue
            stamp = str(created_at or "")[5:16]  # MM-DD HH:MM
            line = f"- [{stamp}] 用户：{u}" if u else f"- [{stamp}]"
            if a:
                line += f" ／ {_assistant_label()}：{a}"
            lines.append(line)
        if not lines:
            return ""
        text = "\n".join(lines)
        logger.bind(tag=TAG).info(
            f"[短期续接] 已回灌 {len(lines)} 轮历史对话（设备 {device_id}，{len(text)} 字）"
        )
        return text
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[短期续接] 回灌失败（不影响对话）: {e}")
        return ""
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# 二、滚动上下文：接住 trim_history 截掉的轮次
# ══════════════════════════════════════════════════════════════════

def capture_trimmed(dialogue_msgs: List[Any], kept_count: int) -> List[Dict[str, str]]:
    """从「截断前的完整消息列表」里挑出将被丢弃的 user/assistant 轮次。

    调用方必须在 `trim_history()` **之前**取一份 `list(conn.dialogue.dialogue)`，
    因为 trim 是就地重建列表，事后拿不到被丢的内容。

    只收 user / assistant，且跳过 is_temporary 的工具提醒；
    tool 消息一律不要（那是天气/设备列表这类环境快照，不是对话内容）。
    """
    if not dialogue_msgs or kept_count <= 0:
        return []
    try:
        conv = [m for m in dialogue_msgs
                if getattr(m, "role", "") in ("user", "assistant")
                and not getattr(m, "is_temporary", False)]
        dropped = conv[:-kept_count] if len(conv) > kept_count else []
        out = []
        for m in dropped:
            text = str(getattr(m, "content", "") or "").strip().replace("\n", " ")
            if not text:
                continue
            out.append({"role": getattr(m, "role", "user"), "text": text})
        return out
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[滚动上下文] 提取被截断轮次失败: {e}")
        return []


def append_rolling(conn, rounds: List[Dict[str, str]]) -> None:
    """把被截断的轮次追加到本连接的滚动上下文（超限丢最旧）。"""
    if not rounds:
        return
    try:
        buf = getattr(conn, "_recent_rolling", None)
        if buf is None:
            buf = []
            conn._recent_rolling = buf
        buf.extend(rounds)
        # 只保留最近 ROLLING_MAX_ROUNDS 轮（一轮 = user + assistant 两条）
        limit = ROLLING_MAX_ROUNDS * 2
        if len(buf) > limit:
            del buf[:len(buf) - limit]
        # 整段字数上限：注入的是线索，不该把上下文窗口吃光
        total = sum(len(r["text"]) for r in buf)
        while total > ROLLING_MAX_CHARS and len(buf) > 2:
            total -= len(buf[0]["text"])
            buf.pop(0)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[滚动上下文] 追加失败: {e}")


def rolling_context_text(conn) -> str:
    """本连接滚动上下文的注入文本。"""
    try:
        buf = getattr(conn, "_recent_rolling", None) or []
        if not buf:
            return ""
        lines = []
        for r in buf:
            who = "用户" if r.get("role") == "user" else _assistant_label()
            text = _plain(r.get("text", ""))
            limit = RECALL_USER_CHARS if who == "用户" else RECALL_AI_CHARS
            lines.append(f"- {who}：{text[:limit]}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_context_block(conn) -> str:
    """合并「跨连接回灌」与「本连接滚动」两段，作为 `<recent_context>` 的内容。"""
    try:
        parts = []
        carried = str(getattr(conn, "_recent_context_db", "") or "").strip()
        rolling = rolling_context_text(conn)
        if carried:
            parts.append("【本次连接之前的对话】\n" + carried)
        if rolling:
            parts.append("【本次连接中已被截断的更早轮次】\n" + rolling)
        if not parts:
            return ""
        return _HEADER + "\n" + "\n\n".join(parts)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════
# 三、落库 conversation_summaries（主人裁决 D4：接入 L1）
# ══════════════════════════════════════════════════════════════════

def persist_trimmed_rounds(
    session_id: Optional[str],
    rounds: List[Dict[str, str]],
    *,
    speaker: Optional[str] = None,
) -> bool:
    """把被截断的轮次写进 conversation_summaries。

    该表原本 1069 行、`topic` 全空、`valuable` 全 0（conversation_summary.py
    只写单句、从不填 topic），等于半成品。现在给它真实内容：
    - `topic` = 'rolling_context'（便于日后按类型清理/统计）
    - `content` = 被截断轮次的紧凑文本
    - `valuable` = 0（这是短期上下文，不是长期事实；长期事实走 mem0）
    """
    if not rounds:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        lines = []
        for r in rounds:
            who = "用户" if r.get("role") == "user" else _assistant_label()
            lines.append(f"{who}：{_plain(r.get('text', ''))[:120]}")
        content = "\n".join(lines)[:1200]
        conn.execute(
            "INSERT INTO conversation_summaries "
            "(session_id, timestamp, speaker, topic, content, keywords, valuable) "
            "VALUES (?,?,?,?,?,NULL,0)",
            (str(session_id or ""), time.time(), str(speaker or "未知"),
             "rolling_context", content),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[滚动上下文] 落库失败: {e}")
        return False
    finally:
        conn.close()
