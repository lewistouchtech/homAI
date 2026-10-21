# -*- coding: utf-8 -*-
"""
邮箱管家：多邮箱自动巡检 + 智能分类 + 垃圾清理 + 重要邮件提醒

设计要点（对应主人需求）：
1. **绑定多个邮箱**：每个邮箱一套 IMAP 配置，主人自己的邮箱都挂进来。
2. **自动巡检**：后台常驻轮询（不依赖网页是否打开——服务端 launchd 常驻，网页只是看报告的界面）。
3. **智能分类**：
   - 重要/工作 → 移到「重要」文件夹 + 立刻提醒
   - 垃圾/广告/杂志 → 移到「已删除邮件」（可恢复，不做真删）
   - 其余 → 留在收件箱
4. **会学习**：主人手动把某封邮件移走/删除 → 自动记住该发件人 → 以后同发件人的邮件自动照做。
   这是「拆分、筛选」的关键——主人不用配规则，动一次就学会了。
5. **提醒**：macOS 本地通知（主人在这台电脑前就能收到）+ 可选的微信/短信渠道。

只用标准库（imaplib / email / smtplib），不引入新依赖。

配置：
- data/.email_config.json      邮箱账户与规则（不含密码）
- data/.email_accounts.json    密码（600 权限，gitignored）
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "data", ".email_config.json")
ACCOUNTS_PATH = os.path.join(BASE_DIR, "data", ".email_accounts.json")
RULES_PATH = os.path.join(BASE_DIR, "data", ".email_rules.json")

# 分类结果
BUCKET_INBOX = "inbox"        # 留在收件箱
BUCKET_IMPORTANT = "important"  # 重要/工作 → 移入「重要」文件夹
BUCKET_TRASH = "trash"        # 垃圾/广告/杂志 → 移入「已删除邮件」

# 发件人本地部分里的垃圾特征
_JUNK_LOCAL_PARTS = (
    "no-reply", "noreply", "no_reply", "do-not-reply", "donotreply",
    "promotions", "promotion", "newsletter", "marketing", "deals",
    "unsubscribe", "mailer", "bounce", "bulk", "sales", "ad-", "ads",
    "notification-billing", "ecomm", "edm",
)
# 主题里的垃圾特征
_JUNK_SUBJECT_HINTS = (
    "退订", "广告", "推广", "限时", "优惠", "特惠", "促销", "秒杀", "折扣",
    "红包", "抽奖", "活动邀约", "清仓", "新品上市", "领取", "优惠券",
    "限时特惠", "会员日", "双十一", "618", "双12",
)
# ⚠️ 系统通知保护：这些邮件发件人常是 no-reply，但内容重要，绝不能当垃圾删
#    （实测踩坑：阿里邮箱「欢迎使用 xxx 邮箱系统」的欢迎信来自 no-reply@mailsupport.aliyun.com，
#      被「批量发送」规则误判成垃圾）
_SYSTEM_NOTIFY_SUBJECTS = (
    "欢迎使用", "邮箱系统", "验证码", "身份验证", "安全验证", "登录通知",
    "异常登录", "异地登录", "账户变更", "账号变更", "开通", "激活", "实名认证",
    "到期", "续费", "发票", "账单", "付款", "订单", "合同", "报价", "对账",
    "会议", "面试", "入职", "离职", "年检", "报税", "报销", "中标", "采购",
    "验收", "交付", "方案", "协议", "公告", "回执", "整改", "报销单",
)
# 阿里邮箱系统信的专属发件域（阿里云自己的商业推广不在这类域里）
_SYSTEM_SENDER_DOMAINS = (
    "mailsupport.aliyun.com", "service.aliyun.com", "notify.aliyun.com",
    # 实测误判：Apple/iCloud 的系统通知（"Bot Eva 已加入你的家庭"、"加入日历"）也来自 noreply，
    # 不能当垃圾删
    "email.apple.com", "apple.com", "icloud.com",
)

# 主题里的工作/重要特征
_WORK_SUBJECT_HINTS = (
    "合同", "报价", "订单", "发票", "对账", "项目", "会议", "面试", "入职",
    "续费", "账单", "年检", "报税", "发票", "回执", "中标", "采购", "报销",
    "整改", "通知", "公告", "验收", "交付", "方案", "协议", "账户变更",
    "安全提醒", "异地登录", "异常登录", "安全中心",
)


def decode_utf7(raw: str) -> str:
    """IMAP modified UTF-7 文件夹名解码

    阿里企业邮箱的文件夹名是编码的（如 `&XfJSIJZkkK5O9g-` = 已删除邮件），
    不解码就匹配不上「已删除邮件 / 垃圾邮件」这些名字。
    """
    import base64 as _b64
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        j = raw.find("-", i)
        if j == -1:
            out.append(ch)
            i += 1
            continue
        chunk = raw[i + 1:j]
        if chunk == "":
            out.append("&")
        else:
            try:
                b = _b64.b64decode(chunk.replace(",", "/") + "=" * (-len(chunk) % 4))
                out.append(b.decode("utf-16-be"))
            except Exception:
                out.append(raw[i:j + 1])
        i = j + 1
    return "".join(out)


def _decode_header(raw: str | None) -> str:
    """解码 MIME 头（可能是 Base64 / QP 编码的中文）"""
    if not raw:
        return ""
    parts = []
    for chunk, enc in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _body_text(msg) -> str:
    """取纯文本正文（截断，避免大邮件拖慢）"""
    chunks: list[str] = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "text" and part.get_content_subtype() == "plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    except Exception:
        pass
    return "\n".join(chunks)[:4000]


@dataclass
class Mailbox:
    """一个被管理的邮箱"""
    address: str
    imap_host: str = "imap.qiye.aliyun.com"
    imap_port: int = 993
    smtp_host: str = "smtp.qiye.aliyun.com"
    smtp_port: int = 465
    enabled: bool = True
    # 分类策略
    mark_important_as: str = ""     # 重要邮件移到哪个文件夹（空=用自动探测的「重要」）
    trash_as: str = ""               # 垃圾移到哪个文件夹（空=用自动探测的「已删除邮件」）
    notify: bool = True
    password: str = ""               # 不落配置文件，从 .email_accounts.json 取

    @property
    def key(self) -> str:
        return self.address.lower()


@dataclass
class Rule:
    """学习到的规则：某个发件人（精确 / 域名 / 关键词）→ 某个去向"""
    pattern: str
    bucket: str = BUCKET_INBOX
    target_folder: str = ""       # 具体文件夹（拆分用）
    hits: int = 0
    learned_at: str = ""
    source: str = "auto"          # auto=学来的, manual=手填

    def matches(self, sender: str, subject: str) -> bool:
        p = self.pattern.strip().lower()
        if not p:
            return False
        s = (sender or "").lower()
        subj = (subject or "").lower()
        if p.startswith("@"):                      # 域名规则
            domain = p[1:]
            return domain in s
        if p in s:                                  # 完整地址或本地部分
            return True
        return p in subj                             # 关键词


@dataclass
class MailItem:
    """一封被检出的邮件（摘要）"""
    uid: str
    message_id: str
    sender: str
    subject: str
    date: str
    bucket: str = BUCKET_INBOX
    reason: str = ""
    folder: str = "INBOX"

    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "message_id": self.message_id, "sender": self.sender,
            "subject": self.subject, "date": self.date, "bucket": self.bucket,
            "reason": self.reason, "folder": self.folder,
        }


class MailAppBackend:
    """走 macOS「邮件」App 的后端（主人 2026-10-21 提问「你不能直接在苹果邮件里去查吗」）

    优点：
    - **不需要任何密码**（Mail 里已经登录好了）
    - Mail 自己用系统代理（Clash）同步，Python 直连 Gmail 反而超时
    - Mail 的 `delete` 本身就是「移到废纸篓」→ 正是主人要的效果
    """

    name = "mailapp"

    def __init__(self, manager: "EmailManager"):
        self.m = manager

    def accounts(self) -> list[dict]:
        from core.utils import mailapp_bridge
        return mailapp_bridge.list_accounts()

    def fetch(self) -> list[dict]:
        from core.utils import mailapp_bridge
        return mailapp_bridge.fetch_unread()

    def trash(self, items: list[dict]) -> int:
        from core.utils import mailapp_bridge
        return mailapp_bridge.delete_to_trash(items)

    def mark_read(self, items: list[dict]) -> int:
        from core.utils import mailapp_bridge
        return mailapp_bridge.mark_read(items)


class EmailManager:
    """邮箱管家"""

    def __init__(self):
        self.config = self._load_json(CONFIG_PATH, {"mailboxes": [], "rules": [], "settings": {}})
        self.accounts = self._load_json(ACCOUNTS_PATH, {})   # address -> password
        self.rules: list[Rule] = [Rule(**r) for r in self.config.get("rules", [])]
        self.settings: dict[str, Any] = self.config.get("settings", {}) or {}
        self.poll_interval = int(self.settings.get("poll_interval_sec", 300))
        self._folder_cache: dict[str, dict[str, str]] = {}   # address -> {role: folder}
        self._learned_message_ids: set[str] = set()
        # 已提醒过的邮件（避免历史积压一次性轰炸主人）
        self._notified_path = os.path.join(BASE_DIR, "data", ".email_notified.json")
        self._notified: set[str] = set(self._load_json(self._notified_path, []) or [])
        self.max_notify_per_round = int(self.settings.get("max_notify_per_round", 5))

    @property
    def dry_run(self) -> bool:
        """动态读配置（实测踩坑：做成 init 快照 → 改了 settings 也不生效）"""
        return bool(self.settings.get("dry_run", False))

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self.settings["dry_run"] = bool(value)

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def _load_json(path: str, default: Any) -> Any:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        if path == ACCOUNTS_PATH:
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass

    def save(self) -> None:
        self.config["rules"] = [r.__dict__ for r in self.rules]
        self.config["settings"] = self.settings
        self._save_json(CONFIG_PATH, self.config)

    def save_accounts(self) -> None:
        self._save_json(ACCOUNTS_PATH, self.accounts)

    # ------------------------------------------------------------- 账户管理
    def mailboxes(self) -> list[Mailbox]:
        out = []
        for m in self.config.get("mailboxes", []):
            mb = Mailbox(**{k: v for k, v in m.items() if k in Mailbox.__dataclass_fields__})
            mb.password = self.accounts.get(mb.key, "")
            out.append(mb)
        return out

    def upsert_mailbox(self, address: str, password: str = "", **kw) -> Mailbox:
        address = address.strip().lower()
        boxes = self.config.setdefault("mailboxes", [])
        found = None
        for m in boxes:
            if m.get("address", "").lower() == address:
                found = m
                break
        if found is None:
            found = {"address": address}
            boxes.append(found)
        found.update({k: v for k, v in kw.items() if k in Mailbox.__dataclass_fields__})
        if password:
            self.accounts[address] = password
            self.save_accounts()
        self.save()
        return Mailbox(**{k: v for k, v in found.items() if k in Mailbox.__dataclass_fields__})

    def remove_mailbox(self, address: str) -> bool:
        address = address.lower()
        before = len(self.config.get("mailboxes", []))
        self.config["mailboxes"] = [m for m in self.config.get("mailboxes", [])
                                    if m.get("address", "").lower() != address]
        self.accounts.pop(address, None)
        self.save_accounts()
        self.save()
        return len(self.config.get("mailboxes", [])) < before

    # --------------------------------------------------------------- 规则
    def add_rule(self, pattern: str, bucket: str, target_folder: str = "",
                 source: str = "manual") -> Rule:
        pattern = pattern.strip()
        for r in self.rules:
            if r.pattern.lower() == pattern.lower():
                r.bucket = bucket
                r.target_folder = target_folder
                r.source = source
                self.save()
                return r
        rule = Rule(pattern=pattern, bucket=bucket, target_folder=target_folder,
                    learned_at=datetime.now().isoformat(timespec="seconds"), source=source)
        self.rules.append(rule)
        self.save()
        return rule

    def rules_list(self) -> list[dict]:
        return [r.__dict__ for r in self.rules]

    def remove_rule(self, pattern: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.pattern.lower() != pattern.strip().lower()]
        self.save()
        return len(self.rules) < before

    # ------------------------------------------------------------- 分类
    def classify(self, sender: str, subject: str, headers: dict | None = None,
                 body: str = "") -> tuple[str, str]:
        """返回 (bucket, reason)。规则按优先级：白名单 > 学习规则 > 特征判断"""
        headers = headers or {}
        subject_l = (subject or "").lower()
        sender_l = (sender or "").lower()
        local = sender_l.split("<")[-1].split("@")[0].strip()

        # 1) 手动白名单（配置里的 important 名单）
        for pat in self.settings.get("important_senders", []) or []:
            p = str(pat).lower().strip()
            if p and (p in sender_l):
                return BUCKET_IMPORTANT, f"白名单 {pat}"

        # 2) 学习到的规则（主人手动动作学来的）
        for r in self.rules:
            if r.matches(sender, subject):
                if r.bucket == BUCKET_TRASH:
                    return BUCKET_TRASH, f"学到的规则：{r.pattern}"
                if r.bucket == BUCKET_IMPORTANT:
                    return BUCKET_IMPORTANT, f"学到的规则：{r.pattern}"
                if r.target_folder:
                    return BUCKET_INBOX, f"归到 {r.target_folder}（学到的规则：{r.pattern}）"

        # 3) 特征判断
        # 3.0 系统通知保护（优先级最高）：no-reply 发来的欢迎信/验证码/安全提醒等不是垃圾
        for hint in _SYSTEM_NOTIFY_SUBJECTS:
            if hint in (subject or ""):
                return BUCKET_IMPORTANT, f"系统通知类邮件（{hint}）"
        for dom in _SYSTEM_SENDER_DOMAINS:
            if dom in sender_l:
                return BUCKET_IMPORTANT, f"邮箱/云平台系统通知（{dom}）"

        has_unsub = any(k.lower() in ("list-unsubscribe", "list-unsubscribe-post") for k in headers)
        if has_unsub and any(t in local for t in _JUNK_LOCAL_PARTS):
            return BUCKET_TRASH, "带退订头的批量营销邮件"
        if any(t in local for t in _JUNK_LOCAL_PARTS):
            return BUCKET_TRASH, f"发件人像批量发送（{local}）"
        for hint in _JUNK_SUBJECT_HINTS:
            if hint in (subject or ""):
                return BUCKET_TRASH, f"主题像广告：{hint}"
        for hint in _WORK_SUBJECT_HINTS:
            if hint in (subject or ""):
                return BUCKET_IMPORTANT, f"主题像正事：{hint}"
        for pat in self.settings.get("important_domains", []) or []:
            p = str(pat).lower().strip().lstrip("@")
            if p and p in sender_l:
                return BUCKET_IMPORTANT, f"重要域名 {pat}"
        if any(t in (body or "").lower() for t in ("退订", "unsubscribe", "promotion")) and \
                any(t in local for t in _JUNK_LOCAL_PARTS + ("noreply",)):
            return BUCKET_TRASH, "正文含退订"

        return BUCKET_INBOX, "普通邮件"

    # ---------------------------------------------------------- IMAP 工具
    @staticmethod
    def _imap(mb: Mailbox) -> imaplib.IMAP4_SSL:
        ctx = ssl.create_default_context()
        return imaplib.IMAP4_SSL(mb.imap_host, mb.imap_port, ssl_context=ctx, timeout=30)

    def folders(self, mb: Mailbox) -> dict[str, str]:
        """探测邮箱文件夹：{role: IMAP 原始文件夹名}；role ∈ inbox/trash/junk/sent/important

        ⚠️ 两个坑（阿里企业邮箱实测）：
        1. 文件夹名是 **UTF-7 编码**（`&XfJSIJZkkK5O9g-` = 已删除邮件），必须解码后再按名字匹配
        2. 特殊用途标志在 LIST 响应的 flags 里（`(\Trash) "/" "name"`），不在文件夹名里
        IMAP 操作必须用**原始编码名**，所以这里保留原名，只用解码名做判断。
        """
        if mb.key in self._folder_cache:
            return self._folder_cache[mb.key]
        roles: dict[str, str] = {"inbox": "INBOX", "trash": "", "junk": "",
                                 "sent": "", "important": ""}
        try:
            M = self._imap(mb)
            M.login(mb.address, mb.password)
            typ, boxes = M.list()
            for raw in boxes or []:
                line = raw.decode(errors="ignore")
                flags = ""
                if line.startswith("("):
                    flags = line[1:line.find(")")].lower()
                name = line.split(' "/" ')[-1].strip('"')
                if name.startswith('"') and name.endswith('"'):
                    name = name[1:-1]
                decoded = decode_utf7(name)
                if "\\trash" in flags or "已删除" in decoded or decoded.lower() in ("trash", "deleted"):
                    roles["trash"] = name
                elif "\\junk" in flags or "\\spam" in flags or "垃圾" in decoded:
                    roles["junk"] = name
                elif "\\sent" in flags or "已发送" in decoded or decoded.lower() == "sent":
                    roles["sent"] = name
                elif "\\drafts" in flags:
                    roles["drafts"] = name
                elif decoded in ("重要", "重要邮件", "starred", "flagged"):
                    roles["important"] = name
            M.logout()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[邮箱] 探测文件夹失败 {mb.address}: {e}")
        self._folder_cache[mb.key] = roles
        logger.bind(tag=TAG).info(f"[邮箱] {mb.address} 文件夹识别: {roles}")
        return roles

    # ------------------------------------------------------------ 巡检
    def _fetch(self, M, folder: str, unread_only: bool = True) -> list[tuple[str, Any]]:
        typ, _ = M.select(folder)
        if typ != "OK":
            return []
        crit = "UNSEEN" if unread_only else "ALL"
        typ, data = M.search(None, crit)
        if typ != "OK":
            return []
        out = []
        for uid in (data[0].split() if data and data[0] else []):
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = next((x[1] for x in msg_data if isinstance(x, tuple)), None)
            if raw:
                out.append((uid.decode(), email.message_from_bytes(raw)))
        return out

    def _move(self, M, uid: str, target: str) -> bool:
        if not target:
            return False
        try:
            typ, _ = M.copy(uid, target)
            if typ != "OK":
                return False
            M.store(uid, "+FLAGS", "\\Deleted")
            M.expunge()
            return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[邮箱] 移动失败 {uid}→{target}: {e}")
            return False

    def scan_once(self, mb: Mailbox) -> dict[str, Any]:
        """巡检一个邮箱：分类 + 移动 + 记录重要邮件"""
        report = {"address": mb.address, "checked": 0, "important": [], "trashed": 0,
                  "moved_folder": 0, "error": None}
        try:
            M = self._imap(mb)
            M.login(mb.address, mb.password)
            roles = self.folders(mb)
            trash_folder = mb.trash_as or roles.get("trash") or ""
            important_folder = mb.mark_important_as or roles.get("important") or ""

            for uid, msg in self._fetch(M, "INBOX"):
                report["checked"] += 1
                sender = _decode_header(msg.get("From"))
                subject = _decode_header(msg.get("Subject"))
                headers = {k: v for k, v in msg.items()}
                body = _body_text(msg)
                bucket, reason = self.classify(sender, subject, headers, body)
                report["bucket"] = bucket

                if bucket == BUCKET_TRASH:
                    if self.dry_run:
                        report["trashed"] += 1
                    elif self._move(M, uid, trash_folder):
                        report["trashed"] += 1
                elif bucket == BUCKET_IMPORTANT:
                    item = MailItem(uid=uid, message_id=str(msg.get("Message-ID", "")),
                                    sender=sender, subject=subject,
                                    date=str(msg.get("Date", "")), bucket=bucket,
                                    reason=reason, folder="INBOX")
                    report["important"].append(item.to_dict())
                    if not self.dry_run and important_folder:
                        if self._move(M, uid, important_folder):
                            item.folder = important_folder
                else:
                    # 拆到别的文件夹（学到的规则指定的）
                    rule = next((r for r in self.rules
                                 if r.bucket == BUCKET_INBOX and r.target_folder
                                 and r.matches(sender, subject)), None)
                    if rule and not self.dry_run and self._move(M, uid, rule.target_folder):
                        report["moved_folder"] += 1

            M.logout()
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            logger.bind(tag=TAG).warning(f"[邮箱] 巡检失败 {mb.address}: {e}")
        return report


    # ---------------------------------------------------- Mail.app 后端
    def scan_mailapp(self, notify_channel: str = "mac", trash: bool | None = None) -> dict:
        """走 macOS「邮件」App 巡检全部已登录账户（不需要密码）

        垃圾 → Mail 的 delete（= 移到废纸篓，可恢复）
        重要 → 通知
        """
        from core.utils.mailapp_bridge import fetch_unread, delete_to_trash
        do_trash = (not self.dry_run) if trash is None else trash
        report = {"checked": 0, "by_account": {}, "important": [], "trash": [],
                  "trashed": 0, "error": None, "backend": "mailapp"}
        try:
            mails = fetch_unread()
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            return report
        for m in mails:
            report["checked"] += 1
            acct = m["account"]
            stat = report["by_account"].setdefault(acct, {"checked": 0, "trash": 0, "important": 0})
            stat["checked"] += 1
            bucket, reason = self.classify(m["sender"], m["subject"], {}, "")
            if bucket == BUCKET_TRASH:
                stat["trash"] += 1
                report["trash"].append({**m, "reason": reason})
            elif bucket == BUCKET_IMPORTANT:
                stat["important"] += 1
                report["important"].append({**m, "reason": reason})
        if do_trash and report["trash"]:
            try:
                report["trashed"] = delete_to_trash(report["trash"])
            except Exception as e:
                report["error"] = f"清理失败: {e}"
        if report["important"]:
            self.notify(report["important"], notify_channel)
        return report

    def learn_from_manual_moves(self, mb: Mailbox) -> int:
        """学习：主人手动把邮件移到了别的文件夹 → 记成规则"""
        learned = 0
        try:
            M = self._imap(mb)
            M.login(mb.address, mb.password)
            roles = self.folders(mb)
            # INBOX 之外的所有文件夹都看一眼（主人手动移过去的）
            for role, folder in roles.items():
                if role in ("inbox", "trash") or not folder:
                    continue
                for uid, msg in self._fetch(M, folder, unread_only=False):
                    sender = _decode_header(msg.get("From"))
                    if not sender:
                        continue
                    bucket = BUCKET_TRASH if role == "trash" else (
                        BUCKET_IMPORTANT if role == "important" else BUCKET_INBOX)
                    target = "" if bucket != BUCKET_INBOX else folder
                    exists = any(r.pattern.lower() == sender.lower() for r in self.rules)
                    if not exists:
                        self.add_rule(sender, bucket, target_folder=target, source="auto")
                        learned += 1
                        logger.bind(tag=TAG).info(f"[邮箱] 学到规则 {sender} → {bucket or folder}")
            M.logout()
        except Exception as e:
            logger.bind(tag=TAG).debug(f"[邮箱] 学习失败 {mb.address}: {e}")
        return learned

    # ------------------------------------------------------------ 提醒
    def notify(self, items: list[dict], channel: str = "mac") -> bool:
        if not items:
            return False
        # 只提醒「没提醒过的」+ 每轮限流（实测一次可能有几百封重要邮件，不能全弹）
        fresh = [i for i in items if i.get("message_id") and i["message_id"] not in self._notified]
        for i in fresh:
            self._notified.add(i["message_id"])
        self._notified = set(list(self._notified)[-5000:])
        self._save_json(self._notified_path, sorted(self._notified))
        items = fresh[: self.max_notify_per_round]
        if not items:
            return False
        title = f"重要邮件 {len(items)} 封"
        body = "\n".join(f"· {i['subject'][:40]} — {i['sender'][:30]}" for i in items[:5])
        if channel == "mac":
            return self._notify_mac(title, body)
        try:
            from core.utils.notify_service import send as notify_send
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                ok, msg = loop.run_until_complete(notify_send(channel, body, title=title))
            finally:
                loop.close()
            return bool(ok)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[邮箱] 通知失败 {channel}: {e}")
            return False

    @staticmethod
    def _notify_mac(title: str, body: str) -> bool:
        """macOS 本地通知（主人不开网页也能收到）"""
        try:
            safe_t = title.replace('"', "'")
            safe_b = body.replace('"', "'")
            script = f'display notification "{safe_b}" with title "{safe_t}" sound name "Ping"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    # -------------------------------------------------------------- 循环
    def run_cycle(self, notify_channel: str = "mac") -> list[dict]:
        reports = []
        for mb in self.mailboxes():
            if not mb.enabled or not mb.password:
                continue
            rep = self.scan_once(mb)
            if rep.get("important") and mb.notify:
                self.notify(rep["important"], notify_channel)
            reports.append(rep)
        return reports

    def run_forever(self, notify_channel: str = "mac") -> None:
        logger.bind(tag=TAG).info("[邮箱] 巡检循环启动")
        while True:
            try:
                self.run_cycle(notify_channel)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[邮箱] 巡检循环异常: {e}")
            time.sleep(self.poll_interval)

    # ------------------------------------------------------------ 发信
    def send_mail(self, sender: str, to: str, subject: str, body: str,
                  password: str = "", smtp_host: str = "", smtp_port: int = 0) -> tuple[bool, str]:
        """用域名邮箱发信（官网表单后端会用到）"""
        import smtplib
        from email.message import EmailMessage
        smtp_host = smtp_host or "smtp.qiye.aliyun.com"
        smtp_port = smtp_port or 465
        password = password or self.accounts.get(sender.lower(), "")
        if not password:
            return False, "缺少发信账号密码"
        try:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as s:
                s.login(sender, password)
                s.send_message(msg)
            return True, "已发送"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


email_manager = EmailManager()


async def email_watch_loop(notify_channel: str = "mac") -> None:
    """后台巡检循环（挂在服务启动流程里，**不依赖网页是否打开**）

    - 主人开着网页与否都能收到提醒
    - pytest 下自动不启动，避免单测里发真实请求
    """
    import asyncio
    if "pytest" in sys.modules:
        logger.bind(tag=TAG).info("[邮箱] pytest 环境，跳过巡检循环")
        return
    mgr = email_manager
    logger.bind(tag=TAG).info(f"[邮箱] 巡检循环启动，间隔 {mgr.poll_interval}s")
    while True:
        try:
            reports = await asyncio.to_thread(mgr.run_cycle, notify_channel)
            total = sum(r.get("trashed", 0) for r in reports)
            imp = sum(len(r.get("important", [])) for r in reports)
            if total or imp:
                logger.bind(tag=TAG).info(
                    f"[邮箱] 本轮处理：清理 {total} 封垃圾，重要 {imp} 封")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[邮箱] 巡检循环异常: {e}")
        await asyncio.sleep(mgr.poll_interval)
