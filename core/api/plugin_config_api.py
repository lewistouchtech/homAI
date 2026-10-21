"""
插件配置API模块 - 处理插件配置的保存和读取
"""
import yaml
import os
from pathlib import Path
from aiohttp import web
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

class PluginConfigAPI:
    def __init__(self, config=None):
        self.config = config or {}
        self.config_file_path = Path(__file__).parent.parent.parent / "config.yaml"
        self.data_config_path = Path(__file__).parent.parent.parent / "data" / ".config.yaml"
        
    async def get_plugin_config(self, request):
        """
        获取插件配置
        """
        try:
            # 优先读取 data/.config.yaml，如果没有则读取 config.yaml
            config_path = self.data_config_path if self.data_config_path.exists() else self.config_file_path
            
            if not config_path.exists():
                return web.json_response({
                    'success': False,
                    'error': '配置文件不存在'
                })
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            plugins_config = config.get('plugins', {})
            
            return web.json_response({
                'success': True,
                'plugins': plugins_config
            })
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取插件配置失败: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            })

    async def save_plugin_config(self, request):
        """
        保存插件配置
        """
        try:
            data = await request.json()
            plugin_name = data.get('plugin_name')
            plugin_config = data.get('config', {})
            
            if not plugin_name:
                return web.json_response({
                    'success': False,
                    'error': '缺少插件名称'
                })
            
            # 读取现有配置
            config_path = self.data_config_path if self.data_config_path.exists() else self.config_file_path
            
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
            else:
                config = {}
            
            # 确保 plugins 部分存在
            if 'plugins' not in config:
                config['plugins'] = {}
            
            # 更新特定插件的配置
            config['plugins'][plugin_name] = plugin_config
            
            # 确保 data 目录存在
            data_dir = self.data_config_path.parent
            data_dir.mkdir(parents=True, exist_ok=True)
            
            # 写入 data/.config.yaml 文件（这样不会覆盖主配置文件）
            with open(self.data_config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
            
            logger.bind(tag=TAG).info(f"插件 {plugin_name} 配置已保存")
            
            return web.json_response({
                'success': True,
                'message': f'{plugin_name} 插件配置已保存'
            })
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"保存插件配置失败: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            })

# 创建全局实例
plugin_config_api = None

def setup_plugin_config_api(config=None):
    global plugin_config_api
    plugin_config_api = PluginConfigAPI(config)
    return plugin_config_api