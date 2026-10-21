import os
import io
import sys
import time
import json
import wave
import shutil
import psutil
import asyncio
import re
import threading
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict, Any

from funasr import AutoModel
from config.logger import setup_logging
from core.providers.asr.utils import lang_tag_filter
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report

TAG = __name__
logger = setup_logging()

MAX_RETRIES = 2
RETRY_DELAY = 1  # 重试延迟(秒)

# 音频参数
SAMPLE_RATE = 16000

# FunASR 流式识别参数（参考官方 websocket 实现）
# chunk_size = [0, 10, 5] 表示：
#   - 前瞻 chunk 数 = 0
#   - 当前 chunk 数 = 10 (600ms at 16kHz)
#   - 后瞻 chunk 数 = 5
# chunk_stride = chunk_size[1] * 960 = 9600 采样点
STREAMING_CHUNK_SAMPLES = 9600  # 600ms 音频块
STREAMING_CHUNK_INTERVAL = 10   # 每 10 个音频帧调用一次识别（opus 约 600ms，PCM 约 1.3s）

# 捕获标准输出（线程安全，防止多连接同时初始化时冲突）
_capture_lock = threading.Lock()

class CaptureOutput:
    def __enter__(self):
        self._lock = _capture_lock
        self._lock.acquire()
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            sys.stdout = self._original_stdout
            self.output = self._output.getvalue()
        finally:
            self._output.close()
            self._lock.release()
        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())


