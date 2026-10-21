import os
import re
import time
import uuid
import queue
import asyncio
import threading
import traceback

from core.utils import p3
from datetime import datetime
from core.utils import textUtils
from typing import Callable, Any
from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.utils import opus_encoder_utils
from core.utils.tts import MarkdownCleaner, convert_percentage_to_range
from core.utils.output_counter import add_device_output
from core.handle.reportHandle import enqueue_tts_report
from core.handle.sendAudioHandle import sendAudioMessage
from core.utils.util import audio_bytes_to_data_stream, audio_to_data_stream
from core.utils.speech_clean import clean_for_speech
from core.providers.tts.dto.dto import (
    TTSMessageDTO,
    SentenceType,
    ContentType,
    InterfaceType,
)

TAG = __name__
logger = setup_logging()


class TTSProviderBase(ABC):
    def __init__(self, config, delete_audio_file):
        self.interface_type = InterfaceType.NON_STREAM
        self.conn = None
        self.delete_audio_file = delete_audio_file
        self.audio_file_type = "wav"
        self.output_file = config.get("output_dir", "tmp/")
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True
        self.before_stop_play_files = []
        self.report_on_last = False

        self.tts_text_buff = []
        self.punctuations = (
            "。",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.first_sentence_punctuations = (
            "，",
            "~",
            "、",
            ",",
            "。",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.tts_stop_request = False
        self.processed_chars = 0
        self.is_first_sentence = True

        # 添加TTS内容去重相关变量
        self.last_processed_text = ""
        self.last_processed_time = 0
        self.PROCESS_DUPLICATE_WINDOW = 0.5  # 0.5秒内的重复内容将被忽略

        # 追踪待完成的 TTS 生成数量，确保 LAST 在所有音频入队后才处理
        self._tts_pending_count = 0
        self._tts_pending_lock = threading.Lock()
        self._tts_all_done = threading.Event()
        self._tts_all_done.set()  # 初始状态：没有待完成项

        # 【合段缓冲 2026-09-08】ChatTTS 串行生成 rtf≈0.9，段越多段间饥饿窗口越多
        # （每段一次 HTTP+推理固定开销）。非首段短句先攒到 TTS_MERGE_MIN_CHARS 字
        # 再合并成一次合成，段数减半、开销摊薄；首段仍立即出声保证首响延迟。
        self._pending_tts_seg = ""
        self._tts_first_seg_sent = False
        self.TTS_MERGE_MIN_CHARS = 24

        # [2026-10-13 删除 TTS_FIRST_MERGE_CHARS]
        # 我 2026-10-12 为治「超短首段音色漂移」自加了「首段攒够 12 字才合成」，
        # 主人并未下过这个指示，且实测首响明显变慢（主人反馈「大模型已经把文字
        # 输出来了，但是 TTS 没跟上」）。主人裁决：**TTS 一定要快，流式 TTS 不做
        # 任何等待，收到文字马上就开始播** → 首段改回立即合成，该配置无引用，删除。
        # 音色问题另走 Qwen3TTS 1.7B + seed 路线，不许再用牺牲首响的方式换音色。

        # 【历史说明】2026-10-07 曾在这里加 `TTS_FIRST_MIN_CHARS = 6`：
        # 主人反馈「说家庭成员名字时音色跟实际的不一样，听着挺别扭」，
        # 根因是 LLM 回复常以「小花朵，」「主人，」「好嘞，」开头，
        # 首段按弱标点切出后**只有 2~4 个字**，被单独送进 TTS 合成一次 ——
        # 而 Qwen3-TTS 这类模型在超短文本上音色/韵律漂移极大，
        # 听起来就像换了个人在喊名字。
        # 当时用「首段不足 6 字就不切、等下文凑够」绕过，那是治症状：
        # 要么首响变慢（要等第一个句号），要么攒到 6 字仍是个短句，漂移照样在。
        # 2026-10-12 主人裁定「音色漂移需要根治」→ 拦截点整体移到合段缓冲
        # （见 `_merge_and_maybe_emit`），该配置已无任何代码引用，按
        # 「没用的逻辑不留」删除。
        self.TTS_STRONG_PUNCTUATIONS = ("。", "！", "!", "？", "?")

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    def handle_opus(self, opus_data: bytes):
        logger.bind(tag=TAG).debug(f"推送数据到队列里面帧数～～ {len(opus_data)}")
        self.tts_audio_queue.put((SentenceType.MIDDLE, opus_data, None, getattr(self, 'current_sentence_id', None)))

    def handle_pcm(self, pcm_data: bytes, end_of_stream: bool = False):
        """PCM直通：跳过Opus编解码，直接推送原始PCM到播放队列。
        必须切成 60ms 小帧（16k=1920字节）再入队：下游流控器按"每包一帧"
        pacing（60ms/包），整段大块入队会被一口气burst发出去，
        浏览器端 buffer 崩溃全是咔咔声（seed-tts-2.0 单次推 10~22KB 实测）。
        跨包累积余量保证每帧恰好1920字节（60ms），播放速度才不变慢；
        end_of_stream=True 时冲刷不足一帧的尾巴（<60ms 可忽略）。"""
        FRAME_BYTES = 1920  # 16kHz * 60ms * 2字节
        buf = getattr(self, "_pcm_tail", b"") + pcm_data
        n_full = len(buf) // FRAME_BYTES
        for i in range(n_full):
            self.tts_audio_queue.put(
                (SentenceType.MIDDLE, buf[i * FRAME_BYTES:(i + 1) * FRAME_BYTES],
                 None, getattr(self, 'current_sentence_id', None))
            )
        self._pcm_tail = buf[n_full * FRAME_BYTES:]
        if end_of_stream and self._pcm_tail:
            self.tts_audio_queue.put(
                (SentenceType.MIDDLE, self._pcm_tail,
                 None, getattr(self, 'current_sentence_id', None))
            )
            self._pcm_tail = b""

    def _get_audio_handler(self):
        """根据客户端音频格式返回对应处理器：PCM客户端跳过Opus编解码"""
        if hasattr(self, 'conn') and self.conn and getattr(self.conn, 'audio_format', None) == 'pcm':
            return self.handle_pcm
        return self.handle_opus

    def encode_and_dispatch(self, pcm_data: bytes, end_of_stream: bool = False):
        """统一编码分发：PCM客户端直接推送原始PCM，Opus客户端编码后推送"""
        if hasattr(self, 'conn') and self.conn and getattr(self.conn, 'audio_format', None) == 'pcm':
            self.handle_pcm(pcm_data, end_of_stream=end_of_stream)
        else:
            self.opus_encoder.encode_pcm_to_opus_stream(pcm_data, end_of_stream, callback=self.handle_opus)

    def handle_audio_file(self, file_audio: bytes, text):
        self.before_stop_play_files.append((file_audio, text))

    def to_tts_stream(self, text, opus_handler: Callable[[bytes], None] = None, flush_tail: bool = True) -> None:
        # 延迟监控：TTS 开始合成
        if hasattr(self, 'conn') and self.conn and hasattr(self.conn, 'latency_monitor'):
            self.conn.latency_monitor.mark('tts_start', text_length=len(text))
        
        text = MarkdownCleaner.clean_markdown(text)
        # 对齐原版：段落文本先行入队（FIRST 空音频帧携带文本），
        # 使 audio_play 线程收到带文本的 FIRST 消息 → 外发含文本的 sentence_start（前端字幕流）
        if text:
            self.tts_audio_queue.put(
                (SentenceType.FIRST, [], text, getattr(self, 'current_sentence_id', None))
            )
        # 注意：不在每段之间重置 Opus 编码器，编码器状态连续才能保证音频流平滑过渡
        max_repeat_time = 2  # 减少重试次数，避免阻塞 LLM 流
        # PCM客户端跳过Opus编解码
        use_opus = not (hasattr(self, 'conn') and self.conn and getattr(self.conn, 'audio_format', None) == 'pcm')
        if self.delete_audio_file:
            # 需要删除文件的直接转为音频数据
            while max_repeat_time > 0:
                try:
                    audio_bytes = asyncio.run(self.text_to_speak(text, None))
                    if audio_bytes is not None and hasattr(audio_bytes, '__len__') and len(audio_bytes) > 0:
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            is_opus=use_opus,
                            callback=opus_handler,
                            sample_rate=self.conn.sample_rate,
                            opus_encoder=self.opus_encoder if use_opus else None,
                            flush_tail=flush_tail,
                        )
                        break
                    else:
                        max_repeat_time -= 1
                        time.sleep(0.2 * (2 - max_repeat_time))  # 减少退避时间：0.2s, 0.4s
                except Exception as e:
                    logger.bind(tag=TAG).warning(
                        f"语音生成失败{2 - max_repeat_time + 1}次: {text}，错误: {e}"
                    )
                    max_repeat_time -= 1
                    time.sleep(0.2 * (2 - max_repeat_time))  # 减少退避时间
            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                    f"语音生成成功: {text}，重试{2 - max_repeat_time}次"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"语音生成失败: {text}，请检查网络或服务是否正常"
                )
            return None
        else:
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and max_repeat_time > 0:
                    try:
                        asyncio.run(self.text_to_speak(text, tmp_file))
                    except Exception as e:
                        logger.bind(tag=TAG).warning(
                            f"语音生成失败{2 - max_repeat_time + 1}次: {text}，错误: {e}"
                        )
                        # 未执行成功，删除文件
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        max_repeat_time -= 1
                        time.sleep(0.2 * (2 - max_repeat_time))  # 减少退避时间

                if max_repeat_time > 0:
                    logger.bind(tag=TAG).info(
                        f"语音生成成功: {text}:{tmp_file}，重试{5 - max_repeat_time}次"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"语音生成失败: {text}，请检查网络或服务是否正常"
                    )
                self._process_audio_file_stream(tmp_file, callback=opus_handler)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
                return None

    def to_tts(self, text):
        text = MarkdownCleaner.clean_markdown(text)
        max_repeat_time = 2  # 减少重试次数，避免阻塞
        if self.delete_audio_file:
            # 需要删除文件的直接转为音频数据
            while max_repeat_time > 0:
                try:
                    audio_bytes = asyncio.run(self.text_to_speak(text, None))
                    if audio_bytes is not None and hasattr(audio_bytes, '__len__') and len(audio_bytes) > 0:
                        audio_datas = []
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            # 2026-10-12：PCM 客户端（网页端）必须出 PCM 帧；写死 Opus 会让网页端把 Opus 包当 PCM 播（咔啦咔啦）
                            is_opus=getattr(self.conn, "audio_format", None) != "pcm",
                            callback=lambda data: audio_datas.append(data),
                            sample_rate=self.conn.sample_rate,
                        )
                        return audio_datas
                    else:
                        max_repeat_time -= 1
                        time.sleep(0.2 * (2 - max_repeat_time))
                except Exception as e:
                    logger.bind(tag=TAG).warning(
                        f"语音生成失败{2 - max_repeat_time + 1}次: {text}，错误: {e}"
                    )
                    max_repeat_time -= 1
                    time.sleep(0.2 * (2 - max_repeat_time))
            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                    f"语音生成成功: {text}，重试{2 - max_repeat_time}次"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"语音生成失败: {text}，请检查网络或服务是否正常"
                )
            return None
        else:
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and max_repeat_time > 0:
                    try:
                        asyncio.run(self.text_to_speak(text, tmp_file))
                    except Exception as e:
                        logger.bind(tag=TAG).warning(
                            f"语音生成失败{2 - max_repeat_time + 1}次: {text}，错误: {e}"
                        )
                        # 未执行成功，删除文件
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        max_repeat_time -= 1
                        time.sleep(0.2 * (2 - max_repeat_time))

                if max_repeat_time > 0:
                    logger.bind(tag=TAG).info(
                        f"语音生成成功: {text}:{tmp_file}，重试{2 - max_repeat_time}次"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"语音生成失败: {text}，请检查网络或服务是否正常"
                    )

                return tmp_file
            except Exception as e:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
                return None

    @abstractmethod
    async def text_to_speak(self, text, output_file):
        pass

    def audio_to_pcm_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """音频文件转换为PCM编码"""
        return audio_to_data_stream(audio_file_path, is_opus=False, callback=callback, sample_rate=self.conn.sample_rate, opus_encoder=None)

    def audio_to_opus_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """音频文件转换为Opus编码"""
        return audio_to_data_stream(audio_file_path, is_opus=True, callback=callback, sample_rate=self.conn.sample_rate, opus_encoder=self.opus_encoder)

    def tts_one_sentence(
        self,
        conn,
        content_type,
        content_detail=None,
        content_file=None,
        sentence_id=None,
    ):
        """发送一句话"""
        if not sentence_id:
            if conn.sentence_id:
                sentence_id = conn.sentence_id
            else:
                sentence_id = str(uuid.uuid4().hex)
                conn.sentence_id = sentence_id
        # [2026-10-05] 在「切段之前」清洗，不能等到队列侧。
        # 原因：分段标点里含 ? 和 :（见 self.punctuations），而高德 URI 本身就是
        # `https://uri.amap.com/marker?markers=...`，`?` 天然是分段点 →
        # URL 必被切成两段，后半段 `marker?markers=...&src=...` 已经没有 https:// 前缀，
        # 正则再也匹配不到，于是被原样念出来（实测漏出 markers=116.57...&callnative=0）。
        # 完整文本在这里才拿得到，所以必须在此处清洗。
        content_detail = clean_for_speech(content_detail)
        if not content_detail:
            return

        # 对于单句的文本，进行分段处理
        segments = re.split(r"([。！？!?；;\n])", content_detail)
        for seg in segments:
            self.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=content_type,
                    content_detail=seg,
                    content_file=content_file,
                )
            )

    async def open_audio_channels(self, conn):
        self.conn = conn

        # 根据conn的sample_rate创建编码器，如果子类已经创建则不覆盖（IndexTTS接口返回为24kHZ-待重采样处理）
        if not hasattr(self, 'opus_encoder') or self.opus_encoder is None:
            self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
                sample_rate=conn.sample_rate, channels=1, frame_size_ms=60
            )

        # tts 消化线程
        self.tts_priority_thread = threading.Thread(
            target=self.tts_text_priority_thread, daemon=True
        )
        self.tts_priority_thread.start()

        # 音频播放 消化线程
        self.audio_play_priority_thread = threading.Thread(
            target=self._audio_play_priority_thread, daemon=True
        )
        self.audio_play_priority_thread.start()

    # 这里默认是非流式的处理方式
    # 流式处理方式请在子类中重写
    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                # 与小智一致：先检查打断信号，跳过所有消息
                # 2026-09-21 修复（打断竞态防御）：FIRST 是新一轮的初始化控制消息（不携带音频），
                # 必须放行——否则 tts_audio_first_sentence 无法重置，client_is_speaking 整轮为 False，
                # 该轮播报将完全无法被打断。放行 FIRST 不会播出内容：后续 MIDDLE/LAST 仍会被 abort 跳过。
                if self.conn.client_abort and message.sentence_type != SentenceType.FIRST:
                    logger.bind(tag=TAG).info("收到打断信息，终止TTS文本处理线程")
                    continue
                # 与小智一致：过滤旧消息，检查sentence_id是否匹配
                if message.sentence_id != self.conn.sentence_id:
                    continue
                if message.sentence_type == SentenceType.FIRST:
                    self.current_sentence_id = message.sentence_id
                    # 初始化参数
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    self._pending_tts_seg = ""
                    self._tts_first_seg_sent = False
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    # [2026-10-05] 播报前清洗（唯一拦截点，覆盖所有入队路径）：
                    # 工具结果/LLM 回复里常含 URL、百分号编码、坐标等技术内容，
                    # 主人反馈「它经常播报一些网址、编码之类的」。放在这里是因为
                    # chat_engine 里有 6 处直接 tts_text_queue.put 绕过
                    # tts_one_sentence，只改 tts_one_sentence 会漏掉 LLM 流式回复。
                    if segment_text:
                        segment_text = clean_for_speech(segment_text)
                    if segment_text:
                        # 合段缓冲：首段不再单独合成（详见
                        # `_merge_and_maybe_emit` 上方的「音色漂移根治」说明）
                        merged_seg = self._merge_and_maybe_emit(segment_text)
                        if merged_seg:
                            self.to_tts_stream(merged_seg, opus_handler=self._get_audio_handler(), flush_tail=False)
                elif ContentType.FILE == message.content_type:
                    # 中途插音频文件：先冲掉合段缓冲，否则那几字会丢在缓冲里
                    merged_seg = self._flush_merge_buffer()
                    if merged_seg:
                        self.to_tts_stream(merged_seg, opus_handler=self._get_audio_handler(), flush_tail=False)
                    self._process_remaining_text_stream(opus_handler=self._get_audio_handler(), flush_tail=False)
                    tts_file = message.content_file
                    if tts_file and os.path.exists(tts_file):
                        self._process_audio_file_stream(
                            tts_file, callback=self._get_audio_handler()
                        )
                if message.sentence_type == SentenceType.LAST:
                    # 流结束：先冲掉合段缓冲再处理剩余未切文本，保证音频按序入队
                    merged_seg = self._flush_merge_buffer()
                    if merged_seg:
                        self.to_tts_stream(merged_seg, opus_handler=self._get_audio_handler(), flush_tail=False)
                    self._process_remaining_text_stream(opus_handler=self._get_audio_handler(), flush_tail=True)
                    self.tts_audio_queue.put(
                        (message.sentence_type, [], message.content_detail, getattr(self, 'current_sentence_id', None))
                    )

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    def _audio_play_priority_thread(self):
        # 需要上报的文本和音频列表
        enqueue_text = None
        enqueue_audio = []
        while not self.conn.stop_event.is_set():
            text = None
            try:
                try:
                    item = self.tts_audio_queue.get(timeout=0.1)
                    # 与小智一致：支持 4-tuple (sentence_type, audio_datas, text, sentence_id)
                    if len(item) == 4:
                        sentence_type, audio_datas, text, sentence_id = item
                    else:
                        sentence_type, audio_datas, text = item
                        sentence_id = None
                except queue.Empty:
                    if self.conn.stop_event.is_set():
                        break
                    continue

                # 与小智一致：打断时跳过所有音频（包括 LAST）
                if self.conn.client_abort:
                    logger.bind(tag=TAG).debug("收到打断信号，跳过当前音频数据")
                    enqueue_text, enqueue_audio = None, []
                    continue

                # 【2026-10-07 主人实测「三段回答叠着播」】上一轮残留音频必须丢弃。
                # 成因：新一轮 chat 开始时会把 client_abort 复位（那是必要的，否则
                # 新轮的 MIDDLE 也会被跳过），于是上一轮**已经合成进音频队列**的数据
                # 会被照常发出去。文本线程早有 sentence_id 过滤，音频线程之前没有。
                # 只过滤带 sentence_id 的 4 元组；3 元组（绑定提示音等）不受影响。
                if (sentence_id is not None and self.current_sentence_id
                        and sentence_id != self.current_sentence_id):
                    logger.bind(tag=TAG).info(
                        f"丢弃上一轮残留音频（sentence_id {sentence_id[:8]} != "
                        f"{self.current_sentence_id[:8]}）")
                    enqueue_text, enqueue_audio = None, []
                    continue

                # 收到下一个文本开始或会话结束时进行上报
                if sentence_type is not SentenceType.MIDDLE:
                    if self.report_on_last:
                        # 累积模式：适用于全程只有一个语音流的TTS（如seed-tts-2.0）
                        # FIRST时只记录文本，音频持续累积，仅在LAST时统一上报
                        if text:
                            enqueue_text = text
                        if sentence_type == SentenceType.LAST:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                            enqueue_audio = []
                            enqueue_text = None
                    else:
                        # 非累积模式：每个句子分别上报
                        if enqueue_text is not None:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                        enqueue_audio = []
                        enqueue_text = text

                # 收集上报音频数据
                if isinstance(audio_datas, bytes):
                    enqueue_audio.append(audio_datas)

                # 延迟监控：音频开始播放（第一个音频数据）
                if sentence_type == SentenceType.FIRST and audio_datas and hasattr(self, 'conn') and self.conn and hasattr(self.conn, 'latency_monitor'):
                    self.conn.latency_monitor.mark('audio_play_start')
                
                # 发送音频
                future = asyncio.run_coroutine_threadsafe(
                    sendAudioMessage(self.conn, sentence_type, audio_datas, text),
                    self.conn.loop,
                )
                future.result()

                # 记录输出和报告
                if self.conn.max_output_size > 0 and text:
                    add_device_output(self.conn.headers.get("device-id"), len(text))

            except Exception as e:
                err_str = str(e)
                # 检测 WebSocket 断连（1011 ping timeout / ConnectionClosed），跳出循环停止发送
                if "1011" in err_str or "ConnectionClosed" in err_str or "keepalive ping timeout" in err_str:
                    logger.bind(tag=TAG).warning(f"WebSocket 已断开，停止音频发送线程: {e}")
                    break
                logger.bind(tag=TAG).error(f"audio_play_priority_thread: {text} {e}")

    async def start_session(self, session_id):
        pass

    async def finish_session(self, session_id):
        pass

    async def close(self):
        """资源清理方法"""
        if hasattr(self, "ws") and self.ws:
            await self.ws.close()

    # ┌─────────────────────────────────────────────────────────────────────┐
    # │ 合段缓冲（2026-10-12 音色漂移根治，主人裁定「音色漂移需要根治」）      │
    # ├─────────────────────────────────────────────────────────────────────┤
    # │ 病根：首段常只有 2~4 个字（「主人，」「好嘞，」「小花朵，」），        │
    # │ 被单独送进 TTS 合成一次 —— 超短文本上音色/韵律漂移极大，             │
    # │ 主人实测「听着像换了个人在喊名字」，明确说「很讨厌」。               │
    # │                                                                     │
    # │ 旧绕法：`_get_segment_text()` 里加 6 字下限、不足就返回 None 攒字      │
    # │ 等强标点。那是治症状 —— 要么首响变慢（要等第一个句号），              │
    # │ 要么攒到 6 字仍是个短句，漂移照样在。                                │
    # │                                                                     │
    # │ 根治：首段与非首段走**同一个合段缓冲**，攒够阈值才合成一次。           │
    # │ 短前缀会和后面的正文合并成一句完整的合成请求，                       │
    # │ 超短文本不再单独进模型。代价是首响晚一点（多等几个字），              │
    # │ 换来音色一致 —— 这是主人要的取舍。                                   │
    # │                                                                     │
    # │ 为什么抽成方法：原来这段逻辑内联在 `tts_text_priority_thread` 里，     │
    # │ 单测只能自己抄一份逻辑去验证，抄的那份和真实代码一脱节就测不出回归。  │
    # │ 抽出来后线程与单测共用同一份实现。                                   │
    # └─────────────────────────────────────────────────────────────────────┘
    def _merge_and_maybe_emit(self, segment_text: str):
        """并入合段缓冲，攒够阈值就返回要合成的文本，否则返回 None。

        [2026-10-13 主人裁决：TTS 一定要快]
        主人原话：「流式 TTS 不做任何等待，收到文字马上就开始播。」

        所以**首段立即合成**，一个字的字都不攒：
          · 首段：拿到就返回 → 引擎立刻开始推理 → 首响最快。
          · 首段之后：沿用原来的 TTS_MERGE_MIN_CHARS（24 字）合并，
            非首段的已验证行为一字不改（那里攒字不影响首响）。

        历史教训（我 2026-10-12 自作主张、主人未下过指示）：
        我曾为治「超短首段音色漂移」把首段改成攒够 12 字才合成，
        结果首响明显变慢 —— 主人反馈「大模型已经把文字输出来了，
        但是 TTS 没跟上」。攒字不是「给 TTS 准备时间」：TTS 在拿到
        文本之前完全空闲，攒字只是把引擎启动时刻往后推，是纯加法。
        音色问题改用别的办法（见 Qwen3TTS 1.7B + seed 方案），
        **不许再用牺牲首响的方式换音色**。
        """
        if not segment_text:
            return None
        self._pending_tts_seg += segment_text
        if not self._tts_first_seg_sent:
            # 首段零等待：立刻合成
            self._tts_first_seg_sent = True
            merged, self._pending_tts_seg = self._pending_tts_seg, ""
            return merged
        if len(self._pending_tts_seg) < self.TTS_MERGE_MIN_CHARS:
            return None
        merged, self._pending_tts_seg = self._pending_tts_seg, ""
        return merged

    def _flush_merge_buffer(self):
        """流结束 / 中途插音频文件时冲掉缓冲，保证音频按序入队。

        不冲就会丢尾巴：缓冲里攒着的那几个字永远等不到阈值，
        而后面紧跟的音频文件/剩余文本已经入队了 → 顺序错乱且漏字。
        """
        if not self._pending_tts_seg:
            return None
        merged, self._pending_tts_seg = self._pending_tts_seg, ""
        self._tts_first_seg_sent = True
        return merged

    def _get_segment_text(self):
        # 合并当前全部文本并处理未分割部分
        full_text = "".join(self.tts_text_buff)
        current_text = full_text[self.processed_chars :]  # 从未处理的位置开始
        last_punct_pos = -1

        # 根据是否是第一句话选择不同的标点符号集合
        punctuations_to_use = (
            self.first_sentence_punctuations
            if self.is_first_sentence
            else self.punctuations
        )

        for punct in punctuations_to_use:
            pos = current_text.rfind(punct)
            if (pos != -1 and last_punct_pos == -1) or (
                pos != -1 and pos > last_punct_pos  # 找最后出现的位置
            ):
                last_punct_pos = pos

        if last_punct_pos != -1:
            segment_text_raw = current_text[: last_punct_pos + 1]

            # [2026-10-12 移除首段 6 字下限]
            # 原逻辑：首段不足 TTS_FIRST_MIN_CHARS=6 且未命中强标点就返回 None，
            # 继续攒字。那是在治「超短首段单独合成导致音色漂移」的症状。
            # 根治手段已经换成「首段也走合段缓冲，攒够再合成一次」
            # （见 tts_text_priority_thread），短前缀会和正文合并，不会再单独进模型。
            # 所以这层门槛不再需要，留着只会让首响无谓变慢。

            # 添加去重检查 - 使用正确的变量名
            # 去掉标点/emoji 后才是真正要合成的文本（标点会让 TTS 念出怪声）
            segment_text = textUtils.get_string_no_punctuation_or_emoji(
                segment_text_raw
            )

            current_time = time.time()
            if (hasattr(self, 'last_processed_text') and
                segment_text_raw.strip() == getattr(self, 'last_processed_text', '').strip() and
                (current_time - getattr(self, 'last_processed_time', 0)) < getattr(self, 'PROCESS_DUPLICATE_WINDOW', 0.5)):
                logger.bind(tag=TAG).debug(f"检测到重复TTS段，跳过: {segment_text_raw[:30]}...")
                # 更新已处理字符位置
                self.processed_chars += len(segment_text_raw)  # 更新已处理字符位置
                return None  # 跳过重复内容

            # 更新最后处理的内容和时间
            if segment_text.strip():
                if not hasattr(self, 'last_processed_text'):
                    self.last_processed_text = ""
                    self.last_processed_time = 0
                self.last_processed_text = segment_text.strip()
                self.last_processed_time = current_time

            self.processed_chars += len(segment_text_raw)  # 更新已处理字符位置

            # [2026-10-12 音色根治的连带修复] 首句模式的结束时机必须跟着
            # 「首段有没有真正合成出去」走，不能跟着「首段有没有切出来」走。
            #
            # 历史：这里原本有两处翻转 ——
            #   ① 首段含强标点（。！）时翻转；
            #   ② 首段切出后无条件翻转（防止整条回复全按弱标点切碎、
            #      每段 3-7 字导致播放"磕磕巴巴"）。
            # 在旧实现下 ② 没问题，因为「首段切出」和「首段合成」是同一步。
            #
            # 但现在首段要先进合段缓冲、攒够 TTS_FIRST_MERGE_CHARS 才合成。
            # 若切出即退出首句模式，通篇用逗号（弱标点）的回复会**再也不切段**，
            # 全堆到 SentenceType.LAST 才一次性合成 → 首响延迟爆炸。
            # 实测复现：喂「小花朵，」「数字炸弹游戏开始啦，」「你猜一个数字，」
            # 三块，第 2、3 块 _get_segment_text 全返回 None，最后只 flush 出
            # 「小花朵」三个字 —— 既丢了首响，也把整段正文压成一次超长合成。
            #
            # 所以两处合并成一处，条件改为 `_tts_first_seg_sent`：
            # 缓冲攒字期间仍允许按弱标点切；攒够即合成并退出首句模式，
            # 后续照旧按强标点合并成完整句子（"不磕巴"的已验证行为不变）。
            if self.is_first_sentence and self._tts_first_seg_sent:
                self.is_first_sentence = False

            return segment_text
        elif self.tts_stop_request and current_text:
            segment_text = current_text
            self.processed_chars += len(segment_text)  # 更新已处理字符位置
            self.is_first_sentence = True  # 重置标志

            # 添加去重检查 - 使用正确的变量名
            current_time = time.time()
            if (hasattr(self, 'last_processed_text') and
                segment_text.strip() == getattr(self, 'last_processed_text', '').strip() and
                (current_time - getattr(self, 'last_processed_time', 0)) < getattr(self, 'PROCESS_DUPLICATE_WINDOW', 0.5)):
                logger.bind(tag=TAG).debug(f"检测到重复TTS段，跳过: {segment_text[:30]}...")
                return None  # 跳过重复内容

            # 更新最后处理的内容和时间
            if segment_text.strip():
                if not hasattr(self, 'last_processed_text'):
                    self.last_processed_text = ""
                    self.last_processed_time = 0
                self.last_processed_text = segment_text.strip()
                self.last_processed_time = current_time

            return segment_text
        else:
            return None

    def _process_audio_file_stream(
        self, tts_file, callback: Callable[[Any], Any]
    ) -> None:
        """处理音频文件并转换为指定格式

        Args:
            tts_file: 音频文件路径
            callback: 文件处理函数
        """
        if tts_file.endswith(".p3"):
            p3.decode_opus_from_file_stream(tts_file, callback=callback)
        elif self.conn.audio_format == "pcm":
            self.audio_to_pcm_data_stream(tts_file, callback=callback)
        else:
            self.audio_to_opus_data_stream(tts_file, callback=callback)

        if (
            self.delete_audio_file
            and tts_file is not None
            and os.path.exists(tts_file)
            and tts_file.startswith(self.output_file)
        ):
            os.remove(tts_file)

    def _process_before_stop_play_files(self):
        for audio_datas, text in self.before_stop_play_files:
            self.tts_audio_queue.put(
                (SentenceType.MIDDLE, audio_datas, text,
                 getattr(self, 'current_sentence_id', None)))
        self.before_stop_play_files.clear()
        self.tts_audio_queue.put(
            (SentenceType.LAST, [], None, getattr(self, 'current_sentence_id', None)))

    def _process_remaining_text_stream(
        self, opus_handler: Callable[[bytes], None] = None, flush_tail: bool = True
    ):
        """处理剩余的文本并生成语音

        Returns:
            bool: 是否成功处理了文本
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_stream(segment_text, opus_handler=opus_handler, flush_tail=flush_tail)
                self.processed_chars += len(remaining_text)
                return True
        return False

    def _apply_percentage_params(self, config):
        """根据子类定义的 TTS_PARAM_CONFIG 批量应用百分比参数"""
        for config_key, attr_name, min_val, max_val, base_val, transform in self.TTS_PARAM_CONFIG:
            if config_key in config:
                val = convert_percentage_to_range(config[config_key], min_val, max_val, base_val)
                setattr(self, attr_name, transform(val) if transform else val)
