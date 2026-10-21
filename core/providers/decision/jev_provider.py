"""云端 Jev 后端。

只是把已有的 `core.utils.jev_decision.JevDecisionClient` 适配到统一接口，
不改动客户端本身的行为（含超时、回退与阈值语义）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from core.providers.decision.base import DecisionProvider
from core.utils.jev_decision import JevDecisionClient, UtteranceDecision


class JevDecisionProvider(DecisionProvider):
    """TypeSafe Jev（云端）。需要 api_key，否则自动保持关闭。"""

    name = "jev"

    def __init__(self, config: Optional[Dict[str, Any]] = None, client: Optional[JevDecisionClient] = None):
        self._client = client or JevDecisionClient(config or {})
        self.enabled = self._client.enabled
        self.endpoint_enabled = self._client.endpoint_enabled
        self.memory_enabled = self._client.memory_enabled
        self.preview_non_stream = self._client.preview_non_stream
        self.incomplete_hold_ms = self._client.incomplete_hold_ms
        self.endpoint_threshold = self._client.endpoint_threshold
        self.memory_threshold = self._client.memory_threshold
        self.memory_candidate_limit = self._client.memory_candidate_limit

    @property
    def client(self) -> JevDecisionClient:
        return self._client

    async def judge_utterance(
        self, text: str, session_active: bool
    ) -> Optional[UtteranceDecision]:
        return await self._client.judge_utterance(text, session_active)

    async def rerank_memory_lines(
        self, query: str, memory_lines: Sequence[str]
    ) -> Optional[List[str]]:
        return await self._client.rerank_memory_lines(query, memory_lines)

    async def judge_rag_relevance(
        self, question: str, chunks: Sequence[str]
    ) -> Optional[List[str]]:
        return await self._client.judge_rag_relevance(question, chunks)
