"""
微信消息发送插件（PushPlus）
让伊娃可以通过微信主动给主人发消息（提醒、通知、留言）
凭证从 data/.notification_channels.json 的 wechat 通道读取
"""
from plugins_func.register import register_function, ToolType, Action, ActionResponse
from core.utils import notify_service

TAG = "SendWechatMessage"


async def send_wechat_message(conn, message: str) -> ActionResponse:
    """发送微信消息。message: 要发送的消息内容。"""
    try:
        if not message or not message.strip():
            return ActionResponse(action=Action.ERROR, response="消息内容不能为空")
        ok, msg = await notify_service.send_wechat(message.strip())
        if ok:
            return ActionResponse(action=Action.RESPONSE, response=f"已通过微信推送消息：{message.strip()[:50]}")
        return ActionResponse(action=Action.ERROR, response=msg)
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"发送微信消息出错: {type(e).__name__}: {e}")


send_wechat_message_function_desc = {
    "type": "function",
    "function": {
        "name": "send_wechat_message",
        "description": "通过微信给主人发送消息。默认走微信 ClawBot（消息直接出现在微信聊天窗口）；后台通知通道微信卡片可切换为服务号推送。用于主动提醒、通知、留言，或主人要求发微信时。message 参数是要发送的内容。需要先在后台「通知通道」页面配置 PushPlus Token。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "要发送的消息内容（必填）"
                }
            },
            "required": ["message"]
        }
    }
}


@register_function("send_wechat_message", send_wechat_message_function_desc, ToolType.SYSTEM_CTL)
async def send_wechat_message_tool(conn, message: str = ""):
    return await send_wechat_message(conn, message)
