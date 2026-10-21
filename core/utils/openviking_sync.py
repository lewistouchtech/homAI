"""把后台权威人设同步到 OpenViking 的 identity.md / soul.md。

后台「AI身份设定」是唯一权威源：
- devices.db 的 ai_butler_config 表存 name / soul / identity
- data/.config.yaml 的 prompt 字段是真正注入 LLM 的人设

同步规则（主人 2026-09-17 指示）：
1. prompt 里已经有的内容，OpenViking 的 MD 只保留几句说明，指向系统提示词；
2. prompt 里没有的内容，保留在 MD 里（如工具调用策略、TTS 约束）。

同步动作：SDK read 取正文 → write 写正文（自动保留 MEMORY_FIELDS）
→ reindex 刷新向量索引（否则 find() 仍返回旧内容）。

本模块所有函数都是"尽力而为"，任何异常都只记录日志、不向上抛，
避免影响后台保存配置这条主链路。
"""

import json
import os
from typing import Any, Dict, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# OpenViking 客户端 URI 作用域不能带 account（只允许 user 等作用域）
_MEMORIES_URI_TMPL = "viking://user/{user_id}/memories/{filename}"
_SETTINGS_URI_TMPL = "viking://user/{user_id}/settings/user_config.json"
_SETTINGS_DIR_TMPL = "viking://user/{user_id}/settings"

# 允许 OpenViking 长期记忆提取的类型（排除 identity / soul，避免与人设重复）
_ALLOWED_MEMORY_TYPES = [
    "cases",
    "entities",
    "events",
    "experiences",
    "preferences",
    "profile",
    "trajectories",
]

# prompt 里没有、需要保存在 soul.md 的独有约束
_SOUL_BOUNDARIES = (
    "保护隐私，外部操作前先询问。避免发送不完整的回复，群聊中保持谨慎。"
    "无法识别或查询特定声音。专注智能家居服务，不涉及复杂技术调试。"
    "仅在用户请求涉及实时信息查询（新闻/天气/股票）或执行操作（音乐/设备/拍照）时调用工具；"
    "每次独立判断，不复用历史结果；<context> 中的信息直接使用。"
    "不主动触发 TTS 语音输出，除非系统收到明确语音指令或用户开启语音交互模式。"
)


def _get_openviking_config() -> Optional[Dict[str, Any]]:
    """读取生效配置里的 openviking 段"""
    try:
        from config.config_loader import load_config

        cfg = load_config()
        selected = cfg.get("selected_module", {}).get("Memory", "")
        if selected != "openviking":
            return None
        return cfg.get("Memory", {}).get("openviking", {}) or {}
    except Exception as exc:
        logger.bind(tag=TAG).debug(f"读取 OpenViking 配置失败，跳过同步: {exc}")
        return None


def _build_client():
    """构造并初始化 OpenViking 同步客户端；失败返回 None"""
    ov_cfg = _get_openviking_config()
    if not ov_cfg:
        return None

    url = ov_cfg.get("server_url", "http://localhost:8765")
    api_key = ov_cfg.get("api_key", "") or ""
    account = ov_cfg.get("account", "") or ""

    try:
        from openviking_sdk import config as ov_config
        from openviking_sdk.client import SyncHTTPClient

        # account 必须用关键字传参，否则会被当成 user_id
        cc = ov_config.resolve_client_config(url=url, api_key=api_key, account=account)
        client = SyncHTTPClient(
            url=getattr(cc, "url", url),
            api_key=getattr(cc, "api_key", api_key) or "",
            account=getattr(cc, "account", account) or "",
            user=getattr(cc, "user", None),
        )
        client.initialize()
        return client
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"连接 OpenViking 失败，跳过人设同步: {exc}")
        return None


def _user_id() -> str:
    """当前 OpenViking 用户 ID（与 provider 保持一致，默认 default）"""
    ov_cfg = _get_openviking_config() or {}
    return (ov_cfg.get("user_id") or "").strip() or "default"


def build_identity_body(name: str) -> str:
    """identity.md 正文：只保留"是谁"，详细人设指向系统提示词"""
    return (
        "# identity.md - Who Am I?\n"
        "\n"
        "_Fill this in during your first conversation. Make it yours._\n"
        "\n"
        f"- **Name:** {name}\n"
        "- **Creature:** AI assistant\n"
        "- **Vibe:** 智能管家\n"
        "- **Emoji:** 🤖\n"
        "- **Avatar:** /images/avatar.jpg\n"
        "\n"
        "---\n"
        "\n"
        f"{name}，家庭 AI 智能管家。（详细人设见系统提示词）"
    )


def build_soul_body(name: str) -> str:
    """soul.md 正文：prompt 已覆盖的部分只留指针，prompt 没有的约束保留"""
    return (
        "# soul.md - Who You Are\n"
        "\n"
        "## Core Truths\n"
        f"{name} 的核心性格与价值观以系统提示词为准。\n"
        "\n"
        "## Boundaries\n"
        f"{_SOUL_BOUNDARIES}\n"
        "\n"
        "## Vibe\n"
        "语气与称呼规则见系统提示词。\n"
        "\n"
        "## Continuity\n"
        "这些文件是你的记忆。阅读它们。更新它们。"
    )


