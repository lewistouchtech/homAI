"""
定时提醒/闹钟插件
2026-09-11 迁移：OpenClaw cron 已弃用 → 改用 HomeBrain 内部调度器（core/utils/scheduler.py）
提醒到点由调度器触发 TTS 播报（announce 通道，网页终端出声）。
"""
import json
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from config.logger import setup_logging
from plugins_func.register import register_function, Action, ActionResponse, ToolType
from core.utils.scheduler import scheduler

TAG = __name__
logger = setup_logging()


def parse_time_to_iso(time_str: str) -> Optional[str]:
    """解析时间字符串为 ISO 格式"""
    now = datetime.now()

    if "分钟后" in time_str or "分钟後" in time_str:
        minutes = int(''.join(filter(str.isdigit, time_str)))
        target = now + timedelta(minutes=minutes)
        return target.isoformat()

    if "小时后" in time_str or "小時後" in time_str:
        hours = int(''.join(filter(str.isdigit, time_str)))
        target = now + timedelta(hours=hours)
        return target.isoformat()

    if ":" in time_str and len(time_str) <= 5:
        try:
            h, m = map(int, time_str.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target < now:
                target += timedelta(days=1)
            return target.isoformat()
        except Exception:
            pass

    if "明天" in time_str:
        time_part = time_str.replace("明天", "").strip()
        if ":" in time_part:
            try:
                h, m = map(int, time_part.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
                return target.isoformat()
            except Exception:
                pass

    return None


# OpenAI 格式的函数描述
SET_REMINDER_DESC = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": "设置定时提醒/闹钟。支持一次性提醒和重复提醒。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "提醒标题/内容"
                },
                "time": {
                    "type": "string",
                    "description": "提醒时间，支持格式：'10分钟后'、'2小时后'、'明天08:00'、'08:00'、ISO格式"
                },
                "repeat": {
                    "type": "string",
                    "description": "重复规则：'none'(不重复)、'daily'(每天)、'weekly'(每周)、'workdays'(工作日)",
                    "enum": ["none", "daily", "weekly", "workdays"]
                },
                "priority": {
                    "type": "string",
                    "description": "优先级",
                    "enum": ["low", "medium", "high"]
                }
            },
            "required": ["title", "time"]
        }
    }
}

LIST_REMINDERS_DESC = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": "查看当前所有定时提醒/闹钟列表",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

CANCEL_REMINDER_DESC = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": "取消/删除指定的定时提醒",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "要取消的提醒ID"
                }
            },
            "required": ["job_id"]
        }
    }
}


@register_function(
    name="set_reminder",
    desc=SET_REMINDER_DESC,
    type=ToolType.SYSTEM_CTL
)
async def set_reminder(conn, title: str = "", time: str = "", repeat: str = "none", priority: str = "medium", **kwargs) -> ActionResponse:
    """设置定时提醒（内部调度器）"""
    try:
        if not title:
            return ActionResponse(action=Action.ERROR, response="请提供提醒内容")
        if not time:
            return ActionResponse(action=Action.ERROR, response="请提供提醒时间")

        iso_time = parse_time_to_iso(time)
        dt = None
        if iso_time:
            try:
                dt = datetime.fromisoformat(iso_time)
            except Exception:
                dt = None

        # 构建 schedule
        if repeat == "none":
            if not dt:
                return ActionResponse(action=Action.ERROR, response=f"无法解析时间格式: {time}")
            schedule = {"kind": "at", "at": dt.isoformat()}
        elif repeat == "daily":
            if dt:
                schedule = {"kind": "daily", "time": f"{dt.hour:02d}:{dt.minute:02d}"}
            elif ":" in time:
                schedule = {"kind": "daily", "time": time.strip()[-5:]}
            else:
                return ActionResponse(action=Action.ERROR, response=f"每日提醒请给出具体时间，如「每天08:00」")
        elif repeat == "weekly":
            if not dt:
                return ActionResponse(action=Action.ERROR, response=f"每周提醒请给出时间，如「周一08:00」")
            schedule = {"kind": "weekly", "time": f"{dt.hour:02d}:{dt.minute:02d}", "weekday": dt.weekday()}
        elif repeat == "workdays":
            if not dt:
                return ActionResponse(action=Action.ERROR, response="工作日提醒请给出具体时间")
            schedule = {"kind": "workdays", "time": f"{dt.hour:02d}:{dt.minute:02d}"}
        else:
            return ActionResponse(action=Action.ERROR, response=f"不支持的重复规则: {repeat}")

        task = scheduler.add_task(
            name=f"提醒: {title}",
            schedule=schedule,
            kind="reminder",
            text=title,
            delete_after_run=(repeat == "none"),
        )
        repeat_text = {"none": "一次性", "daily": "每天", "weekly": "每周", "workdays": "工作日"}.get(repeat, repeat)
        return ActionResponse(
            action=Action.RESPONSE,
            response=f"已设置{repeat_text}提醒：{title}，时间：{time}。提醒ID：{task['id']}"
        )
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"设置提醒出错: {str(e)}")


