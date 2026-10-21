# -*- coding: UTF-8 -*-
"""记忆 scope 解析（2026-10-13 WP-1）

背景（实测确认）：`Mem0Provider` 是 websocket_server 的**单例**，所有连接共用；
原来 `init_memory(role_id=device_id)` 会覆盖全局 `self.user_id`，真跑复现：
    init_memory("web-terminal-56ops5ll") → user_id = web-terminal-56ops5ll
    init_memory("admin-console")         → user_id = admin-console
→ 管理台 WS 一连，所有终端的查询与抽取都换成 admin-console。
库里 user_id 因此裂成 5 种（admin-console 27 条、还有 1 条是会话 UUID）。

本模块把「记忆归属」从 provider 的可变状态里拿出来：
- scope 只有两种（主人 2026-10-13 裁决 D1：单户模型，不设 household_id）
    ``family``        家庭共享事实（全家共用的品牌、习惯、家庭日程…）
    ``m:<member_id>`` 声纹识别出的成员的个人记忆
- device_id / session_id **只作 metadata**，不作身份
- 声纹未识别（D2 裁决 a）：只写 ``family``，且敏感类内容**直接不写**

任何失败都返回 ``family`` 或 False，绝不抛异常影响对话主链。
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from config.logger import setup_logging

logger = setup_logging()
TAG = __name__

# 数据库路径与 member_profile / family_api 同构
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"

FAMILY_SCOPE = "family"
MEMBER_SCOPE_PREFIX = "m:"

# 成员表缓存（成员增删极少，5 分钟足够）
_CACHE_TTL = 300.0
_member_cache: Tuple[float, Dict[str, int]] = (0.0, {})

# 敏感类关键词：健康 / 财务 / 儿童 / 个人行程。
# 未识别说话人时命中即不写（D2 裁决），避免把陌生人或旁人的隐私写进家庭共享记忆。
_SENSITIVE_PATTERN = re.compile(
    r"药|吃药|服药|病|就诊|医院|血压|血糖|体温|体检|复查|过敏|忌口|"
    r"工资|收入|存款|贷款|信用卡|密码|账户|账号余额|"
    # ⚠️ 儿童相关只挡「健康/学校/看护」这类真隐私，**不挡「生日/纪念日」**：
    # 家人生日本来就该记成家庭共享事实（我第一版把「生日」列进敏感词，
    # 结果「我女儿的生日在五月」被整条丢掉，单测当场抓到）
    r"(儿子|女儿|孩子|小孩).{0,6}(病|药|医院|就诊|学校|老师|成绩|作业)|上学|放学|"
    r"行程|航班|机票|酒店|出差|请假"
)


def _db_path() -> Path:
    return _DB_DIR / "devices.db"


def _load_members() -> Dict[str, int]:
    """读 family_members，返回 {name/nickname(小写) -> id}。失败返回空表。"""
    global _member_cache
    now = time.time()
    ts, cached = _member_cache
    if cached and now - ts < _CACHE_TTL:
        return cached
    table: Dict[str, int] = {}
    conn = None
    try:
        conn = sqlite3.connect(str(_db_path()), timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT id, name, nickname FROM family_members")
        for mid, name, nickname in cur.fetchall():
            if name:
                table[str(name).strip().lower()] = int(mid)
            if nickname:
                table[str(nickname).strip().lower()] = int(mid)
    except Exception as e:  # 表不存在 / 库锁住都不能影响对话
        logger.bind(tag=TAG).warning(f"读取 family_members 失败，本轮按家庭共享处理: {e}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _member_cache = (now, table)
    return table


def member_id_of(speaker: Optional[str]) -> Optional[int]:
    """说话人名（声纹给的 name 或 nickname）→ family_members.id。查不到返回 None。"""
    if not speaker:
        return None
    table = _load_members()
    if not table:
        return None
    return table.get(str(speaker).strip().lower())


def resolve_scope(speaker: Optional[str]) -> str:
    """说话人 → 记忆 scope。

    - 声纹识别出成员且在成员表里 → ``m:<id>``（个人记忆）
    - 未识别 / 不在成员表 → ``family``（D2 裁决 a：只写家庭共享）
    """
    mid = member_id_of(speaker)
    if mid is not None:
        return f"{MEMBER_SCOPE_PREFIX}{mid}"
    return FAMILY_SCOPE


def is_member_scope(scope: Optional[str]) -> bool:
    return bool(scope) and str(scope).startswith(MEMBER_SCOPE_PREFIX)


def is_family_scope(scope: Optional[str]) -> bool:
    return str(scope or "") == FAMILY_SCOPE


def is_sensitive_text(text: Optional[str]) -> bool:
    """是否属于敏感类（健康/财务/儿童/个人行程）。

    只用于「未识别说话人时不写」这一道前置门（D2 裁决 a），
    不做成员私有记忆的可见性控制 —— 可见性由 scope 本身保证。
    """
    if not text:
        return False
    return bool(_SENSITIVE_PATTERN.search(str(text)))


def scopes_for_query(speaker: Optional[str]) -> Tuple[str, ...]:
    """查询时要检索哪些 scope。

    识别出成员 → 个人 + 家庭共享（个人优先，家庭兜底）；
    未识别 → 只查家庭共享（不越权读任何人的个人记忆）。
    """
    scope = resolve_scope(speaker)
    if is_member_scope(scope):
        return (scope, FAMILY_SCOPE)
    return (FAMILY_SCOPE,)


def describe_scope(scope: Optional[str]) -> str:
    """给日志/管理台看的可读描述。"""
    s = str(scope or "")
    if s == FAMILY_SCOPE:
        return "家庭共享"
    if s.startswith(MEMBER_SCOPE_PREFIX):
        mid = s[len(MEMBER_SCOPE_PREFIX):]
        conn = None
        try:
            conn = sqlite3.connect(str(_db_path()), timeout=3)
            cur = conn.cursor()
            cur.execute("SELECT name FROM family_members WHERE id = ?", (mid,))
            row = cur.fetchone()
            if row and row[0]:
                return f"成员·{row[0]}"
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return f"成员#{mid}"
    return s or "未指定"
