import time
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.utils.wakeword_provider import get_wakeword_detector
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__

class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        # 检查是否需要路由到其他连接（通过 session_id/device_id）
        target_session_id = msg_json.get("session_id")
        if target_session_id and conn.server:
            # 尝试查找目标设备连接
            target_conn = await conn.server.get_device_handler(target_session_id)
            if target_conn and target_conn is not conn:
                conn.logger.bind(tag=TAG).info(
                    f"路由 listen 消息到目标连接: session_id={target_session_id}"
                )
                # 在目标连接上处理消息
                await self._handle_listen(target_conn, msg_json)
                return
            elif target_conn is None:
                conn.logger.bind(tag=TAG).warning(
                    f"未找到目标连接: session_id={target_session_id}, 在当前连接处理"
                )
            elif target_conn is conn:
                # 目标连接就是当前连接，直接处理（避免重复）
                pass

        # 在当前连接处理
        await self._handle_listen(conn, msg_json)

    async def _handle_listen(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        """实际处理 listen 消息的逻辑"""
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            # 设备从播放模式切回录音模式,清除所有音频状态和缓冲区
            conn.reset_audio_states()
            # 用户主动开始对话（网页终端"开始对话"按钮/设备唤醒），激活会话
            device_id = getattr(conn, 'hello_device_id', '') or ''
            if device_id.startswith('web-terminal'):
                # Web 终端：用户点击"开始对话"按钮，直接激活会话（不需要唤醒词）
                if not getattr(conn, '_session_active', False):
                    conn._session_active = True
                    conn.logger.bind(tag=TAG).info("[Web终端] listen start，直接激活会话")
                # 2026-10-20：无论会话是否已激活，按钮"开始对话"都要（重新）启动
                # 120 秒唤醒会话计时。否则说完一句、TTS 播完后，下一个静音帧就会被
                # _push_standby_if_session_expired 判定"会话已过期"而打回鱼缸。
                # （原先只有设备 detect 路径会调 activate_session，网页按钮从未激活计时）
                try:
                    _wp_start = get_wakeword_detector()
                    if _wp_start:
                        _wp_start.activate_session()
                except Exception as _e:
                    conn.logger.bind(tag=TAG).warning(f"[Web终端] 激活唤醒会话计时失败: {_e}")
            elif getattr(conn, 'idle_guard', False) and not getattr(conn, '_session_active', False):
                conn._session_active = True
                conn.logger.bind(tag=TAG).info("[idle_guard] 收到 listen start，激活会话")
        elif msg_json["state"] == "stop":
            stop_recv_ts = time.time() * 1000
            conn.logger.bind(tag=TAG).info(f"[耗时] 收到 listen stop 消息, ts={stop_recv_ts}")
            # 等待 ASR 初始化完成（解决竞态条件）
            if conn.asr is None:
                # ASR 后台初始化可能尚未完成，等待最多 5 秒
                wait_timeout = 5.0
                wait_interval = 0.1
                waited = 0.0
                while conn.asr is None and waited < wait_timeout:
                    await asyncio.sleep(wait_interval)
                    waited += wait_interval
                if conn.asr is None:
                    conn.logger.bind(tag=TAG).error(
                        f"收到 listen stop 消息，等待 {waited:.1f}s 后 ASR 仍未初始化，忽略"
                    )
                    return
                conn.logger.bind(tag=TAG).info(
                    f"收到 listen stop 消息，等待 {waited:.1f}s 后 ASR 初始化完成"
                )
            conn.logger.bind(tag=TAG).info(f"收到 listen stop 消息, interface_type={conn.asr.interface_type}")
            conn.client_voice_stop = True

            # 防止短时间内重复触发（500ms 内不处理第二次）
            current_time = time.time() * 1000
            if hasattr(conn, '_last_stop_time') and (current_time - conn._last_stop_time) < 500:
                conn.logger.bind(tag=TAG).info(f"短时间内重复的 listen stop 消息，忽略处理")
                return
            conn._last_stop_time = current_time

            # 防止双重触发：标记正在处理 finish
            conn._finish_streaming_in_progress = True
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，结束流式识别
                # 使用 hello_device_id 作为 session key（与 process_streaming_chunk 保持一致）
                device_key = getattr(conn, 'hello_device_id', None) or conn.device_id or conn.session_id
                conn.logger.bind(tag=TAG).info(f"流式模式结束，调用 finish_streaming, device_key={device_key}")
                try:
                    if hasattr(conn.asr, 'finish_streaming'):
                        await conn.asr.finish_streaming(device_key, conn)
                    elif hasattr(conn.asr, '_send_stop_request'):
                        await conn.asr._send_stop_request()
                    else:
                        conn.logger.bind(tag=TAG).warning("ASR 不支持 finish_streaming 或 _send_stop_request")
                finally:
                    conn._finish_streaming_in_progress = False
                    # 流式分支同样要重置，否则 VAD 停止还会再触发一次 finish_streaming（与非流式分支对齐）
                    conn.client_voice_stop = False
            else:
                # 非流式模式：直接触发ASR识别
                try:
                    if len(conn.asr_audio) > 0:
                        asr_audio_task = conn.asr_audio.copy()
                        conn.reset_audio_states()

                        if len(asr_audio_task) > 0:
                            await conn.asr.handle_voice_stop(conn, asr_audio_task)
                finally:
                    conn._finish_streaming_in_progress = False
                    conn.client_voice_stop = False  # 重置标志，防止 receive_audio 再次触发
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # 保留原始文本
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # ========================================================================
                # 禁止修改：设备端唤醒词检测与会话激活
                # 功能：检测设备发送的 detect 消息中的唤醒词，激活会话
                # 负责人：bot-eva
                # 修改需获得 bot-eva 同意
                # 实现要点：
                # 1. 检测到唤醒词后，设置 conn._session_active = True
                # 2. 调用 startToChat 触发 LLM 生成问候语
                # 3. 不要在这里添加超时检查，会话状态由 conn._session_active 管理
                # ========================================================================
                # 识别是否是唤醒词
                wakeword_detector = get_wakeword_detector()
                is_wakeup, matched_word, confidence = wakeword_detector.detect(filtered_text)
                # 是否开启唤醒词回复
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup and not enable_greeting:
                    # 如果是唤醒词，且关闭了唤醒词回复，不用回答
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup:
                    # 对齐上游 xinnan-tech：detect 命中唤醒词后立即触发问候，
                    # 不再等 ASR stop（等 stop 会让"注视激活后不说话"永远等不到问候，
                    # 前端 5 秒超时砍会话，之后说话被 idle_guard 拦截要求重新唤醒）
                    if wakeword_detector:
                        wakeword_detector.activate_session()
                    conn._session_active = True  # 标记会话激活
                    conn.just_woken_up = True    # 上游行为：立即置位（供 ASR 空文本兜底短回复）
                    conn.last_wakeup_time = time.time() * 1000
                    conn.logger.bind(tag=TAG).info(f"检测到唤醒词，立即触发问候: {original_text}")
                    # 通知前端进入 listening 状态
                    try:
                        await conn.websocket.send(json.dumps({
                            "type": "listen",
                            "state": "wakeup",
                            "text": original_text
                        }))
                    except Exception:
                        pass
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, original_text, [])
                    from core.handle.helloHandle import _is_wakeup_only_text
                    if _is_wakeup_only_text(filtered_text):
                        # 纯唤醒词：只激活会话，不播放短回复，等用户后续指令
                        conn.logger.bind(tag=TAG).info("纯唤醒词，跳过打招呼，等待后续指令")
                    else:
                        # 唤醒词+实质指令：直接对全文答复（上游行为）
                        await startToChat(conn, original_text)
                else:
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, original_text, [])
                    # 待机模式（idle_guard 且会话未激活）下，非唤醒词文本静默忽略
                    # Web 终端例外：已通过 listen start 激活会话，不需要检查 idle_guard
                    device_id = getattr(conn, 'hello_device_id', '') or ''
                    is_web_terminal = device_id.startswith('web-terminal')
                    if not is_web_terminal and getattr(conn, 'idle_guard', False) and not getattr(conn, '_session_active', False):
                        conn.logger.bind(tag=TAG).info(
                            f"[idle_guard] 会话未激活，忽略 detect 文本: {filtered_text[:20]}"
                        )
                        # 发送TTS提示用户重新唤醒，而不是完全静默
                        try:
                            try:
                                # [2026-10-06] 原来 except 分支写死 "伊娃"
                                from core.utils.assistant_identity import safe_name

                                _ai_name = safe_name()
                            except Exception:
                                _ai_name = "家庭助手"
                            await send_tts_message(conn, "sentence_start", f"请先说'你好{_ai_name}'唤醒我")
                            await send_tts_message(conn, "stop", None)
                        except Exception as e:
                            conn.logger.bind(tag=TAG).warning(f"[idle_guard] 发送唤醒提示失败: {e}")
                        return
                    # 否则需要LLM对文字内容进行答复
                    await startToChat(conn, original_text)