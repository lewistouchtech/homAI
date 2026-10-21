"""判断层（System-1 决策）provider 包。

对外只暴露工厂函数，调用方不感知具体后端：

    from core.providers.decision import get_decision_provider
    provider = get_decision_provider(conn.config)
    decision = await provider.judge_utterance(text, session_active)
"""
from core.providers.decision.base import (
    DecisionProvider,
    NullDecisionProvider,
    UtteranceDecision,
    get_decision_provider,
    reset_decision_provider_for_tests,
)

__all__ = [
    "DecisionProvider",
    "NullDecisionProvider",
    "UtteranceDecision",
    "get_decision_provider",
    "reset_decision_provider_for_tests",
]
