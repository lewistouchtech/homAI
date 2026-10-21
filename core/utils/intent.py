import os
import sys
from config.logger import setup_logging
import importlib

logger = setup_logging()

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)


def create_instance(class_name, *args, **kwargs):
    # 创建intent实例
    # args[0] 是 config
    provider_type = class_name
    config = args[0] if len(args) > 0 else kwargs.get('config', {})
    
    # 使用绝对路径检查
    provider_path = os.path.join(project_root, 'core', 'providers', 'intent', provider_type, f'{provider_type}.py')
    
    if os.path.exists(provider_path):
        lib_name = f'core.providers.intent.{provider_type}.{provider_type}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].IntentProvider(config)

    raise ValueError(f"不支持的intent类型: {provider_type}，请检查该配置的type是否设置正确")