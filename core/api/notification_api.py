"""通知通道 & 生活缴费 配置 API

统一管理所有通知通道配置（飞书/微信/邮件/电话/短信）和电费查询配置。
前端"📢 通知通道"页面读写此接口。

路由：
    GET  /api/notification/channels      → 获取所有通道配置（密钥脱敏）
    POST /api/notification/channels      → 保存通道配置
    POST /api/notification/test          → 测试某个通道的连通性
"""
import json
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging

logger = setup_logging()
TAG = "NotificationAPI"

SERVER_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = SERVER_ROOT / "data"
CONFIG_FILE = DATA_DIR / ".notification_channels.json"

# 通道定义
CHANNEL_META = {
    "feishu": {
        "name": "飞书",
        "icon": "💬",
        "status": "available",  # available=可配置, placeholder=占位
        "fields": [
            {"key": "app_id", "label": "App ID", "type": "text", "placeholder": "cli_xxxxxxxx", "required": False},
            {"key": "app_secret", "label": "App Secret", "type": "password", "placeholder": "应用密钥", "required": False},
            {"key": "webhook_url", "label": "Webhook URL", "type": "text", "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/...", "required": False},
            {"key": "default_open_id", "label": "默认接收人 Open ID", "type": "text", "placeholder": "ou_xxxxxxxx", "required": False},
        ],
    },
    "wechat": {
        "name": "微信（PushPlus）",
        "icon": "📱",
        "status": "available",
        "fields": [
            {"key": "wechat_provider", "label": "推送方式", "type": "select", "options": [{"value": "clawbot", "label": "微信 ClawBot（直接发到微信聊天，推荐）"}, {"value": "pushplus", "label": "公众号服务号（推送加）"}], "required": True},
            {"key": "pushplus_token", "label": "PushPlus Token", "type": "password", "placeholder": "pushplus.plus 个人中心获取", "required": True},
            {"key": "pushplus_secret_key", "label": "用户密钥（可选）", "type": "password", "placeholder": "用于查询消息真实送达状态", "required": False},
        ],
        "note": "ClawBot：消息直接出现在微信聊天窗口，需先在 pushplus 个人中心→渠道配置→微信ClawBot 扫码绑定；绑定后要主动发一条消息激活；之后每发10条或每24小时需再主动对话一次。服务号：免配置但消息在「推送加」服务号里。短信/语音电话共用此 Token（个人中心绑定手机号）。",
    },
    "email": {
        "name": "邮件（SMTP）",
        "icon": "📧",
        "status": "available",
        "fields": [
            {"key": "smtp_host", "label": "SMTP 服务器", "type": "text", "placeholder": "如 smtp.qq.com", "required": True},
            {"key": "smtp_port", "label": "SMTP 端口", "type": "text", "placeholder": "465", "required": True},
            {"key": "smtp_user", "label": "邮箱账号", "type": "text", "placeholder": "your@email.com", "required": True},
            {"key": "smtp_pass", "label": "邮箱密码/授权码", "type": "password", "placeholder": "QQ邮箱请填授权码", "required": True},
            {"key": "recipient", "label": "收件人邮箱", "type": "text", "placeholder": "多个用英文逗号分隔", "required": True},
        ],
        "note": "QQ邮箱：设置 → 账号 → 开启 SMTP 服务后生成授权码（不是登录密码）。端口 465 走 SSL，587 走 STARTTLS。",
    },
    "phone": {
        "name": "电话（CallPilot）",
        "icon": "📞",
        "status": "available",
        "fields": [
            {"key": "callpilot_enabled", "label": "启用电话通知", "type": "select", "options": [{"value": "true", "label": "启用"}, {"value": "false", "label": "停用"}], "required": False},
            {"key": "emergency_contacts", "label": "紧急联系人（JSON）", "type": "textarea", "placeholder": '[{"name":"张三","phone":"13800138000"}]', "required": False},
        ],
        "note": "电话通道使用 CallPilot 服务，详细配置请在 AI 管家 → 电话功能 中设置。",
    },
    "sms": {
        "name": "短信",
        "icon": "📨",
        "status": "available",
        "fields": [
            {"key": "provider", "label": "服务商", "type": "select", "options": [{"value": "pushplus", "label": "PushPlus（0.1元/条，免审核，推荐）"}, {"value": "aliyun", "label": "阿里云（0.045元/条，需签名模板审核）"}], "required": True},
            {"key": "access_key_id", "label": "阿里云 AccessKey ID（备选）", "type": "text", "placeholder": "仅服务商选阿里云时填写", "required": False},
            {"key": "access_key_secret", "label": "阿里云 AccessKey Secret（备选）", "type": "password", "placeholder": "", "required": False},
            {"key": "sign_name", "label": "阿里云短信签名（备选）", "type": "text", "placeholder": "审核通过的签名", "required": False},
            {"key": "template_code", "label": "阿里云模板 CODE（备选）", "type": "text", "placeholder": "SMS_xxxxxxxx", "required": False},
            {"key": "phone_number", "label": "阿里云接收手机号（备选）", "type": "text", "placeholder": "11位手机号", "required": False},
        ],
        "note": "默认走 PushPlus：免审核免配置，发给微信卡片里 token 账号绑定的手机号。选阿里云需先在阿里云开通短信服务并审核签名+模板。",
    },
    "voice": {
        "name": "语音电话（PushPlus）",
        "icon": "🔊",
        "status": "available",
        "fields": [],
        "note": "语音电话直呼（0.3元/条），接收手机号 = pushplus 账号绑定手机号。适合安防报警等紧急通知，可在个人中心充值积分。无需额外配置，依赖微信卡片的 Token。",
    },
    "electricity": {
        "name": "电费查询（国家电网）",
        "icon": "🔌",
        "status": "available",
        "fields": [
            {"key": "enabled", "label": "启用自动查询", "type": "select", "options": [{"value": "true", "label": "启用"}, {"value": "false", "label": "停用"}], "required": False},
            {"key": "sgcc_user_id", "label": "电力户号", "type": "text", "placeholder": "网上国网的用电户号", "required": False},
            {"key": "sgcc_username", "label": "登录账号", "type": "text", "placeholder": "手机号或用户名", "required": False},
            {"key": "sgcc_password", "label": "登录密码", "type": "password", "placeholder": "网上国网登录密码", "required": False},
            {"key": "balance_threshold", "label": "余额预警阈值（元）", "type": "text", "placeholder": "50", "required": False},
        ],
        "note": "通过 sgcc_electricity 开源方案自动登录 95598pay.com 查询电费余额。凭证仅存在本机。",
    },
}

