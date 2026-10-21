"""本地小千问后端（读 logits，不生成文本）。

后端进程是 `services/decision-mlx/server.py`：用 mlx-lm 加载一个小 Qwen，
对每道题只读"合法标签 token 在答案边界上的 logits"，重归一化后返回概率。
请求/响应形态与 Jev 对齐（`state` + `questions` -> `answers`），
所以解析逻辑一致，将来换后端不需要改调用方。

与 Jev 的差别：
- 提问用中文（实测中文提问下判停 24 题 23 对，概率分离度 +0.859）
- 概率是"合法选项上的重归一化结果"，未做温度校准；阈值只能在自建评测集上定
- 记忆精排默认关闭：逐条判断每条一次前向，20 条约 1~2 秒，收益不匹配
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import httpx

from config.logger import setup_logging
from core.providers.decision.base import DecisionProvider
from core.utils.jev_decision import UtteranceDecision

TAG = __name__
logger = setup_logging()

# 判停题面：与本机实测（24 题中文，acc 0.96 / 分离度 +0.859）保持一致
COMPLETE_QUESTION = {
    "type": "noul",
    "instructions": "判断用户这句话是否已经说完。",
    "criteria": {
        "true": "已经说完，是一个完整、可以立即执行或立即回答的请求。",
        "false": "还没说完，说话人明显还会继续往下说，例如以“把”“那个”“然后”“还有”结尾。",
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


class LocalQwenDecisionProvider(DecisionProvider):
    """本地判断服务（MLX 读 logits）。服务不可达时全部返回 None。"""

    name = "local_qwen"

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
        self.base_url = str(cfg.get("base_url", "http://127.0.0.1:8099")).rstrip("/")
        self.model = str(cfg.get("model", "qwen3.5-4b"))
        self.timeout_s = max(0.1, float(cfg.get("timeout_ms", 400)) / 1000)
        # 判停阈值取偏高：宁可多等一轮（hold 900ms 后照样提交），
        # 也不要把残句当成说完而提前打断——后者正是当前语音链路的痛点。
        # 24 题中文实测扫描：0.60 -> 误提前 2 / 误延长 0；0.75 -> 1 / 1；0.80 -> 0 / 3
        self.endpoint_threshold = float(
            endpoint_cfg.get("complete_threshold", endpoint_cfg.get("threshold", 0.8))
        )
        # "是否在对助手说话"是反向语义：只有明显不像对助手说时才丢弃，
        # 不确定时必须保留，否则会把用户的正常指令当背景人声扔掉。
        self.addressed_drop_threshold = float(endpoint_cfg.get("addressed_drop_threshold", 0.2))
        self.incomplete_hold_ms = max(
            100, min(3000, _int_or(endpoint_cfg.get("incomplete_hold_ms"), 900))
        )
        self.incomplete_hold_max_count = max(
            1, min(10, _int_or(endpoint_cfg.get("incomplete_hold_max_count"), 3))
        )
        self.memory_threshold = float(memory_cfg.get("threshold", 0.6))
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
        payload = {"state": state, "model": self.model, "questions": questions}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, transport=self._transport
            ) as client:
                response = await client.post(f"{self.base_url}/v1/decide", json=payload)
                response.raise_for_status()
                data = response.json()
                answers = data.get("answers")
                diagnostics = data.get("diagnostics")
                self.last_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                return answers if isinstance(answers, dict) else None
        except Exception as exc:
            # 判断层不可用不能影响主链路，只记一条日志后回退
            logger.bind(tag=TAG).warning(f"本地判断服务请求失败，回退原判停: {exc}")
            return None

    async def judge_utterance(
        self, text: str, session_active: bool
    ) -> Optional[UtteranceDecision]:
        if not self.enabled or not self.endpoint_enabled or not text.strip():
            return None

        state = {"utterance": text, "session_active": bool(session_active)}
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
