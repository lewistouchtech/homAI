# 通用话术池工具（C1 话术池化）
# 主人指令："其他功能照此办理"——所有固定播报接入随机话术池，让播报有温度不呆板。
# security_listener._pick 的通用化版本，供 idle_guard 提示、LLM 出错提示等场景复用。
import random


def pick(pool, **fmt):
    """从话术池随机选一条并填充变量"""
    return random.choice(pool).format(**fmt)
