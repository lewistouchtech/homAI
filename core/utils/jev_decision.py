"""TypeSafe Jev 决策客户端。

Jev 只接受文本并返回类型化概率。本模块统一封装 HTTP 细节，
调用方在请求失败时可以安全回退到原有逻辑。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


@dataclass
class UtteranceDecision:
    addressed: Optional[bool]
    complete: Optional[bool]
    addressed_probability: float
    complete_probability: float
    diagnostics: Optional[Dict[str, Any]] = None


class JevDecisionClient:
    def __init__(self, config: Dict[str, Any], transport: Optional[httpx.AsyncBaseTransport] = None):
        cfg = config or {}
        endpoint_cfg = cfg.get("endpoint") or {}
        memory_cfg = cfg.get("memory") or {}

        api_key = str(cfg.get("api_key") or os.getenv("TYPESAFE_API_KEY") or "").strip()
        if api_key.startswith("${"):
            api_key = ""

        self.enabled = bool(cfg.get("enabled", False)) and bool(api_key)
        self.endpoint_enabled = bool(endpoint_cfg.get("enabled", True))
        self.preview_non_stream = bool(endpoint_cfg.get("preview_non_stream", True))
        self.memory_enabled = bool(memory_cfg.get("enabled", True))
        self.api_key = api_key
        self.base_url = str(cfg.get("base_url", "https://api.typesafe.ai")).rstrip("/")
        self.model = str(cfg.get("model", "jev-latest"))
        self.timeout_s = max(0.1, float(cfg.get("timeout_ms", 800)) / 1000)
        # 代理（2026-10-19）：Mini 直连 api.typesafe.ai 返回 451（地区封锁），
        # 需走本机 Clash 的 HTTP 代理。只对 JEV 请求生效，不影响豆包等国内服务。
        self.proxy = str(
            cfg.get("proxy") or os.getenv("HOMEBRAIN_JEV_PROXY") or ""
        ).strip()
        if self.proxy.startswith("${"):
            self.proxy = ""
        self.endpoint_threshold = float(endpoint_cfg.get("threshold", 0.55))
        self.incomplete_hold_ms = max(
            100, min(3000, int(endpoint_cfg.get("incomplete_hold_ms", 900)))
        )
        self.memory_threshold = float(memory_cfg.get("threshold", 0.55))
        # 记忆精排独立超时（2026-09-29）：精排一次要带上全部候选（最多几十个
        # question），本地 ollaya 逐个评估比判停慢一个量级；判停 800ms 不适用。
        # 记忆查询本身异步非阻塞（100ms 快路径，结果下轮复用），给足时间即可。
        self.memory_timeout_s = max(
            0.5, float(memory_cfg.get("timeout_ms", 5000)) / 1000
        )
        self.memory_candidate_limit = max(
            self._int_or(memory_cfg.get("candidate_limit", 30), 30), 5
        )
        self.max_memory_questions = max(
            5, min(100, self._int_or(memory_cfg.get("max_questions", 50), 50))
        )
        self._transport = transport

        # 主链路标识（写入影子对比记录，便于分辨 11435/ollaya 或 8099/decision-mlx）
        if "typesafe.ai" in self.base_url:
            self.provider_name = "typesafe-cloud"
        elif ":11435" in self.base_url:
            self.provider_name = "ollaya"
        elif ":8099" in self.base_url:
            self.provider_name = "decision-mlx"
        else:
            self.provider_name = "custom"

        # 影子对比（2026-10-01 主人批准）：主链路照常执行，同一请求异步复制给
        # 云端 TypeSafe Jev，只记录不执行，用于 ollaya vs 云端 JEV 的效果对比。
        # key 只走环境变量 TYPESAFE_SHADOW_API_KEY（.env，gitignored），不入 git；
        # 影子请求断网/超时/失败全部静默，不影响正常对话。
        shadow_cfg = cfg.get("shadow") or {}
        shadow_key = str(
            shadow_cfg.get("api_key") or os.getenv("TYPESAFE_SHADOW_API_KEY") or ""
        ).strip()
        if shadow_key.startswith("${"):
            shadow_key = ""
        self.shadow_enabled = bool(shadow_cfg.get("enabled", False)) and bool(shadow_key)
        self.shadow_api_key = shadow_key
        self.shadow_base_url = str(
            shadow_cfg.get("base_url", "https://api.typesafe.ai")
        ).rstrip("/")
        self.shadow_model = str(shadow_cfg.get("model", "jev-latest"))
        self.shadow_timeout_s = max(
            0.5, float(shadow_cfg.get("timeout_ms", 3000)) / 1000
        )
        self.shadow_log_path = str(
            shadow_cfg.get("log_path", "data/decision_shadow_log.jsonl")
        )
        self._shadow_tasks: set = set()

        # 主判健康监控（2026-10-04，针对影子报告第二节的整日失效事故）：
        # 2026-10-03 本地主判 3196 次请求全部失败（ollaya runner 持续 502），
        # 而当时只逐条打 warning 并静默回退，无告警、无熔断，故障持续一整天
        # 都没人发现。这里加连续失败计数：达到阈值打 error 并周期性重发，
        # 恢复正常时打 info 记录恢复时刻，便于事后定位故障窗口。
        health_cfg = cfg.get("health") or {}
        self.health_failure_threshold = max(
            1, self._int_or(health_cfg.get("failure_threshold"), 10)
        )
        self.health_repeat_every = max(
            1, self._int_or(health_cfg.get("repeat_every"), 50)
        )
        self._consecutive_failures = 0
        self._total_failures = 0
        self._health_warned = False

        if cfg.get("enabled") and not self.enabled:
            logger.bind(tag=TAG).warning(
                "Jev 已配置开启，但缺少 TYPESAFE_API_KEY/api_key，决策层保持关闭"
            )

    @staticmethod
    def _int_or(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def _ask(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled or not questions:
            return None
        # 熔断（2026-10-12）：连续失败 ≥3 次说明判停服务正被抢占/过载（会话里实测连续 ReadTimeout 6+ 次）。
        # 继续请求只会：① 每次白等超时；② 超时的请求服务端仍在算，叠加占 GPU，把 TTS 合成拖到慢于实时。
        # 冷却 45 秒内直接走本地兜底，冷却后放一次探测请求，成功即恢复。
        if getattr(self, "_breaker_until", 0.0) > time.time():
            # 仍计入健康统计（degraded 告警口径不变），但不延长冷却期，否则永远没有探测机会
            self._record_ask_failure("熔断冷却中，已跳过请求", extend_breaker=False)
            return None

        payload = {
            "state": state,
            "model": self.model,
            "questions": questions,
        }
        _started = time.perf_counter()
        try:
            # 2026-10-11 事件循环隔离：判停预判跑在 VAD/音频处理高峰时，主事件
            # 循环被同步任务（ASR/TTS/回调）占用会把这次 await 拖住 → ReadTimeout
            # 误判（38 次连续失败实证：同端点独立进程 0.5s 正常）。
            # 改为线程内同步请求：超时在线程里独立生效，不受事件循环调度影响。
            import asyncio

            answers = await asyncio.wait_for(
                asyncio.to_thread(self._ask_sync, payload, timeout_s),
                timeout=(timeout_s or self.timeout_s) + 1.0,
            )
            if answers is None:
                return None
            self._record_ask_success()
            return answers
        except Exception as exc:
            self._record_ask_failure(f"{type(exc).__name__}: {exc}")
            return None
        finally:
            self._last_ask_latency_ms = int((time.perf_counter() - _started) * 1000)

    def _ask_sync(self, payload: Dict, timeout_s: Optional[float]) -> Optional[Dict]:
        """同步 HTTP 请求（跑在线程里，超时独立生效，不占事件循环）。"""
        try:
            client_kwargs = {
                "timeout": timeout_s or self.timeout_s,
                "transport": self._transport_sync(),
            }
            # httpx 不允许 proxy 与 transport 同时传；生产环境 transport 为 None
            if self.proxy and self._transport is None:
                client_kwargs["proxy"] = self.proxy
            with httpx.Client(**client_kwargs) as client:
                response = client.post(
                    f"{self.base_url}/v1/systemone",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                answers = data.get("answers")
                if isinstance(answers, dict):
                    return answers
                self._record_ask_failure("响应缺少 answers 字段")
                return None
        except Exception as exc:
            self._record_ask_failure(f"{type(exc).__name__}: {exc}")
            return None

    def _transport_sync(self):
        """从异步 transport 派生同步 transport（测试注入的 transport 需要）。"""
        t = self._transport
        if t is None:
            return None
        # 测试可能注入 AsyncMock/ASGITransport——同步路径用不了就直接放弃注入
        try:
            import httpx
            # MockTransport 同步/异步通用，直接复用（单测注入走这里）
            if isinstance(t, httpx.MockTransport):
                return t
            if isinstance(t, httpx.AsyncHTTPTransport):
                return httpx.HTTPTransport(
                    retries=getattr(t, "_retries", 0) or 0,
                )
        except Exception:
            pass
        return None

    def _record_ask_success(self) -> None:
        """主判成功：若此前处于告警状态，补一条恢复日志。"""
        if self._health_warned:
            logger.bind(tag=TAG).info(
                f"[主判健康] {self.provider_name}/{self.model} 已恢复，"
                f"累计连续失败 {self._consecutive_failures} 次，本次恢复正常"
            )
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._health_warned = False

    BREAKER_FAILURES = 3
    BREAKER_COOLDOWN_S = 45.0

    def _record_ask_failure(self, reason: str, extend_breaker: bool = True) -> None:
        """主判失败：达到阈值后升级为 error 并周期性重发，避免静默降级。"""
        self._consecutive_failures += 1
        self._total_failures += 1
        n = self._consecutive_failures
        if extend_breaker and n >= self.BREAKER_FAILURES:
            self._breaker_until = time.time() + self.BREAKER_COOLDOWN_S
        if n < self.health_failure_threshold:
            logger.bind(tag=TAG).warning(
                f"Jev 决策请求失败，回退本地逻辑（连续第 {n} 次）: {reason}"
            )
            return
        if not self._health_warned or n % self.health_repeat_every == 0:
            self._health_warned = True
            logger.bind(tag=TAG).error(
                f"[主判故障] {self.provider_name}/{self.model} 连续失败 {n} 次"
                f"（累计 {self._total_failures}），判停已全程降级为本地兜底规则。"
                f"最近原因: {reason}"
            )

    def health_snapshot(self) -> Dict[str, Any]:
        """主判健康快照，供监控/诊断接口读取。"""
        return {
            "provider": self.provider_name,
            "model": self.model,
            "enabled": self.enabled,
            "consecutive_failures": self._consecutive_failures,
            "total_failures": self._total_failures,
            "degraded": self._health_warned,
        }

    async def judge_utterance(self, text: str, session_active: bool) -> Optional[UtteranceDecision]:
        if not self.enabled or not self.endpoint_enabled or not text.strip():
            return None

        state = {
            "utterance": text,
            "session_active": bool(session_active),
            "assistant_name": "Eva / 伊娃",
        }
        questions = {
            "addressed": {
                "type": "noul",
                "instructions": "Is this utterance spoken TO the assistant (the home voice assistant)?",
                "criteria": {
                    "true": "Spoken to the assistant: an instruction or question for the voice "
                            "assistant; a short imperative a family member says to the device; "
                            "a follow-up continuing a request made moments earlier (e.g. right "
                            "after the wake word) or part of an ongoing conversation with the "
                            "assistant.",
                    "false": "Not addressed: TV/podcast/radio narration or commercial voice-over, "
                             "storytelling or background speech not directed at the device, "
                             "talking to another person (in the room or on a phone).",
                },
            },
            "complete": {
                "type": "noul",
                "instructions": (
                    "Has the user FINISHED their entire turn of speaking? "
                    "Distinguish a mid-speech pause in a long narration (the sentence "
                    "ends, but the story/list/explanation is still going) from a true "
                    "end of turn."
                ),
                "criteria": {
                    "true": (
                        "The user's WHOLE message is finished: the thought is closed "
                        "(a standalone question/command, a story with its conclusion, "
                        "a farewell) and it is safe to respond now."
                    ),
                    "false": (
                        "Mid-narration pause: this sentence ends but the overall "
                        "story/list/explanation is clearly continuing; the user only "
                        "paused for breath or thought and has more to say. Also false "
                        "if the sentence is cut off or trails off."
                    ),
                },
            },
        }
        started = time.perf_counter()
        answers = await self._ask(state, questions)
        primary_latency_ms = int((time.perf_counter() - started) * 1000)

        if answers is None:
            if self.shadow_enabled:
                self._spawn_shadow(state, questions, None, primary_latency_ms)
            return None

        addressed_p = self._noul_probability(answers.get("addressed"))
        complete_p = self._noul_probability(answers.get("complete"))
        decision = UtteranceDecision(
            addressed=self._optional_bool(addressed_p),
            complete=self._optional_bool(complete_p),
            addressed_probability=addressed_p,
            complete_probability=complete_p,
        )
        if self.shadow_enabled:
            self._spawn_shadow(state, questions, decision, primary_latency_ms)
        return decision

    async def rerank_memory_lines(
        self, query: str, memory_lines: Sequence[str]
    ) -> Optional[List[str]]:
        """返回相关记忆行；Jev 无法判断时返回 None。"""
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
            f"M{index:03d}": {
                "type": "noul",
                "instructions": f"Does memory M{index:03d} matter for the current request?",
                "criteria": {
                    "true": "Knowing this memory would change the answer, action, warning, or omission.",
                    "false": "The memory is unrelated or only shares a topic; the request is handled the same without it.",
                },
            }
            for index in range(1, len(candidates) + 1)
        }

        answers = await self._ask(state, questions, timeout_s=self.memory_timeout_s)
        latency_ms = getattr(self, "_last_ask_latency_ms", 0)
        # 记忆精排影子（2026-10-02 主人指示）：本地主判照常，云端只记录不干预；
        # 本地超时/失败也照发影子——本地慢与超时正是要观察的对比项。
        if self.shadow_enabled:
            self._spawn_memory_shadow(state, questions, answers, latency_ms)
        if answers is None:
            return None

        selected = []
        for index, text in enumerate(candidates, start=1):
            probability = self._noul_probability(answers.get(f"M{index:03d}"))
            if probability >= self.memory_threshold:
                selected.append(text)
        return selected

    async def judge_rag_relevance(
        self, question: str, chunks: Sequence[str]
    ) -> Optional[List[str]]:
        """判断 RAG 检索片段是否回答了用户问题；无法判断时返回 None。"""
        if not self.enabled or not self.memory_enabled or not chunks:
            return None

        candidates = [str(chunk).strip()[:500] for chunk in chunks if chunk and str(chunk).strip()]
        candidates = candidates[: self.max_memory_questions]
        if not candidates:
            return []

        state = {
            "current_request": question,
            "rag_chunks": [
                {"id": f"C{index:03d}", "text": text}
                for index, text in enumerate(candidates, start=1)
            ],
        }
        questions = {
            f"C{index:03d}": {
                "type": "noul",
                "instructions": (
                    f"Does chunk C{index:03d} directly answer the current request "
                    "or provide key evidence that should be used in the final answer?"
                ),
                "criteria": {
                    "true": "The chunk answers the request or contains key facts/steps/warnings needed to answer it.",
                    "false": "The chunk is only vaguely related, background noise, or the answer would be the same without it.",
                },
            }
            for index in range(1, len(candidates) + 1)
        }

        answers = await self._ask(state, questions, timeout_s=self.memory_timeout_s)
        if answers is None:
            return None

        selected = []
        for index, text in enumerate(candidates, start=1):
            probability = self._noul_probability(answers.get(f"C{index:03d}"))
            if probability >= self.memory_threshold:
                selected.append(text)
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

    # ------------------------------------------------------------------
    # 影子对比（2026-10-01 主人批准）：主链路（本地 ollaya）照常执行，
    # 同一请求异步复制一份给云端 TypeSafe Jev，只记录不执行。
    # 断网/超时/失败完全静默，不影响正常对话。
    # ------------------------------------------------------------------

    def _spawn_shadow(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, Any]],
        primary: Optional[UtteranceDecision],
        primary_latency_ms: int,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._shadow_judge(state, questions, primary, primary_latency_ms)
        )
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    def _spawn_memory_shadow(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, Any]],
        primary_answers: Optional[Dict[str, Any]],
        primary_latency_ms: int,
    ) -> None:
        """记忆精排影子：主（本地）照常，云端同题只记录不干预。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._memory_shadow(state, questions, primary_answers, primary_latency_ms)
        )
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def _cloud_ask(
        self, state: Dict[str, Any], questions: Dict[str, Dict[str, Any]]
    ) -> tuple:
        """云端影子请求 → (answers | None, error | None, latency_ms)。断网/超时静默。"""
        started = time.perf_counter()
        answers: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        try:
            client_kwargs = {
                "timeout": self.shadow_timeout_s,
                "transport": self._transport,
            }
            if self.proxy and self._transport is None:
                client_kwargs["proxy"] = self.proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    f"{self.shadow_base_url}/v1/systemone",
                    headers={"Authorization": f"Bearer {self.shadow_api_key}"},
                    json={
                        "state": state,
                        "model": self.shadow_model,
                        "questions": questions,
                    },
                )
                response.raise_for_status()
                data = response.json()
                raw = data.get("answers")
                answers = raw if isinstance(raw, dict) else None
                if answers is None:
                    error = "no answers"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)
        return answers, error, latency_ms

    async def _memory_shadow(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, Any]],
        primary_answers: Optional[Dict[str, Any]],
        primary_latency_ms: int,
    ) -> None:
        shadow_answers, error, shadow_latency_ms = await self._cloud_ask(state, questions)

        def _probs(answers: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
            if not answers:
                return None
            return {
                key: self._noul_probability(value)
                for key, value in answers.items()
            }

        record = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scene": "memory",
            "utterance": str(state.get("current_request", "")),
            "primary": {
                "provider": self.provider_name,
                "base_url": self.base_url,
                "model": self.model,
                "latency_ms": primary_latency_ms,
                "ok": primary_answers is not None,
                "probabilities": _probs(primary_answers),
            },
            "shadow": {
                "provider": "typesafe-cloud",
                "base_url": self.shadow_base_url,
                "model": self.shadow_model,
                "latency_ms": shadow_latency_ms,
                "ok": shadow_answers is not None and error is None,
                "error": error,
                "probabilities": _probs(shadow_answers),
            },
        }
        self._append_shadow_record(record)

    async def _shadow_judge(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Dict[str, Any]],
        primary: Optional[UtteranceDecision],
        primary_latency_ms: int,
    ) -> None:
        started = time.perf_counter()
        shadow_answers: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        try:
            client_kwargs = {
                "timeout": self.shadow_timeout_s,
                "transport": self._transport,
            }
            if self.proxy and self._transport is None:
                client_kwargs["proxy"] = self.proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    f"{self.shadow_base_url}/v1/systemone",
                    headers={"Authorization": f"Bearer {self.shadow_api_key}"},
                    json={
                        "state": state,
                        "model": self.shadow_model,
                        "questions": questions,
                    },
                )
                response.raise_for_status()
                data = response.json()
                answers = data.get("answers")
                shadow_answers = answers if isinstance(answers, dict) else None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        shadow_latency_ms = int((time.perf_counter() - started) * 1000)
        shadow_addressed_p = self._noul_probability(
            (shadow_answers or {}).get("addressed")
        )
        shadow_complete_p = self._noul_probability(
            (shadow_answers or {}).get("complete")
        )
        record = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scene": "endpoint",
            "utterance": str(state.get("utterance", "")),
            "session_active": bool(state.get("session_active")),
            "primary": {
                "provider": self.provider_name,
                "base_url": self.base_url,
                "model": self.model,
                "addressed_probability": primary.addressed_probability if primary else None,
                "complete_probability": primary.complete_probability if primary else None,
                "addressed": primary.addressed if primary else None,
                "complete": primary.complete if primary else None,
                "latency_ms": primary_latency_ms,
                "ok": primary is not None,
            },
            "shadow": {
                "provider": "typesafe-cloud",
                "base_url": self.shadow_base_url,
                "model": self.shadow_model,
                "addressed_probability": shadow_addressed_p if shadow_answers is not None else None,
                "complete_probability": shadow_complete_p if shadow_answers is not None else None,
                "latency_ms": shadow_latency_ms,
                "ok": shadow_answers is not None and error is None,
                "error": error,
            },
        }
        self._append_shadow_record(record)

    def _append_shadow_record(self, record: Dict[str, Any]) -> None:
        try:
            path = Path(self.shadow_log_path)
            if path.parent and str(path.parent) not in ("", "."):
                path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"影子对比记录写入失败（忽略）: {exc}")


_jev_client: Optional[JevDecisionClient] = None


def get_jev_decision(config: Optional[Dict[str, Any]] = None) -> JevDecisionClient:
    global _jev_client
    if _jev_client is None:
        # 连接私有配置可能是裁剪过的，只有全局配置才保证包含 Decision 段。
        if config is None or not ((config or {}).get("Decision") or {}).get("jev"):
            from config.config_loader import load_config
            config = load_config()
        decision_config = ((config or {}).get("Decision") or {}).get("jev") or {}
        _jev_client = JevDecisionClient(decision_config)
    return _jev_client


def reset_jev_decision_for_tests() -> None:
    global _jev_client
    _jev_client = None
