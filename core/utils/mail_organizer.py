"""整理邮箱后台任务（2026-09-23）

参考 gmail-llm-cleanup 思路，适配多邮箱（Google/MSN/Hotmail/QQ/iCloud/info/iuhouse）：
- 逐账户拉取收件箱邮件列表 → 每批 20 封读内容 → LLM 逐封判断保留/删除 → 批量移废纸篓
- 后台 asyncio 任务，不阻塞数字人对话；删除进废纸篓（30 天可恢复）
- 硬保护白名单：主人自己发的邮件、验证码/密码/账单类主题，LLM 不能推翻
- 完成后生成报告文件（data/mail_organizer/）+ 语音播报摘要
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 每批交给 LLM 的邮件数（参考 gmail-llm-cleanup 的 ~20）
BATCH_SIZE = 20
# 读正文时截断长度（get-message 会带引用历史，可能很长）
CONTENT_MAX = 800
# 批量删除单批上限（apple-mcp 限制 100）
DELETE_BATCH = 100
# 读内容并发数
READ_CONCURRENCY = 3
# 报告目录
REPORT_DIR = os.path.join(os.getcwd(), "data", "mail_organizer")

# 主人自己的邮箱地址（这些地址发出的邮件强制保留）
OWNER_EMAILS = {
    "lewis.touchtech@gmail.com",
    "liwei@iuhouse.com",
    "info@iuhouse.com",
    "lewis.li@msn.com",
    "man2net@hotmail.com",
    "6305855@qq.com",
    "liwei.lewis@me.com",
    "liwei.lewis@icloud.com",
}
# 主题命中这些关键词 → 强制保留（LLM 判删也翻不了）
PROTECT_KEYWORDS = [
    "验证码", "密码", "激活", "账单", "发票", "订单", "退款", "预订",
    "invoice", "receipt", "verification", "password", "security code",
]

CLASSIFY_PROMPT = """你是家庭邮箱整理助手。用户是中文用户，请逐封判断下列邮件该"删除"还是"保留"。

【删除】垃圾邮件：
- 广告/促销/优惠券/折扣/限时活动
- 推广邮件（App 推荐、活动邀请、产品营销、积分提醒、平台资讯）
- 不含密码的注册欢迎信（"感谢注册""欢迎加入"）
- 宣传杂志/Newsletter/资讯简报/电子杂志
- 平台定期群发（产品更新、系统通知、例行提醒）
- 开发平台通知（GitHub、Cloudflare 等自动通知）

【保留】重要邮件：
- 工作往来（人与人之间一对一写的邮件）
- 含密码/验证码/激活链接的账号邮件
- 账单/发票/订单/收据/退款
- 银行/支付的账户安全通知
- 亲友来信/预订确认/物流通知
- 政府/税务/法律通知
- 用户自己发出的邮件（发件人是用户自己的邮箱地址）
- 用户正在处理的订阅事务（如退订往来）

【重要】拿不准的一律保留。宁可漏删，不可误删。

