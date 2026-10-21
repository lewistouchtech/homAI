import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__


def _parse_input_channel(input_channel_raw) -> int:
    """
    解析前端传来的 input_channel 为通道号。
    前端格式: "${deviceId}_in_${x}" 或数字字符串/整数
    数据库通道 x → API 通道 x-1
    """
    if input_channel_raw is None:
        return 0

    # 如果是数字，直接处理
    if isinstance(input_channel_raw, int):
        return max(0, input_channel_raw - 1)

    # 如果是字符串，尝试提取数字
    channel_str = str(input_channel_raw).strip()
    if not channel_str:
        return 0

    # 匹配 "_in_X" 或 "_out_X" 格式
    match = re.search(r'_(?:in|out)_(\d+)$', channel_str)
    if match:
        db_channel = int(match.group(1))
        return max(0, db_channel - 1)

    # 尝试直接解析为整数
    try:
        db_channel = int(channel_str)
        return max(0, db_channel - 1)
    except ValueError:
        return 0


class StartConversationMessageHandler(TextMessageHandler):
    """开始连续对话消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.START_CONVERSATION

    async def handle(self, conn: "ConnectionHandler", message: dict) -> None:
        """处理开始连续对话消息"""
        conn.logger.bind(tag=TAG).info(f"收到开始连续对话消息: {message}")

        # 获取参数并解析 input_channel
        input_channel_raw = message.get("input_channel", 0)
        input_channel = _parse_input_channel(input_channel_raw)
        output_channel = message.get("output_channel", "local")
        sample_rate = message.get("sample_rate", 48000)
        room = message.get("room", "default")

        conn.logger.bind(tag=TAG).info(
            f"通道解析: raw='{input_channel_raw}' → input_channel={input_channel}"
        )

        # 设置连续对话模式
        conn.continuous_conversation_mode = True
        conn.continuous_input_channel = input_channel
        conn.continuous_output_channel = output_channel
        conn.continuous_sample_rate = sample_rate


        # 发送确认响应
        response = {
            "type": "conversation_started",
            "input_channel": input_channel,
            "output_channel": output_channel,
            "sample_rate": sample_rate,
            "room": room,
            "message": "连续对话模式已启动"
        }
        await conn.websocket.send(json.dumps(response))

        conn.logger.bind(tag=TAG).info(
            f"连续对话模式已启动: input={input_channel}, output={output_channel}, sample_rate={sample_rate}"
        )


class StopConversationMessageHandler(TextMessageHandler):
    """停止连续对话消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.STOP_CONVERSATION

    async def handle(self, conn: "ConnectionHandler", message: dict) -> None:
        """处理停止连续对话消息"""
        conn.logger.bind(tag=TAG).info(f"收到停止连续对话消息: {message}")

        # 流式模式下，结束流式识别
        from core.providers.asr.dto.dto import InterfaceType
        import asyncio
        if conn.asr.interface_type == InterfaceType.STREAM:
            # 使用固定的 device_id 作为 session key
            device_key = getattr(conn, 'hello_device_id', None) or conn.device_id or conn.session_id
            conn.logger.bind(tag=TAG).info(f"流式模式结束，调用 finish_streaming, device_key={device_key}")
            if hasattr(conn.asr, 'finish_streaming'):
                await conn.asr.finish_streaming(device_key, conn)
            else:
                asyncio.create_task(conn.asr._send_stop_request())

        # 关闭连续对话模式
        conn.continuous_conversation_mode = False
        conn.continuous_input_channel = None
        conn.continuous_output_channel = None


        # 发送确认响应
        response = {
            "type": "conversation_stopped",
            "message": "连续对话模式已停止"
        }
        await conn.websocket.send(json.dumps(response))

        conn.logger.bind(tag=TAG).info("连续对话模式已停止")
