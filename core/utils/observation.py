# -*- coding: utf-8 -*-
"""运行观察计数器（纯旁路，零侵入）

用途
----
把「主人侧无感、但需要用来纠正开发」的事件记下来，供每 3 天一份的
《Mac Mini 运行观察报告》使用（方案见 docs/plans/2026-10-18_..._方案.md）：

    · 被静默丢弃的语音（会话未激活）、疑似唤醒失败、声纹闸门丢弃
    · 每轮对话耗时 / 是否调了工具 / 是否被打断
    · 工具调用成败与耗时
    · 麦克风实时率（最新 / 最小）

设计约束（2026-10-18 主人批准 P1~P9；与 data_collector 同一套纪律）
--------------------------------------------------------------
    · **绝不修改主链路行为**：所有函数不抛异常、不做重 I/O、不加等待
    · 高频事件（声纹丢弃一天上千条）**不逐条落库**：只在内存自增，
      每 10 分钟由守护线程把「计数 + 少量样本」写成 1 行 observation_metrics；
      进程退出时兜底 flush 一次，避免丢最后一段窗口
    · 低频事件（每轮对话、每次工具调用）逐条落库，量级每天几十到几百行

关闭方式：环境变量 ``HB_OBSERVATION=0``（调试/测试时用）。
"""
from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 每 10 分钟把内存计数落一次库（窗口对齐观察报告的粒度）
FLUSH_INTERVAL = 600.0
# 守护线程的 tick（到点才真正 flush）
_TICK = 30.0
# 每个计数键最多保留几条样本
MAX_SAMPLES = 5
# 样本截断长度
_SAMPLE_CLIP = 120

_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {}
_SAMPLES: Dict[str, List[str]] = {}
_GAUGES: Dict[str, Any] = {}       # 最新值
_GAUGE_MINS: Dict[str, Any] = {}   # 窗口内最小值
_WINDOW_START = time.time()
_LAST_FLUSH = time.time()
_THREAD: Optional[threading.Thread] = None


def enabled() -> bool:
    """观察开关。

    默认开。**两个关闭条件**：
      1. 显式设置 `HB_OBSERVATION=0/false/no/off`
      2. 跑在 pytest 里（且未显式设置 HB_OBSERVATION=1）—— 单测会走到本模块的
         调用点，必须保证**测试绝不写生产库 data/devices.db**（2026-10-13 主人裁定）。
         要专门测本模块的落库行为，用 `HB_OBSERVATION=1` 显式打开。
    """
    try:
        raw = os.environ.get("HB_OBSERVATION")
        if raw is not None:
            return raw.strip().lower() not in ("0", "false", "no", "off")
        import sys
        if "pytest" in sys.modules:
            return False
        return True
    except Exception:
        return True


def _clip(s: Any, n: int = _SAMPLE_CLIP) -> str:
    try:
        t = str(s)
    except Exception:
        return ""
    return t if len(t) <= n else t[:n] + "…"


