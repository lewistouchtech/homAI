# -*- coding: utf-8 -*-
"""展厅「迎宾」触发核心（2026-10-15）

两条触发路径共用这里的实现，避免各写一套：

1. **口令**：LLM 调 `plugins_func/functions/welcome_demo.py`（「伊娃，演示迎宾」）
2. **真人到达**：`core/utils/security_listener.py:_on_person_end` 里，
   「没被认出来的人往里走」这个空档（原本静默）→ 调 `maybe_welcome()`

为什么单独成模块：`security_listener.py` 的播报广播 `_broadcast` 是
`start_security_listener()` 闭包内的函数（外部拿不到），所以这里用
**依赖注入**——由调用方把 broadcast 函数传进来，本模块不碰 WebSocket。

灯光编排不在这里：只负责向 HA 抛 `hb_welcome` 事件，由 0.231 上的
自动化 `ying_bin_deng_guang` 执行渐亮。本模块**不直接控制灯**。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Dict, Optional

import aiohttp
import yaml

TAG = "showroom_welcome"
_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", ".config.yaml"
)

# 主人 2026-10-14 选定的 B 版话术
WELCOME_TEXT = "您好，我是伊娃，欢迎光临～"

# HA 侧自动化 ying_bin_deng_guang 监听的事件名
WELCOME_EVENT = "hb_welcome"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)

# 进程内冷却（同一个人反复进出不反复播）
_last_fire: Dict[str, float] = {}


def _cfg() -> Dict[str, Any]:
    """读 security.showroom_welcome 配置；读不到一律当作「关闭」。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = ((data.get("security") or {}) or {}).get("showroom_welcome") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _in_window(cfg: Dict[str, Any], now: float) -> bool:
    """演示时段限制：默认 08:00–21:00，避免深夜误触发。"""
    hours = cfg.get("active_hours") or [8, 21]
    try:
        start, end = int(hours[0]), int(hours[1])
    except Exception:
        start, end = 8, 21
    h = time.localtime(now).tm_hour
    return start <= h < end


def should_welcome(camera_id: str, direction: str, now: Optional[float] = None) -> bool:
    """判断这次「陌生人进门」要不要迎宾。方向未知一律不迎（宁可少说）。"""
    cfg = _cfg()
    if not cfg.get("enabled"):
        return False
    if direction != "in":
        return False
    cams = cfg.get("cameras")
    if isinstance(cams, list) and cams and camera_id not in cams:
        return False
    now = float(now if now is not None else time.time())
    if not _in_window(cfg, now):
        return False
    cooldown = float(cfg.get("cooldown_seconds") or 90)
    last = _last_fire.get(camera_id, 0.0)
    return (now - last) >= cooldown


async def fire_ha_event() -> bool:
    """向 HA 抛 hb_welcome 事件（灯光由 HA 侧自动化执行）。

    返回 False = 没抛成功（HA 不可达/未配置）。此时**不播报欢迎词**——
    灯没亮还喊「欢迎光临」是假成功，与家居控制层的原则一致。
    """
    from core.home.config import get_ha_config, HomeConfigError

    try:
        base_url, token = get_ha_config(force=True)
    except HomeConfigError:
        return False
    except Exception:
        return False
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{base_url.rstrip('/')}/api/events/{WELCOME_EVENT}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={},
            ) as resp:
                return resp.status < 400
    except Exception:
        return False


async def maybe_welcome(
    camera_id: str,
    camera_name: str,
    broadcast: Callable[..., Any],
    assistant_busy: Callable[[], bool] = lambda: False,
    log: Optional[Callable[[str, str], Any]] = None,
) -> bool:
    """陌生人进门的迎宾。`broadcast` 由 security_listener 传入（闭包内的 _broadcast）。

    返回 True = 本次真的迎宾了。
    """
    if not should_welcome(camera_id, "in"):
        return False

    ok = await fire_ha_event()
    if not ok:
        if log:
            log("warning", f"[迎宾] 灯光事件未送达（HA 不可达），本次不播报（camera={camera_id}）")
        return False

    _last_fire[camera_id] = time.time()
    text = WELCOME_TEXT
    try:
        await broadcast({"level": "info", "source": f"摄像头·{camera_name}",
                         "text": text}, speak=not assistant_busy())
    except Exception:
        return False
    if log:
        log("info", f"[迎宾] 已触发（{camera_name}）：灯光 + 「{text}」")
    return True


def reset_cooldown() -> None:
    """测试用：清空冷却记录。"""
    _last_fire.clear()