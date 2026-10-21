import json
import time
import asyncio
import sys
import os
import numpy as np
from typing import TYPE_CHECKING, Optional

# TAG 定义（需要在导入前定义，用于错误日志）
TAG = __name__

# 导入 logger（用于错误日志）
from config.logger import setup_logging
logger = setup_logging()

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils import textUtils
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.providers.asr.dto.dto import InterfaceType
from core.utils.audioRateController import AudioRateController

# 导入 opus 解码器
try:
    import opuslib_next
    OPUS_DECODER_AVAILABLE = True
except ImportError:
    OPUS_DECODER_AVAILABLE = False
    opuslib_next = None



# 音频帧时长（毫秒）

# Opus 编码参数：16kHz, 单声道, 60ms 帧
_OPUS_SAMPLE_RATE = 16000
_OPUS_CHANNELS = 1
_OPUS_FRAME_DURATION_MS = 60
AUDIO_FRAME_DURATION = 60
# 预缓冲包数量，直接发送以减少延迟
PRE_BUFFER_COUNT = 5


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """
    将 PCM 16-bit 数据包装为 WAV 格式

    Args:
        pcm_bytes: PCM 16-bit 音频数据
        sample_rate: 采样率
        channels: 声道数

    Returns:
        WAV 格式的字节数据
    """
    import struct

    num_samples = len(pcm_bytes) // 2  # 16-bit = 2 bytes per sample
    byte_rate = sample_rate * channels * 2  # 2 bytes per sample
    block_align = channels * 2
    data_size = num_samples * channels * 2

    # WAV header (44 bytes)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_size,  # File size - 8
        b'WAVE',
        b'fmt ',
        16,  # Subchunk1Size (PCM)
        1,   # AudioFormat (1 = PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,  # BitsPerSample
        b'data',
        data_size
    )

    return header + pcm_bytes


_OPUS_FRAME_SIZE = int(_OPUS_SAMPLE_RATE * _OPUS_FRAME_DURATION_MS / 1000)  # 960 samples
_global_opus_decoder = None


def _get_opus_decoder():
    """获取或创建全局 Opus 解码器"""
    global _global_opus_decoder
    if not OPUS_DECODER_AVAILABLE:
        return None
    if _global_opus_decoder is None:
        try:
            _global_opus_decoder = opuslib_next.Decoder(_OPUS_SAMPLE_RATE, _OPUS_CHANNELS)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Opus 解码器初始化失败: {e}")
            return None
    return _global_opus_decoder


