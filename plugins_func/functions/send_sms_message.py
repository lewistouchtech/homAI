"""
短信发送插件（阿里云短信服务）
让伊娃可以给主人手机发短信（重要提醒、安防报警等紧急通知）
凭证从 data/.notification_channels.json 的 sms 通道读取
"""
import json

from plugins_func.register import register_function, ToolType, Action, ActionResponse
from core.utils import notify_service

TAG = "SendSmsMessage"


async def send_sms_message(conn, message: str, phone: str = None) -> ActionResponse:
    """发送短信。message: 短信内容（填入模板 ${content} 变量）；phone: 可选，指定其他接收手机号。"""
    try:
        if not message or not message.strip():
            return ActionResponse(action=Action.ERROR, response="短信内容不能为空")
        template_param = json.dumps({"content": message.strip()[:450]}, ensure_ascii=False)
        ok, msg = await notify_service.send_sms(message.strip(), phone, template_param)
        if ok:
            return ActionResponse(action=Action.RESPONSE, response=f"已发送短信：{message.strip()[:50]}")
        return ActionResponse(action=Action.ERROR, response=msg)
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"发送短信出错: {type(e).__name__}: {e}")


send_sms_message_function_desc = {
    "type": "function",
    "function": {
        "name": "send_sms_message",
        "description": "给主人手机发送短信。默认走 PushPlus（0.1元/条，免审核，发给后台绑定手机号）；后台「通知通道」短信卡片选阿里云则走阿里云短信。用于紧急通知、安防报警等重要场景。message 参数是短信内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "要发送的短信内容（必填，会填入模板 ${content} 变量）"
                },
                "phone": {
                    "type": "string",
                    "description": "接收手机号（可选，默认使用配置的号码）"
                }
            },
            "required": ["message"]
        }
    }
}


@register_function("send_sms_message", send_sms_message_function_desc, ToolType.SYSTEM_CTL)
async def send_sms_message_tool(conn, message: str = "", phone: str = None):
    return await send_sms_message(conn, message, phone)
