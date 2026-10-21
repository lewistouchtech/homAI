"""通知通道发送服务

统一实现微信(PushPlus) / 语音电话(PushPlus) / 短信(PushPlus或阿里云) / 邮件(SMTP) 的发送逻辑。
PushPlus 所有渠道（wechat/sms/voice）共用一个 token，配置在 wechat 通道。
配置读写 data/.notification_channels.json（与 core/api/notification_api.py 共用）。

用法：
    from core.utils import notify_service
    ok, msg = await notify_service.send_wechat("测试消息")
    ok, msg = await notify_service.send_voice("安防报警：大门未锁")
    ok, msg = await notify_service.send_sms("紧急通知内容")      # 默认走 PushPlus
    ok, msg = await notify_service.send_email("标题", "正文")    # SMTP
    ok, detail = await notify_service.query_pushplus_result(short_code)  # 查真实送达状态
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import aiohttp

from config.logger import setup_logging
from core.utils.assistant_identity import get_name

logger = setup_logging()
TAG = "NotifyService"

_SERVER_ROOT = Path(__file__).parent.parent.parent
CONFIG_FILE = _SERVER_ROOT / "data" / ".notification_channels.json"

PUSHPLUS_SEND_URL = "http://www.pushplus.plus/send"


def load_channel_config(channel_id: str) -> dict:
    """读取某通道的配置（含明文密钥，仅限服务端内部使用）"""
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get(channel_id, {}) or {}
        except Exception as e:
            logger.bind(tag=TAG).error(f"读取通知配置失败: {e}")
    return {}


def _check(cfg: dict, keys: list) -> str | None:
    """检查必填字段，返回第一个缺失的字段名"""
    for k in keys:
        if not str(cfg.get(k, "")).strip():
            return k
    return None


def _pushplus_token() -> str | None:
    """PushPlus 所有渠道共用 wechat 通道里配置的 token"""
    token = load_channel_config("wechat").get("pushplus_token", "").strip()
    return token or None


# ==================== PushPlus 核心发送 ====================

async def _pushplus_send(channel: str, title: str, content: str, option: str = None, template: str = None) -> tuple[bool, str, str | None]:
    """调用 PushPlus 发送接口。返回 (是否受理, 说明, 消息流水号shortCode)。

    注意：PushPlus 是异步发送，返回 code=200 仅表示已受理，
    真实送达状态需用 query_pushplus_result(shortCode) 查询。
    """
    token = _pushplus_token()
    if not token:
        return False, "PushPlus Token 未配置，请先在「通知通道」页面微信卡片中填写", None
    body = {"token": token, "title": title, "content": content, "channel": channel}
    if option:
        body["option"] = option
    if template:
        body["template"] = template
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            resp = await session.post(PUSHPLUS_SEND_URL, json=body)
            data = await resp.json(content_type=None)
        if data.get("code") == 200:
            return True, "已受理", data.get("data")  # data = 消息流水号
        err = str(data.get("msg", "未知错误"))
        detail = data.get("data")
        if detail and str(detail) != err:
            err = f"{err}: {detail}"
        return False, f"PushPlus 返回错误: {err}", None
    except Exception as e:
        logger.bind(tag=TAG).error(f"PushPlus {channel} 发送失败: {e}")
        return False, f"PushPlus 请求失败: {type(e).__name__}: {e}", None


async def query_pushplus_result(short_code: str, max_wait: float = 8.0) -> tuple[bool, str]:
    """用开放接口查询消息真实送达状态（需要 wechat 通道配置 pushplus_secret_key）。

    轮询直到状态为 2(已发送)/3(发送失败) 或超时。返回 (是否送达, 说明)。
    """
    wx_cfg = load_channel_config("wechat")
    secret = wx_cfg.get("pushplus_secret_key", "").strip()
    if not secret:
        return False, "未配置用户密钥，无法查询真实送达状态（已受理，可在公众号内确认）"
    token = wx_cfg["pushplus_token"].strip()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            # 1. token + secretKey 换 AccessKey
            resp = await session.post(
                "https://www.pushplus.plus/api/common/openApi/getAccessKey",
                json={"token": token, "secretKey": secret},
            )
            data = await resp.json(content_type=None)
            if data.get("code") != 200:
                return False, f"获取 AccessKey 失败: {data.get('msg', data)}"
            access_key = data["data"]["accessKey"]

            # 2. 轮询发送结果
            deadline = time.monotonic() + max_wait
            while True:
                resp = await session.get(
                    "https://www.pushplus.plus/api/open/message/sendMessageResult",
                    params={"shortCode": short_code},
                    headers={"access-key": access_key},
                )
                result = await resp.json(content_type=None)
                if result.get("code") == 200:
                    status = result["data"].get("status")
                    if status == 2:
                        return True, "消息已送达 ✅"
                    if status == 3:
                        return False, f"发送失败: {result['data'].get('errorMessage', '未知原因')}"
                if time.monotonic() > deadline:
                    return False, "等待结果超时（消息可能仍在发送中，可在公众号内确认）"
                await asyncio.sleep(1.5)
    except Exception as e:
        logger.bind(tag=TAG).error(f"查询 PushPlus 结果失败: {e}")
        return False, f"查询结果失败: {type(e).__name__}: {e}"


# ==================== 测试通道（发送 + 查询真实送达） ====================

async def test_pushplus_channel(channel: str, content: str, template: str = None) -> tuple[bool, str]:
    """发送测试消息并尽力确认真实送达（配置了用户密钥时轮询结果，否则只确认已受理）。"""
    ok, msg, short_code = await _pushplus_send(channel, "homAi 通道测试", content, template=template)
    if not ok:
        return False, msg
    if not short_code:
        return True, "已受理（未返回流水号）"
    delivered, detail = await query_pushplus_result(short_code)
    if delivered:
        return True, detail
    # 查询失败不视为通道失败（可能没配密钥或超时），只提示
    return True, f"测试消息已受理。{detail}"


# ==================== 微信（PushPlus：服务号 或 ClawBot） ====================

def _wechat_provider() -> str:
    """微信推送方式：clawbot=微信ClawBot(直接到微信聊天, 默认) / pushplus=公众号服务号"""
    return load_channel_config("wechat").get("wechat_provider", "clawbot").strip() or "clawbot"


async def send_wechat(content: str, title: str = "homAi 通知") -> tuple[bool, str]:
    """推送到微信。默认走 ClawBot（发到微信聊天窗口）；后台微信卡片可切换回服务号推送。"""
    if _wechat_provider() == "clawbot":
        ok, msg, _ = await _pushplus_send("clawbot", title, content, template="txt")
        if not ok and "绑定" in msg:
            msg += "（请先在 pushplus 个人中心 → 渠道配置 → 微信ClawBot 扫码绑定）"
        return ok, msg
    ok, msg, _ = await _pushplus_send("wechat", title, content)
    return ok, msg


# ==================== 语音电话（PushPlus） ====================

async def send_voice(content: str) -> tuple[bool, str]:
    """通过 PushPlus 发起语音电话呼叫（0.3元/条，接收方需已绑定手机号）。适合紧急告警。"""
    ok, msg, _ = await _pushplus_send("voice", "homAi 紧急通知", content)
    return ok, msg


# ==================== 短信（PushPlus 或 阿里云） ====================

_ALIYUN_SMS_ENDPOINT = "dysmsapi.aliyuncs.com"


def _percent_encode(s: str) -> str:
    """阿里云 RPC 签名用的 URL 编码（特殊字符大写 %XX，且 + %2F %7E 处理）"""
    from urllib.parse import quote
    return quote(str(s), safe="~").replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def _aliyun_sms_signature(params: dict, access_secret: str) -> str:
    """计算阿里云 RPC API 签名（HMAC-SHA1）"""
    sorted_params = sorted(params.items())
    canonical = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted_params)
    string_to_sign = f"GET&%2F&{_percent_encode(canonical)}"
    digest = hmac.new(
        (access_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


async def _send_sms_aliyun(cfg: dict, template_param: str, phone: str = None) -> tuple[bool, str]:
    phone_number = (phone or cfg["phone_number"]).strip()
    params = {
        "AccessKeyId": cfg["access_key_id"].strip(),
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone_number,
        "RegionId": "cn-hangzhou",
        "SignName": cfg["sign_name"].strip(),
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TemplateCode": cfg["template_code"].strip(),
        "TemplateParam": template_param.strip() or "{}",
        "Timestamp": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    params["Signature"] = _aliyun_sms_signature(params, cfg["access_key_secret"].strip())
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            resp = await session.get(f"https://{_ALIYUN_SMS_ENDPOINT}/", params=params)
            data = await resp.json(content_type=None)
        if data.get("Code") == "OK":
            return True, f"短信已发送到 {phone_number}（BizId: {data.get('BizId', '')}）"
        return False, f"阿里云短信返回错误: {data.get('Code')}: {data.get('Message', '未知')}"
    except Exception as e:
        logger.bind(tag=TAG).error(f"短信发送失败: {e}")
        return False, f"短信发送请求失败: {type(e).__name__}: {e}"


async def send_sms(content: str, phone: str = None, template_param: str = None) -> tuple[bool, str]:
    """发送短信。默认走 PushPlus（免审核，0.1元/条，发给已绑定手机号）；
    sms 通道 provider 配置为 aliyun 时走阿里云（0.045元/条，需签名模板审核）。"""
    cfg = load_channel_config("sms")
    if cfg.get("provider", "pushplus") == "aliyun":
        missing = _check(cfg, ["access_key_id", "access_key_secret", "sign_name", "template_code", "phone_number"])
        if missing:
            return False, f"阿里云短信未配置 {missing}"
        return await _send_sms_aliyun(cfg, template_param or json.dumps({"content": content[:450]}, ensure_ascii=False), phone)
    # PushPlus 短信：接收方是 pushplus 账号绑定的手机号，忽略 phone 参数
    ok, msg, _ = await _pushplus_send("sms", "homAi 通知", content[:450])
    return ok, msg


# ==================== 邮件（SMTP） ====================

def _send_email_sync(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    """同步阻塞的 SMTP 发送（在线程池中执行）"""
    import smtplib

    host = cfg["smtp_host"].strip()
    port = int(cfg.get("smtp_port") or 465)
    user = cfg["smtp_user"].strip()
    password = cfg["smtp_pass"].strip()
    recipients = [r.strip() for r in cfg["recipient"].split(",") if r.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(f"homAi {get_name()}", "utf-8")), user))
    msg["To"] = ", ".join(recipients)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.starttls()
    try:
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())
    finally:
        server.quit()
    return True, f"邮件已发送到 {', '.join(recipients)}"


async def send_email(subject: str, body: str) -> tuple[bool, str]:
    """通过 SMTP 发送邮件。subject: 标题；body: 正文"""
    cfg = load_channel_config("email")
    missing = _check(cfg, ["smtp_host", "smtp_user", "smtp_pass", "recipient"])
    if missing:
        return False, f"邮件通道未配置 {missing}，请先在「通知通道」页面填写 SMTP 配置"
    if not subject.strip():
        subject = "homAi 通知"
    try:
        return await asyncio.to_thread(_send_email_sync, cfg, subject.strip(), body)
    except Exception as e:
        logger.bind(tag=TAG).error(f"邮件发送失败: {e}")
        return False, f"邮件发送失败: {type(e).__name__}: {e}"


# ==================== 汇总 ====================

async def send(channel_id: str, content: str, title: str = "homAi 通知") -> tuple[bool, str]:
    """按通道 ID 统一发送（供测试接口使用）"""
    if channel_id == "wechat":
        return await send_wechat(content, title)
    if channel_id == "voice":
        return await send_voice(content)
    if channel_id == "sms":
        return await send_sms(content)
    if channel_id == "email":
        return await send_email(title, content)
    return False, f"未知通道: {channel_id}"
