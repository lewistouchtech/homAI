"""判断层统一接口与工厂。

语义判停（话是否说完、是否对助手说话）和记忆候选精排都属于"类型化判断"，
后端可以是云端 Jev、本地小模型（MLX 读 logits），也可以完全关闭。
调用方只依赖本模块的接口，换后端只改配置。

约定：任何后端失败（超时、异常、服务未起）都必须返回 None，
由调用方回退到原有规则逻辑，绝不能让判断层把主链路带崩。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from config.logger import setup_logging
from core.utils.jev_decision import UtteranceDecision

TAG = __name__
logger = setup_logging()


class DecisionProvider(ABC):
    """判断层后端接口。"""

    name = "base"
    enabled = False
    endpoint_enabled = False
    memory_enabled = False
    preview_non_stream = True
    incomplete_hold_ms = 900
    # 一句话内"没说完"最多延迟提交的次数（2026-09-28 主人裁决：
    # 原来只能 hold 一次，长句中途停顿 >1.2s 就被截断；改为可重复，有上限兜底）
    incomplete_hold_max_count = 3
    endpoint_threshold = 0.55
    memory_threshold = 0.55
    memory_candidate_limit = 30

    @abstractmethod
    async def judge_utterance(
        self, text: str, session_active: bool
    ) -> Optional[UtteranceDecision]:
        """判断一句话是否说完、是否对助手说。无法判断时返回 None。"""

    @abstractmethod
    async def rerank_memory_lines(
        self, query: str, memory_lines: Sequence[str]
    ) -> Optional[List[str]]:
        """从候选记忆里挑出真正相关的行。无法判断时返回 None。"""

    async def judge_rag_relevance(
        self, question: str, chunks: Sequence[str]
    ) -> Optional[List[str]]:
        """判断 RAG 检索片段是否真正回答了用户问题。

        返回被判为相关的片段列表；空列表表示明确不相关。
        无法判断（后端关闭/超时/异常）时返回 None，由调用方按"相关"处理，避免阻塞主链路。
        """
        return None


class NullDecisionProvider(DecisionProvider):
    """默认后端：全部关闭，调用方直接走原有规则逻辑。"""

    name = "none"

    async def judge_utterance(
        self, text: str, session_active: bool
    ) -> Optional[UtteranceDecision]:
        return None

    async def rerank_memory_lines(
        self, query: str, memory_lines: Sequence[str]
    ) -> Optional[List[str]]:
        return None


def _resolve_decision_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从全局配置或连接私有配置里取 Decision 段。"""
    decision = ((config or {}).get("Decision") or {})
    if decision:
        return decision
    # 连接私有配置可能是裁剪过的，只有全局配置才保证包含 Decision 段。
    from config.config_loader import load_config

    return (load_config() or {}).get("Decision") or {}


def _build_provider(decision: Dict[str, Any]) -> DecisionProvider:
    jev_cfg = decision.get("jev") or {}
    local_cfg = decision.get("local_qwen") or {}

    provider_name = str(decision.get("provider") or "").strip().lower()
    if not provider_name:
        # 兼容旧配置：没有 provider 字段时，按 jev 是否开启来决定。
        provider_name = "jev" if jev_cfg.get("enabled") else "none"

    if provider_name in ("none", "off", "disabled"):
        return NullDecisionProvider()

    if provider_name == "jev":
        from core.providers.decision.jev_provider import JevDecisionProvider

        return JevDecisionProvider(jev_cfg)

    if provider_name in ("local_qwen", "local", "qwen"):
        from core.providers.decision.local_qwen_provider import LocalQwenDecisionProvider

        return LocalQwenDecisionProvider(local_cfg)

    if provider_name == "ollaya":
        from core.providers.decision.ollaya_provider import OllayaDecisionProvider

        return OllayaDecisionProvider(decision.get("ollaya") or {})

    logger.bind(tag=TAG).warning(
        f"未知判断层后端 '{provider_name}'，回退为关闭状态"
    )
    return NullDecisionProvider()


_provider: Optional[DecisionProvider] = None


def get_decision_provider(config: Optional[Dict[str, Any]] = None) -> DecisionProvider:
    """返回进程内单例。首次调用时按配置构建。"""
    global _provider
    if _provider is None:
        _provider = _build_provider(_resolve_decision_config(config))
        logger.bind(tag=TAG).info(f"判断层后端: {_provider.name}（enabled={_provider.enabled}）")
    return _provider


def reset_decision_provider_for_tests() -> None:
    global _provider
    _provider = None
