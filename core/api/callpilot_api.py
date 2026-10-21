"""CallPilot 电话功能 API

管理 CallPilot 服务配置，支持从 HomeBrain LLM 配置同步。
CallPilot 部署位置：~/opt/callpilot

路由：
    GET  /api/callpilot/status          → CallPilot 服务状态
    POST /api/callpilot/sync-llm        → 同步 HomeBrain LLM 配置到 CallPilot
    GET  /api/callpilot/config          → 读取 CallPilot 配置
    POST /api/callpilot/config          → 更新 CallPilot 配置
"""
import os
import subprocess
from pathlib import Path

import yaml
from aiohttp import web

from config.logger import setup_logging
from config.config_loader import load_yaml_expanded

logger = setup_logging()
TAG = "CallPilotAPI"

# CallPilot 部署路径
CALLPILOT_DIR = Path.home() / "opt" / "callpilot"
CALLPILOT_ENV = CALLPILOT_DIR / ".env"
CALLPILOT_VENV_PY = CALLPILOT_DIR / ".venv" / "bin" / "python"

# HomeBrain 配置路径
SERVER_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = SERVER_ROOT / "data" / ".config.yaml"


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _load_homebrain_config() -> dict:
    """读取 HomeBrain 配置"""
    if CONFIG_PATH.exists():
        try:
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            return load_yaml_expanded(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载 HomeBrain 配置失败: {e}")
    return {}


def _get_homebrain_llm_config(cfg: dict) -> dict:
    """从 HomeBrain 配置读取当前 LLM 设置"""
    selected = cfg.get("selected_module", {}).get("LLM", "")
    llm_cfg = cfg.get("LLM", {}).get(selected, {})
    return {
        "provider": selected,
        "base_url": llm_cfg.get("base_url") or llm_cfg.get("url", ""),
        "api_key": llm_cfg.get("api_key", ""),
        "model": llm_cfg.get("model_name") or llm_cfg.get("model", ""),
    }


def _read_callpilot_env() -> dict:
    """读取 CallPilot .env 文件"""
    if not CALLPILOT_ENV.exists():
        return {}
    env = {}
    try:
        for line in CALLPILOT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as e:
        logger.bind(tag=TAG).error(f"读取 CallPilot .env 失败: {e}")
    return env


def _write_callpilot_env(env: dict) -> None:
    """写入 CallPilot .env 文件"""
    lines = []
    for key, value in env.items():
        # 保留注释格式
        if value == "":
            lines.append(f"{key}=")
        else:
            lines.append(f'{key}="{value}"')
    CALLPILOT_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _map_homebrain_to_callpilot(homebrain_llm: dict) -> dict:
    """将 HomeBrain LLM 配置映射到 CallPilot .env 格式"""
    callpilot_env = {}

    model_lower = homebrain_llm.get("model", "").lower()
    base_url = homebrain_llm.get("base_url", "")
    api_key = homebrain_llm.get("api_key", "")

    # 判断 provider
    if "qwen" in model_lower or "dashscope" in base_url:
        callpilot_env["AGENT_PROVIDER"] = "qwen"
        callpilot_env["DASHSCOPE_API_KEY"] = api_key
    elif "doubao" in model_lower or "volcengine" in base_url:
        callpilot_env["AGENT_PROVIDER"] = "doubao"
        callpilot_env["DOUBAO_ACCESS_KEY"] = api_key
    elif "openai" in model_lower or "openai" in base_url:
        callpilot_env["AGENT_PROVIDER"] = "openai"
        callpilot_env["OPENAI_API_KEY"] = api_key
    else:
        # 默认用 openai-compatible（LM Studio 等本地模型）
        callpilot_env["AGENT_PROVIDER"] = "openai"
        callpilot_env["OPENAI_API_KEY"] = api_key or "dummy"
        callpilot_env["OPENAI_BASE_URL"] = base_url

    callpilot_env["AGENT_MODEL_NAME"] = homebrain_llm.get("model", "")

    return callpilot_env


async def get_callpilot_status(request):
    """获取 CallPilot 服务状态"""
    try:
        # 检查 CallPilot 是否安装
        if not CALLPILOT_DIR.exists():
            return _json_response({
                "success": False,
                "installed": False,
                "error": "CallPilot 未安装",
            })

        # 检查硬件（4G modem）
        modem_connected = False
        try:
            result = subprocess.run(
                ["system_profiler", "SPUSBDataType"],
                capture_output=True, text=True, timeout=5,
            )
            modem_connected = "Quectel" in result.stdout or "EC20" in result.stdout or "EG25" in result.stdout
        except Exception:
            pass

        # 读取当前配置
        env = _read_callpilot_env()

        return _json_response({
            "success": True,
            "installed": True,
            "path": str(CALLPILOT_DIR),
            "modem_connected": modem_connected,
            "config": {
                "provider": env.get("AGENT_PROVIDER", ""),
                "model": env.get("AGENT_MODEL_NAME", ""),
                "language": env.get("AGENT_LANGUAGE", "zh"),
                "summary_enabled": env.get("SUMMARY_ENABLED", "true"),
                "recording_enabled": env.get("RECORDING_ENABLED", "false"),
            },
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取 CallPilot 状态失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def sync_llm_config(request):
    """同步 HomeBrain LLM 配置到 CallPilot"""
    try:
        # 读取 HomeBrain LLM 配置
        homebrain_cfg = _load_homebrain_config()
        homebrain_llm = _get_homebrain_llm_config(homebrain_cfg)

        if not homebrain_llm.get("api_key"):
            return _json_response({
                "success": False,
                "error": "homAi LLM 未配置 API Key",
            })

        # 映射到 CallPilot 格式
        callpilot_updates = _map_homebrain_to_callpilot(homebrain_llm)

        # 读取现有 CallPilot .env
        callpilot_env = _read_callpilot_env()

        # 合并（保留其他配置）
        callpilot_env.update(callpilot_updates)

        # 写入
        _write_callpilot_env(callpilot_env)

        logger.bind(tag=TAG).info(f"已同步 LLM 配置到 CallPilot: provider={callpilot_updates.get('AGENT_PROVIDER')}")

        return _json_response({
            "success": True,
            "message": "LLM 配置已同步到 CallPilot",
            "synced": callpilot_updates,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"同步 LLM 配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def get_callpilot_config(request):
    """读取 CallPilot 完整配置"""
    try:
        env = _read_callpilot_env()
        # 脱敏 API key
        for key in list(env.keys()):
            if "API_KEY" in key or "ACCESS_KEY" in key:
                value = env[key]
                if len(value) > 10:
                    env[key] = value[:8] + "****" + value[-2:]
                else:
                    env[key] = "****" if value else ""

        return _json_response({
            "success": True,
            "config": env,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"读取 CallPilot 配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


async def update_callpilot_config(request):
    """更新 CallPilot 配置"""
    try:
        body = await request.json()

        # 读取现有配置
        callpilot_env = _read_callpilot_env()

        # 更新（只允许修改安全字段）
        safe_fields = {
            "OWNER_NAME", "OWNER_PHONE", "AGENT_LANGUAGE",
            "SUMMARY_ENABLED", "RECORDING_ENABLED",
        }
        for key, value in body.items():
            if key in safe_fields:
                callpilot_env[key] = str(value)

        # 写入
        _write_callpilot_env(callpilot_env)

        return _json_response({
            "success": True,
            "message": "CallPilot 配置已更新",
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"更新 CallPilot 配置失败: {e}")
        return _json_response({"success": False, "error": str(e)}, status=500)


def register_routes(app):
    """注册 CallPilot API 路由"""
    app.router.add_get("/api/callpilot/status", get_callpilot_status)
    app.router.add_post("/api/callpilot/sync-llm", sync_llm_config)
    app.router.add_get("/api/callpilot/config", get_callpilot_config)
    app.router.add_post("/api/callpilot/config", update_callpilot_config)
