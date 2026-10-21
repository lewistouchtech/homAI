"""服务器端声学回声消除器（统一 ESP32）

参照小智 kAecOnServerSide 模式：
- 设备发送麦克风音频 + 时间戳
- 服务器用自身 TTS 输出作为参考信号
- WebRTC AEC 从混合音频中消除 TTS 回声

时间戳格式：
- Web 终端：音频帧前 4 字节大端毫秒时间戳（2026-10-01 起改为绝对 epoch 毫秒，
  与 set_reference 的服务端时间戳同一时间轴，修复此前相对时间导致 AEC 永不生效的问题）
- ESP32: BinaryProtocol2 格式中的 timestamp 字段

依赖：
- pywebrtc-audio（0.2.0，内部为 Google WebRTC AEC3/NS，推荐）
- 或纯 Python 能量减法（备选，仅在 pywebrtc-audio 不可用时兜底）
"""
import time
import struct
from collections import deque
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class AECProcessor:
    """声学回声消除处理器

    每个连接一个实例，用于：
    1. 存储 TTS 参考信号（服务器发送 TTS 时注册）
    2. 处理麦克风音频（收到时根据时间戳做回声消除）
    """

    def __init__(self, sample_rate=16000, frame_ms=60, echo_delay_ms=600):
        self.sample_rate = sample_rate
        self.frame_size = sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_size * 2  # 16-bit PCM

        # 2026-10-07 关键修复：回声延迟（毫秒）。
        #
        # 麦克风在 T 时刻听到的声音，是服务端在 **T - echo_delay_ms** 左右发出去的：
        # 网页端播放器要先缓冲（pcm-player.js BUFFER_THRESHOLD_MS = 600ms）才出声，
        # 再加上服务端 60ms/帧的节流与声卡输出延迟。
        #
        # 原实现按「麦克风时间戳窗口 [T, T+帧长]」去参考缓冲里取包 —— 取到的是
        # **还没播出来的未来音频**，与麦克风里的回声毫无相关性，AEC 等于没开。
        # 现改为往回找 [T - delay, T - delay + 帧长]。
        self.echo_delay_ms = max(0, int(echo_delay_ms))

        # TTS 参考信号环形缓冲区 [(timestamp_ms, pcm_16bit), ...]
        # 保留最近 100 帧（约 6 秒）
        self._ref_buffer = deque(maxlen=100)

        # WebRTC AEC 实例（懒加载）
        self._aec = None
        self._initialized = False
        self._aec_available = True

        # 统计
        self._ref_count = 0
        self._process_count = 0
        self._echo_removed_count = 0

    def _ensure_aec(self):
        """懒加载 WebRTC AEC3（pywebrtc-audio）

        2026-10-01 主人批准的 AEC 统一方案：开发机与 Mac Mini 使用同一接口
        pywebrtc_audio.AudioProcessor（内部 AEC3/NS/AGC2），替代此前声明
        webrtc_audio_processing 但实际从未装上、一直走兜底的项目。
        """
        if self._initialized:
            return

        try:
            from pywebrtc_audio import AudioProcessor
            self._aec = AudioProcessor(
                sample_rate=self.sample_rate,
                num_channels=1,
                echo_cancellation=True,   # 对应原 set_echo_cancellation_enabled(True)
                noise_suppression=True,   # 对应原 set_noise_suppression_level(1) 低噪声抑制
                high_pass_filter=True,    # 高通滤波，去直流/低频噪声
                auto_gain_control=False,  # 对应原 set_gain_control_enabled(False)
                ns_level=1,
            )
            self._initialized = True
            logger.bind(tag=TAG).info(f"WebRTC AEC3 已初始化（pywebrtc-audio）sr={self.sample_rate}")
        except ImportError:
            logger.bind(tag=TAG).warning("pywebrtc_audio 未安装，AEC 使用能量减法备选方案")
            self._aec_available = False
            self._initialized = True
        except Exception as e:
            logger.bind(tag=TAG).error(f"WebRTC AEC3 初始化失败: {e}")
            self._aec_available = False
            self._initialized = True

    def set_reference(self, timestamp_ms: int, audio_data: bytes):
        """注册 TTS 参考信号

        Args:
            timestamp_ms: TTS 发送时间戳（毫秒）
            audio_data: TTS 音频数据（PCM 16-bit 或 Opus）
        """
        self._ref_buffer.append((timestamp_ms, audio_data))
        self._ref_count += 1

        # 首次注册打 INFO：这是判断「AEC 到底有没有参考信号」的关键观测点
        # （debug 级在 log_level=INFO 下等于不存在，曾因此误判 AEC 已生效）
        if self._ref_count == 1:
            logger.bind(tag=TAG).info(
                f"[AEC] 首次注册参考信号: ts={timestamp_ms}ms, {len(audio_data)} bytes, "
                f"延迟设定={self.echo_delay_ms}ms")
        elif self._ref_count % 200 == 0:
            logger.bind(tag=TAG).info(
                f"[AEC] 参考信号已注册 {self._ref_count} 次, 缓冲区={len(self._ref_buffer)}")

    def process(self, timestamp_ms: int, mic_audio: bytes) -> bytes:
        """处理麦克风音频，消除 TTS 回声

        Args:
            timestamp_ms: 麦克风采集时间戳（毫秒，与 set_reference 同一时间轴）
            mic_audio: 麦克风音频数据（16-bit PCM）

        Returns:
            去回声后的 PCM 数据
        """
        self._process_count += 1
        if self._process_count % 200 == 0:
            logger.bind(tag=TAG).info(
                f"[AEC] 统计: 处理={self._process_count} 消除={self._echo_removed_count} "
                f"参考={self._ref_count} 延迟={self.echo_delay_ms}ms")

        if not self._ref_buffer:
            return mic_audio

        self._ensure_aec()

        # 优先：WebRTC AEC3（pywebrtc-audio），要求 near/far 等长，
        # 参考信号按时间戳窗口拼接（mic 帧 2048 样本与 TTS 包时长不同）
        if self._aec_available and self._aec is not None:
            try:
                import numpy as np

                mic = np.frombuffer(mic_audio, dtype=np.int16)
                if len(mic) == 0:
                    return mic_audio
                ref = self._build_reference_window(timestamp_ms, len(mic))
                if ref is None:
                    return mic_audio  # 无对应参考，原样返回
                clean = self._aec.process(mic, ref)
                self._echo_removed_count += 1
                return np.asarray(clean, dtype=np.int16).tobytes()
            except Exception as e:
                logger.bind(tag=TAG).error(f"WebRTC AEC3 处理失败: {e}")
                return mic_audio

        # 兜底：能量减法（仅在 pywebrtc_audio 不可用时走到这里）
        ref_audio = self._find_reference(
            timestamp_ms - self.echo_delay_ms, tolerance_ms=150)
        if ref_audio is None:
            return mic_audio
        return self._nlms_process(mic_audio, ref_audio)

    def _build_reference_window(self, timestamp_ms: int, n_samples: int):
        """按时间轴拼接与麦克风帧对应的 TTS 参考样本

        WebRTC AEC3 要求 near/far 等长，而麦克风帧（2048 样本≈128ms）与
        TTS 参考包时长不同，因此按帧时间窗口 [timestamp_ms, +帧时长]
        从参考缓冲中取样本拼接；参考不足时补零（视为无回声段，
        AEC3 内部自带延迟估计，会自动对齐回声延迟）。

        Args:
            timestamp_ms: 麦克风帧起始时间戳（毫秒）
            n_samples: 麦克风帧样本数

        Returns:
            np.ndarray(int16) 长度 n_samples，或 None（完全无可用参考）
        """
        import numpy as np

        # 往回退 echo_delay_ms：对齐「麦克风此刻听到的」与「服务端当初发的」
        target_ms = timestamp_ms - self.echo_delay_ms
        frame_dur_ms = n_samples * 1000.0 / self.sample_rate
        frame_end_ms = target_ms + frame_dur_ms

        chunks = []
        collected = 0
        for ts, pcm in self._ref_buffer:
            pkg_samples = len(pcm) // 2
            pkg_end_ms = ts + pkg_samples * 1000.0 / self.sample_rate

            if pkg_end_ms <= target_ms:
                continue  # 包完全在帧窗口之前
            if ts >= frame_end_ms:
                break  # 包完全在帧窗口之后（缓冲按时间递增）

            if ts >= target_ms:
                chunks.append(pcm)
            else:
                # 包与帧窗口部分重叠：裁掉窗口起点之前的部分
                offset = int(round((target_ms - ts) * self.sample_rate / 1000.0))
                chunks.append(pcm[offset * 2:])

            collected += len(chunks[-1]) // 2
            if collected >= n_samples:
                break

        if not chunks:
            return None

        ref = np.frombuffer(b"".join(chunks), dtype=np.int16)
        if len(ref) < n_samples:
            ref = np.pad(ref, (0, n_samples - len(ref)))
        else:
            ref = ref[:n_samples].copy()
        return ref

    def _find_reference(self, timestamp_ms: int, tolerance_ms=100) -> bytes | None:
        """根据时间戳查找对应的 TTS 参考信号

        Args:
            timestamp_ms: 麦克风采集时间戳
            tolerance_ms: 时间戳容差（毫秒）

        Returns:
            匹配的参考音频数据，或 None
        """
        # 麦克风采集时间比 TTS 播放时间晚（声音传播延迟）
        # 所以查找时间戳比 mic_timestamp 早的参考信号
        best_match = None
        best_diff = tolerance_ms

        for ts, pcm in self._ref_buffer:
            # 参考信号应该比麦克风采集早
            diff = timestamp_ms - ts
            if 0 < diff < best_diff:
                best_diff = diff
                best_match = pcm

        return best_match

    def _nlms_process(self, mic_pcm: bytes, ref_pcm: bytes) -> bytes:
        """NLMS 自适应滤波器备选方案

        简化实现：对齐后做能量减法
        """
        try:
            import numpy as np

            # 解析 PCM 数据
            mic = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float32)
            ref = np.frombuffer(ref_pcm, dtype=np.int16).astype(np.float32)

            # 对齐长度
            min_len = min(len(mic), len(ref))
            if min_len < 100:  # 太短不做处理
                return mic_pcm

            mic = mic[:min_len]
            ref = ref[:min_len]

            # 简单能量减法（NLMS 的简化版）
            # 计算参考信号的能量
            ref_energy = np.sum(ref ** 2)
            mic_energy = np.sum(mic ** 2)

            if ref_energy > 0 and mic_energy > ref_energy * 0.1:
                # 如果麦克风能量中包含明显的参考信号成分
                # 估算回声衰减系数
                scale = np.dot(mic, ref) / ref_energy
                scale = max(0, min(scale, 1.0))  # 限制范围

                # 减去回声
                clean = mic - scale * ref * 0.8  # 保守系数
                clean = np.clip(clean, -32768, 32767).astype(np.int16)

                return clean.tobytes()

            return mic_pcm
        except Exception as e:
            logger.bind(tag=TAG).error(f"NLMS 处理失败: {e}")
            return mic_pcm

    def clear(self):
        """清理参考缓冲区"""
        self._ref_buffer.clear()
        logger.bind(tag=TAG).info(f"[AEC] 参考缓冲区已清理，统计：注册={self._ref_count}, 处理={self._process_count}, 消除={self._echo_removed_count}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "ref_count": self._ref_count,
            "process_count": self._process_count,
            "echo_removed_count": self._echo_removed_count,
            "ref_buffer_size": len(self._ref_buffer),
            "aec_available": self._aec_available,
        }


def parse_timestamp_audio_frame(data: bytes) -> tuple:
    """解析带时间戳的音频帧

    格式：[4字节timestamp_ms大端序] + 音频数据

    注意：此函数只在 conn.aec_enabled=True 的连接上调用，
    所以假设前4字节就是时间戳（相对流启动的毫秒数）。
    普通音频帧不会调用此函数。

    Args:
        data: WebSocket 接收的二进制数据

    Returns:
        (timestamp_ms, audio_payload) 或 (None, data)
    """
    if len(data) < 4:
        return None, data

    try:
        timestamp_ms = struct.unpack('!I', data[:4])[0]
        # 相对时间戳：发送的是相对于流启动的毫秒数
        return timestamp_ms, data[4:]
    except Exception:
        return None, data


def is_aec_capable_device(hello_msg: dict) -> bool:
    """检查设备是否支持 AEC

    Args:
        hello_msg: 设备发送的 hello 消息

    Returns:
        True 如果设备声明支持 AEC
    """
    features = hello_msg.get("features", {})
    return features.get("aec", False) is True