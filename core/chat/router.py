"""
消息路由模块 — 从 connection.py 拆分

职责：消息分发、MQTT/WebSocket 音频处理
"""
import asyncio
import time

import numpy as np

from core.handle.receiveAudioHandle import handleAudioMessage
from core.handle.abortHandle import handleAbortMessage
from core.handle.textHandle import handleTextMessage
from core.utils.aec import parse_timestamp_audio_frame
from core.utils.system_aec import get_processor as get_system_aec_processor
from core.utils.mic_liveness import MicLiveness

TAG = __name__

# 快速打断路径（barge-in）：speaking 期间用能量检测先于 ASR 队列判断用户插话
# 原因：asr_text_priority_thread 串行处理会积压（处理速率 < 推流速率），打断判断被拖慢数秒
# 2026-09-08 修复：阈值 600 过低，办公室环境人声（远场电视/同事说话）误触发打断，
# 刚生成的 TTS 被清掉导致主人听不到语音播报。参照方案#3 防误触：
# RMS 阈值提到 1500（正常近场说话 2000-8000，远场/环境声 <1500），
# 连续帧数 3→5（300ms，与 VAD 防误触时长一致）
_QUICK_RMS_THRESHOLD = 3000  # RMS 阈值提高（原 1500）：AEC 后音乐残峰仍可超 1500，
                              # 但真人说话 RMS 2000-8000 不受影响
_QUICK_VOICE_FRAMES = 8       # 连续帧数 5→8（480ms）：进一步防音乐突发误触发


def _quick_barge_in_check(conn, pcm_data):
    """speaking 期间的能量快速插话检测，连续超阈值达到帧数阈值时触发打断"""
    audio_int16 = np.frombuffer(pcm_data, dtype=np.int16)
    rms = float(np.sqrt(np.mean(audio_int16.astype(np.float64) ** 2)))
    streak = getattr(conn, "_quick_voice_streak", 0)
    if rms >= _QUICK_RMS_THRESHOLD:
        streak += 1
    else:
        streak = 0
    conn._quick_voice_streak = streak
    if streak >= _QUICK_VOICE_FRAMES:
        conn._quick_voice_streak = 0
        return True
    return False


async def _maybe_abort_on_voice(conn, pcm_data):
    """speaking 且非 manual 模式时，能量检测判定插话即打断"""
    if not getattr(conn, "client_is_speaking", False):
        return
    if getattr(conn, "client_listen_mode", "auto") == "manual":
        return
    if getattr(conn, "just_woken_up", False):
        return
    if _quick_barge_in_check(conn, pcm_data):
        conn.logger.bind(tag=TAG).info("[快速打断] speaking 期间检测到用户插话，触发 abort")
        await handleAbortMessage(conn)


