# -*- coding: utf-8 -*-
"""控制台「本地状态」的服务端存储（2026-10-14）

要解决的问题（主人 2026-10-14）：
    管理台有几样数据存在浏览器 localStorage 里。localStorage 是**按"来源"隔离**的，
    而同一个后台页面现在有两个来源（http://<IP>:8003 与 https://<IP>:8443，见
    docs/plans/2026-10-14_声纹录入与HTTPS必要性方案.md），于是：
    从 A 地址进去存的，换 B 地址进去看不到 —— 主人原话「你从不同的地址进去，有存新数据库的，
    有存老数据库的……这不就乱套了吗？」「都改成服务器来存，让两个地址一模一样」。

归到这个表里的都是**页面级状态**（不是核心业务数据，核心数据本来就在各自表里）：
    - health_reports:          已生成健康报告清单
    - homebrain_voice_card_edits / homebrain_voice_card_deleted: 音色卡片的改名/隐藏
    - cp_task_templates:       呼叫中心的常用话术模板
    -(cp_emergency_contacts:   不用这张表，它属于"电话功能"配置，见 callpilot_api)

设计取舍：
    - 一张 key-value 表就够，不为每种状态单开表/列（这些数据都是页面自己的 JSON 片段）。
    - 只做**覆盖写**（POST 整体替换）+ 全量读，不做增量，避免并发合并逻辑。
    - 单值上限 256KB：这些是 UI 状态，不该塞大对象（真有大对象说明该建正经表）。
    - 外层还包一层 JSON：value 列永远是字符串，读出来自动解析；解析不了就当字符串用。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"

# 单值上限（页面级状态，不该更大）
MAX_VALUE_BYTES = 256 * 1024

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS console_ui_state (
    state_key  TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
)
"""


def is_valid_key(key) -> bool:
    return bool(isinstance(key, str) and _KEY_RE.match(key))


def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(_CREATE_SQL)      # 首次使用自动建表（项目里其他小表也是这个惯例）
    return conn


def load_all() -> dict:
    """读出全部页面状态（值已尝试 JSON 解析）；任何异常都返回空 dict，不影响页面。"""
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT state_key, value FROM console_ui_state").fetchall()
        finally:
            conn.close()
        out = {}
        for key, raw in rows:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                out[key] = raw
        return out
    except Exception:
        return {}


def save(key: str, value) -> tuple[bool, str]:
    """整体覆盖写入；返回 (成功, 说明)"""
    if not is_valid_key(key):
        return False, "非法的状态名（只允许字母数字下划线中划线，≤64 字符）"
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return False, f"值无法序列化: {e}"
    if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
        return False, f"值太大（上限 {MAX_VALUE_BYTES // 1024}KB）"
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO console_ui_state (state_key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(state_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, payload, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return True, "已保存"
    except Exception as e:
        return False, f"写入失败: {e}"


def delete(key: str) -> tuple[bool, str]:
    if not is_valid_key(key):
        return False, "非法的状态名"
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM console_ui_state WHERE state_key=?", (key,))
            conn.commit()
        finally:
            conn.close()
        return True, "已删除"
    except Exception as e:
        return False, f"删除失败: {e}"
