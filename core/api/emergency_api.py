# -*- coding: utf-8 -*-
"""紧急报警配置与检测 API

提供后台管理接口，用于配置紧急报警关键词、确认超时、报警开关等。
配置持久化到 devices.db 的 config_storage 表（key = emergency_settings）。

路由：
    GET  /api/emergency/settings       → 获取紧急报警配置
    POST /api/emergency/settings       → 保存紧急报警配置
    POST /api/emergency/test           → 测试报警通道（按当前配置打电话给紧急联系人）
    POST /api/emergency/detect         → 调试接口：手动提交文本，返回是否命中关键词
"""
import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging

logger = setup_logging()
TAG = "EmergencyAPI"

SERVER_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = SERVER_ROOT / "data" / "devices.db"
CONFIG_KEY = "emergency_settings"

# 默认配置
DEFAULT_SETTINGS = {
    "enabled": True,
    "keywords": ["救命", "help", "120", "SOS", "110", "119"],
    "confirmation_timeout_seconds": 30,
    "confirm_prompt": "听到您说紧急词汇，请问您是否真的需要帮助？请回答是或否。",
    "alert_order": ["voice", "sms", "wechat"],
    "cooldown_seconds": 60,
    "last_alert_at": 0,
}


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _load_settings() -> dict:
    """从数据库读取紧急报警配置，不存在则返回默认配置。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        try:
            cursor = conn.execute(
                "SELECT value FROM config_storage WHERE key=?", (CONFIG_KEY,)
            )
            row = cursor.fetchone()
            if row and row[0]:
                saved = json.loads(row[0])
                merged = {**DEFAULT_SETTINGS, **saved}
                # 确保关键字是字符串列表
                merged["keywords"] = [str(k).strip().lower() for k in merged.get("keywords", []) if str(k).strip()]
                return merged
        finally:
            conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取紧急报警配置失败: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> bool:
    """保存紧急报警配置到数据库。"""
    try:
        # 清理关键词，确保统一小写、去空格
        settings["keywords"] = sorted(
            {str(k).strip().lower() for k in settings.get("keywords", []) if str(k).strip()}
        )
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO config_storage (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    CONFIG_KEY,
                    json.dumps(settings, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存紧急报警配置失败: {e}")
        return False


async def get_emergency_settings(request):
    """GET /api/emergency/settings"""
    try:
        settings = _load_settings()
        return _json_response({"success": True, "data": settings})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取紧急报警配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def save_emergency_settings(request):
    """POST /api/emergency/settings"""
    try:
        data = await request.json()
        old = _load_settings()

        new_settings = {
            "enabled": bool(data.get("enabled", old.get("enabled", True))),
            "keywords": data.get("keywords", old.get("keywords", DEFAULT_SETTINGS["keywords"])),
            "confirmation_timeout_seconds": int(
                data.get("confirmation_timeout_seconds", old.get("confirmation_timeout_seconds", 30))
            ),
            "confirm_prompt": str(data.get("confirm_prompt", old.get("confirm_prompt", DEFAULT_SETTINGS["confirm_prompt"]))).strip(),
            "alert_order": data.get("alert_order", old.get("alert_order", DEFAULT_SETTINGS["alert_order"])),
            "cooldown_seconds": int(data.get("cooldown_seconds", old.get("cooldown_seconds", 60))),
            "last_alert_at": int(old.get("last_alert_at", 0)),
        }

        if new_settings["confirmation_timeout_seconds"] < 5:
            new_settings["confirmation_timeout_seconds"] = 5
        if new_settings["confirmation_timeout_seconds"] > 300:
            new_settings["confirmation_timeout_seconds"] = 300

        if not new_settings["confirm_prompt"]:
            new_settings["confirm_prompt"] = DEFAULT_SETTINGS["confirm_prompt"]

        valid_orders = {"voice", "sms", "wechat"}
        new_settings["alert_order"] = [x for x in new_settings["alert_order"] if x in valid_orders]
        if not new_settings["alert_order"]:
            new_settings["alert_order"] = DEFAULT_SETTINGS["alert_order"]

        if save_settings(new_settings):
            return _json_response({"success": True, "data": new_settings})
        return _json_response({"success": False, "error": "保存失败"}, status=500)
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存紧急报警配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def test_emergency_alert(request):
    """POST /api/emergency/test

    测试报警通道。仅用于管理员验证配置，会真实发送通知。
    请求体可指定 channel（voice/sms/wechat），默认按 alert_order 顺序执行第一个。
    """
    try:
        data = await request.json() or {}
        channel = data.get("channel")
        settings = _load_settings()
        alert_order = settings.get("alert_order", DEFAULT_SETTINGS["alert_order"])
        if channel and channel in alert_order:
            channels = [channel]
        else:
            channels = alert_order

        # 读取通知通道配置
        from core.utils import notify_service

        results = []
        for ch in channels:
            if ch == "voice":
                ok, msg = await notify_service.send_voice(
                    "这是 homAi 紧急报警测试电话。如果听到这段语音，说明语音电话通道正常。"
                )
                results.append({"channel": "voice", "ok": ok, "message": msg})
            elif ch == "sms":
                ok, msg = await notify_service.send_sms("【homAi 测试】紧急报警短信通道测试。")
                results.append({"channel": "sms", "ok": ok, "message": msg})
            elif ch == "wechat":
                ok, msg = await notify_service.test_pushplus_channel(
                    "wechat", "【homAi 测试】紧急报警微信推送测试。"
                )
                results.append({"channel": "wechat", "ok": ok, "message": msg})

            # 测试时每个通道都跑一次，不中断

        return _json_response({"success": True, "results": results})
    except Exception as e:
        logger.bind(tag=TAG).error(f"测试紧急报警失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def detect_emergency_text(request):
    """POST /api/emergency/detect

    调试接口：提交文本，返回是否命中当前配置的关键词。
    用于后台调整关键词时快速验证。
    """
    try:
        data = await request.json() or {}
        text = str(data.get("text", ""))
        settings = _load_settings()
        matched = _match_emergency_keywords(text, settings.get("keywords", []))
        return _json_response({
            "success": True,
            "matched": bool(matched),
            "keyword": matched,
            "text": text,
            "keywords": settings.get("keywords", []),
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"检测紧急文本失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


def _match_emergency_keywords(text: str, keywords: list) -> str | None:
    """判断文本是否命中任意关键词，返回命中的关键词（小写）。"""
    if not text or not keywords:
        return None
    lower_text = text.lower()
    for kw in keywords:
        kw = str(kw).strip().lower()
        if kw and kw in lower_text:
            return kw
    return None


def get_emergency_settings_sync() -> dict:
    """供其他模块同步读取紧急报警配置。"""
    return _load_settings()


def match_emergency_keywords_sync(text: str) -> str | None:
    """供 emergency_listener 同步调用。"""
    settings = _load_settings()
    if not settings.get("enabled", True):
        return None
    return _match_emergency_keywords(text, settings.get("keywords", []))


def register_routes(app):
    """注册紧急报警 API 路由。"""
    app.router.add_get("/api/emergency/settings", get_emergency_settings)
    app.router.add_post("/api/emergency/settings", save_emergency_settings)
    app.router.add_post("/api/emergency/test", test_emergency_alert)
    app.router.add_post("/api/emergency/detect", detect_emergency_text)
