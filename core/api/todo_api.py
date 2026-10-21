#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""家庭待办 API（2026-10-08 改造：数据源 = macOS「提醒事项」App）

主人裁决（2026-10-08）：
- 原来只做了一张 sqlite 表（todos），与系统「提醒事项」没有关联 → 清除、覆盖
- 现在数据源改为 macOS Reminders：语音走 mac_reminder 工具写进系统提醒事项，
  网页端看板「待办」卡片从同处读 → 语音添加/删除与看板自动同步
- 完成的事项不再显示在待办卡片（系统里已完成的提醒会被过滤掉）

兼容：AppleScript 不可用（无权限/非 macOS）时回落旧 sqlite 表，接口形态不变。
前端仍按 {id, content, done, created_at} 消费（terminal/index.html + led/board-runtime.js）。
"""
import asyncio
import json
import os
import sqlite3
from pathlib import Path

from aiohttp import web

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# 与 mac_control/executor.py 同款环境变量读取助手：
# 中文经 system attribute 取回会被 osascript 按 MacRoman 误解成乱码，
# 必须走 do shell script 读原始字节。
_ENV_HELPER = """
on envv(n)
  try
    return do shell script "printf %s \\\"$" & n & "\\\""
  on error
    return system attribute n
  end try
end envv
"""

# 列出【所有列表】中未完成的提醒：每行 `提醒ID<TAB>标题`
# （实测 2026-10-08：主人的待办分散在 IFTTT/Alexaro do list 等列表，
#   默认列表「提醒」是空的 → 只读默认列表会一条都读不到）
_SCRIPT_REMINDER_LIST = """
on run
  tell application "Reminders"
    set out to ""
    repeat with l in lists
      repeat with r in (every reminder of l whose completed is false)
        set out to out & (id of r) & tab & (name of r) & linefeed
      end repeat
    end repeat
    return out
  end tell
end run
"""

# 按 ID 切换完成状态（提醒 ID 全局唯一，遍历所有列表查找）
_SCRIPT_REMINDER_TOGGLE = """
on run
  set rid to envv("HB_ID")
  tell application "Reminders"
    repeat with l in lists
      repeat with r in (every reminder of l)
        if (id of r) is rid then
          set completed of r to not (completed of r)
          return "ok"
        end if
      end repeat
    end repeat
    return "not found"
  end tell
end run
"""

# 按 ID 删除提醒（whose 过滤后再删：嵌套 repeat 里直接 delete 引用会报 -1728）
_SCRIPT_REMINDER_DELETE = """
on run
  set rid to envv("HB_ID")
  tell application "Reminders"
    repeat with l in lists
      set targets to (every reminder of l whose id is rid)
      if (count of targets) > 0 then
        delete (item 1 of targets)
        return "ok"
      end if
    end repeat
    return "not found"
  end tell
end run
"""

# 新增提醒（标题走环境变量，中文不乱码）
_SCRIPT_REMINDER_ADD = """
on run
  set t to envv("HB_TITLE")
  tell application "Reminders"
    make new reminder with properties {name:t}
  end tell
  return "ok"
end run
"""


async def _osascript(script: str, args: dict = None, timeout: float = 15.0) -> str:
    """执行 AppleScript（参数经 HB_* 环境变量注入，防注入 + 中文不乱码）"""
    env = dict(os.environ)
    for k, v in (args or {}).items():
        env[f"HB_{k.upper()}"] = str(v)
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _ENV_HELPER + script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Reminders AppleScript 超时")
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or "osascript 失败")
    return out.decode("utf-8", "replace").strip()


def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables():
    """仅保留 dashboard_settings（todo_source 显示配置），todos 表已废弃仅作回落"""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO dashboard_settings (setting_key, setting_value)
            VALUES ('todo_source', 'family')
            """
        )
        conn.commit()
    finally:
        conn.close()


async def _list_from_reminders() -> list:
    """从 macOS 提醒事项读未完成项 → 前端格式"""
    out = await _osascript(_SCRIPT_REMINDER_LIST)
    items = []
    if not out or out == "没有未完成的提醒":
        return items
    for i, line in enumerate(out.splitlines()):
        line = line.rstrip("\r")
        if not line or "\t" not in line:
            continue
        rid, _, name = line.partition("\t")
        items.append({
            "id": rid,             # 提醒事项 ID 是字符串，前端只当键用
            "scope": "family",
            "member_id": None,
            "content": name.strip(),
            "done": False,          # 只读未完成，完成的不再显示
            "created_at": None,
        })
        if len(items) >= 50:
            break
    return items


