"""麦克风活性检测（2026-10-15 新增，纯新增模块）

【为什么需要它】

2026-10-15 排查「Mac Mini 上助手每隔一段时间就听不见、刷新页面才恢复」时，
把整条链路的可观测性摸了一遍，发现**三处全是瞎的**：

1. 页面侧 `mic_watchdog_event`（2026-10-13 加的「变聋自愈」）只检测
   `onaudioprocess` 是否**停止回调**；而实测故障形态是「回调照常、内容全是数字零」，
   该看门狗 24 小时内一次都没触发 → 完全看不见。
2. 服务端**没有**任何音频帧活性/内容检测：`_last_audio_frame_time` 只写不读，
   页面不送音频也不会断连（实测：WebSocket 存活、ping 正常、帧计数器持续增长，
   而 VAD 连续两小时零人声）。
3. `mic_watchdog_event` 服务端没有处理器，被 `textMessageProcessor` 当成
   「收到未知类型消息」打两遍 error，注释里写的「上报给服务端日志便于远程观察」不成立。

本模块补上第 2 块：按连接统计「音频实时率」「连续全零时长」「AEC 吃掉内容」，
**只打日志、不改行为**。

【两个信号为什么要分开看】

调用方同时喂两份音频：
- `raw_pcm`：网页端发来的原始麦克风音频（未经服务端 AEC）
- `vad_pcm`：服务端 AEC 处理后的音频（VAD/判停实际吃的）

判定：
- **raw 全零** → 设备 USB 音频流在送数字静音，或浏览器侧就是静音流（软件层造不出声音）
- **raw 有信号、vad 全零** → 是**服务端 AEC** 把内容吃掉了（另一类问题，指向 aec.py）

【实测口径（Mini，网页端每帧 2048 采样 @16kHz = 128ms）】

正常 100%；实测异常形态两个：长期停在 23~33%（吞吐退化）、连续数小时全零。

【不改行为】

只读音频、只写日志，不触碰 VAD/ASR/判停任何判定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 结算窗口：与 aec.py 的 `[AEC] 统计` 同口径（每 200 帧打一次），便于两个日志对读。
WINDOW_FRAMES = 200
# 每帧字节数 → 毫秒：16kHz / 16bit / 单声道
BYTES_PER_MS = 16000 * 2 / 1000.0
# 实时率低于此值判为「掉速」
RATE_WARN = 0.8
# 连续全零达到此秒数才告警（真实房间底噪不会连续几十秒恰好是数字零；
# 取 60s 是为了不给「非常安静的深夜」误报）
ZERO_ALERT_SECONDS = 60.0
# 同一连接重复告警的最小间隔，避免刷屏
ZERO_REPEAT_SECONDS = 600.0

TAG = "mic_liveness"


def _is_all_zero(pcm: bytes) -> bool:
    """int16 PCM 是否**每一个采样点**都是 0（真·数字静音）。

    空块返回 False：把「没数据」误判成静音会误报。
    """
    if not pcm:
        return False
    return not any(pcm)


@dataclass
class LivenessStats:
    """单个连接（= 单个网页端）的活性统计。"""

    frames: int = 0            # 窗口内帧数（每帧只计一次）
    ms: float = 0.0            # 窗口内音频时长
    zero_ms: float = 0.0       # 连续「原始音频全零」时长
    aec_zero_ms: float = 0.0   # 连续「raw 有信号但 AEC 后全零」时长
    zero_alerted_at: float = 0.0
    aec_alerted_at: float = 0.0
    # 窗口起点由**第一帧**的时间戳决定（不用构造时的墙上时钟，否则单测注入假时钟、
    # 或连接复用 tracker 时会算出负的墙上时长 → 实时率爆炸）
    win_started: float = 0.0
    total_frames: int = 0
    last_rate: float = 1.0
    state: str = "unknown"     # ok / slow / zero / aec_zero

    def reset_window(self) -> None:
        self.frames = 0
        self.ms = 0.0
        self.win_started = time.time()


class MicLiveness:
    """按连接跟踪音频活性。挂在 conn 上复用（与 asr/vad 的 conn 级状态一致）。"""

    ATTR = "_mic_liveness"

    def __init__(self) -> None:
        self.stats = LivenessStats()

    @classmethod
    def of(cls, conn) -> "MicLiveness":
        inst = getattr(conn, cls.ATTR, None)
        if inst is None:
            inst = cls()
            setattr(conn, cls.ATTR, inst)
        return inst

    @classmethod
    def feed(cls, conn, raw_pcm: bytes = None, vad_pcm: bytes = None,
             now: float = None):
        """喂一帧音频，返回本次结算出的状态（None = 本帧没到结算点）。

        Args:
            raw_pcm: 浏览器发来的原始麦克风音频（AEC 之前）
            vad_pcm: 服务端 AEC 之后的音频（VAD/判停吃的）
        """
        st = cls.of(conn).stats
        now = time.time() if now is None else now
        if st.win_started <= 0:
            st.win_started = now

        # 一帧只计一次：以原始音频为准，没有原始音频时退回 AEC 后的那份
        primary = raw_pcm if raw_pcm is not None else vad_pcm
        if not primary:
            return None
        dur_ms = len(primary) / BYTES_PER_MS

        st.frames += 1
        st.total_frames += 1
        st.ms += dur_ms

        if _is_all_zero(primary):
            st.zero_ms += dur_ms
            st.aec_zero_ms = 0.0
        else:
            st.zero_ms = 0.0
            if vad_pcm is not None and _is_all_zero(vad_pcm):
                st.aec_zero_ms += dur_ms
                if (st.aec_zero_ms >= ZERO_ALERT_SECONDS * 1000.0
                        and now - st.aec_alerted_at >= ZERO_REPEAT_SECONDS):
                    st.aec_alerted_at = now
                    cls._log(conn, "warning",
                             "[麦克风活性] 原始音频有信号、但服务端 AEC 后全零 "
                             f"（持续 {st.aec_zero_ms / 1000.0:.0f}s）"
                             f" device={cls._dev(conn)} → 指向服务端 aec.py，不是设备")
            else:
                st.aec_zero_ms = 0.0

        if st.frames < WINDOW_FRAMES:
            return None
        return cls._settle(conn, st, now)

    @staticmethod
    def _dev(conn) -> str:
        return (getattr(conn, "hello_device_id", "")
                or getattr(conn, "device_id", "") or "?")

    @classmethod
    def _log(cls, conn, level: str, msg: str) -> None:
        logger = getattr(conn, "logger", None)
        if logger is None:
            return
        try:
            getattr(logger.bind(tag=TAG), level)(msg)
        except Exception:
            pass

    @classmethod
    def _settle(cls, conn, st: LivenessStats, now: float) -> str:
        wall = max(now - st.win_started, 1e-6)
        rate = (st.ms / 1000.0) / wall
        st.last_rate = rate
        zero_s = st.zero_ms / 1000.0
        dev = cls._dev(conn)

        if zero_s >= ZERO_ALERT_SECONDS:
            st.state = "zero"
            if now - st.zero_alerted_at >= ZERO_REPEAT_SECONDS:
                st.zero_alerted_at = now
                cls._log(conn, "warning",
                         f"[麦克风活性] 设备持续送数字静音 {zero_s:.0f}s"
                         f"（帧还在到、内容全零）device={dev}"
                         f" → 设备/USB 音频流侧无数据，软件层无法凭空造出声音；"
                         f"建议重插 USB 或换 USB 口。")
        elif st.aec_zero_ms >= ZERO_ALERT_SECONDS * 1000.0:
            st.state = "aec_zero"
        elif rate < RATE_WARN:
            st.state = "slow"
            cls._log(conn, "warning",
                     f"[麦克风活性] 音频吞吐退化：实时率 {rate * 100:.0f}%"
                     f"（正常 ≥{RATE_WARN * 100:.0f}%）device={dev} "
                     f"window={wall:.0f}s frames={st.frames} audio={st.ms / 1000.0:.1f}s"
                     f" → 识别残缺/丢词，疑似 USB 音频流供数不足或采集端被抢占。")
        else:
            st.state = "ok"

        # [2026-10-18 P7，主人批准] 运行观察：实时率快照（只在内存聚合，
        # 每 10 分钟随其他计数一起落一行），报告里用来看麦克风有没有在退化。
        try:
            from core.utils import observation as _obs
            _obs.gauge_min("mic_rate_min", round(rate, 3))
            if st.state == "slow":
                _obs.bump("mic_slow_windows")
            elif st.state == "zero":
                _obs.bump("mic_zero_windows")
            elif st.state == "aec_zero":
                _obs.bump("mic_aec_zero_windows")
        except Exception:
            pass

        st.reset_window()
        st.win_started = now
        return st.state

    @classmethod
    def snapshot(cls, conn) -> dict:
        st = cls.of(conn).stats
        return {
            "state": st.state,
            "last_rate": round(st.last_rate, 3),
            "zero_ms": round(st.zero_ms, 1),
            "aec_zero_ms": round(st.aec_zero_ms, 1),
            "total_frames": st.total_frames,
        }
