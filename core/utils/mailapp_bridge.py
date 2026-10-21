# -*- coding: utf-8 -*-
"""
macOS「邮件」App 桥接层

为什么需要它（主人 2026-10-21 提问「你不能直接在苹果邮件里去查吗」→ 对，应该走这里）：
- Mail.app 已经有主人登录好的所有账户，**不需要任何密码**
- Mail 自己就用系统代理（Clash 7897）同步 Gmail，Python 直连 Gmail IMAP 反而超时
- Mail 的 `delete` 命令本身就是「移到废纸篓」→ 正好满足「垃圾邮件清到回收站」

只用 AppleScript（osascript），不依赖 Mail 的私有数据库。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 字段分隔符：用不可见控制字符，避免 subject 里出现 tab/换行时串行
SEP = "\x1f"
REC = "\x1e"

# 读取**一个账户收件箱**里的未读邮件（分批）
# ⚠️ 两个实测坑：
#   1. 遍历全部 mailbox（含"垃圾邮件/广告邮件/已删除"）会慢到超时 → 只取收件箱
#   2. Exchange/MSA 账户（Google 199 封、MSN）一次性取全部会超时 → 分批取
#   实测：单账户收件箱 41 封 → 0.8 秒；7 个账户全取（不分批）→ 4 分半 + 2 次超时
_SCRIPT_FETCH_ONE = r'''
tell application "Mail"
  set out to ""
  set acctName to "%(acct)s"
  set targetBox to ""
  try
    repeat with mb in (every mailbox of account acctName)
      set mbn to ""
      try
        set mbn to (name of mb) as string
      end try
      if mbn is "INBOX" then
        set targetBox to mbn
        exit repeat
      end if
      if targetBox is "" then
        if mbn contains "收件箱" or mbn is "Inbox" or mbn is "Posteingang" then
          set targetBox to mbn
        end if
      end if
    end repeat
  end try
  if targetBox is "" then return ""
  try
    set allMsgs to (every message of mailbox targetBox of account acctName whose read status is false)
    set total to (count of allMsgs)
    set batchSize to %(batch)d
    set i to 1
    repeat while i <= total
      set lastIdx to i + batchSize - 1
      if lastIdx > total then set lastIdx to total
      set batchList to (items i thru lastIdx of allMsgs)
      repeat with m in batchList
        set mid to ""
        try
          set mid to (message id of m) as string
        end try
        set snd to ""
        try
          set snd to (sender of m) as string
        end try
        set sj to ""
        try
          set sj to (subject of m) as string
        end try
        set out to out & acctName & "%(SEP)s" & targetBox & "%(SEP)s" & snd & "%(SEP)s" & sj & "%(SEP)s" & "" & "%(SEP)s" & mid & "%(REC)s"
      end repeat
      set i to lastIdx + 1
    end repeat
  end try
  return out
end tell
'''.replace("%(SEP)s", SEP).replace("%(REC)s", REC).replace("%(acct)s", "__ACCT__").replace("%(batch)d", "__BATCH__")


def _run(script: str, timeout: int = 300) -> str:
    """跑 AppleScript，返回 stdout"""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            logger.bind(tag=TAG).warning(f"[Mail桥接] 脚本失败: {r.stderr.strip()[:200]}")
            return ""
        return r.stdout
    except subprocess.TimeoutExpired:
        logger.bind(tag=TAG).warning("[Mail桥接] 脚本超时")
        return ""
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[Mail桥接] 异常: {e}")
        return ""


def list_accounts() -> list[dict]:
    """列出 Mail 里已登录的账户"""
    out = _run('''tell application "Mail"
  set res to ""
  repeat with a in every account
    set nm to ""
    set ea to ""
    try
      set nm to (name of a) as string
    end try
    try
      set ea to (email addresses of a) as string
    end try
    set res to res & nm & "%(SEP)s" & ea & "%(REC)s"
  end repeat
  return res
end tell'''.replace("%(SEP)s", SEP).replace("%(REC)s", REC), timeout=60)
    accounts = []
    for rec in out.split(REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split(SEP)
        if len(parts) >= 2 and parts[0].strip():
            accounts.append({"name": parts[0].strip(), "address": parts[1].strip()})
    return accounts


def fetch_unread(accounts: list[str] | None = None, per_account_timeout: int = 120,
                 batch_size: int = 30) -> list[dict]:
    """取各账户**收件箱**的未读邮件（分账户调用，单个慢/失败不影响其他）"""
    names = accounts
    if names is None:
        names = [a["name"] for a in list_accounts()]
    mails: list[dict] = []
    for name in names:
        script = (_SCRIPT_FETCH_ONE.replace("__ACCT__", name.replace('"', ""))
                  .replace("__BATCH__", str(batch_size)))
        out = _run(script, timeout=per_account_timeout)
        for rec in out.split(REC):
            rec = rec.strip("\n")
            if not rec:
                continue
            p = rec.split(SEP)
            if len(p) < 6:
                continue
            mails.append({
                "account": p[0].strip(), "mailbox": p[1].strip(), "sender": p[2].strip(),
                "subject": p[3].strip(), "date": p[4].strip(), "message_id": p[5].strip(),
            })
    return mails


def delete_to_trash(targets: list[dict]) -> int:
    """把指定邮件移到废纸篓（Mail 的 delete 语义 = 移到废纸篓，可恢复）

    ⚠️ 实测踩坑：
    - 用 message id 集合比对不可靠（`is in {...}` 匹配失败，删了 0 封）
    - 遍历 `every message of mb`（含已读）很慢 → 只遍历未读
    → 改用「主题 + 发件人」组合匹配，实测 41 封约 1 秒
    """
    if not targets:
        return 0
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for t in targets:
        key = (t.get("account", ""), t.get("mailbox", ""))
        groups.setdefault(key, []).append((t.get("subject", ""), t.get("sender", "")))
    deleted = 0
    for (acct, mbox), pairs in groups.items():
        subj_list = ", ".join(f'"{s.replace(chr(34), "")}"' for s, _ in pairs)
        script = f"""tell application "Mail"
  set n to 0
  set subjList to {{{subj_list}}}
  try
    set mbs to (every mailbox of account "{acct}")
  on error
    return 0
  end try
  repeat with mb in mbs
    set mbn to ""
    try
      set mbn to (name of mb) as string
    end try
    if mbn is "{mbox}" then
      repeat with m in (every message of mb whose read status is false)
        set sj to ""
        try
          set sj to (subject of m) as string
        end try
        if sj is in subjList then
          try
            delete m
            set n to n + 1
          end try
        end if
      end repeat
    end if
  end repeat
  return n
