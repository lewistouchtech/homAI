"""
配置同步API模块
用于处理从控制台同步AI配置到服务器配置文件的请求
"""

import os
import json
import re
from datetime import datetime
from typing import Dict, Any
from config.logger import setup_logging

TAG = "config_sync_api"

# 获取配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.yaml")
BACKUP_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "config_backups")

# 确保备份目录存在
os.makedirs(BACKUP_DIR, exist_ok=True)

logger = setup_logging()

class ConfigSyncAPI:
    """配置同步API处理器"""
    
    def __init__(self):
        self.config_path = CONFIG_PATH
        self.backup_dir = BACKUP_DIR
        self.logger = logger.bind(tag=TAG)
    
    def backup_config_file(self) -> Dict[str, Any]:
        """备份配置文件"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"config.yaml.backup.{timestamp}"
            backup_path = os.path.join(self.backup_dir, backup_filename)
            
            # 读取原配置
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_content = f.read()
            
            # 写入备份
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(config_content)
            
            self.logger.info(f"配置文件已备份: {backup_path}")
            
            return {
                "success": True,
                "backup_path": backup_path,
                "message": "配置文件备份成功"
            }
            
        except Exception as e:
            self.logger.error(f"备份配置文件失败: {e}")
            return {
                "success": False,
                "error": f"备份失败: {str(e)}"
            }
    
    def get_current_prompt(self) -> Dict[str, Any]:
        """获取当前配置文件中的prompt配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_content = f.read()
            
            # 查找prompt配置
            # 查找prompt: | 开始的行
            prompt_match = re.search(r'prompt:\s*\|[\s\S]*?(?=\n\w+:|$)', config_content)
            
            if not prompt_match:
                return {
                    "success": False,
                    "error": "未找到prompt配置"
                }
            
            prompt_content = prompt_match.group(0)
            
            return {
                "success": True,
                "prompt_content": prompt_content,
                "config_path": self.config_path
            }
            
        except Exception as e:
            self.logger.error(f"获取当前prompt失败: {e}")
            return {
                "success": False,
                "error": f"获取失败: {str(e)}"
            }
    
    def sync_prompt_config(self, prompt_config: str) -> Dict[str, Any]:
        """同步prompt配置到服务器配置文件

        支持替换任意格式的顶层 prompt 块：
        - block 格式（prompt: |）—— 控制台生成的默认格式
        - 单引号多行格式（prompt: '...'）—— 手工编辑过的 config.yaml
        """
        try:
            # 先备份原配置
            backup_result = self.backup_config_file()
            if not backup_result["success"]:
                self.logger.warning("备份失败，但继续尝试更新配置")

            # 读取原配置
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_content = f.read()

            # 定位顶层 prompt 块：从 ^prompt: 行开始，到下一个顶层 key（^字母开头:）或文件尾
            lines = config_content.split('\n')
            start = end = None
            for i, ln in enumerate(lines):
                if re.match(r'^prompt:', ln) and start is None:
                    start = i
                    continue
                if start is not None and i > start and re.match(r'^[A-Za-z_][A-Za-z0-9_]*:', ln):
                    end = i
                    break
            if start is None:
                return {"success": False, "error": "在原配置中找不到prompt配置位置"}
            if end is None:
                end = len(lines)

            # 归一化新 prompt：允许前端直接传 prompt: | 块或纯文本
            new_prompt = prompt_config.strip()
            if not new_prompt.startswith("prompt:"):
                # 纯文本 → 转为 YAML block 格式，缩进两空格
                body = "\n".join("  " + ln if ln.strip() else "" for ln in new_prompt.split('\n'))
                new_prompt = "prompt: |\n" + body

            # 提取 prompt 内容的正文行（去掉 "prompt:" 头行）保持缩进
            body_lines = new_prompt.split('\n')[1:]

            new_block = [new_prompt.split('\n')[0]] + body_lines
            new_lines = lines[:start] + new_block + lines[end:]
            new_config_content = '\n'.join(new_lines)

            # 写入前验证 YAML 有效性
            try:
                import yaml as _yaml
                _yaml.safe_load(new_config_content)
            except Exception as ye:
                return {"success": False, "error": f"新配置 YAML 校验失败，已放弃写入: {str(ye)[:120]}"}

            # 写入新配置
            with open(self.config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)

            self.logger.info(f"配置已更新，原 prompt {end-start} 行，新 prompt {len(new_block)} 行")

            # 后台人设是唯一权威源：配置写入后异步同步到 Ltm identity.md / soul.md
            try:
                import threading
                from core.utils.ltm_sync import sync_assistant_identity_to_LTM

                threading.Thread(
                    target=sync_assistant_identity_to_LTM, daemon=True
                ).start()
            except Exception as _sync_err:
                self.logger.warning(f"同步人设到 Ltm 失败（不影响配置保存）: {_sync_err}")

            return {
                "success": True,
                "config_path": self.config_path,
                "message": "配置更新成功，请重启服务器使配置生效"
            }

        except Exception as e:
            self.logger.error(f"同步配置失败: {e}")
            return {
                "success": False,
                "error": f"同步失败: {str(e)}"
            }

def create_config_sync_api_router(web):
    """创建配置同步API路由 - 返回路由列表供app.add_routes使用"""
    config_sync = ConfigSyncAPI()
    
    async def sync_config(request):
        """同步配置API"""
        try:
            data = await request.json()
            prompt_config = data.get("prompt_config", "")
            
            if not prompt_config:
                return web.json_response({"success": False, "error": "未提供prompt配置"})
            
            result = config_sync.sync_prompt_config(prompt_config)
            return web.json_response(result)
            
        except Exception as e:
            logger.error(f"API调用失败: {e}")
            return web.json_response({"success": False, "error": f"API调用失败: {str(e)}"})
    
    async def backup_config(request):
        """备份配置API"""
        try:
            result = config_sync.backup_config_file()
            return web.json_response(result)
        except Exception as e:
            logger.error(f"备份API调用失败: {e}")
            return web.json_response({"success": False, "error": f"备份失败: {str(e)}"})
    
    async def get_current_prompt(request):
        """获取当前prompt配置API"""
        try:
            result = config_sync.get_current_prompt()
            return web.json_response(result)
        except Exception as e:
            logger.error(f"获取当前prompt API调用失败: {e}")
            return web.json_response({"success": False, "error": f"获取失败: {str(e)}"})
    
    logger.info("配置同步API路由已创建")
    return [
        web.post("/api/config/sync", sync_config),
        web.post("/api/config/backup", backup_config),
        web.get("/api/config/current-prompt", get_current_prompt),
    ]