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
# 主题里的工作/重要特征
_WORK_SUBJECT_HINTS = (
    "合同", "报价", "订单", "发票", "对账", "项目", "会议", "面试", "入职",
    "续费", "账单", "年检", "报税", "发票", "回执", "中标", "采购", "报销",
    "整改", "通知", "公告", "验收", "交付", "方案", "协议", "账户变更",
    "安全提醒", "异地登录", "异常登录", "安全中心",
)


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


class EmailManager:
    """邮箱管家"""

    def __init__(self):
        self.config = self._load_json(CONFIG_PATH, {"mailboxes": [], "rules": [], "settings": {}})
        self.accounts = self._load_json(ACCOUNTS_PATH, {})   # address -> password
        self.rules: list[Rule] = [Rule(**r) for r in self.config.get("rules", [])]
        self.settings: dict[str, Any] = self.config.get("settings", {}) or {}
        self.poll_interval = int(self.settings.get("poll_interval_sec", 300))
        self.dry_run = bool(self.settings.get("dry_run", False))
        self._folder_cache: dict[str, dict[str, str]] = {}   # address -> {role: folder}
        self._learned_message_ids: set[str] = set()

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
        """探测邮箱文件夹：{role: 实际文件夹名}；role ∈ inbox/trash/important/sent"""
        if mb.key in self._folder_cache:
            return self._folder_cache[mb.key]
        roles: dict[str, str] = {"inbox": "INBOX", "trash": "", "important": "", "sent": ""}
        try:
            M = self._imap(mb)
            M.login(mb.address, mb.password)
            typ, boxes = M.list()
            for raw in boxes or []:
                line = raw.decode(errors="ignore")
                name = line.split(' "/" ')[-1].strip('"')
                low = name.lower()
                if "\\trash" in low or "已删除" in name or name.lower() in ("trash", "deleted"):
                    roles["trash"] = name
                elif "\\sent" in low or "已发送" in name or low == "sent":
                    roles["sent"] = name
                elif name in ("重要", "重要邮件", "starred", "flagged"):
                    roles["important"] = name
                elif "垃圾" in name or "junk" in low or "spam" in low:
                    roles.setdefault("junk", name)
            M.logout()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[邮箱] 探测文件夹失败 {mb.address}: {e}")
        self._folder_cache[mb.key] = roles
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
