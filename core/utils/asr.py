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
    # 创建ASR实例
    # args[0] 是 config, args[1] 是 delete_audio
    provider_type = class_name
    config = args[0] if len(args) > 0 else kwargs.get('config', {})
    delete_audio = args[1] if len(args) > 1 else kwargs.get('delete_audio', True)
    
    # 使用绝对路径检查 - 支持两种格式：
    # 1. core/providers/asr/{provider_type}/{provider_type}.py
    # 2. core/providers/asr/{provider_type}.py
    provider_path1 = os.path.join(project_root, 'core', 'providers', 'asr', provider_type, f'{provider_type}.py')
    provider_path2 = os.path.join(project_root, 'core', 'providers', 'asr', f'{provider_type}.py')
    
    if os.path.exists(provider_path1):
        lib_name = f'core.providers.asr.{provider_type}.{provider_type}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].ASRProvider(config, delete_audio)
    elif os.path.exists(provider_path2):
        lib_name = f'core.providers.asr.{provider_type}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].ASRProvider(config, delete_audio)

    raise ValueError(f"不支持的ASR类型: {provider_type}，请检查该配置的type是否设置正确")