async def list_todos(request):
    """GET /api/todos?scope=family —— 待办列表（数据源 = macOS 提醒事项）"""
    try:
        _ensure_tables()
        try:
            items = await _list_from_reminders()
            return web.json_response({"success": True, "todos": items, "source": "reminders"})
        except Exception as e:
            # 回落旧 sqlite 表（AppleScript 不可用时）
            scope = request.query.get("scope", "family")
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT id, scope, member_id, content, done, created_at "
                    "FROM todos WHERE scope = ? ORDER BY done ASC, id DESC LIMIT 50",
                    (scope,),
                ).fetchall()
            finally:
                conn.close()
            items = [
                {
                    "id": r["id"], "scope": r["scope"], "member_id": r["member_id"],
                    "content": r["content"], "done": bool(r["done"]), "created_at": r["created_at"],
                }
                for r in rows
            ]
            return web.json_response({
                "success": True, "todos": items, "source": "sqlite_fallback",
                "fallback_reason": str(e),
            })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def add_todo(request):
    """POST /api/todos {content, scope?, member_id?} —— 写入 macOS 提醒事项"""
    try:
        body = await request.json()
        content = (body.get("content") or "").strip()
        if not content:
            return web.json_response({"success": False, "message": "content 不能为空"}, status=400)
        try:
            await _osascript(_SCRIPT_REMINDER_ADD, {"title": content})
            return web.json_response({"success": True, "source": "reminders"})
        except Exception as e:
            # 回落旧表
            _ensure_tables()
            conn = _connect()
            try:
                scope = body.get("scope", "family")
                member_id = body.get("member_id")
                cur = conn.execute(
                    "INSERT INTO todos (scope, member_id, content) VALUES (?, ?, ?)",
                    (scope, member_id, content),
                )
                todo_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"success": True, "id": todo_id, "source": "sqlite_fallback"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def toggle_todo(request):
    """PUT /api/todos/{todo_id}/toggle —— 切换提醒事项完成状态"""
    try:
        todo_id = str(request.match_info["todo_id"])
        try:
            out = await _osascript(_SCRIPT_REMINDER_TOGGLE, {"id": todo_id})
            if out == "not found":
                return web.json_response({"success": False, "message": "提醒不存在"}, status=404)
            return web.json_response({"success": True, "source": "reminders"})
        except Exception:
            # 回落旧表
            _ensure_tables()
            conn = _connect()
            try:
                row = conn.execute("SELECT done FROM todos WHERE id = ?", (todo_id,)).fetchone()
                if not row:
                    return web.json_response({"success": False, "message": "待办不存在"}, status=404)
                conn.execute(
                    "UPDATE todos SET done = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (0 if row["done"] else 1, todo_id),
                )
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"success": True, "source": "sqlite_fallback"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def delete_todo(request):
    """DELETE /api/todos/{todo_id} —— 删除提醒事项"""
    try:
        todo_id = str(request.match_info["todo_id"])
        try:
            out = await _osascript(_SCRIPT_REMINDER_DELETE, {"id": todo_id})
            if out == "not found":
                return web.json_response({"success": False, "message": "提醒不存在"}, status=404)
            return web.json_response({"success": True, "source": "reminders"})
        except Exception:
            # 回落旧表
            _ensure_tables()
            conn = _connect()
            try:
                conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"success": True, "source": "sqlite_fallback"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def get_dashboard_settings(request):
    """GET /api/dashboard/settings —— 终端页配置（todo_source: family/member）"""
    try:
        _ensure_tables()
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT setting_value FROM dashboard_settings WHERE setting_key = 'todo_source'"
            ).fetchone()
        finally:
            conn.close()
        return web.json_response({"success": True, "todo_source": row["setting_value"] if row else "family"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def save_dashboard_settings(request):
    """POST /api/dashboard/settings {todo_source}"""
    try:
        body = await request.json()
        todo_source = body.get("todo_source", "family")
        if todo_source not in ("family", "member"):
            return web.json_response({"success": False, "message": "todo_source 仅支持 family/member"}, status=400)
        _ensure_tables()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE dashboard_settings SET setting_value = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE setting_key = 'todo_source'",
                (todo_source,),
            )
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"success": True, "todo_source": todo_source})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)
