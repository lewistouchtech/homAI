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
import os
import time

from aiohttp import web

from core.utils.email_manager import email_manager

routes = web.RouteTableDef()

_LAST_REPORT: dict = {}


def _local_origin_allowed(request: web.Request) -> tuple[str, bool]:
    """只放行「本机来源」：null(file:// 预览面板) / 127.0.0.1 / localhost / 本机 IP。

    不放行任意外站，避免外部网页读取主人邮箱信息。
    """
    origin = request.headers.get("Origin", "")
    host = request.host.split(":")[0]
    allow = (origin == "null"
             or origin.startswith("http://127.0.0.1")
             or origin.startswith("http://localhost")
             or origin.startswith(f"http://{host}"))
    return origin, allow


def cors_local(fn):
    """给 handler 补 CORS 头（邮箱规则页可能被 file:// 打开预览面板）

    前端刻意用 `text/plain` 发 POST（简单请求）→ 不触发 CORS 预检，
    所以这里不需要处理 OPTIONS，只需在响应上加 Allow-Origin。
    """
    async def wrapper(request: web.Request):
        resp = await fn(request)
        origin, allow = _local_origin_allowed(request)
        if origin and allow:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            resp.headers["Vary"] = "Origin"
        return resp
    wrapper.__name__ = fn.__name__
    return wrapper


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




# ---------------------------------------------------------------- 来源清单
# 扫描状态与缓存（后台线程跑，结果落 data/.email_sources_cache.json）
_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", ".email_sources_cache.json")
_SCAN = {"scanning": False, "started_at": None, "last_done": None, "error": None}


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _build_sources() -> dict:
    """真扫一遍（慢，放后台线程）"""
    from collections import Counter, defaultdict
    from core.utils.mailapp_bridge import fetch_unread
    mails = fetch_unread()
    buckets = defaultdict(lambda: {"count": 0, "accounts": Counter(), "samples": [],
                                   "trash": 0, "important": 0})
    for m in mails:
        b = buckets[_domain_of(m["sender"])]
        b["count"] += 1
        b["accounts"][m["account"]] += 1
        bucket, reason = email_manager.classify(m["sender"], m["subject"], {}, "")
        if bucket == "trash":
            b["trash"] += 1
        elif bucket == "important":
            b["important"] += 1
        if len(b["samples"]) < 3:
            b["samples"].append({"subject": m["subject"][:90], "sender": m["sender"][:70],
                                 "account": m["account"], "reason": reason})
    rows = []
    for dom, b in sorted(buckets.items(), key=lambda kv: -kv[1]["count"]):
        rows.append({
            "domain": dom, "count": b["count"], "trash": b["trash"], "important": b["important"],
            "accounts": dict(b["accounts"]), "samples": b["samples"],
        })
    return {"ts": int(time.time()), "total_unread": len(mails),
            "domains": len(rows), "rows": rows}


def _start_scan() -> None:
    import asyncio
    _SCAN.update({"scanning": True, "started_at": int(time.time()), "error": None})

    async def _run():
        try:
            data = await asyncio.to_thread(_build_sources)
            _save_cache(data)
            _SCAN["last_done"] = int(time.time())
        except Exception as e:
            _SCAN["error"] = f"{type(e).__name__}: {e}"
        finally:
            _SCAN["scanning"] = False

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # 没有事件循环（同步调用场景）→ 直接同步跑
        try:
            _save_cache(_build_sources())
            _SCAN["last_done"] = int(time.time())
        except Exception as e:
            _SCAN["error"] = f"{type(e).__name__}: {e}"
        finally:
            _SCAN["scanning"] = False


def _domain_of(sender: str) -> str:
    s = (sender or "").lower()
    if "<" in s:
        s = s.split("<")[-1]
    s = s.split(">")[0]
    if "@" in s:
        return s.split("@")[-1].strip()
    return s.strip() or "(未知)"


@routes.get("/api/email/sources")
@cors_local
async def list_sources(request: web.Request):
    """来源清单：**立即返回缓存**；缓存缺失/过期则后台开扫，前端轮询。

    ⚠️ 必须异步（主人 2026-10-21 报「一直显示正在扫描」→ 查实因）：
       扫 1000+ 封邮件要走 4700+ 次 Apple Event，**同步要 100~180 秒**，
       浏览器请求必然被中途掐断 → 页面永远停在「正在扫描」。
    """
    cache = _load_cache()
    age = int(time.time()) - int(cache.get("ts", 0) or 0)
    if (not cache or age > 300) and not _SCAN["scanning"]:
        _start_scan()
    trash = email_manager.settings.get("trash_domains", []) or []
    keep = email_manager.settings.get("keep_domains", []) or []
    for r in cache.get("rows", []):
        r["decision"] = ("清" if r["domain"] in trash else
                         "留" if r["domain"] in keep else "")
    return web.json_response({
        "scanning": _SCAN["scanning"],
        "last_done": _SCAN["last_done"],
        "error": _SCAN["error"],
        "cache_age_sec": age,
        "total_unread": cache.get("total_unread", 0),
        "domains": cache.get("domains", 0),
        "rows": cache.get("rows", []),
        "trash_domains": trash,
        "keep_domains": keep,
    })


@routes.post("/api/email/domains")
@cors_local
async def set_domain_rules(request: web.Request):
    """批量设置发件域规则：{"trash": [...], "keep": [...]}"""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    trash = [str(x).strip().lower().lstrip("@") for x in (data.get("trash") or []) if str(x).strip()]
    keep = [str(x).strip().lower().lstrip("@") for x in (data.get("keep") or []) if str(x).strip()]
    keep = [k for k in keep if k not in trash]      # 冲突时以「清」为准
    email_manager.settings["trash_domains"] = sorted(set(trash))
    email_manager.settings["keep_domains"] = sorted(set(keep))
    email_manager.save()
    return web.json_response({"ok": True,
                              "trash_domains": email_manager.settings["trash_domains"],
                              "keep_domains": email_manager.settings["keep_domains"]})


@routes.post("/api/email/clean-now")
@cors_local
async def clean_now(request: web.Request):
    """按当前规则立刻清理一轮（垃圾 → 移到废纸篓，可恢复）"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    report = email_manager.scan_mailapp(notify_channel=data.get("notify_channel", "none"), trash=True)
    return web.json_response({"ok": True, "report": report})

def register_routes(app_or_router):
    app_or_router.add_routes(routes)
