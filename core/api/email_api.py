# -*- coding: utf-8 -*-
"""邮箱管家 管理接口

- GET    /api/email/mailboxes          列出被管理的邮箱
- POST   /api/email/mailboxes          添加/更新邮箱（address + password）
- DELETE /api/email/mailboxes/{address} 移除邮箱
- GET    /api/email/rules              规则列表
- POST   /api/email/rules              手动加规则
- DELETE /api/email/rules/{pattern}    删规则
- POST   /api/email/scan               立刻巡检一次（返回处理报告）
- POST   /api/email/learn              从主人手动动作里学习规则
- POST   /api/email/send               用域名邮箱发信（官网表单后端用）
- GET    /api/email/status             状态（含最近一次报告）
"""
from __future__ import annotations

import json

from aiohttp import web

from core.utils.email_manager import email_manager

routes = web.RouteTableDef()

_LAST_REPORT: dict = {}


@routes.get("/api/email/mailboxes")
async def list_mailboxes(request: web.Request):
    out = []
    for mb in email_manager.mailboxes():
        d = {k: v for k, v in mb.__dict__.items() if k != "password"}
        d["has_password"] = bool(mb.password)
        out.append(d)
    return web.json_response({"mailboxes": out})


@routes.post("/api/email/mailboxes")
async def upsert_mailbox(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    address = (data.get("address") or "").strip()
    if "@" not in address:
        return web.json_response({"error": "address 不合法"}, status=400)
    kw = {k: v for k, v in data.items()
          if k in ("imap_host", "imap_port", "smtp_host", "smtp_port", "enabled",
                   "mark_important_as", "trash_as", "notify")}
    mb = email_manager.upsert_mailbox(address, data.get("password", ""), **kw)
    return web.json_response({"ok": True, "address": mb.address})


@routes.delete("/api/email/mailboxes/{address}")
async def delete_mailbox(request: web.Request):
    address = request.match_info["address"]
    ok = email_manager.remove_mailbox(address)
    return web.json_response({"ok": ok})


@routes.get("/api/email/rules")
async def list_rules(request: web.Request):
    return web.json_response({"rules": email_manager.rules_list()})


@routes.post("/api/email/rules")
async def add_rule(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    pattern = (data.get("pattern") or "").strip()
    if not pattern:
        return web.json_response({"error": "pattern 不能为空"}, status=400)
    bucket = data.get("bucket") or "inbox"
    rule = email_manager.add_rule(pattern, bucket, data.get("target_folder", ""), "manual")
    return web.json_response({"ok": True, "rule": rule.__dict__})


@routes.delete("/api/email/rules/{pattern}")
async def delete_rule(request: web.Request):
    ok = email_manager.remove_rule(request.match_info["pattern"])
    return web.json_response({"ok": ok})


@routes.post("/api/email/scan")
async def scan_now(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    reports = email_manager.run_cycle(data.get("notify_channel", "mac"))
    _LAST_REPORT["reports"] = reports
    _LAST_REPORT["at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    return web.json_response({"ok": True, "reports": reports})


@routes.post("/api/email/learn")
async def learn_now(request: web.Request):
    learned = 0
    for mb in email_manager.mailboxes():
        if mb.enabled and mb.password:
            learned += email_manager.learn_from_manual_moves(mb)
    return web.json_response({"ok": True, "learned": learned})


@routes.post("/api/email/send")
async def send_mail(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    missing = [k for k in ("sender", "to", "subject", "body") if not data.get(k)]
    if missing:
        return web.json_response({"error": f"缺少字段: {','.join(missing)}"}, status=400)
    ok, msg = email_manager.send_mail(
        data["sender"], data["to"], data["subject"], data["body"],
        password=data.get("password", ""),
        smtp_host=data.get("smtp_host", ""), smtp_port=int(data.get("smtp_port", 0) or 0))
    return web.json_response({"ok": ok, "message": msg},
                            status=200 if ok else 502)


@routes.get("/api/email/status")
async def status(request: web.Request):
    return web.json_response({
        "mailboxes": len([m for m in email_manager.mailboxes() if m.enabled]),
        "rules": len(email_manager.rules_list()),
        "poll_interval_sec": email_manager.poll_interval,
        "dry_run": email_manager.dry_run,
        "last_report": _LAST_REPORT,
    })


def register_routes(app_or_router):
    app_or_router.add_routes(routes)
