#!/usr/bin/env python3
"""
飞书应用凭证验证 + 配置恢复工具（HomeBrain）

用途：
  1. 验证 App ID / App Secret 是否有效（直连飞书开放平台，纯 HTTP）
  2. 可选：向主人飞书发一条测试消息，验证 im:message 权限真的可用
  3. 可选：验证通过后把 lark-mcp 块写回 data/.mcp_server_settings.json

重要：
  - 本脚本**不经过 node / lark-mcp**，不会触发 macOS 钥匙串弹窗。
  - App Secret 请**不要**写在命令行参数里（会进 shell 历史和 ps 列表）。
    推荐交互输入（隐藏回显），或用环境变量 FEISHU_APP_SECRET。

用法：
    # 只验证凭证（交互式输入 secret）
    python3 tools/check_feishu.py --app-id cli_xxxxxxxx

    # 验证 + 发测试消息（用 data/.lark_contacts.json 里存的 open_id）
    python3 tools/check_feishu.py --app-id cli_xxxxxxxx --send-test

    # 验证通过后顺便写回配置文件（会先备份）
    python3 tools/check_feishu.py --app-id cli_xxxxxxxx --send-test --write-config

    # 免交互（CI / 脚本内调用）
    FEISHU_APP_SECRET=xxx python3 tools/check_feishu.py --app-id cli_xxxxxxxx

    # 只看权限清单，不发消息
    python3 tools/check_feishu.py --app-id cli_xxxxxxxx --check-perms

    # 只测网络可达性（怀疑被墙/代理问题时先跑这个）
    python3 tools/check_feishu.py --ping

凭证在哪拿：飞书开放平台 https://open.feishu.cn/ → 开发者后台 →
自建应用 → 「凭证与基础信息」页 → App ID / App Secret。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://open.feishu.cn"
SERVER_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = SERVER_ROOT / "data" / ".mcp_server_settings.json"
EXAMPLE_PATH = SERVER_ROOT / "data" / ".mcp_server_settings.example.json"
CONTACTS_PATH = SERVER_ROOT / "data" / ".lark_contacts.json"

# 要注册给 LLM 的飞书工具（与 example 模板保持一致）
LARK_TOOL_LIST = "im.v1.message.create,im.v1.message.list,im.v1.chat.create,preset.calendar.default"

# tenant_access_token 常见返回码（仅作人话提示，最终以飞书返回的 msg 为准）
CODE_HINTS = {
    0: "凭证有效 ✅",
    10003: "App ID 无效或不存在 —— 检查是否抄错、应用是否已删除",
    10014: "App Secret 无效 —— 到「凭证与基础信息」页重新复制",
    99991661: "请求参数格式错误",
    99991663: "应用不存在或未发布 —— 自建应用需先提交发布版本",
    99991668: "应用被停用",
}


def http_json(method: str, url: str, payload: dict | None = None,
              token: str | None = None, timeout: int = 15) -> tuple[int, dict | str]:
    """返回 (http_status, 解析后的 json 或原始文本)。"""
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
    except Exception as e:  # noqa: BLE001
        return 0, f"请求异常: {type(e).__name__}: {e}"
    try:
        return 200, json.loads(body)
    except json.JSONDecodeError:
        return 200, body


def mask(secret: str) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:3]}…{secret[-3:]}（{len(secret)} 位，已打码）"


def get_secret(cli_value: str | None, interactive_ok: bool) -> str | None:
    """按 环境变量 > --secret-file > 交互输入 的顺序取 secret；--secret 仅作兜底并警告。"""
    env = os.environ.get("FEISHU_APP_SECRET")
    if env:
        print("  App Secret 来源: 环境变量 FEISHU_APP_SECRET")
        return env.strip()
    if cli_value:
        print("  ⚠️ 你用了 --secret 命令行参数，这个 secret 会留在 shell 历史里，"
              "验证后建议去飞书后台重置一次。")
        return cli_value
    if not interactive_ok:
        return None
    try:
        return getpass.getpass("  请输入 App Secret（输入不回显）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        return None


def ping() -> bool:
    print("【网络可达性】GET https://open.feishu.cn/ …")
    status, body = http_json("GET", f"{BASE}/")
    if status == 0:
        print(f"  ❌ {body}")
        print("     → 直连失败。检查本机网络 / 是否需要代理。")
        return False
    print(f"  ✅ 可达（HTTP {status}，响应 {len(str(body))} 字节）")
    return True


def fetch_token(app_id: str, app_secret: str) -> tuple[bool, str | None, dict]:
    """取 tenant_access_token，成功返回 (True, token, resp)。"""
    status, resp = http_json(
        "POST",
        f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if not isinstance(resp, dict):
        print(f"  ❌ 返回不是 JSON（HTTP {status}）: {str(resp)[:200]}")
        return False, None, {}
    code = resp.get("code")
    msg = resp.get("msg", "")
    hint = CODE_HINTS.get(code, "")
    if code == 0:
        token = resp.get("tenant_access_token") or ""
        print(f"  ✅ code=0 {hint}")
        print(f"     token: {token[:6]}…{token[-4:]}（{len(token)} 位，已打码）")
        exp = resp.get("expire")
        if exp:
            print(f"     有效期: {int(exp) // 60} 分钟")
        return True, token, resp
    print(f"  ❌ code={code}  msg={msg}")
    if hint:
        print(f"     含义: {hint}")
    if code not in CODE_HINTS:
        print("     （该错误码未在本地字典里，以上 msg 为飞书原文，按它处理）")
    return False, None, resp


def check_perms(token: str) -> None:
    """探一把最关键的接口，判断 im 权限是否真的开通了。"""
    print("\n【权限探测】im.v1.chat.list（能否列会话）…")
    status, resp = http_json("GET", f"{BASE}/open-apis/im/v1/chats?page_size=10", token=token)
    if not isinstance(resp, dict):
        print(f"  ❌ 返回异常（HTTP {status}）: {str(resp)[:160]}")
        return
    code = resp.get("code")
    if code == 0:
        chats = (resp.get("data") or {}).get("items") or []
        print(f"  ✅ im 权限可用，当前机器人在 {len(chats)} 个会话中")
        for c in chats[:5]:
            print(f"     · {c.get('name') or '(未命名会话)'}")
        if not chats:
            print("     ⚠️ 机器人不在任何群里 —— 单聊需要你先在飞书里主动给它发一条消息")
    else:
        print(f"  ⚠️ code={code} msg={resp.get('msg')}")
        print("     常见原因：未开通 im:message 权限，或应用版本未发布/未审批。")
        print("     → 到 开放平台 → 权限管理 开通 `im:message`，然后创建版本并发布。")


def load_owner_open_id() -> tuple[str | None, str | None]:
    if not CONTACTS_PATH.exists():
        return None, None
    try:
        data = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    owner = data.get("owner") or {}
    return owner.get("open_id"), owner.get("name")


def send_test(token: str) -> bool:
    open_id, name = load_owner_open_id()
    if not open_id:
        print("  ❌ 找不到 open_id（data/.lark_contacts.json 缺失或无 owner 字段）")
        print("     可先手动发消息给机器人，或用 --refresh-openid 重解。")
        return False
    print(f"\n【发测试消息】给 {name or '主人'} <{open_id[:10]}…>")
    status, resp = http_json(
        "POST",
        f"{BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
        {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps(
                {"text": "HomeBrain 飞书连通性测试 ✅ —— 凭证与 im:message 权限均正常"},
                ensure_ascii=False,
            ),
        },
        token=token,
    )
    if isinstance(resp, dict) and resp.get("code") == 0:
        mid = (resp.get("data") or {}).get("message_id", "")
        print(f"  ✅ 发送成功 message_id={mid[:12]}…  去飞书查收")
        return True

    code = resp.get("code") if isinstance(resp, dict) else None
    print(f"  ❌ 发送失败: {str(resp)[:220]}")
    if code == 99992361 or "cross app" in str(resp):
        print("     原因：**open_id 按应用隔离** —— 通讯录里存的 open_id 是别的应用解出来的，")
        print("     换 App 后失效。用当前凭证重解即可：")
        print("       python3 tools/check_feishu.py --app-id <ID> --refresh-openid")
    elif code in (99991672, 230002, 234001):
        print("     多为权限未开通 / 未发布版本 / 机器人无法主动私聊（需对方先发起会话）")
    return False


def refresh_open_id(token: str, app_id: str, mobile: str | None = None) -> bool:
    """用当前应用的凭证把手机号重解成 open_id，并回写 .lark_contacts.json。"""
    contacts = {}
    if CONTACTS_PATH.exists():
        try:
            contacts = json.loads(CONTACTS_PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            contacts = {}
    owner = contacts.get("owner") or {}
    mobile = mobile or owner.get("mobile")
    if not mobile:
        try:
            mobile = input("  请输入主人手机号（含国家区号，如 18910824587）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
    if not mobile:
        print("  ❌ 没有手机号，无法解 open_id")
        return False

    print(f"\n【重解 open_id】用当前 App 解手机号 **{mobile[:3]}****{mobile[-2:]}** …")
    status, resp = http_json(
        "POST",
        f"{BASE}/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id",
        {"mobiles": [mobile]},
        token=token,
    )
    if not isinstance(resp, dict):
        print(f"  ❌ 返回异常: {str(resp)[:160]}")
        return False

    code = resp.get("code")
    if code != 0:
        print(f"  ❌ code={code} msg={str(resp.get('msg'))[:160]}")
        if code == 99991672:
            print("     缺权限 `contact:user.id:readonly`。点这个链接直接申请开通：")
            print(f"       {BASE}/app/{app_id}/auth?q=contact:user.id:readonly"
                  "&op_from=openapi&token_type=tenant")
            print("     开通后需「创建版本 → 发布」，自建应用一般自动通过，再重跑本命令。")
        return False

    users = (resp.get("data") or {}).get("user_list") or []
    if not users:
        print("  ❌ 返回空 —— 该手机号在飞书没查到（或区号格式不对，试试 +86 前缀）")
        return False

    new_open_id = users[0].get("user_id") or users[0].get("open_id")  # batch_get_id 在 user_id_type=open_id 时字段名仍叫 user_id
    old = owner.get("open_id")
    if not new_open_id:
        print("  ❌ 返回里没有 open_id")
        return False
    if old == new_open_id:
        print(f"  ℹ️ open_id 未变化（{new_open_id[:10]}…）—— 那发送失败另有原因")
        return True

    print(f"  ✅ 解到新 open_id: {new_open_id[:10]}…（旧值 {str(old)[:10]}… 已失效）")
    if not CONTACTS_PATH.parent.exists():
        print("  ❌ data/ 目录不存在，未回写")
        return False
    if CONTACTS_PATH.exists():
        shutil.copy2(CONTACTS_PATH, CONTACTS_PATH.with_name(
            f".lark_contacts.json.bak_{datetime.now():%Y%m%d_%H%M%S}"))
        print("     旧文件已备份")
    owner.update({"mobile": mobile, "open_id": new_open_id})
    owner.setdefault("name", "主人")
    contacts["owner"] = owner
    CONTACTS_PATH.write_text(json.dumps(contacts, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    os.chmod(CONTACTS_PATH, 0o600)
    print(f"  ✅ 已回写 {CONTACTS_PATH.name}（0600）")
    return True


def add_family_member(token: str, app_id: str, mobile: str, name: str) -> bool:
    """用手机号解 open_id，添加到 .lark_contacts.json 的 family 数组。"""
    print(f"\n【添加家庭成员】{name} <{mobile[:3]}****{mobile[-2:]}…>")

    # 1. 用手机号解 open_id
    status, resp = http_json(
        "POST",
        f"{BASE}/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id",
        {"mobiles": [mobile]},
        token=token,
    )
    if not isinstance(resp, dict) or resp.get("code") != 0:
        print(f"  ❌ 手机号解析失败: {str(resp)[:200]}")
        if isinstance(resp, dict) and resp.get("code") == 99991672:
            print("     缺权限 contact:user.id:readonly，先去飞书后台开通")
        return False

    users = (resp.get("data") or {}).get("user_list") or []
    if not users:
        print("  ❌ 该手机号在飞书未查到")
        return False

    open_id = users[0].get("user_id") or users[0].get("open_id")
    if not open_id:
        print("  ❌ 返回里没有 open_id")
        return False

    # 2. 读现有文件
    contacts = {}
    if CONTACTS_PATH.exists():
        try:
            contacts = json.loads(CONTACTS_PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            contacts = {}

    # 3. 检查是否已存在（owner 或 family 里）
    owner_id = (contacts.get("owner") or {}).get("open_id")
    if owner_id == open_id:
        print(f"  ℹ️ 该用户已是主人（owner），无需重复添加")
        return True

    family = contacts.get("family") or []
    for m in family:
        if m.get("open_id") == open_id:
            print(f"  ℹ️ 该用户已在 family 里（{m.get('name')}），无需重复添加")
            return True

    # 4. 添加到 family
    family.append({"name": name, "mobile": mobile, "open_id": open_id})
    contacts["family"] = family

    # 5. 写回
    if CONTACTS_PATH.exists():
        shutil.copy2(CONTACTS_PATH, CONTACTS_PATH.with_name(
            f".lark_contacts.json.bak_{datetime.now():%Y%m%d_%H%M%S}"))
        print("     旧文件已备份")

    CONTACTS_PATH.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONTACTS_PATH, 0o600)
    print(f"  ✅ 已添加 {name}（open_id={open_id[:10]}…）到 family")
    print(f"     重启服务后生效：bash restart.sh")
    return True


def write_config(app_id: str, app_secret: str, dry_run: bool = False) -> bool:
    """把 lark 块写回 mcp_server_settings.json，保留其他已存在的 server。"""
    if not SETTINGS_PATH.parent.exists():
        print(f"  ❌ 目录不存在: {SETTINGS_PATH.parent}")
        return False

    existing = {}
    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            print("  ⚠️ 现有 settings 文件不是合法 JSON，将重建（旧内容已备份）")

    servers = dict(existing.get("mcpServers") or {})
    lark_block = {
        "command": "npx",
        "args": [
            "-y", "--registry=https://registry.npmmirror.com",
            "@larksuiteoapi/lark-mcp", "mcp",
            "-a", app_id,
            "-s", app_secret,
            "-t", LARK_TOOL_LIST,
        ],
    }
    if "lark" in servers:
        print("  ℹ️ 已存在 lark 配置，将被覆盖")
    servers["lark"] = lark_block
    payload = {**existing, "mcpServers": servers}

    if dry_run:
        preview = json.dumps(payload, ensure_ascii=False, indent=2)
        print("  【dry-run】将写入以下内容（secret 已打码）：")
        print("  " + preview.replace(app_secret, mask(app_secret)).replace("\n", "\n  "))
        return True

    if SETTINGS_PATH.exists():
        bak = SETTINGS_PATH.with_name(
            f".mcp_server_settings.json.bak_{datetime.now():%Y%m%d_%H%M%S}"
        )
        shutil.copy2(SETTINGS_PATH, bak)
        print(f"  已备份旧配置 → {bak.name}")

    SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    os.chmod(SETTINGS_PATH, 0o600)  # 内含 secret，收紧权限
    print(f"  ✅ 已写入 {SETTINGS_PATH}（权限 0600）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="飞书凭证验证 / 配置恢复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1],
    )
    ap.add_argument("--app-id", help="飞书 App ID（cli_ 开头）")
    ap.add_argument("--secret", help="App Secret（不推荐，会进 shell 历史；优先用环境变量或交互输入）")
    ap.add_argument("--send-test", action="store_true", help="验证通过后发一条测试消息")
    ap.add_argument("--check-perms", action="store_true", help="探测 im 权限（不发消息）")
    ap.add_argument("--write-config", action="store_true", help="验证通过后写回 mcp_server_settings.json")
    ap.add_argument("--dry-run", action="store_true", help="配合 --write-config：只预览不落盘")
    ap.add_argument("--ping", action="store_true", help="只测飞书域名可达性")
    ap.add_argument("--refresh-openid", action="store_true", help="用当前 App 重解主人手机号→open_id，回写 .lark_contacts.json")
    ap.add_argument("--add-family", nargs=2, metavar=("MOBILE", "NAME"), help="添加家庭成员（手机号+称呼），如 --add-family 13800138000 太太")
    args = ap.parse_args()

    print("=" * 62)
    print(" HomeBrain · 飞书凭证验证工具")
    print("=" * 62)

    if args.ping and not args.app_id:
        return 0 if ping() else 1

    if not ping():
        print("\n网络不通，后续步骤无法进行。先解决网络/代理。")
        return 1

    if not args.app_id:
        try:
            args.app_id = input("  请输入 App ID（cli_ 开头）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            return 1
    if not args.app_id:
        print("  ❌ 缺少 App ID")
        return 1

    secret = get_secret(args.secret, interactive_ok=sys.stdin.isatty())
    if not secret:
        print("  ❌ 缺少 App Secret。可用：交互输入 / FEISHU_APP_SECRET 环境变量 / --secret")
        return 1

    print(f"\n【凭证校验】app_id={args.app_id}  secret={mask(secret)}")
    ok, token, _ = fetch_token(args.app_id, secret)
    if not ok:
        print("\n❌ 凭证无效，未做任何写入。请核对后重试。")
        return 2

    if args.check_perms and token:
        check_perms(token)

    sent = None
    if args.send_test and token:
        sent = send_test(token)

    if args.refresh_openid and token:
        refresh_open_id(token, args.app_id)

    if args.add_family and token:
        mobile, name = args.add_family
        add_family_member(token, args.app_id, mobile, name)

    if args.write_config:
        if args.send_test and sent is False:
            print("\n⚠️ 凭证有效但测试消息失败 —— 为避免配上去仍然不可用，已跳过写入。")
            print("   确认要强行写入的话：去掉 --send-test 单独跑 --write-config。")
            return 3
        print("\n【写回配置】")
        if not write_config(args.app_id, secret, dry_run=args.dry_run):
            return 1
        if not args.dry_run:
            print("\n   下一步：重启服务生效 ——")
            print("     cd server/homebrain-server && bash restart.sh")
            print("   然后确认 8 个飞书工具被注册：")
            print("     grep -a \"lark\\|mcp\" tmp/server.log | tail")
        else:
            print("\n   （dry-run，未落盘）")

    print("\n" + "=" * 62)
    print(" 结论：凭证有效 ✅" if ok else " 结论：凭证无效 ❌")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())