async def sendAudioMessage(conn: "ConnectionHandler", sentenceType, audios, text):
    # 修复：如果 text 为 None，使用 conn.tts_MessageText 作为显示文本
    display_text = text if text is not None else getattr(conn, 'tts_MessageText', '')

    # 回声过滤（2026-09-11）：记录最近播报文本与时间，供 startToChat 比对丢弃自激 ASR
    if display_text:
        import time as _t
        conn.last_tts_text = display_text
        conn.last_tts_sent_at = _t.time()

    if conn.tts.tts_audio_first_sentence:
        conn.logger.bind(tag=TAG).info(f"发送第一段语音: {display_text}")
        conn.tts.tts_audio_first_sentence = False
        # [20261020 修复] 恢复「tts start」帧：ESP32 固件协议要求收到它才置
        # expecting_tts_audio_ 并切 speaking，否则所有 opus 音频帧被丢
        # （设备串口实证：Dropping audio packet, state=5, expecting_tts=0 → 有字无声）。
        # 该帧因修网页端咔咔杂音在 9-15 被整体移除；现两网页宿主保存有
        # state="start" 处理器（clearQueue+acceptIncoming+状态机），加回即恢复原协议。
        await send_tts_message(conn, "start", None)
        # 第一段语音时向前端发送 sentence_start，让 AI 空间麦等页面能实时显示回复文本
        await send_tts_message(conn, "sentence_start", display_text)
        # 有实际音频时设置客户端说话状态
        conn.client_is_speaking = True

        # 记录TTS开始时间，用于智能打断
        import time
        conn.tts_start_time = time.time()
    elif sentenceType == SentenceType.FIRST:
        # 后续段落：每段一条 sentence_start（字幕流）
        # 注意不能与"第一段"分支同时触发，否则第一段文本会重复发送两次
        # 同一句子的后续消息加入流控队列，其他情况立即发送
        if (
            hasattr(conn, "audio_rate_controller")
            and conn.audio_rate_controller
            and getattr(conn, "audio_flow_control", {}).get("sentence_id")
            == conn.sentence_id
        ):
            conn.audio_rate_controller.add_message(
                lambda: send_tts_message(conn, "sentence_start", display_text)
            )
        else:
            # 新句子或流控器未初始化，立即发送
            await send_tts_message(conn, "sentence_start", display_text)

    await sendAudio(conn, audios)
    # 发送句子开始消息
    if sentenceType is not SentenceType.MIDDLE:
        conn.logger.bind(tag=TAG).info(f"发送音频消息: {sentenceType}, {text}")

    # 发送结束消息（如果是最后一个文本）
    if sentenceType == SentenceType.LAST:
        await send_tts_message(conn, "stop", None)
        conn.client_is_speaking = False
        # [2026-10-13 音乐不停对话·第三段，主人批准方案 A] 旧「回复播完 →
        # schedule_restore 恢复音量 + is_music_paused_by_us/schedule_music_resume
        # 恢复被暂停音乐 + 音乐对话会话续期」整块已删除：双路径 AEC 上线后
        # 对话不再暂停音乐、不降音量，无恢复对象。
        # ⚠️ 顺带发现的既有 bug（删除时坐实）：原会话续期子块嵌在
        # is_music_paused_by_us() 分支内，且本函数第 121 行有函数级 `import time`
        # （仅在首句分支执行），LAST 调用里续期代码引用 `time.time()` 必抛
        # UnboundLocalError 被 except 吞掉、只执行 conn._session_active=False ——
        # 即续期从未真正生效过。删除后音乐对话的会话行为与无音乐对话完全一致
        # （无音乐时该块本来就整体跳过，数月生产行为）。
        if conn.close_after_chat:
            await conn.close()


async def _wait_for_audio_completion(conn: "ConnectionHandler"):
    """
    等待音频队列清空并等待预缓冲包播放完成

    Args:
        conn: 连接对象
    """
    if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
        rate_controller = conn.audio_rate_controller
        conn.logger.bind(tag=TAG).debug(
            f"等待音频发送完成，队列中还有 {len(rate_controller.queue)} 个包"
        )
        # 等待上限：按队列剩余音频帧数估算播放时长 + 5s 余量。
        # 2026-09-28 修复：原实现无上限等待，音频队列异常积压时（合成快于
        # 播放、多轮对话音频堆叠）会把调用方挂在实时播放等待上（实测卡 74s），
        # 导致"停止"类指令迟迟不能完成。正常播放路径估算时长即上限，不受影响。
        pending_frames = sum(
            1 for item in rate_controller.queue if item[0] == "audio"
        )
        wait_budget = max(
            5.0, pending_frames * rate_controller.frame_duration / 1000.0 + 5.0
        )
        try:
            await asyncio.wait_for(
                rate_controller.queue_empty_event.wait(), timeout=wait_budget
            )
        except asyncio.TimeoutError:
            conn.logger.bind(tag=TAG).warning(
                f"等待音频发送完成超时({wait_budget:.1f}s，积压{pending_frames}帧)，强制继续"
            )

        # 等待预缓冲包播放完成
        # 前N个包直接发送，增加2个网络抖动包，需要额外等待它们在客户端播放完成
        frame_duration_ms = rate_controller.frame_duration
        pre_buffer_playback_time = (PRE_BUFFER_COUNT + 2) * frame_duration_ms / 1000.0
        await asyncio.sleep(pre_buffer_playback_time)

        conn.logger.bind(tag=TAG).debug("音频发送完成")


