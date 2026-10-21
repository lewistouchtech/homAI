"""夜间短期记忆梳理（2026-10-13，主人定的需求）。

做什么：每天凌晨 03:30，把「上次梳理之后」的整段对话（dialogue_history）重新过一遍，
按 ltm 的现行规则（指令/乱码过滤、说话人归属、敏感门、助手上文消歧）补抽进长期记忆。

跟实时抽取的关系：实时抽取是「说一句抽一句」，会因为失败、熔断、过滤窗口而漏；
夜间梳理是「兜底补漏」。**去重由 ltm 写入链路负责**（`_add_with_dedup`：先用文本相似度
挡掉已有内容，再让 ltm 拿候选事实去比对库里已有记忆，重复不写、已有事实被更新则 UPDATE），
所以重跑、与实时抽取重叠都不会产生重复记忆。

可靠性：
- 用「最后处理的 dialogue_history.id」当游标（data/.memory_consolidation_state.json），
  服务停机几天也不会漏，也不会重复扫已处理的行；首次运行只回看 24 小时，最多回看 3 天。
- 入队用稳定幂等键（turn=nightly-<行id>），同一行重复入队只会有一条待办。
- 抽取走 ltm 的落盘待办队列（outbox），失败自动退避重试，不会因为夜里模型暂时不可用而丢。
- 本模块**不删除任何对话历史**，只读。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = "memory_consolidation"
logger = setup_logging()

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

DEFAULT_HOUR = 3
DEFAULT_MINUTE = 30
FIRST_RUN_LOOKBACK_HOURS = 24
MAX_LOOKBACK_DAYS = 3
MAX_ROWS_PER_RUN = 5000
DRAIN_POLL_SECONDS = 30.0
DRAIN_MAX_SECONDS = 2 * 3600.0
ASSISTANT_CTX_CHARS = 60
STATE_HISTORY_KEEP = 14


def _db_path() -> Path:
    return _DATA_DIR / "devices.db"


def _state_path() -> Path:
    return _DATA_DIR / ".memory_consolidation_state.json"


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else _state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path) if path else _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"夜间梳理状态保存失败: {e}")


def seconds_until_next_run(now: datetime, hour: int = DEFAULT_HOUR,
                           minute: int = DEFAULT_MINUTE) -> float:
    """距离下一个「每天 hour:minute」还有多少秒（恰好等于该时刻则算明天）。"""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def fetch_rows(after_id: Optional[int], *, lookback_seconds: float,
               db_path: Optional[Path] = None, limit: int = MAX_ROWS_PER_RUN) -> List[Dict[str, Any]]:
    """取待梳理的对话行。created_at 是 SQLite CURRENT_TIMESTAMP（UTC），阈值必须同为 UTC。"""
    p = Path(db_path) if db_path else _db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(p), timeout=5)
        rows = conn.execute(
            "SELECT id, device_id, session_id, user_text, ai_text, created_at "
            "FROM dialogue_history WHERE id > ? AND created_at >= datetime('now', ?) "
            "ORDER BY id LIMIT ?",
            (int(after_id or 0), f"-{int(lookback_seconds)} seconds", int(limit)),
        ).fetchall()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"夜间梳理读取对话历史失败: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()
    return [
        {"id": r[0], "device_id": r[1], "session_id": r[2],
         "user_text": r[3], "ai_text": r[4], "created_at": r[5]}
        for r in rows
    ]


def _split_speaker(user_text: Any) -> tuple:
    """语音链路的 user_text 是 {"speaker":"李威","content":"…"}；返回 (说话人或None, 原文)。"""
    t = str(user_text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                sp = data.get("speaker")
                return (str(sp) if sp else None), data["content"].strip()
        except (ValueError, TypeError):
            pass
    return None, t


def _counters_snapshot(provider) -> Dict[str, int]:
    c = getattr(provider, "counters", {}) or {}
    return {k: int(c.get(k, 0)) for k in ("created", "updated", "deduped", "empty", "failed")}


async def _wait_drain(provider, max_seconds: float, poll: float) -> bool:
    """等待待办队列清空（pending + in_flight 都为 0）。返回是否清空。"""
    outbox = provider._ensure_outbox()
    if outbox is None:
        return True
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        counts = await asyncio.to_thread(outbox.counts) or {}
        if int(counts.get("pending", 0)) + int(counts.get("in_flight", 0)) == 0:
            return True
        provider._kick_worker()
        await asyncio.sleep(poll)
    return False


async def run_once(provider, *, now: Optional[datetime] = None,
                   db_path: Optional[Path] = None, state_path: Optional[Path] = None,
                   lookback_hours: Optional[float] = None,
                   drain_max_seconds: float = DRAIN_MAX_SECONDS,
                   drain_poll_seconds: float = DRAIN_POLL_SECONDS) -> Dict[str, Any]:
    """执行一次梳理，返回报告 dict（同时写进状态文件的 history）。"""
    started = time.time()
    now = now or datetime.now()
    state = load_state(state_path)
    last_id = state.get("last_id")

    if lookback_hours is not None:
        lookback = float(lookback_hours) * 3600
        after_id = 0
    elif last_id is None:
        lookback, after_id = FIRST_RUN_LOOKBACK_HOURS * 3600, 0
    else:
        lookback, after_id = MAX_LOOKBACK_DAYS * 86400, int(last_id)

    rows = await asyncio.to_thread(
        fetch_rows, after_id, lookback_seconds=lookback, db_path=db_path
    )
    report: Dict[str, Any] = {
        "run_at": now.isoformat(timespec="seconds"),
        "scanned": len(rows), "enqueued": 0, "skipped": 0,
        "drained": True, "from_id": after_id, "to_id": after_id,
    }
    if not rows:
        logger.bind(tag=TAG).info("夜间梳理：没有新的对话需要处理")
    else:
        before = _counters_snapshot(provider)
        for r in rows:
            speaker, content = _split_speaker(r["user_text"])
            if not content:
                report["skipped"] += 1
                continue
            ctx = str(r.get("ai_text") or "").strip()[:ASSISTANT_CTX_CHARS] or None
            try:
                n = await provider.save_memory(
                    [{"role": "user", "content": content, "speaker": speaker}],
                    r.get("device_id"),
                    speaker=speaker,
                    assistant_ctx=ctx,
                    device_id=r.get("device_id"),
                    turn=f"nightly-{r['id']}",
                )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"夜间梳理入队失败（行 {r['id']}）: {e}")
                n = None
            if n:
                report["enqueued"] += int(n)
            else:
                report["skipped"] += 1
        report["to_id"] = rows[-1]["id"]
        report["drained"] = await _wait_drain(provider, drain_max_seconds, drain_poll_seconds)
        after = _counters_snapshot(provider)
        for k in before:
            report[k] = after[k] - before[k]
        logger.bind(tag=TAG).info(
            f"夜间梳理完成：扫描 {report['scanned']} 行，跳过 {report['skipped']}（被过滤或已处理过），"
            f"入队 {report['enqueued']}；新增 {report.get('created', 0)}、更新 {report.get('updated', 0)}、"
            f"重复跳过 {report.get('deduped', 0)}、模型判空 {report.get('empty', 0)}、失败 {report.get('failed', 0)}"
            f"{'' if report['drained'] else '（队列未在时限内清空，剩余由后台继续处理）'}"
        )

    # 游标只在「入队完成」后前进：队列里的待办本身是落盘的，后续失败由 outbox 重试，不会丢
    if report["to_id"]:  # 没扫到任何行时不写游标，否则首次定时运行会错用「回看 3 天」
        state["last_id"] = max(int(last_id or 0), int(report["to_id"]))
    state["last_run"] = report["run_at"]
    hist = list(state.get("history") or [])
    report["seconds"] = round(time.time() - started, 1)
    hist.append(report)
    state["history"] = hist[-STATE_HISTORY_KEEP:]
    save_state(state, state_path)
    return report


def _ltm_provider_of(ws_server):
    mem = getattr(ws_server, "_memory", None)
    ok = mem is not None and hasattr(mem, "save_memory") and hasattr(mem, "_ensure_outbox")
    return mem if ok else None


async def consolidation_loop(ws_server, config: Dict[str, Any]) -> None:
    """常驻循环：每天 hour:minute 执行一次。服务停机错过的时刻，下次到点补跑（游标保证不漏）。"""
    hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
    try:
        sel = (config.get("selected_module") or {}).get("Memory")
        mem_cfg = (config.get("Memory") or {}).get(sel) or {}
        if mem_cfg.get("nightly_consolidation") is False:
            logger.bind(tag=TAG).info("夜间梳理已在配置中关闭")
            return
        hhmm = str(mem_cfg.get("nightly_consolidation_time") or "")
        if ":" in hhmm:
            h, m = hhmm.split(":", 1)
            hour, minute = int(h), int(m)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"夜间梳理配置解析失败，用默认 03:30: {e}")
    logger.bind(tag=TAG).info(f"夜间梳理已排程：每天 {hour:02d}:{minute:02d}")
    while True:
        try:
            await asyncio.sleep(seconds_until_next_run(datetime.now(), hour, minute))
            provider = _ltm_provider_of(ws_server)
            if provider is None:
                logger.bind(tag=TAG).info("当前记忆后端不是 ltm，跳过夜间梳理")
                continue
            await run_once(provider)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(tag=TAG).error(f"夜间梳理异常（明天继续）: {e}")
            await asyncio.sleep(60)