# 敏感字段（脱敏用）
SENSITIVE_FIELDS = {"app_secret", "smtp_pass", "pushplus_token", "pushplus_secret_key", "sgcc_password", "access_key_id", "access_key_secret"}


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载通知配置失败: {e}")
    return {}


def _save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _mask_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****" + value[-2:]


async def get_channels(request):
    """获取所有通道配置（密钥脱敏）"""
    try:
        cfg = _load_config()
        channels = []
        for cid, meta in CHANNEL_META.items():
            channel_cfg = cfg.get(cid, {})
            # 脱敏
            masked = {}
            for field in meta["fields"]:
                key = field["key"]
                val = channel_cfg.get(key, "")
                if key in SENSITIVE_FIELDS and val:
                    masked[key] = _mask_value(str(val))
                else:
                    masked[key] = val
            channels.append({
                "id": cid,
                "name": meta["name"],
                "icon": meta["icon"],
                "status": meta["status"],
                "fields": meta["fields"],
                "note": meta.get("note", ""),
                "config": masked,
                "configured": any(
                    channel_cfg.get(f["key"], "")
                    for f in meta["fields"]
                ),
            })
        return _json_response({"success": True, "channels": channels})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取通道配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def save_channels(request):
    """保存通道配置"""
    try:
        body = await request.json()
        channel_id = body.get("channel_id")
        config = body.get("config", {})

        if not channel_id or channel_id not in CHANNEL_META:
            return _json_response({"success": False, "error": f"无效的通道: {channel_id}"}, status=400)

        cfg = _load_config()

        # 对敏感字段：如果前端传来的是脱敏值（含 ****），则保留原值不覆盖
        existing = cfg.get(channel_id, {})
        for key, val in config.items():
            if key in SENSITIVE_FIELDS and ("****" in str(val) or not val):
                config[key] = existing.get(key, "")

        cfg[channel_id] = config
        _save_config(cfg)

        logger.bind(tag=TAG).info(f"已保存通道配置: {channel_id}")
        return _json_response({"success": True, "message": f"{CHANNEL_META[channel_id]['name']} 配置已保存"})
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存通道配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def test_channel(request):
    """测试通道连通性"""
    try:
        body = await request.json()
        channel_id = body.get("channel_id")
        if not channel_id or channel_id not in CHANNEL_META:
            return _json_response({"success": False, "error": f"无效的通道: {channel_id}"}, status=400)

        meta = CHANNEL_META[channel_id]
        if meta["status"] == "placeholder":
            return _json_response({
                "success": False,
                "error": f"{meta['name']} 通道尚未实现，当前为占位状态",
            })

        cfg = _load_config().get(channel_id, {})

        if channel_id == "feishu":
            if not cfg.get("app_id") or not cfg.get("app_secret"):
                return _json_response({"success": False, "error": "请先填写 App ID 和 App Secret"})
            # 测试飞书 token 获取
            import aiohttp
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
                )
                data = await resp.json()
                if data.get("code") == 0:
                    return _json_response({"success": True, "message": "飞书连通成功， tenant_access_token 获取正常"})
                else:
                    return _json_response({"success": False, "error": f"飞书返回错误: {data.get('msg', '未知')}"})

        if channel_id == "wechat":
            if not cfg.get("pushplus_token"):
                return _json_response({"success": False, "error": "请先填写 PushPlus Token"})
            from core.utils import notify_service
            provider = cfg.get("wechat_provider", "clawbot")
            if provider == "clawbot":
                ok, msg = await notify_service.test_pushplus_channel(
                    "clawbot",
                    "这是一条来自 homAi 的测试消息。如果你在微信聊天里看到它，说明 ClawBot 通道已打通 ✅",
                    template="txt",
                )
            else:
                ok, msg = await notify_service.test_pushplus_channel(
                    "wechat", "这是一条来自 homAi 的测试消息。如果你在微信里看到它，说明微信通道已打通 ✅"
                )
            return _json_response({"success": ok, "message" if ok else "error": msg})

        if channel_id == "voice":
            if not _load_config().get("wechat", {}).get("pushplus_token"):
                return _json_response({"success": False, "error": "请先在微信卡片中填写 PushPlus Token"})
            from core.utils import notify_service
            ok, msg = await notify_service.test_pushplus_channel(
                "voice", "这是一条来自 homAi 的语音电话测试。如果你接到电话，说明语音通道已打通。"
            )
            return _json_response({"success": ok, "message" if ok else "error": msg})

        if channel_id == "sms":
            from core.utils import notify_service
            if cfg.get("provider", "pushplus") == "aliyun":
                for key in ("access_key_id", "access_key_secret", "sign_name", "template_code", "phone_number"):
                    if not cfg.get(key):
                        return _json_response({"success": False, "error": f"请先填写 {key} 配置"})
                ok, msg = await notify_service.send_sms("这是一条来自 homAi 的测试短信，说明短信通道已打通")
                return _json_response({"success": ok, "message" if ok else "error": msg})
            if not _load_config().get("wechat", {}).get("pushplus_token"):
                return _json_response({"success": False, "error": "请先在微信卡片中填写 PushPlus Token"})
            ok, msg = await notify_service.test_pushplus_channel(
                "sms", "这是一条来自 homAi 的测试短信，说明短信通道已打通"
            )
            return _json_response({"success": ok, "message" if ok else "error": msg})

        if channel_id == "electricity":
            if not cfg.get("sgcc_user_id") or not cfg.get("sgcc_password"):
                return _json_response({"success": False, "error": "请先填写电力户号和登录密码"})
            return _json_response({
                "success": True,
                "message": "电费查询需要 sgcc_electricity 服务运行，请先部署该服务。配置已保存。",
            })

        return _json_response({"success": False, "error": f"{meta['name']} 连通测试暂未实现"})
    except Exception as e:
        logger.bind(tag=TAG).error(f"测试通道失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


def register_routes(app):
    """注册通知通道 API 路由"""
    app.router.add_get("/api/notification/channels", get_channels)
    app.router.add_post("/api/notification/channels", save_channels)
    app.router.add_post("/api/notification/test", test_channel)
