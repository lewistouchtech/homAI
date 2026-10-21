# -*- coding: utf-8 -*-
"""运行观察与反馈 API（2026-10-18，P9）

用途
----
管理台「运行观察」页的数据源：把最近几天真实发生的对讲列出来，让主人可以
直接点 👍 / 👎 或写一句备注。这些反馈进 `feedback_log`，与 `interaction_log`
的对话一一对应，是「纠错与调整」最直接的一手材料。

设计约束
--------
    · 只读列表 + 只写 feedback_log，**不碰对话链路**
    · 与采集器同一口径：created_at 是 UTC，前端按本地时间显示（+8）
    · 反馈类型白名单，避免脏数据
"""
import json
import os
import sqlite3

from aiohttp import web

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')

# 反馈类型白名单（与 data_collector.record_feedback 的约定一致）
FEEDBACK_TYPES = ("thumbs_up", "thumbs_down", "rephrase", "note")

_MAX_LIMIT = 200
_MAX_DAYS = 30


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(cur, name: str) -> bool:
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _parse_user_text(raw) -> dict:
    """interaction_log.user_text 是 JSON（含 content/speaker）或纯文本。"""
    out = {"content": "", "speaker": ""}
    if not raw:
        return out
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            out["content"] = str(j.get("content") or j.get("text") or "")
            out["speaker"] = str(j.get("speaker") or "")
            return out
    except Exception:
        pass
    out["content"] = str(raw)
    return out


async def handle_observation_recent(request: web.Request):
    """GET /api/observation/recent?days=3&limit=50&device=&only_unrated=0

    返回最近 N 天的对讲记录（含已有反馈），倒序。
    """
    try:
        days = int(request.query.get("days", 3))
    except Exception:
        days = 3
    try:
        limit = int(request.query.get("limit", 50))
    except Exception:
        limit = 50
    days = max(1, min(days, _MAX_DAYS))
    limit = max(1, min(limit, _MAX_LIMIT))
    device = (request.query.get("device") or "").strip()
    only_unrated = (request.query.get("only_unrated") or "") in ("1", "true", "yes")

    items, summary = [], {}
    try:
        conn = _db()
        try:
            cur = conn.cursor()
            if not _table_exists(cur, "interaction_log"):
                return web.json_response(
                    {"success": False, "error": "采集表不存在（服务尚未启动过采集？）",
                     "items": [], "summary": {}}, status=200)

            sql = ("SELECT i.id, i.device_id, i.user_text, i.ai_text, i.latency_ms, "
                   " i.tool_called, i.interrupted, i.created_at, "
                   " (SELECT f.type FROM feedback_log f WHERE f.interaction_id = i.id "
                   "  ORDER BY f.id DESC LIMIT 1) AS fb_type "
                   "FROM interaction_log i "
                   "WHERE i.created_at >= datetime('now', ?) ")
            params = [f"-{days} days"]
            if device:
                sql += "AND i.device_id = ? "
                params.append(device)
            sql += "ORDER BY i.id DESC LIMIT ?"
            params.append(limit)
            cur.execute(sql, params)
            for r in cur.fetchall():
                u = _parse_user_text(r["user_text"])
                item = {
                    "id": r["id"],
                    "device_id": r["device_id"] or "",
                    "created_at": r["created_at"] or "",
                    "user_text": u["content"],
                    "speaker": u["speaker"],
                    "ai_text": (r["ai_text"] or "")[:300],
                    "latency_ms": r["latency_ms"],
                    "tool_called": r["tool_called"],
                    "interrupted": int(r["interrupted"] or 0),
                    "feedback": r["fb_type"] or "",
                }
                if only_unrated and item["feedback"]:
                    continue
                items.append(item)

            # 汇总（同一窗口）
            summary = {"days": days, "returned": len(items)}
            try:
                cur.execute("SELECT COUNT(*) FROM turn_log "
                            "WHERE created_at >= datetime('now', ?)", (f"-{days} days",))
                summary["turns"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM tool_call_log "
                            "WHERE created_at >= datetime('now', ?)", (f"-{days} days",))
                summary["tool_calls"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM tool_call_log "
                            "WHERE ok = 0 AND created_at >= datetime('now', ?)",
                            (f"-{days} days",))
                summary["tool_failed"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM observation_metrics "
                            "WHERE created_at >= datetime('now', ?)", (f"-{days} days",))
                summary["metric_windows"] = cur.fetchone()[0]
                cur.execute("SELECT type, COUNT(*) FROM feedback_log "
                            "WHERE created_at >= datetime('now', ?) GROUP BY type",
                            (f"-{days} days",))
                summary["feedback"] = {t or "?": n for t, n in cur.fetchall()}
            except Exception:
                pass
        finally:
            conn.close()
    except Exception as e:
        return web.json_response({"success": False, "error": f"{e!r}",
                                  "items": [], "summary": {}}, status=200)

    return web.json_response({"success": True, "items": items, "summary": summary})


async def handle_observation_feedback(request: web.Request):
    """POST /api/observation/feedback

    body: {"interaction_id": 123, "type": "thumbs_up|thumbs_down|rephrase|note",
           "detail": "可选备注"}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体格式不对"}, status=400)

    ftype = str(body.get("type") or "").strip()
    if ftype not in FEEDBACK_TYPES:
        return web.json_response(
            {"success": False, "error": f"type 必须是 {list(FEEDBACK_TYPES)} 之一"}, status=400)
    detail = str(body.get("detail") or "")[:1000]
    iid = body.get("interaction_id")
    try:
        iid = int(iid) if iid is not None and str(iid) != "" else None
    except Exception:
        return web.json_response({"success": False, "error": "interaction_id 不是整数"}, status=400)

    # 校验 interaction_id 真的存在（防止前端传错把反馈挂到不存在的记录上）
    if iid is not None:
        try:
            conn = _db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM interaction_log WHERE id = ?", (iid,))
                if cur.fetchone() is None:
                    return web.json_response(
                        {"success": False, "error": f"interaction_log 里没有 id={iid}"}, status=400)
            finally:
                conn.close()
        except Exception as e:
            return web.json_response({"success": False, "error": f"{e!r}"}, status=500)

    try:
        from core.utils.data_collector import record_feedback
        rid = record_feedback(iid, ftype, detail)
    except Exception as e:
        return web.json_response({"success": False, "error": f"{e!r}"}, status=500)
    if rid is None:
        return web.json_response({"success": False, "error": "写入失败（见服务日志）"}, status=500)

    return web.json_response({"success": True, "id": rid,
                              "interaction_id": iid, "type": ftype})
