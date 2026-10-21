import os
import sys
import importlib
from config.logger import setup_logging
from core.providers.memory.factory import MemoryProviderFactory

logger = setup_logging()

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)


def create_instance(class_name, *args, **kwargs):
    # 创建记忆服务实例
    # args[0] 是 config, args[1] 是 summaryMemory
    provider_type = class_name
    config = args[0] if len(args) > 0 else kwargs.get('config', {})
    summary_memory = args[1] if len(args) > 1 else kwargs.get('summary_memory', None)

    # 如果 config 中没有 summaryMemory，使用传入的参数
    if 'summaryMemory' not in config and summary_memory is not None:
        config = {**config, 'summaryMemory': summary_memory}

    try:
        # 使用工厂创建实例（summary_memory 必须透传：mem_local_short.load_memory 依赖它，
        # 早先漏传成 None 会让后台下发的总结记忆失效）
        return MemoryProviderFactory.create_provider(provider_type, config, summary_memory)
    except ValueError as e:
        # 如果工厂不支持，则尝试原来的实现
        provider_path = os.path.join(project_root, 'core', 'providers', 'memory', provider_type, f'{provider_type}.py')

        if os.path.exists(provider_path):
            lib_name = f'core.providers.memory.{provider_type}.{provider_type}'
            if lib_name not in sys.modules:
                sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
            return sys.modules[lib_name].MemoryProvider(config, summary_memory)

        raise e  # 如果工厂和原实现都不支持，则抛出异常