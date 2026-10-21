# -*- coding: utf-8 -*-
"""把今日资讯全屏展示到家庭看板中央舞台

用户对伊娃说"打开新闻""显示新闻""看看今日资讯"时，LLM 调用本工具：
向所有已连接的看板（Web 终端）广播 news_full_view 指令，
看板中央舞台区域展示新闻列表卡片，填满整个舞台。
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


SHOW_NEWS_FULL_DESC = {
    "type": "function",
    "function": {
        "name": "show_news_full",
        "description": (
            "把今日新闻资讯全屏展示到家庭看板屏幕中央的窗口里，"
            "以卡片列表形式展示多条新闻。这是**打开窗口**的工具，不是语音播报工具。"
            "当用户说'打开新闻''显示新闻''看看今日资讯''打开资讯''打开今日新闻'"
            "'给我看看新闻''把新闻放到屏幕上''主窗口显示新闻'时调用此工具。"
            "注意：用户只是想'听'一条新闻时不要用本工具。"
            # [2026-10-12 主人实测修正] 模型会「先关再开」，用户看到窗口闪一下就没了。
            "【窗口规则·重要】如果新闻窗口**已经打开着**，用户只是想看新闻或翻内容"
            "（如'下一条''第5条''再看看'），**不要**调用本工具，也不要先关再开 —— "
            "那样窗口会闪一下，用户正看着的内容就没了。已经打开时直接用 "
            "get_news_from_newsnow 播报即可。"
            "不需要任何参数，工具会自动获取当天最新新闻。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


@register_function("show_news_full", SHOW_NEWS_FULL_DESC, ToolType.SYSTEM_CTL)
async def show_news_full(conn: "ConnectionHandler"):
    # 获取新闻数据
    try:
        from core.api.news_api import get_realtime_news
        news_list = await get_realtime_news()
    except Exception as e:
        logger.bind(tag=TAG).error(f"[新闻全屏] 获取新闻失败: {e}")
        return ActionResponse(Action.REQLLM, "获取新闻数据时出错了，请稍后再试。", None)

    if not news_list:
        return ActionResponse(Action.REQLLM, "暂时没有可用的新闻数据。", None)

    # 取前 15 条展示
    display_news = news_list[:15]

    server = get_websocket_server_instance()
    if server is None:
        logger.bind(tag=TAG).warning("[新闻全屏] WebSocket 服务实例未就绪，无法广播")
        return ActionResponse(Action.REQLLM, "看板连接服务还没有启动，新闻打不开，请稍后再试。", None)

    async with server.connections_lock:
        conns = list(server.active_connections)
    payload = json.dumps(
        {"type": "news_full_view", "action": "open", "news": display_news},
        ensure_ascii=False,
    )
    sent = 0
    for ws in conns:
        try:
            await ws.send(payload)
            sent += 1
        except Exception:
            pass

    logger.bind(tag=TAG).info(f"[新闻全屏] {len(display_news)} 条新闻已广播（送达 {sent} 个连接）")
    if sent == 0:
        return ActionResponse(Action.REQLLM, "家庭看板目前不在线，新闻没地方展示。请确认看板页面已打开。", None)
    return ActionResponse(
        Action.REQLLM,
        f"已把今日资讯展示到家庭看板上了，共 {len(display_news)} 条新闻。",
        None,
    )
