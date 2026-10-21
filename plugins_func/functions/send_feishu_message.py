"""
飞书消息发送插件
让伊娃可以通过飞书主动给主人发消息（如提醒、通知、留言）
凭证从 data/.mcp_server_settings.json 读取，收件人默认是白名单里的主人
"""
import json
import os
from pathlib import Path

from plugins_func.register import register_function, ToolType, Action, ActionResponse

TAG = "SendFeishuMessage"

_SERVER_ROOT = Path(__file__).parent.parent.parent
_CONTACTS_PATH = _SERVER_ROOT / "data" / ".lark_contacts.json"
_SETTINGS_PATH = _SERVER_ROOT / "data" / ".mcp_server_settings.json"


def _get_credentials() -> tuple[str | None, str | None]:
    """从 data/.mcp_server_settings.json 读飞书凭证"""
    if not _SETTINGS_PATH.exists():
        return None, None
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            ms = json.load(f)
        args = (ms.get("mcpServers", {}) or {}).get("lark", {}).get("args") or []
        # args: ["-y", "--registry=...", "@larksuiteoapi/lark-mcp", "mcp", "-a", ID, "-s", SECRET, "-t", TOOLS]
        if len(args) >= 8 and args[4] == "-a" and args[6] == "-s":
            return args[5], args[7]
    except Exception:
        pass
    return None, None


def _get_owner_open_id() -> str | None:
    """从 data/.lark_contacts.json 读主人的 open_id"""
    if not _CONTACTS_PATH.exists():
        return None
    try:
        with open(_CONTACTS_PATH, encoding="utf-8") as f:
            contacts = json.load(f)
        return (contacts.get("owner") or {}).get("open_id")
    except Exception:
        return None


async def send_feishu_message(conn, message: str, receive_id: str = None) -> ActionResponse:
    """发送飞书消息。message: 消息内容；receive_id: 可选，指定接收人 open_id（默认发给主人）。"""
    try:
        if not message or not message.strip():
            return ActionResponse(action=Action.ERROR, response="消息内容不能为空")

        app_id, app_secret = _get_credentials()
        if not app_id or not app_secret:
            return ActionResponse(action=Action.ERROR, response="飞书凭证未配置")

        open_id = receive_id or _get_owner_open_id()
        if not open_id:
            return ActionResponse(action=Action.ERROR, response="找不到收件人（主人 open_id 未配置）")

        from core.utils.feishu_client import FeishuClient
        client = FeishuClient(app_id, app_secret)
        try:
            ok = await client.send_text(open_id, message.strip())
        finally:
            await client.close()

        if ok:
            return ActionResponse(action=Action.RESPONSE, response=f"已通过飞书发送消息：{message.strip()[:50]}")
        return ActionResponse(action=Action.ERROR, response="飞书消息发送失败，请检查服务日志")
    except Exception as e:
        return ActionResponse(action=Action.ERROR, response=f"发送飞书消息出错: {type(e).__name__}: {e}")


send_feishu_message_function_desc = {
    "type": "function",
    "function": {
        "name": "send_feishu_message",
        "description": "通过飞书给主人发送文字消息。用于主动提醒、通知、留言，或主人要求发消息时。message 参数是要发送的内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "要发送的消息内容（必填）"
                },
                "receive_id": {
                    "type": "string",
                    "description": "接收人 open_id（可选，默认发给主人）"
                }
            },
            "required": ["message"]
        }
    }
}


@register_function("send_feishu_message", send_feishu_message_function_desc, ToolType.SYSTEM_CTL)
async def send_feishu_message_tool(conn, message: str = "", receive_id: str = None):
    return await send_feishu_message(conn, message, receive_id)
