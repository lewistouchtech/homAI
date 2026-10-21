import json
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils import confirm_gate

TAG = __name__


class ConfirmMessageHandler(TextMessageHandler):
    """人确认回复处理器（危险操作确认闸门 · 网页端点击入口）

    网页端确认条点击后回发 {"type":"confirm","request_id":"...","approved":true|false}，
    此处交给 :func:`core.utils.confirm_gate.resolve_by_request_id` 校验并执行。

    与语音路径并存：主人也可以不点屏幕，下一轮直接说「发吧」，
    由 LLM 再次调用带 confirmed=true 的工具，闸门按轮次校验放行。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.CONFIRM

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        request_id = msg_json.get("request_id") or ""
        approved = bool(msg_json.get("approved", False))

        if not request_id:
            conn.logger.bind(tag=TAG).warning(
                "收到确认回复但缺少 request_id，已忽略"
            )
            return

        ok, msg = await confirm_gate.resolve_by_request_id(conn, request_id, approved)
        if ok:
            conn.logger.bind(tag=TAG).info(f"网页确认已处理: request_id={request_id}, {msg}")
            # 已确认则把结果播报给主人；已取消也播报，明确告知状态
            await self._speak(conn, msg)
        else:
            conn.logger.bind(tag=TAG).warning(
                f"网页确认处理失败: request_id={request_id}, {msg}"
            )

    async def _speak(self, conn, text: str) -> None:
        """把确认结果用 TTS 播报，让主人不看屏幕也知道结果。"""
        try:
            from core.handle.sendAudioHandle import send_tts_message

            await send_tts_message(conn, "TTS_FINISH", text)
        except Exception as e:  # noqa: BLE001 — 播报失败不能影响确认状态
            conn.logger.bind(tag=TAG).warning(f"确认结果播报失败: {e}")