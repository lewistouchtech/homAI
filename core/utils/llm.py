import os
import sys

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)

from config.logger import setup_logging
import importlib

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    # 创建LLM实例
    # class_name 是 provider_type（type）
    # args[0] 是 config（modules_initialize.py 只传递一个参数）
    provider_type = class_name
    config = args[0] if len(args) > 0 else kwargs.get('config', {})
    
    # 使用绝对路径检查
    provider_path = os.path.join(project_root, 'core', 'providers', 'llm', provider_type, f'{provider_type}.py')
    
    # 根据 type 加载对应的适配器
    if os.path.exists(provider_path):
        lib_name = f'core.providers.llm.{provider_type}.{provider_type}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].LLMProvider(config)

    raise ValueError(f"不支持的LLM类型: {provider_type}，请检查该配置的type是否设置正确")
