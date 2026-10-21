"""Memory provider factory.

设计要点（2026-09-12）：
- **惰性导入**：不在模块顶层 import 各 provider。此前顶层全量 import 导致
  任何一个 provider 的类名/文件名不匹配时，`core/utils/memory.py` 连 import 都失败，
  整个记忆子系统一起崩、服务起不来。
  现在坏掉的那一个只在被选中时报错，其余 provider 照常可用。
- **类名双向兜底**：优先用小驼峰约定名（OpenVikingProvider 等），找不到再退到
  上游 xiaozhi 惯例的 `MemoryProvider`，反向亦然 —— 避免并发改名造成互断。
- **构造函数兼容**：先试 (config, summary_memory)，TypeError 再退到 (config)。

历史（2026-09-17）：曾移除 mem0 / mem0ai / mempalace 三个提供者及其注册项
（长期记忆统一为 OpenViking，见 docs/plans/2026-09-15_OpenViking记忆系统迁移记录.md）。
⚠️ 该历史已于后续反转，勿据此认为 mem0 不存在：2026-10-06 重新接回 mem0（OpenViking
检索恒 0，上游 issue #4482），2026-10-13 记忆系统大修后 mem0 为唯一主用长期记忆后端
（scope 模型 family/m:<id>、outbox 写入、中文抽取提示词，见
docs/plans/2026-10-13_记忆系统调整方案.md）。
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, Optional, Tuple

from .base import BaseMemoryProvider

# provider_type -> (模块路径, 候选类名, 可读标签)
_REGISTRY: Dict[str, Tuple[str, Tuple[str, ...], str]] = {
    "mem_local_short": (
        "core.providers.memory.mem_local_short.mem_local_short",
        ("MemLocalShortProvider", "MemoryProvider"),
        "本地短期记忆",
    ),
    "openviking": (
        "core.providers.memory.openviking.provider",
        ("OpenVikingProvider", "MemoryProvider"),
        "OpenViking 长期记忆",
    ),
    "mem0": (
        "core.providers.memory.mem0.provider",
        ("Mem0Provider", "MemoryProvider"),
        "mem0 长期记忆",
    ),
}

# 兼容旧的目录式命名（如 selected_module 写成别名的情况）
_ALIASES = {"mem_local": "mem_local_short"}


class MemoryProviderFactory:
    """按类型创建记忆提供者（惰性导入）。"""

    @staticmethod
    def available() -> list:
        return sorted(_REGISTRY)

    @staticmethod
    def create_provider(
        provider_type: str,
        config: Dict[str, Any],
        summary_memory: Optional[str] = None,
    ) -> BaseMemoryProvider:
        key = (provider_type or "").strip().lower()
        key = _ALIASES.get(key, key)

        if key not in _REGISTRY:
            raise ValueError(
                f"不支持的记忆服务类型: {provider_type}"
                f"（可用: {', '.join(_REGISTRY)}）"
            )

        module_path, class_candidates, label = _REGISTRY[key]

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise RuntimeError(
                f"记忆提供者「{label}」加载失败（{module_path}）：{e}。"
                f"其他记忆类型仍可用，切换 selected_module.Memory 即可临时绕过。"
            ) from e

        provider_cls = None
        for name in class_candidates:
            cls = getattr(module, name, None)
            if isinstance(cls, type):
                provider_cls = cls
                break
        if provider_cls is None:
            raise RuntimeError(
                f"记忆提供者「{label}」模块存在，但找不到类 {list(class_candidates)}"
                f"（实际导出: {[n for n in dir(module) if n.endswith('Provider')]}"
            )

        try:
            return provider_cls(config, summary_memory)
        except TypeError:
            return provider_cls(config)


def create_provider(
    provider_type: str,
    config: Dict[str, Any],
    summary_memory: Optional[str] = None,
) -> BaseMemoryProvider:
    """模块级便捷入口。"""
    return MemoryProviderFactory.create_provider(provider_type, config, summary_memory)