输出要求：只输出 JSON 数组，不要任何其他文字。格式：
[{"id": "邮件ID", "action": "delete"或"keep", "reason": "不超过20字的理由"}]
"""


def _parse_accounts(text: str) -> List[str]:
    """解析 list-accounts 返回：'Found 7 account(s):\\n  - xxx\\n  - yyy'"""
    accounts = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            name = line[2:].strip()
            if name:
                accounts.append(name)
    return accounts


def _parse_msg_line(line: str) -> Optional[Dict[str, str]]:
    """解析 list-messages 单行：'- ID: 43763 | 9/22/2026 | Subject (from: Name <addr>)'"""
    try:
        body = line.strip()
        if body.startswith("- "):
            body = body[2:].strip()
        if not body.startswith("ID:"):
            return None
        parts = body.split(" | ", 2)
        msg_id = parts[0].replace("ID:", "").strip()
        date = parts[1].strip() if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if " (from: " in rest:
            subject, sender = rest.rsplit(" (from: ", 1)
            sender = sender.rstrip(")")
        else:
            subject, sender = rest, ""
        return {"id": msg_id, "date": date, "subject": subject.strip(), "from": sender.strip()}
    except Exception:
        return None


def _parse_llm_json(text: str) -> Optional[List[Dict[str, str]]]:
    """解析 LLM 输出的 JSON 数组（容忍 ```json 包裹与前后杂讯）"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        data = json.loads(t)
    except Exception:
        start, end = t.find("["), t.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(t[start:end + 1])
        except Exception:
            return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item["id"]).strip(),
                "action": str(item.get("action", "keep")).strip().lower(),
                "reason": str(item.get("reason", "")).strip(),
            })
    return out


class MailOrganizer:
    """整理邮箱后台任务管理器（单例）"""

    _instance: Optional["MailOrganizer"] = None

    @classmethod
    def get(cls) -> "MailOrganizer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.status = "idle"          # idle / running / done / failed
        self.progress: Dict[str, Any] = {
            "account": "", "processed": 0, "total": 0, "deleted": 0, "kept": 0,
        }
        self.report_summary = ""      # 一句话摘要（供语音/对话使用）
        self.report_text = ""         # 完整报告文本
        self.started_at = 0.0
        self.finished_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._config: Optional[dict] = None
        self._server = None
        self.dry_run = False           # True=只记录不真删（测试用）

    # ---------------- 对外接口 ----------------

    def progress_text(self) -> str:
        p = self.progress
        return (f"正在整理「{p['account']}」（{p['processed']}/{p['total']}），"
                f"已删 {p['deleted']} 封，保留 {p['kept']} 封")

    async def start(self, config: dict, server=None, accounts: List[str] = None, force: bool = False):
        """启动后台整理任务（非阻塞）。返回 (是否启动, 给用户的回复文本)"""
        if self.status == "running":
            return False, f"邮箱整理正在进行中：{self.progress_text()}。完成后我会向您报告。"
        if self.status == "done" and self.report_summary and not force:
            return False, f"上一次整理的结果：{self.report_summary} 需要我再整理一次吗？"

        self._config = config or {}
        self._server = server
        self.status = "running"
        self.started_at = time.time()
        self.progress = {"account": "", "processed": 0, "total": 0, "deleted": 0, "kept": 0}
        self.report_summary = ""
        self.report_text = ""
        self._task = asyncio.get_running_loop().create_task(self._run(accounts or None))
        return True, "好的，我开始整理邮箱了。我会在后台慢慢处理，不耽误咱们聊天，完成后向您报告。"

    # ---------------- 主流程 ----------------

    async def _run(self, only_accounts: Optional[List[str]]):
        from core.providers.tools.server_mcp.mcp_client import ServerMCPClient

        report: Dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "accounts": [], "total_scanned": 0, "total_deleted": 0, "total_kept": 0,
        }
        client = None
        try:
            cfg_path = os.path.join(os.getcwd(), "data", ".mcp_server_settings.json")
            with open(cfg_path, "r", encoding="utf-8") as f:
                mcp_cfg = (json.load(f) or {}).get("mcpServers", {}).get("apple-mail")
            if not mcp_cfg:
                raise RuntimeError("未找到 apple-mail MCP 配置")
            client = ServerMCPClient(mcp_cfg)
            await client.initialize()

            res = await client.call_tool("list-accounts", {})
            accounts = _parse_accounts(res.content[0].text)
            if only_accounts:
                accounts = [a for a in accounts if a in only_accounts]
            logger.bind(tag=TAG).info(f"[整理邮箱] 开始，账户: {accounts}")

            for account in accounts:
                try:
                    await self._process_account(client, account, report)
                except Exception as e:
                    logger.bind(tag=TAG).error(f"[整理邮箱] 账户 {account} 处理失败: {e}")
                    report["accounts"].append({"account": account, "error": str(e)})

            self.status = "done"
            self.finished_at = time.time()
            self._finalize(report)
        except Exception as e:
            logger.bind(tag=TAG).error(f"[整理邮箱] 任务失败: {e}")
            self.status = "failed"
            self.report_summary = f"邮箱整理失败：{e}"
            self.report_text = self.report_summary
        finally:
            if client is not None:
                try:
                    await client.cleanup()
                except Exception:
                    pass

    async def _process_account(self, client, account: str, report: Dict[str, Any]):
        """处理单个账户：列表 → 读内容 → 分类 → 删除"""
        # 1. 确定收件箱名（Exchange 系是「收件箱」，其余是 INBOX）
        mailbox = "INBOX"
        messages = await self._list_messages(client, account, mailbox)
        if messages is None:
            mailbox = "收件箱"
            messages = await self._list_messages(client, account, mailbox)
        if messages is None:
            raise RuntimeError("无法列出收件箱邮件")
        if not messages:
            logger.bind(tag=TAG).info(f"[整理邮箱] {account} 收件箱为空")
            report["accounts"].append({"account": account, "mailbox": mailbox,
                                       "scanned": 0, "deleted": 0, "kept": 0,
                                       "delete_list": [], "keep_list": []})
            return

        self.progress["account"] = account
        self.progress["processed"] = 0
        self.progress["total"] = len(messages)
        logger.bind(tag=TAG).info(f"[整理邮箱] {account}/{mailbox} 共 {len(messages)} 封，开始逐批处理")

        # 2. 分批：读内容 → LLM 分类
        sem = asyncio.Semaphore(READ_CONCURRENCY)
        to_delete: List[Dict[str, str]] = []
        keep_list: List[Dict[str, str]] = []
        kept = 0

        for i in range(0, len(messages), BATCH_SIZE):
            batch = messages[i:i + BATCH_SIZE]
            contents = await asyncio.gather(
                *[self._read_content(client, account, mailbox, m, sem) for m in batch]
            )
            decisions = await self._classify(batch, contents)
            for m in batch:
                d = decisions.get(m["id"], {"action": "keep", "reason": "分类缺失，保守保留"})
                if d["action"] == "delete" and not self._is_protected(m):
                    to_delete.append({**m, "reason": d["reason"]})
                else:
                    kept += 1
                    keep_list.append({**m, "reason": d.get("reason") or "受保护或保守保留"})
            self.progress["processed"] = min(i + BATCH_SIZE, len(messages))
            self.progress["deleted"] = len(to_delete)
            self.progress["kept"] = kept

        # 3. 批量删除
        deleted_ok, failed = await self._batch_delete(client, account, mailbox, to_delete)

        report["accounts"].append({
            "account": account, "mailbox": mailbox,
            "scanned": len(messages), "deleted": deleted_ok, "kept": kept,
            "failed": failed, "delete_list": to_delete, "keep_list": keep_list,
        })
        report["total_scanned"] += len(messages)
        report["total_deleted"] += deleted_ok
        report["total_kept"] += kept
        logger.bind(tag=TAG).info(f"[整理邮箱] {account} 完成：扫描 {len(messages)}，删除 {deleted_ok}，保留 {kept}，失败 {failed}")

    async def _list_messages(self, client, account: str, mailbox: str) -> Optional[List[Dict[str, str]]]:
        """翻页拉取整个收件箱的邮件列表（ID/日期/主题/发件人）"""
        messages: List[Dict[str, str]] = []
        offset = 0
        page = 50
        while True:
            try:
                res = await client.call_tool("list-messages", {
                    "account": account, "mailbox": mailbox, "limit": page, "offset": offset,
                })
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[整理邮箱] {account}/{mailbox} 列表失败(offset={offset}): {e}")
                return messages if offset > 0 else None
            text = res.content[0].text or ""
            got = 0
            for line in text.splitlines():
                item = _parse_msg_line(line)
                if item:
                    messages.append(item)
                    got += 1
            if got < page:
                break
            offset += page
        return messages

    async def _read_content(self, client, account: str, mailbox: str,
                            msg: Dict[str, str], sem: asyncio.Semaphore) -> str:
        """读取单封邮件正文（截断）"""
        async with sem:
            try:
                res = await client.call_tool("get-message", {
                    "account": account, "id": str(msg["id"]), "mailbox": mailbox,
                })
                text = res.content[0].text or ""
                # 去掉引用历史（很多邮件带 "---- 回复的原邮件 ----"）
                text = text.split("---- 回复的原邮件 ----")[0]
                return text[:CONTENT_MAX]
            except Exception as e:
                logger.bind(tag=TAG).debug(f"[整理邮箱] 读 {msg['id']} 失败: {e}")
                return ""

    async def _classify(self, batch: List[Dict[str, str]], contents: List[str]) -> Dict[str, Dict[str, str]]:
        """调用 LLM 对一批邮件分类，返回 {id: {action, reason}}；失败时全部保守保留"""
        lines = []
        for m, c in zip(batch, contents):
            lines.append(
                f"邮件 ID={m['id']} | 发件人={m['from']} | 主题={m['subject']} | 内容={c or '(读取失败)'}"
            )
        user_text = "\n\n".join(lines)

        try:
            text = await self._llm_chat(CLASSIFY_PROMPT, user_text)
            parsed = _parse_llm_json(text)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[整理邮箱] 分类调用失败: {e}")
            parsed = None

        if not parsed:
            logger.bind(tag=TAG).warning("[整理邮箱] 分类解析失败，本批全部保守保留")
            return {}
        return {d["id"]: d for d in parsed}

    async def _llm_chat(self, system_prompt: str, user_text: str) -> str:
        """调用主 LLM（doubao）做分类（参考 morning_digest 的直连方式）"""
        import aiohttp
        llm_cfg = ((self._config or {}).get("LLM") or {}).get("UserDefinedLLM") or {}
        base = str(llm_cfg.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("LLM 配置缺失")
        payload = {
            "model": llm_cfg.get("model_name"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
            "enable_thinking": False,
        }
        headers = {
            "Authorization": f"Bearer {llm_cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/chat/completions", json=payload,
                                    headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                data = await resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    def _is_protected(self, msg: Dict[str, str]) -> bool:
        """硬保护：主人自己的邮件、验证码/密码/账单类主题"""
        sender = (msg.get("from") or "").lower()
        subject = msg.get("subject") or ""
        for addr in OWNER_EMAILS:
            if addr.lower() in sender:
                return True
        for kw in PROTECT_KEYWORDS:
            if kw.lower() in subject.lower():
                return True
        return False

    async def _batch_delete(self, client, account: str, mailbox: str,
                            to_delete: List[Dict[str, str]]):
        """批量删除（移废纸篓），每批 ≤100，返回 (成功数, 失败数)"""
        ok = 0
        failed = 0
        for i in range(0, len(to_delete), DELETE_BATCH):
            chunk = to_delete[i:i + DELETE_BATCH]
            ids = [m["id"] for m in chunk]
            if self.dry_run:
                # 测试模式：只记录不真删
                logger.bind(tag=TAG).info(f"[整理邮箱] dry-run 跳过删除 {len(ids)} 封: {ids[:5]}")
                ok += len(ids)
                continue
            try:
                res = await client.call_tool("batch-delete-messages", {
                    "ids": ids, "sourceMailbox": mailbox, "sourceAccount": account,
                })
                text = (res.content[0].text or "").lower()
                if "fail" in text or "error" in text:
                    logger.bind(tag=TAG).warning(f"[整理邮箱] 批量删除部分失败: {res.content[0].text[:200]}")
                ok += len(ids)
            except Exception as e:
                logger.bind(tag=TAG).error(f"[整理邮箱] 批量删除失败（{len(ids)} 封）: {e}")
                failed += len(ids)
        return ok, failed

    # ---------------- 报告 ----------------

    def _finalize(self, report: Dict[str, Any]):
        """生成摘要 + 保存报告文件 + 语音播报"""
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.finished_at))
        report["duration_min"] = round((self.finished_at - self.started_at) / 60, 1)

        self.report_summary = (
            f"邮箱整理完成：共检查 {report['total_scanned']} 封，"
            f"删除 {report['total_deleted']} 封垃圾邮件，保留 {report['total_kept']} 封。"
        )
        # 各账户明细
        detail_lines = []
        for acc in report["accounts"]:
            if "error" in acc:
                detail_lines.append(f"  - {acc['account']}：处理失败（{acc['error']}）")
            else:
                detail_lines.append(
                    f"  - {acc['account']}：扫描 {acc['scanned']}，删除 {acc['deleted']}，保留 {acc['kept']}"
                    + (f"，失败 {acc['failed']}" if acc.get("failed") else "")
                )
        self.report_text = self.report_summary + "\n各账户明细：\n" + "\n".join(detail_lines)

        # 保存文件
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started_at))
            with open(os.path.join(REPORT_DIR, f"report_{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            with open(os.path.join(REPORT_DIR, f"report_{stamp}.txt"), "w", encoding="utf-8") as f:
                f.write(self.report_text + "\n\n删除明细：\n")
                for acc in report["accounts"]:
                    for m in (acc.get("delete_list") or []):
                        f.write(f"  [{acc['account']}] {m['id']} | {m['subject']} | {m['from']} | {m['reason']}\n")
                f.write("\n保留明细：\n")
                for acc in report["accounts"]:
                    for m in (acc.get("keep_list") or []):
                        f.write(f"  [{acc['account']}] {m['id']} | {m['subject']} | {m['from']} | {m['reason']}\n")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[整理邮箱] 报告保存失败: {e}")

        logger.bind(tag=TAG).info(f"[整理邮箱] {self.report_summary}")
        # 异步播报（不阻塞任务收尾）
        try:
            asyncio.get_running_loop().create_task(self._announce(self.report_summary))
        except Exception:
            pass

    async def _announce(self, text: str):
        """通过 WebSocket 向所有在线终端播报（复用提醒的 announce 机制）"""
        server = self._server
        if server is None:
            return
        try:
            import aiohttp
            tts_url = "http://127.0.0.1:8086/api/tts"
            try:
                import yaml
                with open(os.path.join(os.getcwd(), "config.yaml"), "r", encoding="utf-8") as f:
                    md_cfg = (yaml.safe_load(f) or {}).get("morning_digest") or {}
                tts_url = str(md_cfg.get("tts_url", tts_url))
            except Exception:
                pass
            audio_b64 = await self._tts_base64(f"邮箱整理完成。{text}", tts_url)
            payload = {
                "type": "announce", "kind": "mail_organizer",
                "text": f"邮箱整理完成。{text}",
                "ts": int(time.time()),
            }
            if audio_b64:
                payload["audio_b64"] = audio_b64
            body = json.dumps(payload, ensure_ascii=False)
            async with server.connections_lock:
                conns = list(server.active_connections)
            for ws in conns:
                try:
                    await ws.send(body)
                except Exception:
                    pass
            logger.bind(tag=TAG).info(f"[整理邮箱] 播报完成: {text}")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[整理邮箱] 播报失败: {e}")

    @staticmethod
    async def _tts_base64(text: str, tts_url: str) -> Optional[str]:
        """TTS 合成（参考 reminder._tts_base64）"""
        import aiohttp
        import base64
        try:
            payload = {"text": text, "voice": "zh_3", "speed": 1.0, "volume": 5.0}
            async with aiohttp.ClientSession() as session:
                async with session.post(tts_url, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    if not data:
                        return None
                    return base64.b64encode(data).decode("utf-8")
        except Exception:
            return None