@dataclass
class StreamingSession:
    """流式识别会话状态"""
    created_at: float = 0.0
    last_seen: float = 0.0
    # FunASR 流式 cache（模型内部维护状态）
    asr_cache: Dict = field(default_factory=dict)
    # 累积的 PCM 音频（用于 finish_streaming 的最终识别）
    pcm_buffer: List[float] = field(default_factory=list)
    # 当前累积的完整识别文本
    accumulated_text: str = ""
    # 接收的 opus 包计数
    chunk_count: int = 0
    # 最后一次发送的文本（用于避免重复发送）
    last_sent_text: str = ""


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()

        # 内存检测
        min_mem_bytes = 2 * 1024 * 1024 * 1024
        total_mem = psutil.virtual_memory().total
        if total_mem < min_mem_bytes:
            logger.bind(tag=TAG).error(f"可用内存不足2G,当前仅有 {total_mem / (1024*1024):.2f} MB,可能无法启动FunASR")

        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        # fun_local.py 仅支持 paraformer-zh-streaming（FunASR AutoModel 真流式）
        # 不兼容 SenseVoice/Nano，请使用 sherpa_onnx_local.py
        if self.model_dir and "sensevoice" in self.model_dir.lower():
            logger.bind(tag=TAG).error(
                f"fun_local.py 不支持 SenseVoice 模型: {self.model_dir}。"
                f"请使用 sherpa_onnx_local.py（model_type=sense_voice, streaming=false）"
            )
        if self.model_dir and "nano" in self.model_dir.lower():
            logger.bind(tag=TAG).error(
                f"fun_local.py 不支持 FunASR-Nano 模型: {self.model_dir}。"
                f"请使用 sherpa_onnx_local.py（model_type=funasr_nano, streaming=true）"
            )

        # paraformer-zh-streaming 真流式模式
        self.interface_type = InterfaceType.STREAM
        logger.bind(tag=TAG).info(f"FunASR paraformer-zh-streaming（真流式）: {self.model_dir}")

        # 会话管理
        self.SESSIONS: Dict[str, StreamingSession] = {}
        self.SESSION_TTL_SEC = 10 * 60

        os.makedirs(self.output_dir, exist_ok=True)

        # 校验模型目录完整性：FunASR 需要 configuration.json 或 config.yaml + model.pt
        if self.model_dir and os.path.isdir(self.model_dir):
            has_config_json = os.path.exists(os.path.join(self.model_dir, "configuration.json"))
            has_config_yaml = os.path.exists(os.path.join(self.model_dir, "config.yaml"))
            has_model_pt = os.path.exists(os.path.join(self.model_dir, "model.pt"))
            if not has_config_json and not (has_config_yaml and has_model_pt):
                logger.bind(tag=TAG).warning(
                    f"模型目录不完整（缺少 configuration.json 或 config.yaml+model.pt），"
                    f"将尝试重新下载: {self.model_dir}"
                )
                import shutil
                shutil.rmtree(self.model_dir, ignore_errors=True)

        # 加载模型（不带 VAD，纯识别）
        with CaptureOutput():
            self.model = AutoModel(
                model=self.model_dir,
                disable_update=True,
                hub="hf",
            )

        logger.bind(tag=TAG).info(f"FunASR 模型加载完成: {self.model_dir}")

    def _get_session(self, session_id: str) -> Optional[StreamingSession]:
        session = self.SESSIONS.get(session_id)
        if session:
            session.last_seen = time.time()
        return session

    def _init_session(self, session_id: str) -> StreamingSession:
        session = StreamingSession(
            created_at=time.time(),
            last_seen=time.time(),
            asr_cache={},  # 空 cache，模型会自动初始化并更新
            pcm_buffer=[],
            accumulated_text="",
            chunk_count=0,
            last_sent_text="",
        )
        self.SESSIONS[session_id] = session
        logger.bind(tag=TAG).info(f"[流式] 初始化新会话: {session_id}")
        return session

    def _extract_text_from_result(self, result) -> str:
        """从 FunASR 识别结果中提取文本（兼容 model.generate 嵌套 list 格式）"""
        if not result:
            return ""
        # model.generate 返回 [[{"text": "..."}]] 嵌套格式，递归解包
        while isinstance(result, (list, tuple)) and len(result) > 0:
            result = result[0]
        if result is None:
            return ""
        if isinstance(result, dict):
            t = result.get("text", "")
        else:
            t = str(result)
        t = lang_tag_filter(t)
        if isinstance(t, dict):
            t = t.get("content", "")
        return t if t else ""

    def _is_meaningful_text(self, text: str) -> bool:
        """判断识别结果是否有意义"""
        if not text:
            return False
        cleaned = text.strip()
        cleaned = re.sub(r'<\|[a-z]+\|>', '', cleaned).strip()
        content_only = re.sub(r'[\s　！-～ -⁯　-〿＀-￯]', '', cleaned)
        has_chinese = bool(re.search(r'[一-鿿]', cleaned))
        has_english = bool(re.search(r'[a-zA-Z]{2,}', cleaned))
        if not content_only or (len(content_only) < 2 and not has_chinese and not has_english):
            return False
        return True

    async def process_streaming_chunk(
        self, pcm_chunk: bytes, session_id: str, is_final: bool = False
    ) -> Optional[str]:
        """流式处理音频块（参考 FunASR 官方 websocket 实现）

        FunASR 流式识别关键点：
        1. 每次调用 generate 时传入从上次调用后累积的新音频块
        2. cache 持续复用，模型内部维护流式状态
        3. 调用后清空当前累积帧（下一轮只处理新的增量）
        4. 模型返回的是基于 cache 和新音频的综合识别结果

        注意：不清空 accumulated_text，只清空 pcm_buffer
        """
        try:
            session = self._get_session(session_id)
            if not session:
                session = self._init_session(session_id)

            # 解码音频并追加到 buffer
            # 【关键修复】直接传入 int16，FunASR 内部会自动归一化
            # 如果在这里除以 32768.0，FunASR 内部会再次除，导致数据几乎为 0
            audio_data = np.frombuffer(pcm_chunk, dtype=np.int16)
            session.pcm_buffer.extend(audio_data.tolist())

            # 增加块计数
            session.chunk_count += 1

            # 判断是否需要调用识别：
            # - 每 STREAMING_CHUNK_INTERVAL 块调用一次
            # - 或者 is_final=True
            should_recognize = (
                session.chunk_count >= STREAMING_CHUNK_INTERVAL
                or is_final
            )

            if not should_recognize:
                return None

            # 取当前累积的音频块进行识别
            # 保持 int16 格式传入，FunASR 内部会自行转换
            audio_chunk = np.array(session.pcm_buffer, dtype=np.int16)

            # 【修复 2026-09-11】FunASR paraformer-zh-streaming 要求每次输入样本数
            # 精确等于 chunk_size[1] * 960 = 9600（600ms @16kHz）。
            # Web 终端 PCM 帧大小不固定，累积后可能多/少，导致 tensor 维度不匹配。
            # 解决方案：强制截断到标准大小，保证模型 cache 一致性。
            expected_samples = 9600  # chunk_size[1] * 960 = 10 * 960
            if len(audio_chunk) > expected_samples:
                audio_chunk = audio_chunk[:expected_samples]
            elif len(audio_chunk) < expected_samples and not is_final:
                # 不足标准大小但非 final，等待更多数据（下次识别会补上）
                return None

            # 调用 generate（cache 会被模型内部更新）
            result = await asyncio.to_thread(
                self.model.generate,
                input=audio_chunk,
                cache=session.asr_cache,
                is_final=is_final,
                chunk_size=[0, 10, 5],
                # 【修复 2026-09-07】官方流式参数：回看块数。缺失时默认 0（无回看），
                # 跨块边界会产生重复字伪影（如"明白白"），按数据库 asr_config 传入
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )

            text = self._extract_text_from_result(result)

            # 【关键】调用 generate 后清空 buffer（下一轮只处理新音频）
            if not is_final:
                session.pcm_buffer = []
                session.chunk_count = 0

            # 【修复 2026-09-06】FunASR 流式语义实测：所有帧（含 is_final=True）返回的都是
            # 从上次 cache 分界点开始的"增量文本"，不是全量。
            # （实测：块1→'今天'，块2→''，块3(final,空输入)→'天气怎'）
            # 因此必须无条件追加，原 startswith 判断会把增量误判为全量，导致文本回跳/丢失（蹦字根因）
            if text:
                session.accumulated_text += text

                logger.bind(tag=TAG).info(
                    f"[流式] chunk={len(audio_chunk)}samples, is_final={is_final}, 增量文本='{text}', 累积文本='{session.accumulated_text}'"
                )
                # 只在文本有意义时返回
                if self._is_meaningful_text(session.accumulated_text) and session.accumulated_text != session.last_sent_text:
                    session.last_sent_text = session.accumulated_text
                    return session.accumulated_text

            return None

        except Exception as e:
            logger.bind(tag=TAG).error(f"流式识别失败: {e}", exc_info=True)
            # 【关键修复】失败后重置 session 状态，避免损坏的 cache 持续传递
            # 导致后续调用全部失败，pcm_buffer 无限累积
            if not is_final:
                session.pcm_buffer = []
                session.chunk_count = 0
            # 重新初始化 cache，清除损坏的内部状态
            session.asr_cache = {}
            return None

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本 — VAD 切片识别（非流式模式不使用此方法）"""
        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                if artifacts is None or artifacts.pcm_bytes is None or len(artifacts.pcm_bytes) == 0:
                    return "", None

                start_time = time.time()

                result = await asyncio.to_thread(
                    self.model.generate,
                    input=artifacts.pcm_bytes,
                    language="auto",
                    use_itn=True,
                )

                text = self._extract_text_from_result(result)
                elapsed = time.time() - start_time

                logger.bind(tag=TAG).info(
                    f"识别耗时: {elapsed:.3f}s | 结果: {text}"
                )
                return text, artifacts.file_path

            except OSError as e:
                retry_count += 1
                if retry_count >= MAX_RETRIES:
                    logger.bind(tag=TAG).error(
                        f"语音识别失败（已重试{retry_count}次）: {e}", exc_info=True
                    )
                    return "", None
                logger.bind(tag=TAG).warning(
                    f"语音识别失败，正在重试（{retry_count}/{MAX_RETRIES}）: {e}"
                )
                time.sleep(RETRY_DELAY)

            except Exception as e:
                logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
                return "", None

    async def finish_streaming(self, session_id: str, conn: "ConnectionHandler") -> str:
        """结束流式识别 — 调用最终识别并触发 LLM

        关键：必须调用 startToChat 触发 LLM 回复

        FunASR 流式模式：
        - 中间帧 (is_final=False) 返回部分结果
        - 最终帧 (is_final=True) 应该返回完整句子，利用 cache 中的完整上下文
        - 即使 pcm_buffer 为空，也必须调用 generate(is_final=True) 来获取最终完整结果
        """
        try:
            session = self._get_session(session_id)
            if not session:
                logger.bind(tag=TAG).warning(f"[finish_streaming] 会话不存在: {session_id}")
                return ""

            logger.bind(tag=TAG).info(f"[finish_streaming] 开始处理, session_id={session_id}, pcm_buffer_size={len(session.pcm_buffer)}, accumulated_text='{session.accumulated_text}'")

            # 【关键修复】即使 pcm_buffer 为空，也必须调用 generate(is_final=True)
            # 因为 FunASR 的 cache 中维护了完整的流式状态，is_final=True 会触发最终解码
            # 传入空数组即可，模型会从 cache 中获取完整上下文
            # 保持 int16 格式传入，FunASR 内部会自行转换
            audio_chunk = np.array(session.pcm_buffer, dtype=np.int16) if session.pcm_buffer else np.array([], dtype=np.int16)

            result = await asyncio.to_thread(
                self.model.generate,
                input=audio_chunk,
                cache=session.asr_cache,
                is_final=True,  # 最终识别，触发完整解码
                chunk_size=[0, 10, 5],
            )
            final_text = self._extract_text_from_result(result)
            logger.bind(tag=TAG).info(f"[finish_streaming] 最终识别结果: '{final_text}' (pcm_buffer_size={len(session.pcm_buffer)})")

            # 【修复 2026-09-06】final 帧返回的也是增量文本（实测验证），
            # 必须追加到累积文本，而不是与 accumulated_text 比长度取舍
            if final_text:
                session.accumulated_text += final_text
            text = session.accumulated_text
            if final_text:
                logger.bind(tag=TAG).info(f"[finish_streaming] 追加增量后完整文本: {text}")

            # 清理会话
            self.SESSIONS.pop(session_id, None)
            logger.bind(tag=TAG).info(f"[finish_streaming] 会话清理完成: {session_id}")

            if not self._is_meaningful_text(text):
                if text:
                    logger.bind(tag=TAG).info(f"[finish_streaming] 识别结果为无意义片段，跳过: {text}")
                return ""

            logger.bind(tag=TAG).info(f"[finish_streaming] 识别完成，准备触发 LLM: {text}")

            # 免唤醒守卫：Web 终端常开推流，会话未激活时只放行唤醒词语音
            if not await self._idle_guard_allow(conn, text):
                return ""

            # 双重触发去重：VAD 停止与 listen stop 消息可能先后触发 finish_streaming，
            # 两次调用间 session 会被常开推流的 process_streaming_chunk 重建，
            # 导致同一句话重复 startToChat（两轮 LLM+两份 TTS 互相抢）。
            # 同一连接 1.5s 内相同文本只触发一次。
            now_ts = time.time()
            last_text = getattr(conn, "_last_starttochat_text", "")
            last_ts = getattr(conn, "_last_starttochat_ts", 0)
            if text == last_text and (now_ts - last_ts) < 1.5:
                logger.bind(tag=TAG).info(f"[finish_streaming] {now_ts - last_ts:.2f}s 内相同文本已触发过 startToChat，跳过重复: {text}")
                return text
            conn._last_starttochat_text = text
            conn._last_starttochat_ts = now_ts

            # 【修复 2026-09-09】流式路径补挂声纹识别：
            # 原先声纹只挂在非流式 handle_voice_stop（base.py），实际对话走流式
            # finish_streaming 绕过声纹 → current_speaker 永远为空 → 成员档案
            # 永远不注入（伊娃答不出"我是谁/我在家里的关系"）。
            # 用流式累积的 PCM 做声纹，识别失败不影响对话主链。
            speaker_name = None
            if conn.voiceprint_provider and len(session.pcm_buffer) > 0:
                try:
                    pcm_bytes = audio_chunk.tobytes()
                    wav_bytes = self._pcm_to_wav(pcm_bytes)
                    speaker_name = await conn.voiceprint_provider.identify_speaker(
                        wav_bytes, session_id)
                    if speaker_name:
                        logger.bind(tag=TAG).info(f"[finish_streaming] 声纹识别: {speaker_name}")
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"[finish_streaming] 声纹识别失败（不影响对话）: {e}")
                    speaker_name = None

            # 触发后续对话（startToChat 内部会调用 send_stt_message 发送 STT 消息）
            try:
                await startToChat(conn, text, speaker=speaker_name)
                logger.bind(tag=TAG).info(f"[finish_streaming] startToChat 调用成功")
            except Exception as e:
                logger.bind(tag=TAG).error(f"[finish_streaming] startToChat 失败: {e}", exc_info=True)

            # 上报 ASR 结果
            try:
                enqueue_asr_report(conn, text, [])
            except Exception as e:
                logger.bind(tag=TAG).debug(f"[finish_streaming] 上报 ASR 结果失败: {e}")

            return text

        except Exception as e:
            logger.bind(tag=TAG).error(f"[finish_streaming] 失败: {e}", exc_info=True)
            # 【关键修复】即使失败也要清理会话，避免损坏的 session 持续累积
            self.SESSIONS.pop(session_id, None)
            return ""