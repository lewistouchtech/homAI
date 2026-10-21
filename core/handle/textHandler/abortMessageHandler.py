from typing import Dict, Any

from core.handle.abortHandle import handleAbortMessage
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType


class AbortTextMessageHandler(TextMessageHandler):
    """Abort消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.ABORT

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        # standby=True：网页端"结束"按钮显式结束对话（复位会话回待机）；
        # 缺省 False：打断播报，不打断会话连续性
        await handleAbortMessage(conn, standby=bool(msg_json.get("standby", False)))