def _ensure_thread() -> None:
    """懒启动 flush 线程（只启一次）。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_loop, name="hb-observation-flush", daemon=True)
    _THREAD.start()


def _loop() -> None:
    while True:
        try:
            time.sleep(_TICK)
            if time.time() - _LAST_FLUSH >= FLUSH_INTERVAL:
                flush()
        except Exception:
            # 观察线程永不把异常抛出去
            pass


# ────────────────────────── 记录接口（调用方只需一行） ──────────────────────────

def bump(key: str, n: int = 1, sample: Any = None) -> None:
    """计数 +n（高频事件用这个）。sample 会保存少量样本供报告举例。"""
    if not enabled():
        return
    try:
        with _LOCK:
            _COUNTS[key] = _COUNTS.get(key, 0) + int(n)
            if sample is not None:
                lst = _SAMPLES.setdefault(key, [])
                if len(lst) < MAX_SAMPLES:
                    lst.append(_clip(sample))
        _ensure_thread()
    except Exception:
        pass


def gauge(key: str, value: Any) -> None:
    """记录一个「最新值」（如当前麦克风实时率）。"""
    if not enabled():
        return
    try:
        with _LOCK:
            _GAUGES[key] = value
        _ensure_thread()
    except Exception:
        pass


def gauge_min(key: str, value: Any) -> None:
    """记录窗口内最小值（如最低实时率）；同键同时更新「最新值」。"""
    if not enabled():
        return
    try:
        with _LOCK:
            _GAUGES[key] = value
            old = _GAUGE_MINS.get(key)
            try:
                if old is None or float(value) < float(old):
                    _GAUGE_MINS[key] = value
            except Exception:
                _GAUGE_MINS[key] = value
        _ensure_thread()
    except Exception:
        pass


def flush(force: bool = False) -> Optional[int]:
    """把内存计数写成一行业务无关的 observation_metrics。返回行 id（无数据/失败为 None）。"""
    global _WINDOW_START, _LAST_FLUSH
    if not enabled():
        return None
    try:
        with _LOCK:
            if not _COUNTS and not _GAUGES:
                _LAST_FLUSH = time.time()
                _WINDOW_START = _LAST_FLUSH
                return None
            counters = dict(_COUNTS)
            samples = {k: list(v) for k, v in _SAMPLES.items()}
            gauges = dict(_GAUGES)
            gauge_mins = dict(_GAUGE_MINS)
            win_start = _WINDOW_START
            now = time.time()
            # 清空窗口
            _COUNTS.clear()
            _SAMPLES.clear()
            _GAUGES.clear()
            _GAUGE_MINS.clear()
            _WINDOW_START = now
            _LAST_FLUSH = now
        from core.utils import data_collector as dc
        return dc.record_metrics(
            window_start=win_start, window_end=now,
            counters=counters, samples=samples,
            gauges={**gauges, **{f"{k}__min": v for k, v in gauge_mins.items()}},
        )
    except Exception as e:
        try:
            logger.bind(tag=TAG).debug(f"[运行观察] flush 失败（忽略）: {e}")
        except Exception:
            pass
        return None


def reset_for_test() -> None:
    """仅供单测使用：清空内存计数。"""
    global _WINDOW_START, _LAST_FLUSH
    with _LOCK:
        _COUNTS.clear()
        _SAMPLES.clear()
        _GAUGES.clear()
        _GAUGE_MINS.clear()
        _WINDOW_START = time.time()
        _LAST_FLUSH = time.time()


# ────────────────────────── 低频事件：逐条落库 ──────────────────────────

def note_turn(
    device_id: str = "",
    session_id: str = "",
    user_head: str = "",
    turn_ms: Optional[int] = None,
    reply_chars: int = 0,
    with_tool: bool = False,
    aborted: bool = False,
) -> Optional[int]:
    """记一轮对话的结果（P5）。**绝不抛异常。**"""
    if not enabled():
        return None
    try:
        from core.utils import data_collector as dc
        return dc.record_turn(
            device_id=device_id, session_id=session_id, user_head=user_head,
            turn_ms=turn_ms, reply_chars=reply_chars,
            with_tool=with_tool, aborted=aborted,
        )
    except Exception:
        return None


def note_tool(
    tool_name: str,
    latency_ms: Optional[int] = None,
    ok: bool = True,
    device_id: str = "",
    error: str = "",
) -> Optional[int]:
    """记一次工具调用（P6）。**绝不抛异常。**"""
    if not enabled():
        return None
    try:
        from core.utils import data_collector as dc
        return dc.record_tool_call(
            tool_name=tool_name, device_id=device_id,
            ok=ok, latency_ms=latency_ms, error=error,
        )
    except Exception:
        return None


def snapshot() -> Dict[str, Any]:
    """当前内存窗口内容的只读快照（调试/测试用）。"""
    with _LOCK:
        return {
            "counters": dict(_COUNTS),
            "samples": {k: list(v) for k, v in _SAMPLES.items()},
            "gauges": dict(_GAUGES),
            "gauge_mins": dict(_GAUGE_MINS),
        }


def _atexit_flush() -> None:
    try:
        flush(force=True)
    except Exception:
        pass


atexit.register(_atexit_flush)