async def _send_to_mqtt_gateway(
    conn: "ConnectionHandler", opus_packet, timestamp, sequence
):
    """
    发送带16字节头部的opus数据包给mqtt_gateway
    Args:
        conn: 连接对象
        opus_packet: opus数据包
        timestamp: 时间戳
        sequence: 序列号
    """
    # 为opus数据包添加16字节头部
    header = bytearray(16)
    header[0] = 1  # type
    header[2:4] = len(opus_packet).to_bytes(2, "big")  # payload length
    header[4:8] = sequence.to_bytes(4, "big")  # sequence
    header[8:12] = timestamp.to_bytes(4, "big")  # 时间戳
    header[12:16] = len(opus_packet).to_bytes(4, "big")  # opus长度

    # 发送包含头部的完整数据包
    complete_packet = bytes(header) + opus_packet
    await conn.websocket.send(complete_packet)


async def sendAudio(
    conn: "ConnectionHandler", audios, frame_duration=AUDIO_FRAME_DURATION
):
    """
    发送音频包，使用 AudioRateController 进行精确的流量控制

    Args:
        conn: 连接对象
        audios: 单个opus包(bytes) 或 opus包列表
        frame_duration: 帧时长（毫秒），默认使用全局常量AUDIO_FRAME_DURATION
    """
    if audios is None or len(audios) == 0:
        return

    send_delay = conn.config.get("tts_audio_send_delay", -1) / 1000.0
    is_single_packet = isinstance(audios, bytes)

    # 初始化或获取 RateController
    rate_controller, flow_control = _get_or_create_rate_controller(
        conn, frame_duration, is_single_packet
    )

    # 统一转换为列表处理
    audio_list = [audios] if is_single_packet else audios

    # 发送音频包
    await _send_audio_with_rate_control(
        conn, audio_list, rate_controller, flow_control, send_delay
    )


def _get_or_create_rate_controller(
    conn: "ConnectionHandler", frame_duration, is_single_packet
):
    """
    获取或创建 RateController 和 flow_control

    Args:
        conn: 连接对象
        frame_duration: 帧时长
        is_single_packet: 是否单包模式（True: TTS流式单包, False: 批量包）

    Returns:
        (rate_controller, flow_control)
    """
    # 检查是否需要重置控制器
    need_reset = False

    if not hasattr(conn, "audio_rate_controller") or conn.audio_rate_controller is None:
        # 控制器不存在或为 None，需要创建
        need_reset = True
    else:
        rate_controller = conn.audio_rate_controller

        # 后台发送任务已停止, 则需要重置
        if (
            not rate_controller.pending_send_task
            or rate_controller.pending_send_task.done()
        ):
            need_reset = True
        # 当sentence_id 变化，需要重置
        elif (
            getattr(conn, "audio_flow_control", {}).get("sentence_id")
            != conn.sentence_id
        ):
            need_reset = True

    if need_reset:
        # 创建或获取 rate_controller
        if not hasattr(conn, "audio_rate_controller") or conn.audio_rate_controller is None:
            conn.audio_rate_controller = AudioRateController(frame_duration)
        else:
            conn.audio_rate_controller.reset()

        # 初始化 flow_control
        conn.audio_flow_control = {
            "packet_count": 0,
            "sequence": 0,
            "sentence_id": conn.sentence_id,
        }

        # 启动后台发送循环
        _start_background_sender(
            conn, conn.audio_rate_controller, conn.audio_flow_control
        )

    return conn.audio_rate_controller, conn.audio_flow_control


def _start_background_sender(conn: "ConnectionHandler", rate_controller, flow_control):
    """
    启动后台发送循环任务

    Args:
        conn: 连接对象
        rate_controller: 速率控制器
        flow_control: 流控状态
    """

    async def send_callback(packet):
        # 检查是否应该中止
        if conn.client_abort:
            raise asyncio.CancelledError("客户端已中止")

        # 注意：发送音频时不更新 last_activity_time
        # last_activity_time 只记录用户活动（语音输入、唤醒等），用于超时检测
        await _do_send_audio(conn, packet, flow_control)

    # 使用 start_sending 启动后台循环
    rate_controller.start_sending(send_callback)


