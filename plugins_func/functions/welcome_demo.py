# -*- coding: utf-8 -*-
"""展厅「演示迎宾」触发工具

用途：主人对伊娃说"演示迎宾""来个迎宾""欢迎演示"时，
本工具做两件事：
  1. 向 Home Assistant 触发 `hb_welcome` 事件 → 由 HA 侧的自动化
     `ying_bin_deng_guang`（迎宾灯光）执行：前厅灯 3 秒渐亮到 70%（2700K 暖光）。
     灯光编排细节全部在 HA 里，本工具只负责"敲门"。
  2. 直接返回播报文案（主人 2026-10-14 选定 B 版）：
     「您好，我是伊娃，欢迎光临～」

为什么不自己控制灯：灯光节奏（渐亮、错峰、回落到 20%）属于"家居场景编排"，
放在 HA 侧的自动化里更合适——断电重启后仍在、也能在 HA 界面里直接试。
本工具保持薄，只做触发 + 说话。
"""
import asyncio
from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.home.config import get_ha_config, HomeConfigError
from core.utils import showroom_welcome

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_WELCOME_EVENT = showroom_welcome.WELCOME_EVENT
_WELCOME_TEXT = showroom_welcome.WELCOME_TEXT


WELCOME_DEMO_DESC = {
    "type": "function",
    "function": {
        "name": "welcome_demo",
        "description": (
            "展厅「迎宾演示」：触发前厅灯光渐亮（暖光），并播报一句欢迎词。"
            "当用户说'演示迎宾''来个迎宾''欢迎演示''迎宾一下''演示一下欢迎'时调用此工具。"
            "【用途】这是展厅/演示用的迎宾流程（灯自己亮起来 + 打招呼），"
            "触发时机由人决定（通常是有人走到门口时，或主人想当场演示时）。"
            "注意：这不是'回家'场景——家里日常回家的问候由系统自动判断，不要用本工具。"
            "不需要任何参数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


@register_function("welcome_demo", WELCOME_DEMO_DESC, ToolType.SYSTEM_CTL)
async def welcome_demo(conn: "ConnectionHandler"):
    # 1) 触发 HA 侧迎宾灯光自动化（与「真人到达」路径共用同一实现）
    if not await showroom_welcome.fire_ha_event():
        try:
            get_ha_config(force=True)
        except HomeConfigError as e:
            logger.bind(tag=TAG).warning(f"[演示迎宾] HA 配置不可用: {e}")
            return ActionResponse(
                Action.RESPONSE,
                "抱歉，展厅的灯暂时联系不上，等会儿再试。",
                None,
            )
        logger.bind(tag=TAG).warning("[演示迎宾] HA 触发事件失败")
        # 灯没亮成也要说实话——不能像原来那样"控制不了也报成功"
        return ActionResponse(
            Action.RESPONSE,
            "灯这边暂时没响应，欢迎词我先说给你听。",
            None,
        )

    logger.bind(tag=TAG).info("[演示迎宾] 已触发 HA 迎宾灯光 + 播报欢迎词")
    # 2) B 版话术（主人 2026-10-14 选定）：带自我介绍，更适合演示
    return ActionResponse(Action.RESPONSE, _WELCOME_TEXT, None)