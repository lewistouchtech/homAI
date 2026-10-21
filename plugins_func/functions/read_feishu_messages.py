"""
飞书消息读取插件
让伊娃可以查看飞书里主人私聊的最新消息（"查一下飞书的消息""飞书有什么消息"）

凭证与主人 open_id 来源与 send_feishu_message 一致；
私聊 chat_id 首次解析后缓存到 data/.lark_chat_cache.json（避免每次调 API 查找）。
"""
import json
import time
from pathlib import Path

from plugins_func.register import register_function, ToolType, Action, ActionResponse

TAG = "ReadFeishuMessages"

_SERVER_ROOT = Path(__file__).parent.parent.parent
_CONTACTS_PATH = _SERVER_ROOT / "data" / ".lark_contacts.json"
_SETTINGS_PATH = _SERVER_ROOT / "data" / ".mcp_server_settings.json"
_CHAT_CACHE_PATH = _SERVER_ROOT / "data" / ".lark_chat_cache.json"


def _get_credentials() -> tuple[str | None, str | None]:
    """从 data/.mcp_server_settings.json 读飞书凭证（与 send_feishu_message 一致）"""
    if not _SETTINGS_PATH.exists():
        return None, None
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            ms = json.load(f)
        args = (ms.get("mcpServers", {}) or {}).get("lark", {}).get("args") or []
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


def _load_cached_chat_id(owner_open_id: str) -> str | None:
    try:
        if _CHAT_CACHE_PATH.exists():
            with open(_CHAT_CACHE_PATH, encoding="utf-8") as f:
                cache = json.load(f) or {}
            return cache.get(owner_open_id)
    except Exception:
        pass
    return None


def _save_cached_chat_id(owner_open_id: str, chat_id: str) -> None:
    try:
        cache = {}
        if _CHAT_CACHE_PATH.exists():
            with open(_CHAT_CACHE_PATH, encoding="utf-8") as f:
                cache = json.load(f) or {}
        cache[owner_open_id or "owner"] = chat_id
        _CHAT_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


async def _resolve_p2p_chat_id(client, owner_open_id: str) -> str | None:
    """找与主人的单聊 chat_id：本地缓存 → chats 列表 + 成员匹配 → 唯一 p2p 退化"""
    cached = _load_cached_chat_id(owner_open_id)
    if cached:
        return cached
    chats = await client.list_p2p_chats()
    if not chats:
        return None
    if owner_open_id:
        for c in chats:
            cid = c.get("chat_id")
            if not cid:
                continue
            members = await client.get_chat_members(cid)
            if owner_open_id in members:
                _save_cached_chat_id(owner_open_id, cid)
                return cid
    if len(chats) == 1:
        cid = chats[0].get("chat_id")
        if cid:
            _save_cached_chat_id(owner_open_id or "owner", cid)
        return cid
    return None


async def read_feishu_messages(conn, count: int = 5) -> ActionResponse:
    """读飞书最新消息。count: 返回最近多少条（1-20，默认 5）。"""
    try:
        app_id, app_secret = _get_credentials()
        if not app_id or not app_secret:
            return ActionResponse(action=Action.ERROR, response="飞书凭证未配置")
        owner_open_id = _get_owner_open_id() or ""

        from core.utils.feishu_client import FeishuClient

        client = FeishuClient(app_id, app_secret)
        try:
            chat_id = await _resolve_p2p_chat_id(client, owner_open_id)
            if not chat_id:
                return ActionResponse(
                    action=Action.ERROR,
                    response="找不到与主人的飞书单聊会话（请先在飞书里给伊娃发一条消息）",
                )
            n = max(1, min(int(count or 5), 20))
            messages = await client.list_messages(chat_id, page_size=n)
        finally:
            await client.close()

        if not messages:
            return ActionResponse(action=Action.RESPONSE, response="飞书里最近没有消息。")

        lines = []
        for m in messages[:n]:
            ts = (
                time.strftime("%m-%d %H:%M", time.localtime((m.get("time_ms") or 0) / 1000))
                if m.get("time_ms")
                else "?"
            )
            who = "主人" if (owner_open_id and m.get("sender_id") == owner_open_id) else "伊娃"
            lines.append(f"[{ts}] {who}：{m.get('text', '')[:80]}")
        report = (
            "根据下列数据，用中文回答用户的飞书消息查询：\n\n"
            f"最近 {len(lines)} 条飞书消息（最新在前）：\n" + "\n".join(lines) +
            "\n\n(请自然地向用户转述这些消息的关键内容，不要逐条机械朗读)"
        )
        return ActionResponse(Action.REQLLM, report, None)
    except Exception as e:
        return ActionResponse(
            action=Action.ERROR, response=f"读取飞书消息出错: {type(e).__name__}: {e}"
        )


read_feishu_messages_function_desc = {
    "type": "function",
    "function": {
        "name": "read_feishu_messages",
        "description": "读取飞书里主人单聊的最新消息。用于回答「查一下飞书的消息」「飞书有什么消息」等查询，会读出最近几条消息的内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "读取最近多少条消息（1-20，默认 5）"
                }
            },
            "required": []
        }
    }
}


@register_function(
    "read_feishu_messages", read_feishu_messages_function_desc, ToolType.SYSTEM_CTL
)
async def read_feishu_messages_tool(conn, count: int = 5):
    return await read_feishu_messages(conn, count)
