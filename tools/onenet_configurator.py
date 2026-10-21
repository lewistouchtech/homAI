#!/usr/bin/env python3
"""
OneNET 配置工具
用于配置钛极智能桌面呼设备与中国移动物联网开放平台的连接
"""

import os
import yaml
import json
from pathlib import Path

def load_config():
    """加载现有配置"""
    config_path = Path(__file__).parent.parent / "config" / "onenet_config.yaml"
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}

def save_config(config):
    """保存配置"""
    config_path = Path(__file__).parent.parent / "config" / "onenet_config.yaml"
    os.makedirs(config_path.parent, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

def interactive_setup():
    """交互式配置设置"""
    print("=" * 60)
    print("OneNET 云平台健康数据对接配置工具")
    print("=" * 60)
    
    config = load_config()
    
    print("\n当前配置:")
    product_id = config.get('product', {}).get('id', '')
    device_name = config.get('device', {}).get('name', '')
    access_token = config.get('auth', {}).get('access_token', '')
    
    print(f"  产品ID: {product_id if product_id else '<未设置>'}")
    print(f"  设备名称: {device_name if device_name else '<未设置>'}")
    print(f"  访问令牌: {'已设置' if access_token else '<未设置>'}")
    
    print("\n" + "-" * 60)
    print("请提供以下信息（直接回车跳过，保持当前设置）:")
    
    # 产品ID
    current_product_id = config.get('product', {}).get('id', '')
    new_product_id = input(f"\n产品ID (当前: {current_product_id if current_product_id else '未设置'}) > ").strip()
    if new_product_id:
        current_product_id = new_product_id
    
    # 设备名称
    current_device_name = config.get('device', {}).get('name', '')
    new_device_name = input(f"设备名称/IMEI (当前: {current_device_name if current_device_name else '未设置'}) > ").strip()
    if new_device_name:
        current_device_name = new_device_name
    
    # 访问令牌
    current_access_token = config.get('auth', {}).get('access_token', '')
    new_access_token = input(f"访问令牌 (当前: {'已设置' if current_access_token else '未设置'}) > ").strip()
    if new_access_token:
        current_access_token = new_access_token
    
    # API密钥
    current_api_key = config.get('auth', {}).get('api_key', '')
    new_api_key = input(f"API密钥 (当前: {'已设置' if current_api_key else '未设置'}) > ").strip()
    if new_api_key:
        current_api_key = new_api_key
    
    # 更新配置
    if 'product' not in config:
        config['product'] = {}
    if 'device' not in config:
        config['device'] = {}
    if 'auth' not in config:
        config['auth'] = {}
    
    config['product']['id'] = current_product_id
    config['device']['name'] = current_device_name
    config['auth']['access_token'] = current_access_token
    config['auth']['api_key'] = current_api_key
    
    # 保存配置
    save_config(config)
    
    print("\n" + "=" * 60)
    print("配置已保存!")
    print("=" * 60)
    print(f"产品ID: {config['product']['id']}")
    print(f"设备名称: {config['device']['name']}")
    print(f"访问令牌: {'已设置' if config['auth']['access_token'] else '未设置'}")
    print(f"API密钥: {'已设置' if config['auth']['api_key'] else '未设置'}")
    
    print("\n提示:")
    print("1. 重启服务器使配置生效")
    print("2. 使用以下命令重启服务器:")
    print("   cd /Users/bot-eva/.openclaw/projects/homebrain/server/homebrain-server")
    print("   pkill -f 'python.*app.py'")
    print("   python3 app.py")
    print("\n3. 测试API: curl http://localhost:8003/api/onenet/latest")

if __name__ == "__main__":
    interactive_setup()