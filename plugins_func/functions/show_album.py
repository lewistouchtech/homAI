# -*- coding: utf-8 -*-
"""把家庭相册展示到家庭看板中央舞台

用户对伊娃说"打开家庭相册""看看相册"时，LLM 调用本工具：
向所有已连接的看板（Web 终端）广播 album_view 指令，看板中央弹出相册窗口。
"""
import json
from typing import TYPE_CHECKING

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.api.config_api import get_websocket_server_instance

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


SHOW_ALBUM_DESC = {
    "type": "function",
    "function": {
        "name": "show_album",
        "description": (
            "把家庭相册展示到家庭看板屏幕中央的窗口里。"
            "当用户说'打开家庭相册''看看相册''打开相册''展示照片'时调用此工具。"
            "不需要任何参数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


@register_function("show_album", SHOW_ALBUM_DESC, ToolType.SYSTEM_CTL)
async def show_album(conn: "ConnectionHandler"):
    server = get_websocket_server_instance()
    if server is None:
        logger.bind(tag=TAG).warning("[家庭相册] WebSocket 服务实例未就绪，无法广播")
        return ActionResponse(Action.REQLLM, "看板连接服务还没有启动，相册打不开，请稍后再试。", None)

    async with server.connections_lock:
        conns = list(server.active_connections)
    payload = json.dumps(
        {"type": "album_view", "action": "open"},
        ensure_ascii=False)
    sent = 0
    for ws in conns:
        try:
            await ws.send(payload)
            sent += 1
        except Exception:
            pass

    logger.bind(tag=TAG).info(f"[家庭相册] 相册指令已广播（送达 {sent} 个连接）")
    if sent == 0:
        return ActionResponse(Action.REQLLM, "家庭看板目前不在线，相册没地方展示。请确认看板页面已打开。", None)
    return ActionResponse(Action.REQLLM, "已把家庭相册展示到家庭看板上了。", None)