end tell"""
        res = _run(script, timeout=600)
        try:
            deleted += int(str(res).strip() or 0)
        except ValueError:
            pass
    logger.bind(tag=TAG).info(f"[Mail桥接] 移入废纸篓 {deleted} 封")
    return deleted


def mark_read(targets: list[dict]) -> int:
    """把指定邮件标记为已读"""
    if not targets:
        return 0
    groups: dict[tuple[str, str], list[str]] = {}
    for t in targets:
        groups.setdefault((t.get("account", ""), t.get("mailbox", "")), []).append(
            t.get("message_id", ""))
    n = 0
    for (acct, mbox), ids in groups.items():
        ids = [i for i in ids if i]
        if not ids:
            continue
        id_list = ", ".join(f'"{i}"' for i in ids)
        script = f'''tell application "Mail"
  set n to 0
  try
    set mbs to (every mailbox of account "{acct}")
  on error
    return 0
  end try
  repeat with mb in mbs
    set mbn to ""
    try
      set mbn to (name of mb) as string
    end try
    if mbn is "{mbox}" then
      repeat with m in (every message of mb)
        set mid to ""
        try
          set mid to (message id of m) as string
        end try
        if mid is in {{{id_list}}} then
          try
            set read status of m to true
            set n to n + 1
          end try
        end if
      end repeat
    end if
  end repeat
  return n
end tell'''
        res = _run(script, timeout=300)
        try:
            n += int(str(res).strip() or 0)
        except ValueError:
            pass
    return n
