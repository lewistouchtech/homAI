"""
飞书通讯录管理 API（家庭成员白名单）
支持添加/删除飞书白名单成员
"""
import json
import os
import shutil
import ssl
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from aiohttp import web

from config.logger import setup_logging

logger = setup_logging()
TAG = "FeishuContactsAPI"

BASE = "https://open.feishu.cn"
SERVER_ROOT = Path(__file__).parent.parent.parent
SETTINGS_PATH = SERVER_ROOT / "data" / ".mcp_server_settings.json"
CONTACTS_PATH = SERVER_ROOT / "data" / ".lark_contacts.json"


def http_json(method: str, url: str, payload: dict | None = None,
              token: str | None = None, timeout: int = 15) -> tuple[int, dict | str]:
    """HTTP 请求辅助函数"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except urllib.error.URLError as e:
        return 0, f"网络不可达: {e.reason}"
    except Exception as e:
        return 0, f"请求异常: {type(e).__name__}: {e}"
    try:
        return 200, json.loads(body)
    except json.JSONDecodeError:
        return 200, body


def get_lark_credentials() -> tuple[str | None, str | None]:
    """从 data/.mcp_server_settings.json 读飞书凭证"""
    if not SETTINGS_PATH.exists():
        return None, None
    try:
        with open(SETTINGS_PATH) as f:
            ms = json.load(f)
        args = (ms.get("mcpServers", {}) or {}).get("lark", {}).get("args") or []
        # args: ["-y", "--registry=...", "@larksuiteoapi/lark-mcp", "mcp", "-a", ID, "-s", SECRET, "-t", TOOLS]
        if len(args) >= 8 and args[4] == "-a" and args[6] == "-s":
            return args[5], args[7]
    except Exception as e:
        logger.bind(tag=TAG).error(f"读飞书凭证失败: {e}")
    return None, None


def get_tenant_token(app_id: str, app_secret: str) -> str | None:
    """获取 tenant_access_token"""
    status, resp = http_json(
        "POST",
        f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if isinstance(resp, dict) and resp.get("code") == 0:
        return resp.get("tenant_access_token")
    return None


class FeishuContactsAPI:
    """飞书通讯录管理 API 处理器"""

    def __init__(self, ws_server):
        self.ws_server = ws_server

    async def get_contacts(self, request):
        """GET /api/feishu/contacts - 获取白名单"""
        try:
            contacts = {}
            if CONTACTS_PATH.exists():
                with open(CONTACTS_PATH) as f:
                    contacts = json.load(f)
            return web.json_response({"success": True, "contacts": contacts})
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取白名单失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def add_contact(self, request):
        """POST /api/feishu/contacts - 添加成员"""
        try:
            body = await request.json()
            mobile = body.get("mobile", "").strip()
            name = body.get("name", "").strip()

            if not mobile or not name:
                return web.json_response({"success": False, "error": "手机号和称呼不能为空"}, status=400)

            # 1. 获取飞书凭证
            app_id, app_secret = get_lark_credentials()
            if not app_id or not app_secret:
                return web.json_response({"success": False, "error": "飞书凭证未配置"}, status=500)

            # 2. 获取 token
            token = get_tenant_token(app_id, app_secret)
            if not token:
                return web.json_response({"success": False, "error": "获取飞书 token 失败"}, status=500)

            # 3. 用手机号解 open_id
            status, resp = http_json(
                "POST",
                f"{BASE}/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id",
                {"mobiles": [mobile]},
                token=token,
            )
            if not isinstance(resp, dict) or resp.get("code") != 0:
                error_msg = str(resp)[:200]
                logger.bind(tag=TAG).error(f"手机号解析失败: {error_msg}")
                return web.json_response({"success": False, "error": f"手机号解析失败: {error_msg}"}, status=400)

            users = (resp.get("data") or {}).get("user_list") or []
            if not users:
                return web.json_response({"success": False, "error": "该手机号在飞书未查到"}, status=400)

            open_id = users[0].get("user_id") or users[0].get("open_id")
            if not open_id:
                return web.json_response({"success": False, "error": "返回里没有 open_id"}, status=500)

            # 4. 读现有文件
            contacts = {}
            if CONTACTS_PATH.exists():
                try:
                    with open(CONTACTS_PATH) as f:
                        contacts = json.load(f) or {}
                except json.JSONDecodeError:
                    contacts = {}

            # 5. 检查是否已存在
            owner_id = (contacts.get("owner") or {}).get("open_id")
            if owner_id == open_id:
                return web.json_response({"success": False, "error": "该用户已是主人（owner）"}, status=400)

            family = contacts.get("family") or []
            for m in family:
                if m.get("open_id") == open_id:
                    return web.json_response({"success": False, "error": f"该用户已在 family 里（{m.get('name')}）"}, status=400)

            # 6. 添加到 family
            family.append({"name": name, "mobile": mobile, "open_id": open_id})
            contacts["family"] = family

            # 7. 写回
            if CONTACTS_PATH.exists():
                shutil.copy2(CONTACTS_PATH, CONTACTS_PATH.with_name(
                    f".lark_contacts.json.bak_{datetime.now():%Y%m%d_%H%M%S}"))

            CONTACTS_PATH.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(CONTACTS_PATH, 0o600)

            logger.bind(tag=TAG).info(f"已添加 {name}（open_id={open_id[:10]}…）到飞书白名单")
            return web.json_response({"success": True, "message": f"已添加 {name}", "open_id": open_id})

        except Exception as e:
            logger.bind(tag=TAG).error(f"添加成员失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def remove_contact(self, request):
        """DELETE /api/feishu/contacts - 移除成员"""
        try:
            body = await request.json()
            index = body.get("index")

            if index is None:
                return web.json_response({"success": False, "error": "缺少 index 参数"}, status=400)

            # 读文件
            contacts = {}
            if CONTACTS_PATH.exists():
                with open(CONTACTS_PATH) as f:
                    contacts = json.load(f) or {}

            family = contacts.get("family") or []
            if index < 0 or index >= len(family):
                return web.json_response({"success": False, "error": "索引越界"}, status=400)

            removed = family.pop(index)
            contacts["family"] = family

            # 写回
            if CONTACTS_PATH.exists():
                shutil.copy2(CONTACTS_PATH, CONTACTS_PATH.with_name(
                    f".lark_contacts.json.bak_{datetime.now():%Y%m%d_%H%M%S}"))

            CONTACTS_PATH.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")

            logger.bind(tag=TAG).info(f"已从飞书白名单移除 {removed.get('name')}")
            return web.json_response({"success": True, "message": f"已移除 {removed.get('name')}"})

        except Exception as e:
            logger.bind(tag=TAG).error(f"移除成员失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)


def register_routes(app, ws_server):
    """注册路由"""
    api = FeishuContactsAPI(ws_server)
    app.router.add_get("/api/feishu/contacts", api.get_contacts)
    app.router.add_post("/api/feishu/contacts", api.add_contact)
    app.router.add_delete("/api/feishu/contacts", api.remove_contact)
    logger.bind(tag=TAG).info("飞书通讯录管理 API 已注册")