@register_function(
    name="list_reminders",
    desc=LIST_REMINDERS_DESC,
    type=ToolType.SYSTEM_CTL
)
async def list_reminders(conn, **kwargs) -> ActionResponse:
    """查看所有提醒"""
    try:
        tasks = [t for t in scheduler.list_tasks() if t.get("kind") == "reminder"]
        if not tasks:
            return ActionResponse(action=Action.RESPONSE, response="当前没有设置任何提醒")

        lines = ["当前提醒列表："]
        for i, t in enumerate(tasks, 1):
            sch = t.get("schedule", {})
            kind = sch.get("kind", "unknown")
            if kind == "at":
                info = f"一次性，时间: {sch.get('at', '')[:16]}"
            elif kind == "daily":
                info = f"每天 {sch.get('time', '')}"
            elif kind == "workdays":
                info = f"工作日 {sch.get('time', '')}"
            elif kind == "weekly":
                info = f"每周周{['一','二','三','四','五','六','日'][int(sch.get('weekday', 0))]} {sch.get('time', '')}"
            else:
                info = kind
            lines.append(f"{i}. {t.get('name', '未命名')}（{info}）[ID: {t.get('id')}]")
        return ActionResponse(action=Action.RESPONSE, response="\n".join(lines))
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"获取提醒列表出错: {str(e)}")


@register_function(
    name="cancel_reminder",
    desc=CANCEL_REMINDER_DESC,
    type=ToolType.SYSTEM_CTL
)
async def cancel_reminder(conn, job_id: str = "", **kwargs) -> ActionResponse:
    """取消提醒"""
    try:
        if not job_id:
            return ActionResponse(action=Action.ERROR, response="请提供要取消的提醒ID")
        if scheduler.remove_task(job_id.strip()):
            return ActionResponse(action=Action.RESPONSE, response=f"已取消提醒 {job_id}")
        return ActionResponse(action=Action.ERROR, response=f"未找到提醒 {job_id}")
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"取消提醒出错: {str(e)}")


# ---------- 调度器回调（app.py 启动时调用） ----------

async def _tts_base64(text: str, tts_url: str):
    """MOSS-Nano 合成 → base64(wav)"""
    try:
        import base64
        async with aiohttp.ClientSession() as s:
            async with s.post(
                tts_url,
                json={"text": text, "voice": "zh_3", "speed": 1.0, "volume": 5.0},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    return None
                return base64.b64encode(await r.read()).decode()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[提醒] TTS 合成失败: {e}")
        return None


def setup_reminder_handler(sched, server):
    """注册调度器触发回调：到点的提醒通过 TTS 广播到所有终端。"""
    async def on_fire(task):
        if task.get("kind") != "reminder":
            return
        text = task.get("text") or task.get("name", "")
        md_cfg = {}  # 从全局 config 读取 tts_url
        try:
            import yaml, os
            cfg_path = os.path.join(os.getcwd(), "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                md_cfg = (yaml.safe_load(f) or {}).get("morning_digest") or {}
        except Exception:
            pass
        tts_url = str(md_cfg.get("tts_url", "http://127.0.0.1:8086/api/tts"))
        audio_b64 = await _tts_base64(f"提醒：{text}", tts_url)
        payload = {
            "type": "announce",
            "kind": "reminder",
            "text": f"提醒：{text}",
            "ts": int(time.time()),
        }
        if audio_b64:
            payload["audio_b64"] = audio_b64
        body = json.dumps(payload, ensure_ascii=False)
        if server is not None:
            async with server.connections_lock:
                conns = list(server.active_connections)
            for ws in conns:
                try:
                    await ws.send(body)
                except Exception:
                    pass
            logger.bind(tag=TAG).info(f"[调度器] 提醒已播报: {text}")

    sched.set_fire_handler(on_fire)
    logger.bind(tag=TAG).info("[调度器] 提醒回调已注册")