async def _send_audio_with_rate_control(
    conn: "ConnectionHandler", audio_list, rate_controller, flow_control, send_delay
):
    """
    使用 rate_controller 发送音频包

    Args:
        conn: 连接对象
        audio_list: 音频包列表
        rate_controller: 速率控制器
        flow_control: 流控状态
        send_delay: 固定延迟（秒），-1表示使用动态流控
    """
    for packet in audio_list:
        if conn.client_abort:
            return

        # 注意：发送音频时不更新 last_activity_time
        # last_activity_time 只记录用户活动（语音输入、唤醒等），用于超时检测

        # 预缓冲：前N个包直接发送
        if flow_control["packet_count"] < PRE_BUFFER_COUNT:
            await _do_send_audio(conn, packet, flow_control)
        elif send_delay > 0:
            # 固定延迟模式
            await asyncio.sleep(send_delay)
            await _do_send_audio(conn, packet, flow_control)
        else:
            # 动态流控模式：仅添加到队列，由后台循环负责发送
            rate_controller.add_audio(packet)


async def _do_send_audio(conn: "ConnectionHandler", opus_packet, flow_control):
    """
    执行实际的音频发送 - 优化音频质量，但不改变音量控制策略
    """
    packet_index = flow_control.get("packet_count", 0)
    sequence = flow_control.get("sequence", 0)

    # 延迟监控：本轮对话首个音频包外发（只打一次，mark 是覆盖语义）
    if packet_index == 0 and hasattr(conn, 'latency_monitor'):
        if 'audio_play_start' not in conn.latency_monitor.timestamps:
            conn.latency_monitor.mark('audio_play_start', sentence_id=getattr(conn, 'sentence_id', None))

    # 判断客户端音频格式：PCM客户端的数据已经是PCM（跳过Opus编解码）
    is_pcm_client = getattr(conn, "audio_format", None) == "pcm"

    # AEC 参考信号注册（参照小智 kAecOnServerSide）
    # 注意：注册参考信号是"附加动作"，不是"替代下发"——命中后仍需继续走
    # mqtt/pcm/continuous/opus 分发，否则声明的 pcm 客户端（Web 终端）收不到音频
    # 2026-09-15 修复：PCM 客户端（Web 终端）也需要注册参考信号！此前 `not is_pcm_client`
    # 守卫导致服务端 AEC 对 Web 终端完全旁路，浏览器 AEC 双讲抑制用户语音后无法补救
    if conn.aec_enabled and conn.aec:
        import time
        send_ts_ms = int(time.time() * 1000) & 0xFFFFFFFF

        if is_pcm_client:
            # PCM 客户端（Web 终端）：opus_packet 实际是 PCM 数据，直接注册为参考信号
            conn.aec.set_reference(send_ts_ms, opus_packet)
            conn.logger.bind(tag=TAG).debug(f"[AEC] PCM 客户端注册参考信号: ts={send_ts_ms}ms, pcm={len(opus_packet)} bytes")
        else:
            # Opus 客户端（ESP32）：解码为 PCM 后注册
            try:
                decoder = _get_opus_decoder()
                if decoder is not None:
                    pcm_ref = decoder.decode(opus_packet, _OPUS_FRAME_SIZE)
                    conn.aec.set_reference(send_ts_ms, pcm_ref)
                    conn.logger.bind(tag=TAG).debug(f"[AEC] 注册参考信号: ts={send_ts_ms}ms, pcm={len(pcm_ref)} bytes")
            except Exception as e:
                conn.logger.bind(tag=TAG).warning(f"[AEC] 参考信号解码失败: {e}")

    if conn.conn_from_mqtt_gateway:
        # MQTT网关处理
        start_time = time.time()
        timestamp = int(start_time * 1000) % (2**32)
        await _send_to_mqtt_gateway(conn, opus_packet, timestamp, sequence)
    elif is_pcm_client:
        # 浏览器 Web 终端：直接发送PCM数据（TTS已跳过Opus编码）
        await conn.websocket.send(opus_packet)
    elif conn.continuous_conversation_mode and conn.continuous_output_channel == "local":
        # 本地输出 - 高质量转换WAV格式，不改变音量
        try:
            decoder = _get_opus_decoder()
            if decoder is not None:
                # 解码 opus -> PCM (16kHz, 16-bit) - 高质量解码
                pcm_bytes = decoder.decode(opus_packet, _OPUS_FRAME_SIZE)

                # 包装为 WAV 格式（浏览器可播放）- 优化格式转换
                wav_data = _pcm_to_wav_optimized(pcm_bytes, sample_rate=16000, channels=1)

                # 广播 WAV 到所有连接
                if hasattr(conn, 'server') and conn.server and hasattr(conn.server, 'broadcast_message'):
                    await conn.server.broadcast_message(wav_data)
                else:
                    await conn.websocket.send(wav_data)
                conn.logger.bind(tag=TAG).debug(f"本地输出: 广播 WAV {len(wav_data)} bytes")
            else:
                if hasattr(conn, 'server') and conn.server and hasattr(conn.server, 'broadcast_message'):
                    await conn.server.broadcast_message(opus_packet)
                else:
                    await conn.websocket.send(opus_packet)
                conn.logger.bind(tag=TAG).warning("Opus 解码器不可用，发送原始 Opus 数据")
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"本地音频处理失败: {e}")
            await conn.websocket.send(opus_packet)
    else:
        # 直接发送opus数据包（ESP32 客户端或其他连接）
        await conn.websocket.send(opus_packet)

    # 更新流控状态
    flow_control["packet_count"] = packet_index + 1
    flow_control["sequence"] = sequence + 1


