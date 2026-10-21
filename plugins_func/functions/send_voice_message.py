"""
语音电话通知插件（PushPlus）
让伊娃可以通过语音电话呼叫主人（0.3元/条），用于安防报警、紧急通知等必须确保送达的场景
凭证从 data/.notification_channels.json 的 wechat 通道读取（PushPlus 各渠道共用 token）
"""
from plugins_func.register import register_function, ToolType, Action, ActionResponse
from core.utils import notify_service

TAG = "SendVoiceMessage"


async def send_voice_message(conn, message: str) -> ActionResponse:
    """发起语音电话呼叫。message: 电话里播报的内容（尽量简短，50字内）。"""
    try:
        if not message or not message.strip():
            return ActionResponse(action=Action.ERROR, response="语音内容不能为空")
        ok, msg = await notify_service.send_voice(message.strip()[:100])
        if ok:
            return ActionResponse(action=Action.RESPONSE, response=f"已发起语音电话呼叫，将播报：{message.strip()[:50]}")
        return ActionResponse(action=Action.ERROR, response=msg)
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"发起语音电话出错: {type(e).__name__}: {e}")


send_voice_message_function_desc = {
    "type": "function",
    "function": {
        "name": "send_voice_message",
        "description": "给主人拨打语音电话并播报消息（PushPlus，0.3元/条）。仅用于紧急情况：安防报警、漏水烟雾告警、重要事项必须确保主人收到时。日常提醒请用 send_wechat_message。message 是电话里播报的内容，要简短口语化。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "电话里播报的内容（必填，简短，50字以内）"
                }
            },
            "required": ["message"]
        }
    }
}


@register_function("send_voice_message", send_voice_message_function_desc, ToolType.SYSTEM_CTL)
async def send_voice_message_tool(conn, message: str = ""):
    return await send_voice_message(conn, message)
