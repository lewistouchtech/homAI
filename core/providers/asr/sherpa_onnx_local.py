import asyncio
import threading
import time
import wave
import os
import sys
import io
import json
from config.logger import setup_logging
from typing import Optional, Tuple, List, Dict
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

import numpy as np
import sherpa_onnx

from modelscope.hub.file_download import model_file_download

TAG = __name__
logger = setup_logging()

# 模拟流式参数（优化：缩短处理间隔，减少延迟）
SIMULATED_STREAM_CHUNK_SEC = 0.15  # 每 150ms 处理一次（原 300ms，提速一倍）
SAMPLE_RATE = 16000
CHUNK_SAMPLES = int(SAMPLE_RATE * SIMULATED_STREAM_CHUNK_SEC)  # 2400 samples


# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()

        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())


class ASRProvider(ASRProviderBase):
    _decode_lock = threading.Lock()  # 类级：进程内所有实例共用一个 recognizer 串行解码

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir", "tmp/")
        self.model_type = config.get("model_type", "sense_voice")
        self.streaming = str(config.get("streaming", "true")).lower() in ("true", "1", "yes")
        self.delete_audio_file = delete_audio_file

        # 【关键修复】根据 streaming 配置决定 interface_type
        # streaming=false（SenseVoice 整句离线）应使用 LOCAL 模式
        # streaming=true（模拟流式）使用 STREAM 模式
        if self.streaming:
            self.interface_type = InterfaceType.STREAM
        else:
            self.interface_type = InterfaceType.LOCAL

        logger.bind(tag=TAG).info(
            f"Sherpa-ONNX ASR 初始化: model_type={self.model_type}, streaming={self.streaming}, interface_type={self.interface_type}, model_dir={self.model_dir}"
        )

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

        # 根据 model_type 初始化模型文件路径
        if self.model_type == "funasr_nano":
            raise ValueError(
                "FunASR-Nano 模型已停止支持。请使用 Paraformer-zh（真流式）或 SenseVoice（整句离线）。"
            )
        else:
            # SenseVoice / Paraformer 使用 sherpa-onnx 格式
            model_files = {
                "model.int8.onnx": os.path.join(self.model_dir, "model.int8.onnx"),
                "tokens.txt": os.path.join(self.model_dir, "tokens.txt"),
            }

            # 下载并检查模型文件
            try:
                for file_name, file_path in model_files.items():
                    if not os.path.isfile(file_path):
                        logger.bind(tag=TAG).info(f"正在下载模型文件: {file_name}")
                        model_file_download(
                            model_id="pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue",
                            file_path=file_name,
                            local_dir=self.model_dir,
                        )

                        if not os.path.isfile(file_path):
                            raise FileNotFoundError(f"模型文件下载失败: {file_path}")

                self.model_path = model_files["model.int8.onnx"]
                self.tokens_path = model_files["tokens.txt"]

            except Exception as e:
                logger.bind(tag=TAG).error(f"模型文件处理失败: {str(e)}")
                raise

        with CaptureOutput():
            if self.model_type == "paraformer":
                self.model = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=self.model_path,
                    tokens=self.tokens_path,
                    num_threads=2,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    debug=False,
                )
                logger.bind(tag=TAG).info(f"sherpa-onnx Paraformer 模型加载完成: {self.model_dir}")
            else:  # sense_voice
                self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=self.model_path,
                    tokens=self.tokens_path,
                    num_threads=2,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    debug=False,
                    use_itn=True,
                )
                logger.bind(tag=TAG).info(f"sherpa-onnx SenseVoice 模型加载完成: {self.model_dir}")

        # 会话管理
        self._sessions: Dict[str, List[float]] = {}  # session_id -> pending samples
        self._session_text: Dict[str, str] = {}  # session_id -> accumulated text

    def read_wave(self, wave_filename: str) -> Tuple[np.ndarray, int]:
        with wave.open(wave_filename) as f:
            assert f.getnchannels() == 1, f.getnchannels()
            assert f.getsampwidth() == 2, f.getsampwidth()
            num_samples = f.getnframes()
            samples = f.readframes(num_samples)
            samples_int16 = np.frombuffer(samples, dtype=np.int16)
            samples_float32 = samples_int16.astype(np.float32)
            samples_float32 = samples_float32 / 32768
            return samples_float32, f.getframerate()

    def requires_file(self) -> bool:
        return False

    def _get_session(self, session_id: str) -> List[float]:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
            self._session_text[session_id] = ""
        return self._sessions[session_id]

    def _recognize_samples(self, samples: List[float]) -> str:
        """识别一段音频样本，返回文本"""
        if not samples:
            logger.bind(tag=TAG).warning("_recognize_samples: 样本为空")
            return ""
        try:
            s = self.model.create_stream()
            s.accept_waveform(SAMPLE_RATE, samples)
            self.model.decode_stream(s)
            result_text = s.result.text
            text = result_text.strip() if result_text else ""
            logger.bind(tag=TAG).info(f"[{self.model_type}] 识别原始结果: '{text}' (样本数={len(samples)})")
            return text
        except Exception as e:
            logger.bind(tag=TAG).error(f"[{self.model_type}] 识别异常: {e}", exc_info=True)
            return ""

    async def process_streaming_chunk(
        self, pcm_chunk: bytes, session_id: str
    ) -> Optional[str]:
        """处理音频块：根据 streaming 配置决定行为"""
        try:
            if self.streaming:
                return await self._process_streaming_chunk_streaming(pcm_chunk, session_id)
            else:
                return self._process_streaming_chunk_offline(pcm_chunk, session_id)
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理音频块失败: {e}", exc_info=True)
            return None

    def _process_streaming_chunk_offline(self, pcm_chunk: bytes, session_id: str) -> None:
        """整句离线模式：只累积音频，不做识别"""
        pending = self._get_session(session_id)
        samples = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        pending.extend(samples.tolist())
        return None

    async def _process_streaming_chunk_streaming(self, pcm_chunk: bytes, session_id: str) -> Optional[str]:
        """模拟流式模式：增量识别音频块，返回增量文本（优化：只识别新加入的音频）"""
        pending = self._get_session(session_id)

        # 将 PCM bytes 转换为 float32 samples
        samples = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        pending.extend(samples.tolist())

        # 关键修复：只识别最近 CHUNK_SAMPLES 长度的音频（增量识别），而非全部累积音频
        # 原逻辑识别全部累积音频，导致每次识别时间随语音变长而线性增长
        if len(pending) >= CHUNK_SAMPLES:
            samples_to_recognize = pending[-CHUNK_SAMPLES:]  # 只取最近 CHUNK_SAMPLES
            text = self._recognize_samples(samples_to_recognize)

            if text:
                prev_text = self._session_text.get(session_id, "")
                # 只在文本真正变化时才返回（避免重复）
                if text != prev_text:
                    self._session_text[session_id] = text
                    logger.bind(tag=TAG).info(f"[{self.model_type}] 流式中间结果: {text}")
                    return text

        return None

    async def finish_streaming(self, session_id: str, conn=None) -> str:
        """结束流式会话，对全部累积音频进行最终识别"""
        try:
            pending = self._sessions.pop(session_id, [])
            accumulated_text = self._session_text.pop(session_id, "")
            logger.bind(tag=TAG).info(f"[{self.model_type}] finish_streaming 被调用: session_id={session_id}, pending_samples={len(pending)}, accumulated_text='{accumulated_text}'")

            if not pending:
                logger.bind(tag=TAG).warning(f"[{self.model_type}] 无音频样本，跳过识别")
                return accumulated_text

            # 对全部累积音频进行最终识别
            text = self._recognize_samples(pending)
            logger.bind(tag=TAG).info(f"[{self.model_type}] 最终识别结果: '{text}'")

            if text and text.strip():
                logger.bind(tag=TAG).info(f"[{self.model_type}] 识别成功，准备触发对话: {text}")

                # 触发后续对话（流式模式下 finish_streaming 需要负责触发对话）
                if conn:
                    try:
                        await self._trigger_chat(conn, text)
                    except Exception as chat_err:
                        logger.bind(tag=TAG).error(f"[{self.model_type}] 触发对话失败: {chat_err}")
                else:
                    logger.bind(tag=TAG).warning(f"[{self.model_type}] conn 为 None，无法触发对话")

                return text
            else:
                logger.bind(tag=TAG).warning(f"[{self.model_type}] 识别结果为空，不触发对话")

            return accumulated_text

        except Exception as e:
            logger.bind(tag=TAG).error(f"[{self.model_type}] 结束流式会话失败: {e}", exc_info=True)
            return ""

    async def _trigger_chat(self, conn, text: str):
        """触发后续对话流程"""
        try:
            from core.handle.receiveAudioHandle import startToChat
            # 免唤醒守卫：流式路径原先绕过 _idle_guard_allow（非流式 handle_voice_stop
            # 才有），导致待机时的环境噪声/音乐被 SenseVoice 幻听成文本后直接提交 LLM
            # 作答（2026-09-17 实测：背景声被识别成英文乱码并触发多轮无关回复）。
            # 这里与非流式路径对齐。
            if not await self._idle_guard_allow(conn, text):
                return
            logger.bind(tag=TAG).info(f"[{self.model_type}] 调用 startToChat: {text[:50]}...")
            await startToChat(conn, text)
            logger.bind(tag=TAG).info(f"[{self.model_type}] startToChat 调用完成")
        except Exception as e:
            logger.bind(tag=TAG).error(f"[{self.model_type}] startToChat 失败: {e}", exc_info=True)

    def _cleanup_offline_session(self, session_id: str) -> None:
        """丢弃指定会话的流式缓冲（2026-10-04 修内存泄漏）

        离线识别路径不经过 finish_streaming()，这两个字典永不清空。
        识别只用 artifacts.pcm_bytes，与缓冲无关，故可直接丢弃。
        """
        self._sessions.pop(session_id, None)
        self._session_text.pop(session_id, None)

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本（离线模式）"""
        if artifacts is None or artifacts.pcm_bytes is None:
            return "", None

        # 清理流式缓冲区（2026-10-04 修内存泄漏）：
        # 离线模式从不调用 finish_streaming()，导致 _sessions/_session_text
        # 只增不减。本方法只用 artifacts.pcm_bytes（来自已正确清空的
        # conn.asr_audio），不依赖 _sessions，故可在入口安全丢弃。
        self._cleanup_offline_session(session_id)

        try:
            start_time = time.time()

            # 将 PCM bytes 转换为 float32 数组
            pcm_bytes = artifacts.pcm_bytes
            samples_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            samples_float32 = samples_int16.astype(np.float32) / 32768.0

            # 诊断：检查 ASR 收到的 PCM 数据质量
            rms = np.sqrt(np.mean(samples_int16.astype(np.float64) ** 2))
            max_val = np.max(np.abs(samples_int16))
            duration = len(samples_int16) / 16000.0
            logger.bind(tag=TAG).warning(f"[ASR-PCM诊断] {len(pcm_bytes)} bytes, {len(samples_int16)} samples, {duration:.2f}s, RMS={rms:.1f}, MAX={max_val}, opus_blocks={len(opus_data)}")

            # 【修复 2026-09-12】低能量音频仅记录诊断，不拦截：
            # 原能量门限（RMS<1500 丢弃）把正常说话音量（实测 1100~1400）与
            # 回声（实测 ~1093）混为一谈，音量稍轻即被误杀，导致"ASR 不工作"。
            # 回声已由 receiveAudioHandle 的文本级过滤（与 TTS 播报相似度判定）兜住，
            # 此处不再按能量拦截。
            if rms < 1500.0:
                logger.bind(tag=TAG).info(
                    f"[ASR-低能量] RMS={rms:.0f}（未拦截，交由文本级回声过滤兜底）"
                )

            # 2026-10-11：识别 40~180ms 原先同步跑在事件循环上（预览+正式各一次），
            # 期间整个连接的音频接收/TTS 发送全部停摆。放线程池跑，并用锁串行
            # （同一个 recognizer 不并发解码，预览与正式识别可能重叠）。
            def _decode_sync():
                with self._decode_lock:
                    st = self.model.create_stream()
                    st.accept_waveform(SAMPLE_RATE, samples_float32)
                    self.model.decode_stream(st)
                    res = st.result
                    # SenseVoice 除文字外还输出声学事件/语种/情感标签
                    # （<|Speech|>/<|Cough|>/<|Laughter|>/<|Breath|>…）。
                    # 2026-10-13 实测：键盘/点击/脚步/拍手等非语音被识别成
                    # 「The. Yeah. I. .」这类垃圾文本，与播报期垃圾同型；
                    # 咳嗽约一半能被标成 Cough。先只记录，积累播报期真实数据。
                    meta = tuple(
                        str(getattr(res, k, "") or "").strip("<|>")
                        for k in ("event", "lang", "emotion"))
                    return res.text, meta

            text, _meta = await asyncio.to_thread(_decode_sync)
            # 供播报期打断闸门读取事件标签（咳嗽/笑声/呼吸不打断）；带上文本，
            # 读取方用文本比对，防被并发的另一次识别覆盖
            self.last_meta = {"text": (text or "").strip(), "event": _meta[0],
                              "lang": _meta[1], "emotion": _meta[2]}
            asr_latency_ms = (time.time() - start_time) * 1000
            logger.bind(tag=TAG).info(
                f"[{self.model_type}] 离线识别耗时: {asr_latency_ms:.1f}ms | 结果: {text}"
            )
            if self.model_type == "sense_voice":
                logger.bind(tag=TAG).info(
                    f"[sense_voice标签] 事件={_meta[0]} 语种={_meta[1]} "
                    f"情感={_meta[2]} | 时长={duration:.2f}s RMS={rms:.0f} | 文本={text}"
                )
            
            # 延迟监控：ASR 完成
            if hasattr(self, 'conn') and self.conn and hasattr(self.conn, 'latency_monitor'):
                self.conn.latency_monitor.mark('asr_complete', text_length=len(text), asr_ms=asr_latency_ms)
            
            return text, artifacts.file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
            return "", artifacts.file_path if artifacts else None
