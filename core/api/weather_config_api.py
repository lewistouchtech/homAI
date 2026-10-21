"""
天气插件配置API - 处理天气设置的保存和加载
"""

import json
import yaml
import os
import threading
from pathlib import Path
from aiohttp import web
import logging
from config.config_loader import load_yaml_expanded

TAG = __name__
logger = logging.getLogger(TAG)

# 配置文件路径
CONFIG_DIR = Path(__file__).parent.parent.parent / "data"
CONFIG_FILE = CONFIG_DIR / ".config.yaml"

def load_weather_config() -> dict:
    """从配置文件读取天气插件配置"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
                config = load_yaml_expanded(f)
                if config and 'plugins' in config and 'get_weather' in config['plugins']:
                    return config['plugins']['get_weather']
    except Exception as e:
        logger.error(f"读取天气配置失败: {e}")
    
    # 返回默认配置
    return {
        'api_host': 'mj7p3y7naa.re.qweatherapi.com',
        'api_key': 'YOUR_QWEATHER_API_KEY',
        'default_location': '广州'
    }

def save_weather_config(config: dict) -> dict:
    """保存天气插件配置到配置文件"""
    try:
        # 确保目录存在
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        # 读取现有配置
        full_config = {}
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                full_config = yaml.safe_load(f) or {}
        
        # 更新天气配置
        if 'plugins' not in full_config:
            full_config['plugins'] = {}
        full_config['plugins']['get_weather'] = config
        
        # 写入配置文件
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            # 写前归集：即使这里用的是裸读，也加一道防线——若将来有人把上面
            # 的读换成插值读，真值就会被写回被 git 跟踪的文件（见 search_api 同型事故）
            from core.utils.secret_store import sweep_dict_secrets
            yaml.dump(sweep_dict_secrets(full_config), f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)
        
        logger.info(f"天气配置已保存: {config}")
        return {
            "success": True,
            "message": "天气配置已保存"
        }
        
    except Exception as e:
        logger.error(f"保存天气配置失败: {e}")
        return {
            "success": False,
            "message": f"保存失败: {str(e)}"
        }

async def handle_get_weather_config(request) -> web.Response:
    """处理获取天气配置请求"""
    try:
        config = load_weather_config()
        return web.json_response({
            "success": True,
            "config": config
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

async def handle_save_weather_config(request) -> web.Response:
    """处理保存天气配置请求"""
    try:
        data = await request.json()
        config = {
            'api_host': data.get('api_host', 'mj7p3y7naa.re.qweatherapi.com'),
            'api_key': data.get('api_key', 'YOUR_QWEATHER_API_KEY'),
            'default_location': data.get('default_location', '广州')
        }
        
        result = save_weather_config(config)

        # 地点权威源是后台天气设置：保存后让身份缓存失效，并异步把地点同步到
        # Ltm 的记忆档案 profile.md（主人 2026-09-17 指示）
        if result.get("success"):
            try:
                from core.utils.assistant_identity import invalidate_cache
                from core.utils.ltm_sync import sync_assistant_identity_to_LTM

                invalidate_cache()
                threading.Thread(
                    target=sync_assistant_identity_to_LTM, daemon=True
                ).start()
            except Exception as sync_err:
                logger.warning(f"同步地点到 Ltm 失败（不影响保存）: {sync_err}")

        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })
