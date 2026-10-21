from enum import Enum


class TextMessageType(Enum):
    """消息类型枚举"""
    HELLO = "hello"
    ABORT = "abort"
    LISTEN = "listen"
    IOT = "iot"
    MCP = "mcp"
    SERVER = "server"
    PING = "ping"
    START_CONVERSATION = "start_conversation"
    STOP_CONVERSATION = "stop_conversation"
    CONFIRM = "confirm"
    # [2026-10-16] 网页端麦克风看门狗/音频实时率自愈事件（此前无处理器，被打成「未知类型消息」）
    MIC_WATCHDOG_EVENT = "mic_watchdog_event"