def _pcm_to_wav_optimized(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1):
    """
    优化的PCM到WAV转换函数，提高质量但不改变音量
    """
    import struct

    num_samples = len(pcm_bytes) // 2  # 16-bit = 2 bytes per sample
    byte_rate = sample_rate * channels * 2  # 2 bytes per sample
    block_align = channels * 2
    data_size = num_samples * channels * 2

    # WAV header (44 bytes) - 确保格式正确
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_size,  # File size - 8
        b'WAVE',
        b'fmt ',
        16,  # Subchunk1Size (PCM)
        1,   # AudioFormat (1 = PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,  # BitsPerSample
        b'data',
        data_size
    )

    # 避免点击声，但不改变音量
    return header + pcm_bytes


async def send_tts_message(conn: "ConnectionHandler", state, text=None):
    """发送 TTS 状态消息（广播到所有连接的客户端）"""
    if text is None and state == "sentence_start":
        return
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = textUtils.check_emoji(text)

    # TTS播放结束
    if state == "stop":
        # 播放提示音
        tts_notify = conn.config.get("enable_stop_tts_notify", False)
        if tts_notify:
            stop_tts_notify_voice = conn.config.get(
                "stop_tts_notify_voice", "config/assets/tts_notify.mp3"
            )
            audios = await audio_to_data(stop_tts_notify_voice, is_opus=True)
            await sendAudio(conn, audios)
        # 等待所有音频包发送完成
        await _wait_for_audio_completion(conn)
        # 停止音频发送循环
        if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
            conn.audio_rate_controller.stop_sending()
        # 清除服务端讲话状态
        conn.clearSpeakStatus()

    # 发送 TTS 消息到设备（ESP32）和所有连接的客户端
    tts_message = json.dumps(message)
    # 直接发送到当前连接的设备（ESP32），不使用 broadcast（broadcast 会 exclude 当前连接）
    await conn.websocket.send(tts_message)


