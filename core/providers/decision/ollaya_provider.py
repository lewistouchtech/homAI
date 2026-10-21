"""ollaya 本地决策模型后端（winnow:e4b 等）。

通过 ollaya 守护进程（默认 127.0.0.1:11435）调用本地决策模型，
协议与 TypeSafe Jev 线兼容（POST /v1/systemone），但 ollaya 要求
请求体带 model 字段、不需要 API key。模型常驻 llama.cpp Metal，
中文实测：路由 8/9、判停 6/6、约 180ms（详见 docs/调研/2026-09-29）。

与 Jev 云端后端的差别：
- 本地运行，无外网依赖、零调用费；ollaya 未启动时全部回退 None
- 题面用中文（实测分离度：完整句 ~0.97 vs 残句 ~0.04）
- 概率经作者拟合温度（calibrated），阈值语义与 Jev 一致
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import httpx

from config.logger import setup_logging
from core.providers.decision.base import DecisionProvider
from core.utils.jev_decision import UtteranceDecision

TAG = __name__
logger = setup_logging()

# 判停题面：与本机实测（6 题中文全对、分离度极大）保持一致
COMPLETE_QUESTION = {
    "type": "noul",
    "instructions": "判断用户这句话是否已经说完。",
    "criteria": {
        "true": "已经说完，是一个完整、可以立即执行或立即回答的请求。",
        "false": "还没说完，说话人明显还会继续往下说。",
    },
}
ADDRESSED_QUESTION = {
    "type": "noul",
    "instructions": "判断这句话是不是在对助手说话。",
    "criteria": {
        "true": "是在对助手说话，或是在延续上一轮对助手的请求。",
        "false": "是背景人声、电视声音、自言自语，或是在跟别人说话。",
    },
}
MEMORY_QUESTION_TEMPLATE = {
    "type": "noul",
    "instructions": "判断这条记忆是否与当前请求相关、是否应该提供给助手参考。",
    "criteria": {
        "true": "知道这条记忆会改变助手的回答、动作、提醒或省略。",
        "false": "这条记忆与请求无关，或只是话题相同，有没有它处理方式都一样。",
    },
}


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class OllayaDecisionProvider(DecisionProvider):
    """ollaya 本地决策服务（/v1/systemone，Jev 线兼容）。"""

    name = "ollaya"

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        cfg = config or {}
        endpoint_cfg = cfg.get("endpoint") or {}
        memory_cfg = cfg.get("memory") or {}

        self.enabled = bool(cfg.get("enabled", False))
        self.endpoint_enabled = bool(endpoint_cfg.get("enabled", True))
        self.preview_non_stream = bool(endpoint_cfg.get("preview_non_stream", True))
        self.ask_addressed = bool(endpoint_cfg.get("ask_addressed", False))
        self.memory_enabled = bool(memory_cfg.get("enabled", False))
        self.base_url = str(cfg.get("base_url", "http://127.0.0.1:11435")).rstrip("/")
        self.model = str(cfg.get("model", "winnow:e4b"))
        self.timeout_s = max(0.1, float(cfg.get("timeout_ms", 600)) / 1000)
        # 实测分离度极大（0.97 vs 0.04），0.5 阈值足够；
        # 判停取偏高宁可多等一轮（hold），也不要提前打断
        self.endpoint_threshold = float(
            endpoint_cfg.get("complete_threshold", endpoint_cfg.get("threshold", 0.5))
        )
        # 反向语义：只有明显不像对助手说时才丢弃
        self.addressed_drop_threshold = float(endpoint_cfg.get("addressed_drop_threshold", 0.2))
        self.incomplete_hold_ms = max(
            100, min(3000, _int_or(endpoint_cfg.get("incomplete_hold_ms"), 900))
        )
        self.incomplete_hold_max_count = max(
            1, min(10, _int_or(endpoint_cfg.get("incomplete_hold_max_count"), 3))
        )
        self.memory_threshold = float(memory_cfg.get("threshold", 0.5))
        self.memory_candidate_limit = max(
            _int_or(memory_cfg.get("candidate_limit", 20), 20), 5
        )
        self.max_memory_questions = max(
            2, min(50, _int_or(memory_cfg.get("max_questions", 20), 20))
        )
        self._transport = transport
        self.last_diagnostics: Dict[str, Any] = {}

    async def _ask(
        self, state: Any, questions: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled or not questions:
            return None
        payload = {"model": self.model, "state": state, "questions": questions}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}/v1/systemone", json=payload
                )
                response.raise_for_status()
                data = response.json()
                answers = data.get("answers")
                diagnostics = data.get("diagnostics")
                self.last_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                return answers if isinstance(answers, dict) else None
        except Exception as exc:
            # 判断层不可用不能影响主链路，只记一条日志后回退
            logger.bind(tag=TAG).warning(f"ollaya 决策请求失败，回退原判停: {exc}")
            return None

    async def judge_utterance(
        self, text: str, session_active: bool
    ) -> Optional[UtteranceDecision]:
        if not self.enabled or not self.endpoint_enabled or not text.strip():
            return None

        # 实测：state 只放原文最准——加 session_active 字段会显著抬高"完整"概率
        state = {"utterance": text}
        questions: Dict[str, Dict[str, Any]] = {"complete": dict(COMPLETE_QUESTION)}
        if self.ask_addressed:
            questions["addressed"] = dict(ADDRESSED_QUESTION)

        answers = await self._ask(state, questions)
        if answers is None:
            return None

        complete_p = self._noul_probability(answers.get("complete"))
        addressed_p = (
            self._noul_probability(answers.get("addressed"))
            if self.ask_addressed
            else -1.0
        )
        addressed: Optional[bool] = None
        if addressed_p >= 0:
            addressed = addressed_p > self.addressed_drop_threshold
        return UtteranceDecision(
            addressed=addressed,
            complete=self._optional_bool(complete_p),
            addressed_probability=addressed_p,
            complete_probability=complete_p,
            diagnostics=dict(self.last_diagnostics),
        )

    async def rerank_memory_lines(
        self, query: str, memory_lines: Sequence[str]
    ) -> Optional[List[str]]:
        if not self.enabled or not self.memory_enabled or not memory_lines:
            return None

        candidates = [line.strip() for line in memory_lines if line and line.strip()]
        candidates = candidates[: self.max_memory_questions]
        if not candidates:
            return []

        state = {
            "current_request": query,
            "memories": [
                {"id": f"M{index:03d}", "text": text}
                for index, text in enumerate(candidates, start=1)
            ],
        }
        questions = {
            f"M{index:03d}": dict(MEMORY_QUESTION_TEMPLATE)
            for index in range(1, len(candidates) + 1)
        }
        answers = await self._ask(state, questions)
        if answers is None:
            return None

        selected = []
        for index in range(1, len(candidates) + 1):
            probability = self._noul_probability(answers.get(f"M{index:03d}"))
            if probability >= self.memory_threshold:
                selected.append(candidates[index - 1])
        return selected

    @staticmethod
    def _noul_probability(answer: Any) -> float:
        if not isinstance(answer, dict):
            return -1.0
        try:
            return max(0.0, min(1.0, float(answer.get("noul", -1))))
        except (TypeError, ValueError):
            return -1.0

    def _optional_bool(self, probability: float) -> Optional[bool]:
        if probability < 0:
            return None
        return probability >= self.endpoint_threshold