def _sync_file(client, uri: str, body: str) -> bool:
    """写入正文并刷新向量索引"""
    client.write(uri, body)
    try:
        client.reindex(uri, mode="vectors_only", wait=True, recursive=False)
    except Exception as exc:
        logger.bind(tag=TAG).debug(f"reindex 跳过（{uri}）: {exc}")
    return True


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _merge_profile_body(existing: str, location: str) -> str:
    """把后台「天气预报设置」的地点写进 profile.md 的「# 用户」段，其余内容保留。

    地点是"设一次基本不变"的持久状态（主人 2026-09-17 指示），
    因此已记录同一地点时不改动、不刷日期；只有地点变了才更新。
    """
    location = (location or "").strip()
    if not location:
        return existing

    lines = (existing or "").splitlines()
    loc_prefix = "- 当前位于"
    new_line = f"{loc_prefix}{location} (as of {_today()})"

    # 定位「# 用户」段
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == "# 用户":
            start = idx
            break

    def _is_loc_line(line: str) -> bool:
        return line.strip().startswith(loc_prefix)

    if start is not None:
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            if lines[idx].startswith("# "):
                end = idx
                break
        for idx in range(start + 1, end):
            if _is_loc_line(lines[idx]):
                if location in lines[idx]:
                    return "\n".join(lines)  # 地点未变，保持原样
                lines[idx] = new_line
                return "\n".join(lines)
        lines.insert(start + 1, new_line)
        return "\n".join(lines)

    # 没有「# 用户」段时补一个
    body = f"# 用户\n{new_line}"
    return f"{body}\n\n{existing}".rstrip() if existing and existing.strip() else body


def sync_assistant_identity_to_openviking() -> Dict[str, Any]:
    """把后台保存的人设同步到 OpenViking identity.md / soul.md"""
    try:
        from core.utils.assistant_identity import get_name

        name = (get_name() or "伊娃").strip() or "伊娃"
    except Exception:
        name = "伊娃"

    client = _build_client()
    if client is None:
        return {"success": False, "reason": "openviking_unavailable"}

    user_id = _user_id()
    identity_uri = _MEMORIES_URI_TMPL.format(user_id=user_id, filename="identity.md")
    soul_uri = _MEMORIES_URI_TMPL.format(user_id=user_id, filename="soul.md")

    results: Dict[str, Any] = {}
    try:
        _sync_file(client, identity_uri, build_identity_body(name))
        results["identity.md"] = "ok"
    except Exception as exc:
        results["identity.md"] = f"failed: {exc}"
        logger.bind(tag=TAG).warning(f"同步 identity.md 失败: {exc}")

    try:
        _sync_file(client, soul_uri, build_soul_body(name))
        results["soul.md"] = "ok"
    except Exception as exc:
        results["soul.md"] = f"failed: {exc}"
        logger.bind(tag=TAG).warning(f"同步 soul.md 失败: {exc}")

    # profile.md 的「用户」地点跟随后台「天气预报设置」（默认城市）
    try:
        from core.utils.assistant_identity import get_location

        location = (get_location() or "").strip()
    except Exception:
        location = ""

    if location:
        profile_uri = _MEMORIES_URI_TMPL.format(user_id=user_id, filename="profile.md")
        try:
            try:
                existing = client.read(profile_uri) or ""
            except Exception:
                existing = ""
            _sync_file(client, profile_uri, _merge_profile_body(existing, location))
            results["profile.md"] = "ok"
        except Exception as exc:
            results["profile.md"] = f"failed: {exc}"
            logger.bind(tag=TAG).warning(f"同步 profile.md 失败: {exc}")

    # 顺带确保记忆策略存在，避免人设文件被 OpenViking 自动提取重新覆盖
    try:
        results["memory_policy"] = ensure_openviking_memory_policy(client)["status"]
    except Exception as exc:
        results["memory_policy"] = f"failed: {exc}"

    ok = any(v == "ok" for v in results.values())
    logger.bind(tag=TAG).info(f"人设同步结果: {results}")
    return {"success": ok, "name": name, "files": results}


def ensure_openviking_memory_policy(client=None) -> Dict[str, Any]:
    """确保 OpenViking 关闭 identity / soul 的自动提取（写入用户策略白名单）

    幂等：已存在且类型一致时直接跳过。返回 status 为 ok / exists / failed。
    """
    client = client or _build_client()
    if client is None:
        return {"success": False, "status": "failed", "reason": "openviking_unavailable"}

    user_id = _user_id()
    settings_dir = _SETTINGS_DIR_TMPL.format(user_id=user_id)
    settings_uri = _SETTINGS_URI_TMPL.format(user_id=user_id)

    try:
        existing = None
        try:
            raw = client.read(settings_uri)
            existing = json.loads(raw) if raw else None
        except Exception:
            existing = None

        if isinstance(existing, dict):
            types = (existing.get("memory_policy") or {}).get("memory_types")
            if sorted(types or []) == sorted(_ALLOWED_MEMORY_TYPES):
                return {"success": True, "status": "exists", "uri": settings_uri}

        try:
            client.mkdir(settings_dir)
        except Exception:
            pass  # 目录已存在时会报 ConflictError，忽略

        policy = {
            "memory_policy": {
                "self": {"enabled": True},
                "peer": {"enabled": True},
                "memory_types": list(_ALLOWED_MEMORY_TYPES),
            }
        }
        client.write(settings_uri, json.dumps(policy, ensure_ascii=False))
        logger.bind(tag=TAG).info(f"OpenViking 记忆策略已写入: {settings_uri}")
        return {"success": True, "status": "ok", "uri": settings_uri}
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"写入 OpenViking 记忆策略失败: {exc}")
        return {"success": False, "status": "failed", "error": str(exc)}