async def route_message(conn, message):
    """消息路由 — 根据消息类型分发到对应处理器"""
    if not conn.bind_completed_event.is_set():
        try:
            await asyncio.wait_for(conn.bind_completed_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            await _discard_message_with_bind_prompt(conn)
            return

    if conn.need_bind:
        await _discard_message_with_bind_prompt(conn)
        return

    if isinstance(message, str):
        await handleTextMessage(conn, message)
    elif isinstance(message, bytes):
        if conn.vad is None or conn.asr is None:
            return

        # 处理来自MQTT网关的音频包
        if conn.conn_from_mqtt_gateway and len(message) >= 16:
            handled = await _process_mqtt_audio_message(conn, message)
            if handled:
                return

        # 检测 PCM 头部标记
        if len(message) >= 4 and message[:4] == b'PCM\x00':
            conn.audio_format = "pcm"
            payload = message[4:]
            # [2026-10-13 音乐不停对话·双路径] 原始麦克风音频（raw_pcm）与 AEC 处理后
            # 音频（vad_pcm）自此分家：
            #   · 原始音频 → ASR + 声纹（实测音乐与人声同电平时 CER 0.04、声纹 0.73；
            #     AEC 处理后劣化到 0.24/0.42，不能喂 ASR）；
            #   · AEC 处理后 → VAD/判停/快速打断（原始音频在音乐下判停失效——5 句里
            #     4 句到不了 800ms 静音；AEC 后 5/5 成功 0.8~0.96s，不能喂 VAD）。
            # 接线点在 handleAudioMessage(raw_audio=...)：audio 参数进 VAD，
            # raw_audio 进 conn.asr.receive_audio。
            raw_pcm = payload
            # 服务端 AEC（Web 终端等声明 features.aec 的客户端）：
            # 上行帧格式 PCM\x00 + [4B 时间戳大端] + PCM，消除 TTS 扬声器回采
            if getattr(conn, "aec_enabled", False) and conn.aec is not None:
                ts_ms, mic_pcm = parse_timestamp_audio_frame(payload)
                if ts_ms is not None:
                    raw_pcm = mic_pcm
                    try:
                        pcm_data = conn.aec.process(ts_ms, mic_pcm)
                    except Exception as e:
                        conn.logger.bind(tag=TAG).warning(f"[AEC] 处理失败，直通: {e}")
                        pcm_data = mic_pcm
                else:
                    pcm_data = payload
            else:
                pcm_data = payload
            # 系统音频 AEC（Music 等 Mac 系统声音被设备麦克风回采的消除；
            # 2026-10-13 起参考源为 Process Tap 采集器，见 core/utils/system_aec.py）。
            # 处理结果只用于 VAD/判停/快速打断，不再喂 ASR。
            vad_pcm = pcm_data
            _sys_aec = get_system_aec_processor(conn)
            if _sys_aec is not None:
                try:
                    vad_pcm = _sys_aec.process(time.time() * 1000, pcm_data)
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"[系统AEC] 处理失败，直通: {e}")
            # [2026-10-16 主人批准·麦克风活性观测] 只统计、只打日志，不改任何判定：
            # 按连接算「音频实时率 / 连续全零时长 / AEC 后是否被吃空」，
            # 让「回调照常但每秒只收到 3 成帧」这类故障在服务端有日志可查
            # （此前服务端零日志，只能靠主人在现场喊不醒才发现）。
            MicLiveness.feed(conn, raw_pcm=raw_pcm, vad_pcm=vad_pcm)
            # 快速打断路径：不等 ASR 队列，先做能量插话检测。
            # [2026-10-13] 用 AEC 处理后的音频：音乐被参考消除后残余能量不足以触发
            # RMS 门限（旧「降音量期间跳过快速打断」的守卫已随降音量流程删除）。
            await _maybe_abort_on_voice(conn, vad_pcm)
            # 统一走 handleAudioMessage 完整链路（VAD→打断判断→唤醒忽略→超时检测→ASR）
            # 此前 PCM 旁路只做 VAD+ASR，跳过了 speaking 时的打断判断（barge-in 失效根因之一）
            await handleAudioMessage(conn, vad_pcm, raw_audio=raw_pcm)
            return

        # opus 快速打断路径：解码出 PCM 做能量检测（真实 ESP32 设备走此路径）
        if getattr(conn, "client_is_speaking", False):
            try:
                import opuslib_next
                if not hasattr(conn, "_abort_opus_decoder"):
                    conn._abort_opus_decoder = opuslib_next.Decoder(16000, 1)
                pcm_data = conn._abort_opus_decoder.decode(message, 1920)
                await _maybe_abort_on_voice(conn, pcm_data)
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"[快速打断] opus 解码失败: {e}")

        conn.asr_audio_queue.put(message)


async def _process_mqtt_audio_message(conn, message):
    """处理来自MQTT网关的音频消息"""
    try:
        timestamp = int.from_bytes(message[8:12], "big")
        audio_length = int.from_bytes(message[12:16], "big")

        if audio_length > 0 and len(message) >= 16 + audio_length:
            audio_data = message[16: 16 + audio_length]
            _process_websocket_audio(conn, audio_data, timestamp)
            return True
        elif len(message) > 16:
            audio_data = message[16:]
            conn.asr_audio_queue.put(audio_data)
            return True
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"解析WebSocket音频包失败: {e}")

    return False


def _process_websocket_audio(conn, audio_data, timestamp):
    """处理WebSocket格式的音频包"""
    if not hasattr(conn, "audio_timestamp_buffer"):
        conn.audio_timestamp_buffer = {}
        conn.last_processed_timestamp = 0
        conn.max_timestamp_buffer_size = 20

    if timestamp >= conn.last_processed_timestamp:
        conn.asr_audio_queue.put(audio_data)
        conn.last_processed_timestamp = timestamp

        processed_any = True
        while processed_any:
            processed_any = False
            for ts in sorted(conn.audio_timestamp_buffer.keys()):
                if ts > conn.last_processed_timestamp:
                    buffered_audio = conn.audio_timestamp_buffer.pop(ts)
                    conn.asr_audio_queue.put(buffered_audio)
                    conn.last_processed_timestamp = ts
                    processed_any = True
                    break
    else:
        if len(conn.audio_timestamp_buffer) < conn.max_timestamp_buffer_size:
            conn.audio_timestamp_buffer[timestamp] = audio_data
        else:
            conn.asr_audio_queue.put(audio_data)


async def _discard_message_with_bind_prompt(conn):
    """丢弃消息并检查是否需要播放绑定提示"""
    current_time = __import__('time').time()
    if current_time - conn.last_bind_prompt_time >= conn.bind_prompt_interval:
        conn.last_bind_prompt_time = current_time
        from core.handle.receiveAudioHandle import check_bind_device
        asyncio.create_task(check_bind_device(conn))