async def send_stt_message(conn: "ConnectionHandler", text):
    """发送 STT 状态消息（广播到所有连接的客户端）"""
    logger.bind(tag=TAG).info(f"[STT] send_stt_message 被调用, text={text[:30]}")
    end_prompt_str = conn.config.get("end_prompt", {}).get("prompt")
    if end_prompt_str and end_prompt_str == text:
        # 不再发送 TTS start 消息，避免工具调用时的"咔咔"杂音
        # end_prompt 的特殊处理应该在有实际音频时才发送 TTS start
        pass

    # 解析JSON格式，提取实际的用户说话内容
    display_text = text
    try:
        # 尝试解析JSON格式
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                # 如果是包含说话人信息的JSON格式，只显示content部分
                display_text = parsed_data["content"]
                # 保存说话人信息到conn对象
                if "speaker" in parsed_data:
                    conn.current_speaker = parsed_data["speaker"]
    except (json.JSONDecodeError, TypeError):
        # 如果不是JSON格式，直接使用原始文本
        display_text = text
    stt_text = textUtils.get_string_no_punctuation_or_emoji(display_text)
    # 获取当前 ASR 模块名称（用于前端确认识别模型）
    # 优先根据实际 ASR provider 的类模块名确定（避免配置文件/深拷贝未同步导致标签错误）
    asr_model = "Unknown"
    if conn.asr:
        module_path = conn.asr.__class__.__module__
        class_name = conn.asr.__class__.__name__

    # 更精确的模型类型判断
        if "fun_local" in module_path or "FunLocalASR" in class_name:
            # 对于FunASR，检查具体模型类型
            model_attr = getattr(conn.asr, 'model', getattr(conn.asr, 'model_dir', ''))
            if 'paraformer' in str(model_attr) and 'streaming' in str(model_attr):
                asr_model = "FunASR-Paraformer-Streaming"
            elif 'paraformer' in str(model_attr):
                asr_model = "FunASR-Paraformer"
            else:
                asr_model = "FunASR"
        elif "sherpa_onnx_local" in module_path or "SherpaOnnxLocalASR" in class_name:
            model_type = getattr(conn.asr, 'model_type', 'sense_voice')
            streaming_attr = getattr(conn.asr, 'streaming', False)
            if model_type == 'sense_voice':
                asr_model = "SenseVoice" if not streaming_attr else "SenseVoice-Streaming"
            elif 'paraformer' in model_type:
                asr_model = "Paraformer" if not streaming_attr else "Paraformer-Streaming"
            else:
                asr_model = f"sherpa-onnx-{model_type}" if not streaming_attr else f"sherpa-onnx-{model_type}-streaming"
        else:
            asr_model = module_path.split('.')[-1].replace('_', ' ').title()

        # 特殊情况：对于流式模型，进一步区分
        if hasattr(conn.asr, 'interface_type'):
            if conn.asr.interface_type == InterfaceType.STREAM:
                if asr_model == "SenseVoice":
                    asr_model = "SenseVoice-Streaming"
                elif asr_model == "FunASR":
                    asr_model = "FunASR-Paraformer-Streaming"

    # 如果 ASR 未初始化，回退到配置文件读取
    if asr_model == "Unknown":
        if hasattr(conn, 'server') and conn.server and hasattr(conn.server, 'config'):
            asr_model = conn.server.config.get("selected_module", {}).get("ASR", "Unknown")
        else:
            asr_model = conn.config.get("selected_module", {}).get("ASR", "Unknown")
    # 发送最终的STT结果，标记为非流式传输
    stt_message = json.dumps({"type": "stt", "text": stt_text, "session_id": conn.session_id, "streaming": False, "asr_model": asr_model})
    logger.bind(tag=TAG).info(f"[STT] 准备广播 STT 消息: text={stt_text}, model={asr_model}")
    
    # 广播 STT 消息到所有连接的客户端
    # 对于AI连续对话等场景，发送者也需要收到STT结果
    try:
        if hasattr(conn, 'server') and conn.server and hasattr(conn.server, 'broadcast_message'):
            logger.bind(tag=TAG).info(f"[STT] server 存在，调用 broadcast_message")
            # 发送消息给其他连接
            try:
                await conn.server.broadcast_message(stt_message, exclude=conn.websocket)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"广播STT消息给其他连接失败: {e}")
            # 同时也发送给发送者
            try:
                await conn.websocket.send(stt_message)
                logger.bind(tag=TAG).info(f"[STT] 已发送 STT 消息给发送者")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"发送STT消息给发送者失败: {e}")
        else:
            logger.bind(tag=TAG).warning(f"[STT] server 不存在，直接发送")
            try:
                await conn.websocket.send(stt_message)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"发送STT消息失败: {e}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[STT] 消息发送异常（不影响对话主链）: {e}")

    # 不再发送 TTS start 消息，避免工具调用时的"咔咔"杂音
    # TTS start 应该在有实际音频时才发送（由 sentence_start 触发）
    # conn.client_is_speaking 将在收到实际 TTS 音频时设置
