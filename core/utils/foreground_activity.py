"""前台语音活动标记（2026-10-12）：让后台重负载（mem0 记忆抽取等）错开对话高峰。

实测（同一台机器）：TTS 单独合成 1.4s；同时有一个 JEV 推理在跑就变 3.6s；会话中 mem0 抽取
（8B 模型）与 TTS/判停同时占 GPU，TTS 中位从 0.9s 涨到 17s。后台任务没有时间压力，
所以：前台刚有活动（收到用户话、LLM 在回复、TTS 在出块）时先等，空闲一小会儿再跑。

用法：前台各环节调用 touch()；后台任务 `await wait_until_idle()` 后再开跑。
"""
import asyncio
import time

_last = 0.0


def touch() -> None:
    global _last
    _last = time.time()


def idle_for() -> float:
    """距上次前台活动多少秒（从未活动返回一个很大的数）。"""
    return time.time() - _last if _last else 1e9


async def wait_until_idle(idle_seconds: float = 6.0, max_wait: float = 120.0, poll: float = 0.5) -> float:
    """等到前台空闲 idle_seconds 秒；最多等 max_wait 秒（到点也放行，避免永远不抽取）。返回实际等待秒数。"""
    t0 = time.time()
    while idle_for() < idle_seconds and time.time() - t0 < max_wait:
        await asyncio.sleep(poll)
    return time.time() - t0
