import os
import io
import wave
import uuid
import json
import time
import queue
import shutil
import asyncio
import tempfile
import traceback
import threading
import numpy as np
import opuslib_next  # macOS 兼容性跳过

from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.helloHandle import play_short_wakeup_reply
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message
from core.utils.util import remove_punctuation_and_length
from core.utils.wakeword_provider import get_wakeword_detector
from core.handle.receiveAudioHandle import handleAudioMessage
from typing import Optional, Tuple, List, NamedTuple, TYPE_CHECKING


if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProviderBase(ABC):
    def __init__(self):
        pass

    def _get_asr_model_label(self, conn: "ConnectionHandler") -> str:
        """根据实际 ASR provider 的类模块名确定模型标签"""
        asr_model = "Unknown"
        if conn.asr:
            module_path = conn.asr.__class__.__module__
            if "fun_local" in module_path:
                asr_model = "FunASR"
            elif "sherpa_onnx_local" in module_path:
                model_type = getattr(conn.asr, 'model_type', 'sense_voice')
                asr_model = "SenseVoice" if model_type == 'sense_voice' else "Sherpa-ONNX"
            else:
                asr_model = module_path.split('.')[-1].replace('_', ' ').title()
        if asr_model == "Unknown":
            asr_model = conn.config.get("selected_module", {}).get("ASR", "Unknown")
        return asr_model

    # 打开音频通道
    async def open_audio_channels(self, conn: "ConnectionHandler"):
        conn.asr_priority_thread = threading.Thread(
            target=self.asr_text_priority_thread, args=(conn,), daemon=True
        )
        conn.asr_priority_thread.start()

    # 有序处理ASR音频
    def asr_text_priority_thread(self, conn: "ConnectionHandler"):
        while not conn.stop_event.is_set():
            try:
                message = conn.asr_audio_queue.get(timeout=1)
                future = asyncio.run_coroutine_threadsafe(
                    handleAudioMessage(conn, message),
                    conn.loop,
                )
                future.result()
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理ASR文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    # 接收音频（恢复小智原始逻辑）
    async def receive_audio(self, conn: "ConnectionHandler", audio, audio_have_voice):
        # 记录接收时间戳用于延迟分析
        receive_timestamp = time.time()
        logger.bind(tag=TAG).debug(f"receive_audio: 收到音频块, 大小={len(audio)} bytes, interface_type={conn.asr.interface_type}, voice={audio_have_voice}")

        # 记录最后收到音频帧的时间（用于无帧超时检测）
        conn._last_audio_frame_time = receive_timestamp

        # 手动模式：只缓存音频，不做识别
        if conn.client_listen_mode == "manual":
            conn.asr_audio.append(audio)
            return

        # 自动模式：缓存音频
        conn.asr_audio.append(audio)

        # 流式 ASR：每个音频块到达时调用 process_streaming_chunk（不依赖 VAD）
        # 中间识别不依赖 VAD，最终识别仍由 client_voice_stop 触发
        if conn.asr.interface_type == InterfaceType.STREAM:
            device_key = getattr(conn, 'hello_device_id', None) or conn.device_id or conn.session_id
            logger.bind(tag=TAG).debug(f"[流式ASR] process_streaming_chunk 调用, device_key={device_key}, audio_size={len(audio)}, format={conn.audio_format}, have_voice={audio_have_voice}")
            try:
                # 解码 Opus 为 PCM（如果需要）
                if conn.audio_format == "opus":
                    # 使用 VAD 的解码器
                    if not hasattr(conn.vad, '_vad_opus_decoder') or conn.vad._vad_opus_decoder is None:
                        conn.vad._vad_opus_decoder = opuslib_next.Decoder(16000, 1)
                    pcm_chunk = conn.vad._vad_opus_decoder.decode(audio, 1920)
                else:
                    pcm_chunk = audio
                
                # 调用流式 ASR 的 process_streaming_chunk（不管 have_voice）
                if hasattr(conn.asr, 'process_streaming_chunk'):
                    result = await conn.asr.process_streaming_chunk(pcm_chunk, device_key)
                    if result:
                        logger.bind(tag=TAG).info(f"[流式ASR] 中间结果: {result}")
                        # 发送中间结果到客户端（流式显示）
                        text = result[0] if isinstance(result, (list, tuple)) else result
                        if isinstance(text, dict):
                            text = text.get("text", text.get("content", ""))
                        if text and len(text.strip()) > 0:
                            conn._asr_partial_text = str(text).strip()
                            session_id = getattr(conn, 'session_id', 'unknown')
                            stt_message = json.dumps({
                                "type": "stt",
                                "text": text,
                                "session_id": session_id,
                                "streaming": True
                            })
                            # 广播流式中间结果给所有客户端（包括控制台）
                            if hasattr(conn, 'server') and conn.server and hasattr(conn.server, 'broadcast_message'):
                                try:
                                    logger.bind(tag=TAG).info(f"[流式ASR] 广播stt消息: {stt_message[:200]}")
                                    await conn.server.broadcast_message(stt_message)
                                except Exception as broadcast_err:
                                    logger.bind(tag=TAG).warning(f"广播流式STT失败: {broadcast_err}")
                            else:
                                logger.bind(tag=TAG).warning(f"[流式ASR] 无法广播: server={getattr(conn, 'server', None)}")
                                try:
                                    await conn.websocket.send(stt_message)
                                except Exception as send_err:
                                    logger.bind(tag=TAG).warning(f"发送流式STT失败: {send_err}")
            except Exception as e:
                logger.bind(tag=TAG).error(f"[流式ASR] process_streaming_chunk 失败: {e}", exc_info=True)

        # 如果没有语音且还没检测到语音，只保留最近10帧（与小智一致）
        if not audio_have_voice and not conn.client_have_voice:
            conn.asr_audio = conn.asr_audio[-10:]
            return

        # 自动模式下通过VAD检测到语音停止时触发识别（与小智一致）
        if conn.client_voice_stop:
            if getattr(conn, "_vad_hold_expired", False):
                # hold 期满用户没再开口：已经问过判停模型一次，不再重复调用，直接提交
                conn._vad_hold_expired = False
                await self._commit_stopped_audio(conn)
                return
            semantic_status = await self._semantic_endpoint_status(conn)
            if semantic_status == "drop":
                conn.reset_audio_states()
                conn._asr_partial_text = ""
                self._reset_vad_utterance(conn)
                return
            if semantic_status == "hold":
                self._schedule_semantic_endpoint_hold(conn)
                return
            await self._commit_stopped_audio(conn)

    @staticmethod
    def _reset_vad_utterance(conn: "ConnectionHandler") -> None:
        """一段话结束（提交/丢弃）后清掉 VAD 判停遗留状态：候选、确认窗口、hold、预判（代际+1）。

        reset_audio_states 不清这些字段，不清会让下一句的首帧被当成确认窗口毛刺免疫掉。
        """
        conn._vad_candidate_end = False
        conn._vad_confirm_voice_frames = 0
        conn._vad_hold_until = 0.0
        conn._vad_hold_expired = False
        conn._vad_probe_gen = getattr(conn, "_vad_probe_gen", 0) + 1
        conn._vad_probe_pending = False
        conn._vad_probe_verdict = None
        conn._vad_probe_text = None
        conn._vad_probe_audio_sig = None
        conn._vad_probe_voice_seq = None
        conn._vad_probe_decision = None

    @staticmethod
    def _probe_still_valid(conn: "ConnectionHandler", asr_audio_task=None) -> bool:
        """预判结论（complete + 文本）能否直接复用：自预判快照以来没有新的人声帧。

        原先只用「音频块数+字节数」指纹校验，但预判结论回来之前通常又进了几帧静音，
        块数对不上 → 实测 3177 次预判 complete 提交里文本只复用了 116 次（4%），
        其余每次都重复调用判停模型并重新识别。静音帧不会改变识别文本，
        所以改用「VAD 累计人声帧数没变」作为校验（含确认窗口里被免疫的毛刺人声帧）。
        """
        if getattr(conn, "_vad_probe_verdict", None) != "complete":
            return False
        if not str(getattr(conn, "_vad_probe_text", "") or "").strip():
            return False
        seq0 = getattr(conn, "_vad_probe_voice_seq", None)
        if seq0 is not None and seq0 == getattr(conn, "_vad_voice_seq", None):
            return True
        sig = getattr(conn, "_vad_probe_audio_sig", None)
        if sig and asr_audio_task is not None:
            return sig[0] == len(asr_audio_task) and sig[1] == sum(len(b) for b in asr_audio_task)
        return False

    async def _commit_stopped_audio(self, conn: "ConnectionHandler"):
        """按当前 ASR 模式提交一段已停止的音频。"""
        if conn.asr.interface_type != InterfaceType.STREAM:
            # 非流式 ASR：调用 handle_voice_stop
            asr_audio_task = conn.asr_audio.copy()
            conn.reset_audio_states()
            # 2026-09-23 近场爆发切分尾帧保留（silero.py 置 _vad_keep_tail_blocks）：
            # 切分点以来尾帧进新段，防止主人话头被随旧段一起提交。
            # 仅 extend 回 asr_audio 缓冲（reset_audio_states 不动它），不动 connection.py。
            _keep = getattr(conn, "_vad_keep_tail_blocks", 0)
            if _keep > 0:
                # 60ms 块(960样本) ↔ 32ms VAD帧(512样本) ≈ 1.875 块/帧
                conn.asr_audio.extend(asr_audio_task[-int(_keep * 1.875):])
                conn._vad_keep_tail_blocks = 0
                conn._vad_seg_rms_ema = None
                conn._vad_burst_frames = 0
                conn._vad_seg_frames = 0  # 新段重新计段首保护
            if len(asr_audio_task) > 15:
                await self.handle_voice_stop(conn, asr_audio_task)
            conn._semantic_endpoint_hold_used = False
            conn._semantic_endpoint_hold_count = 0
            conn._asr_partial_text = ""
            # 音频已提交消费，清掉 A1 预判缓存（含音频指纹），避免下一段误复用
            self._reset_vad_utterance(conn)
            return

        # 流式 ASR：调用 finish_streaming 完成最终识别
        logger.bind(tag=TAG).info("[流式ASR] VAD检测到语音停止，触发 finish_streaming")
        await self._trigger_finish_streaming(conn)
        conn._semantic_endpoint_hold_used = False
        conn._semantic_endpoint_hold_count = 0
        conn._asr_partial_text = ""
        self._reset_vad_utterance(conn)

    async def _semantic_endpoint_status(self, conn: "ConnectionHandler") -> str:
        """提交前判断是否说完、是否对助手说话。返回 commit/hold/drop。

        2026-10-04 主人裁决：唤醒路径零 JEV。
        唤醒词的判定是纯字符串/拼音匹配（core/utils/wakeword_provider.detect），
        「你好伊娃」本身就是完整的触发词，说完即结束，不需要也不应该走 JEV 判停。
        JEV 的职责只有一个：判断**已经进入对话后**这句话是否说完。
        因此会话尚未激活（正在尝试唤醒）时，直接 commit，不加载也不调用 JEV。
        """
        # —— 唤醒路径：JEV 一律不介入 ——
        # 放在 try 之外，确保任何异常/回退路径都不会绕回来调 JEV。
        if bool(getattr(conn, "idle_guard", False)) and not bool(
            getattr(conn, "_session_active", False)
        ):
            if not getattr(conn, "_wake_path_bypass_logged", False):
                conn._wake_path_bypass_logged = True
                logger.bind(tag=TAG).info(
                    "[判停] 唤醒路径：会话未激活，跳过 JEV 判停，直接提交交给唤醒词匹配"
                )
            return "commit"

        try:
            from core.providers.decision import get_decision_provider
            provider = get_decision_provider(getattr(conn, "config", None))
            if not provider.enabled or not provider.endpoint_enabled:
                return "commit"

            # 云端 ASR 联动停用（2026-09-28 主人裁决）：豆包 seedasr 服务端自带
            # VAD/断句/标点，本地判断模型（Qwen3.5-4B）不再需要；
            # 保留原 provider.enabled 配置作为手动覆盖手段。
            from core.utils.model_stack import endpoint_judge_active
            if not endpoint_judge_active(conn.config):
                if not getattr(conn, "_endpoint_judge_bypass_logged", False):
                    conn._endpoint_judge_bypass_logged = True
                    logger.bind(tag=TAG).info(
                        "[判停] 云端ASR已联动停用本地断句判断，直接提交"
                    )
                return "commit"

            # B1（2026-10-11）：并行预判已经问过判停模型且结论仍有效 → 直接复用，
            # 不再预览识别+再问一次（实测 3097 次重复调用，每次约 0.6~1.5s）。
            _pd = getattr(conn, "_vad_probe_decision", None)
            if _pd is not None and self._probe_still_valid(conn, getattr(conn, "asr_audio", None)):
                _sa = (bool(getattr(conn, "_session_active", False))
                       if bool(getattr(conn, "idle_guard", False)) else True)
                _ptext = str(getattr(conn, "_vad_probe_text", "") or "")
                conn._last_jev_utterance_decision = {
                    "provider": provider.name,
                    "text": _ptext[:100],
                    "addressed": _pd.addressed,
                    "complete": _pd.complete,
                    "addressed_probability": _pd.addressed_probability,
                    "complete_probability": _pd.complete_probability,
                }
                if _sa and _pd.addressed is False:
                    logger.bind(tag=TAG).info(f"[判停] 复用预判结论: drop（非对助手说话） text={_ptext[:40]!r}")
                    return "drop"
                logger.bind(tag=TAG).info(f"[判停] 复用预判结论: commit，省一次判停调用 text={_ptext[:40]!r}")
                return "commit"

            if conn.asr.interface_type == InterfaceType.STREAM:
                text = getattr(conn, "_asr_partial_text", "")
            elif provider.preview_non_stream and len(conn.asr_audio) >= 3:
                preview, _ = await self.speech_to_text_wrapper(
                    conn.asr_audio.copy(),
                    conn.session_id,
                    conn.audio_format,
                )
                if isinstance(preview, dict):
                    text = str(preview.get("text", preview.get("content", "")) or "")
                else:
                    text = str(preview or "")
                # 不缓存这份文本：commit 时一律用当次完整音频真识别，
                # 避免 hold 后的旧快照被当成最终结果（2026-10-04 主人裁决）。
            else:
                text = ""

            if not text.strip():
                return "commit"

            idle_guard = bool(getattr(conn, "idle_guard", False))
            session_active = (
                bool(getattr(conn, "_session_active", False)) if idle_guard else True
            )
            started = time.perf_counter()
            decision = await provider.judge_utterance(text, session_active)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            if decision is None:
                return "commit"

            diagnostics = decision.diagnostics or {}
            complete_diag = diagnostics.get("complete", {}) if isinstance(diagnostics, dict) else {}
            in_set_mass = complete_diag.get("in_set_mass") if isinstance(complete_diag, dict) else None

            def _log_semantic_endpoint(action: str):
                logger.bind(tag=TAG).info(
                    json.dumps(
                        {
                            "event": "semantic_endpoint",
                            "action": action,
                            "provider": provider.name,
                            "complete": decision.complete,
                            "complete_probability": decision.complete_probability,
                            "addressed": decision.addressed,
                            "addressed_probability": decision.addressed_probability,
                            "threshold": provider.endpoint_threshold,
                            "hold_used": bool(getattr(conn, "_semantic_endpoint_hold_used", False)),
                            "latency_ms": elapsed_ms,
                            "in_set_mass": in_set_mass,
                            "text_length": len(text),
                        },
                        ensure_ascii=False,
                    )
                )

            conn._last_jev_utterance_decision = {
                "provider": provider.name,
                "text": text[:100],
                "addressed": decision.addressed,
                "complete": decision.complete,
                "addressed_probability": decision.addressed_probability,
                "complete_probability": decision.complete_probability,
            }
            if session_active and decision.addressed is False:
                _log_semantic_endpoint("drop")
                return "drop"
            # 可重复 hold（2026-09-28 主人裁决）：同一句话内每次"没说完"都再延迟
            # incomplete_hold_ms，最多 incomplete_hold_max_count 次；超限后照常提交，
            # 避免模型持续误判导致用户永远说不完。计数在提交成功后复位。
            _hold_count = int(getattr(conn, "_semantic_endpoint_hold_count", 0))
            _max_holds = int(getattr(provider, "incomplete_hold_max_count", 3))
            if decision.complete is False and _hold_count < _max_holds:
                _log_semantic_endpoint("hold")
                return "hold"
            _log_semantic_endpoint("commit")
            return "commit"
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"[判停] 执行失败，回退原判停: {exc}")
            return "commit"

    def _schedule_semantic_endpoint_hold(self, conn: "ConnectionHandler"):
        """话未说完：登记 hold 截止时刻，期间 VAD 不再触发判停。

        由 VAD 帧循环驱动（silero._endpoint_decision_for_silence）：截止前返回 False；
        截止时用户仍没再开口则直接提交，用户再开口则人声开始时清掉 hold 重新计。
        不再用定时器——旧定时器入口判断了粘性的 client_have_voice，永远提前返回。
        """
        from core.providers.decision import get_decision_provider
        provider = get_decision_provider(getattr(conn, "config", None))
        delay_s = provider.incomplete_hold_ms / 1000
        conn._semantic_endpoint_hold_used = True
        conn._semantic_endpoint_hold_count = int(
            getattr(conn, "_semantic_endpoint_hold_count", 0)
        ) + 1
        conn._vad_hold_until = time.time() + delay_s
        conn._vad_hold_expired = False
        conn.client_voice_stop = False

    async def _trigger_finish_streaming(self, conn: "ConnectionHandler"):
        """统一触发 finish_streaming，含错误处理（用于超时兜底）"""
        conn._finish_streaming_in_progress = True
        device_key = getattr(conn, 'hello_device_id', None) or conn.device_id or conn.session_id
        try:
            if hasattr(conn.asr, 'finish_streaming'):
                await conn.asr.finish_streaming(device_key, conn)
            elif conn.asr_audio:
                await self.handle_voice_stop(conn, conn.asr_audio.copy())
        except Exception as e:
            logger.bind(tag=TAG).error(f"[ASR兜底] finish_streaming 失败: {e}", exc_info=True)
        finally:
            conn._finish_streaming_in_progress = False
        conn.client_voice_stop = False
        conn.client_have_voice = False
        conn.asr_audio = []
        # 清理超时计时器
        for attr in ('_last_voice_time', '_asr_voice_start_time', '_last_audio_frame_time'):
            if hasattr(conn, attr):
                delattr(conn, attr)

    @staticmethod
    def _audio_rms(conn, asr_audio_task) -> float | None:
        """算这段音频的 RMS（音量）。拿不到就返回 None（调用方按「不判回声」处理）。"""
        try:
            import numpy as np

            if not asr_audio_task:
                return None
            if getattr(conn, "audio_format", None) == "pcm":
                pcm = b"".join(asr_audio_task)
            else:
                # opus 客户端：解出来再算；解码不可用则放弃判定（不误杀）
                dec = getattr(conn, "asr", None)
                if dec is None or not hasattr(dec, "decode_opus"):
                    return None
                pcm = b"".join(dec.decode_opus(asr_audio_task))
            if not pcm:
                return None
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size == 0:
                return None
            return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
        except Exception:
            return None

    def _looks_like_self_echo(self, conn, asr_audio_task, text: str) -> bool:
        """播报期间「声纹低 + 能量低」→ 判定为自激回声（助手听到自己说话）。

        2026-10-07 主人实测数据（同一段真实会话）：
            回声：声纹相似度 0.24~0.34，RMS 49~617
            真人：声纹相似度 0.36~0.66，RMS 800~3127
        文本相似度抓不到回声（ASR 会把自己的话听成完全不同的词），
        但声纹 + 音量两个信号合起来可以干净分开。

        阈值可配：echo_voiceprint_threshold（默认 0.35）、echo_rms_threshold（默认 1000）。
        拿不到声纹或音量时一律返回 False（宁可不判，也不误杀主人的真实插话）。
        """
        try:
            vp = getattr(conn, "voiceprint_provider", None)
            score = getattr(vp, "last_score", None)
            rms = self._audio_rms(conn, asr_audio_task)
            if score is None or rms is None:
                return False
            score_th = float(conn.config.get("echo_voiceprint_threshold", 0.35) or 0.35)
            rms_th = float(conn.config.get("echo_rms_threshold", 1000) or 1000)
            if float(score) < score_th and rms < rms_th:
                logger.bind(tag=TAG).info(
                    f"[回声过滤] 判为自激回声（声纹 {score:.2f}<{score_th:.2f}，"
                    f"能量 {rms:.0f}<{rms_th:.0f}），丢弃不应答: {str(text)[:30]}"
                )
                return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[回声过滤] 判定异常，按非回声处理: {e!r}")
        return False

    async def _simulated_streaming_recognize(self, conn: "ConnectionHandler", asr_audio_task: List[bytes]):
        """模拟流式识别：识别部分累积音频，返回中间结果"""
        try:
            if conn.audio_format == "pcm":
                pcm_data = asr_audio_task
            else:
                pcm_data = self.decode_opus(asr_audio_task)

            combined_pcm_data = b"".join(pcm_data)
            artifacts = ASRProviderBase.AudioArtifacts(
                pcm_frames=pcm_data,
                pcm_bytes=combined_pcm_data,
                file_path=None,
                temp_path=None,
            )

            result = await self.speech_to_text(
                asr_audio_task, conn.session_id, conn.audio_format, artifacts
            )

            if result and len(result) > 0:
                raw_text = result[0] if isinstance(result, (list, tuple)) else result
                if isinstance(raw_text, dict):
                    text = raw_text.get("text", raw_text.get("content", ""))
                else:
                    text = str(raw_text)

                if text:
                    logger.bind(tag=TAG).info(f"[模拟流式] 中间结果: {text}")
                    session_id = getattr(conn, 'session_id', 'unknown')
                    stt_message = json.dumps({
                        "type": "stt",
                        "text": text,
                        "session_id": session_id,
                        "streaming": True
                    })
                    if hasattr(conn, 'websocket') and conn.websocket:
                        await conn.websocket.send(stt_message)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"模拟流式识别失败: {e}")

    @staticmethod
    def _wakeword_fragments(wp) -> set:
        """从当前唤醒词生成「疑似音」片段集（仅用于唤醒失败提示，策略B）。

        把每个唤醒词按空格分段后取「连续 2 段」组合的首尾两个：
        'ni hao yi wa' → nihao / yiwa（'haoyi' 这类跨意群组合不取，
        实测 'haoyi' 会被「小鹦鹉 xiaoyingwu」的子串 aoyi 误命中）。
        只回答「听起来像不像在喊唤醒词」，不参与 detect 命中判定。
        """
        frags = set()
        for word in getattr(wp, 'wakewords', []) or []:
            segs = [s for s in word.lower().split() if s]
            pairs = [''.join(segs[i:i + 2]) for i in range(len(segs) - 1)]
            keep = set()
            if pairs:
                keep.add(pairs[0])
                keep.add(pairs[-1])
            for frag in keep:
                if len(frag) >= 4:
                    frags.add(frag)
        return frags

    @staticmethod
    def _edit_distance(s1: str, s2: str) -> int:
        """编辑距离（与唤醒词检测同口径，仅用于疑似音判断）"""
        if len(s1) < len(s2):
            s1, s2 = s2, s1
        if not s2:
            return len(s1)
        prev = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            cur = [i + 1]
            for j, c2 in enumerate(s2):
                cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (c1 != c2)))
            prev = cur
        return prev[-1]

    def _is_likely_wakeword_attempt(self, text: str) -> bool:
        """宽松判断文本是否为「疑似唤醒失败」的音（2026-10-01 主人批准，策略B）。

        在清洗后的拼音串上滑动窗口，与唤醒词片段比较编辑距离（阈值同 detect）。
        宁可多提示（3 秒轻提示）也不漏，仅用于 Web 终端待机提示，不改变门控行为。
        """
        try:
            import re as _re
            from pypinyin import pinyin, Style
            text_py = ''.join([p[0] for p in pinyin(text, style=Style.NORMAL)]).lower()
            text_py = _re.sub(r'[^a-z0-9]', '', text_py)
        except Exception:
            return False
        if not text_py:
            return False
        wp = get_wakeword_detector()
        if not wp or not wp.enabled:
            return False
        for frag in self._wakeword_fragments(wp):
            max_dist = max(1, int(len(frag) * 0.1))
            for win_len in (len(frag) - 1, len(frag), len(frag) + 1):
                if win_len <= 0:
                    continue
                for i in range(0, len(text_py) - win_len + 1):
                    sub = text_py[i:i + win_len]
                    if abs(len(sub) - len(frag)) > max_dist:
                        continue
                    if self._edit_distance(sub, frag) <= max_dist:
                        return True
        return False

    async def _idle_guard_allow(self, conn: "ConnectionHandler", text: str) -> bool:
        """免唤醒守卫（静默模式，2026-09-10 恢复）。
        会话未激活时只放行唤醒词，但**不发 TTS 骚扰提示**，静默忽略。
        激活方式：①注视激活（前端发送 listen detect）②唤醒词"你好伊娃"。
        """
        # 紧急关键词优先于免唤醒守卫（2026-09-28 主人裁决）：
        # 待机/未唤醒时说"救命"等必须能直接触发报警确认链路，
        # 不能被门控静默丢弃。命中即放行，后续 startToChat 里的
        # emergency_listener 检测会消费这条文本。
        try:
            from core.api.emergency_api import match_emergency_keywords_sync
            if match_emergency_keywords_sync(text):
                conn.logger.bind(tag=TAG).warning(
                    f"[idle_guard] 命中紧急关键词，绕过免唤醒门控: {text[:30]}"
                )
                return True
        except Exception:
            pass
        # 报警确认窗口期同样放行：用户回复"是/否"不含紧急词，
        # 但会话正在等这句答复，不能被门控吃掉。
        try:
            from core.utils.emergency_listener import emergency_listener
            if emergency_listener.has_pending_session():
                return True
        except Exception:
            pass
        if not getattr(conn, 'idle_guard', False):
            return True
        if getattr(conn, '_session_active', False):
            # 2026-09-29：打断宽限机制已整体移除（主人裁决：打断不碰会话，
            # 会话按"每轮对话自动续期、说再见才结束"的约定管理），
            # 此处不再有复位任务需要取消。
            return True
        # 检查是否包含唤醒词
        from core.utils.wakeword_provider import get_wakeword_detector
        wp = get_wakeword_detector()
        if wp and wp.enabled:
            is_wake, _matched, _conf = wp.detect(text)
            if is_wake:
                conn._session_active = True
                # 唤醒成功：清掉「已通知待机」标记，下次会话才会正常过期并复位
                if getattr(conn, '_standby_notified', False):
                    conn._standby_notified = False
                    conn._standby_notified_ts = 0.0
                # [2026-10-20 主人批准] 同步启动 120 秒会话计时。
                # 原先只有设备端 listen detect 会调 activate_session()，网页端
                # 语音唤醒从不调 → is_session_active() 恒为 False，待机推送要么
                # 把刚唤醒的页面立刻打回鱼缸，要么（标记没清时）彻底不触发。
                try:
                    wp.activate_session()
                except Exception:
                    pass
                conn.logger.bind(tag=TAG).info(f"[idle_guard] 唤醒词命中，激活会话: {text}")
                # 2026-10-05 修复：同步下发唤醒通知。原先这里只改服务端
                # _session_active，前端收不到任何消息，按钮不会变对话态。
                # 与 checkWakeupWords 保持一致：有唤醒词就通知，且只发一条。
                try:
                    await conn.websocket.send(
                        json.dumps({"type": "listen", "state": "wakeup"})
                    )
                    conn.logger.bind(tag=TAG).info("[idle_guard] 已通知前端进入对话态")
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"下发唤醒通知失败: {e}")
                return True
        # 策略B（2026-10-01 主人批准）：仅 Web 终端在「疑似喊唤醒词但没喊醒」时
        # 下发 wake_miss 让前端高亮提示；其他待机语音继续静默忽略。
        device_id = getattr(conn, 'hello_device_id', '') or ''
        if self._is_likely_wakeword_attempt(text):
            # [2026-10-18 P2，主人批准] 运行观察：疑似唤醒失败一律计数（不再只限网页端），
            # 报告里用「唤醒失败 vs 唤醒命中」判断唤醒词是不是太难喊。
            try:
                from core.utils import observation as _obs
                _obs.bump("wake_miss", sample=text)
            except Exception:
                pass
            if device_id.startswith('web-terminal'):
                try:
                    await conn.websocket.send(json.dumps({"type": "wake_miss"}))
                    conn.logger.bind(tag=TAG).info(f"[idle_guard] 疑似唤醒失败，已提示前端: {text[:20]}...")
                except Exception as e:
                    conn.logger.bind(tag=TAG).debug(f"[idle_guard] wake_miss 下发失败: {e}")

        # [2026-10-12 主人实测 bug] 长时间不说话后，网页端「对话中」按钮不复位。
        #
        # 现象：主人长时间不交互，网页端按钮还停在「对话中」，刷新网页也没用；
        #       但后台其实已经回待机了（得用唤醒词才能唤醒它）。
        #
        # 根因：前端状态机只有 4 个入口，回到待机（IDLE）唯一靠服务端下发
        #       `{"type":"state","state":"sleep"}`。而服务端只在「显式说退出/
        #       点结束」时发这条（intentHandler.check_direct_exit 分支）。
        #       唤醒会话超时（wakeword_provider.session_timeout，默认 120s）之后
        #       没有任何地方补发 sleep → 前端既没收到 sleep 也没有别的消息，
        #       按钮就一直亮着。刷新网页也不解决：那是**前端状态**，重连后由
        #       服务端的会话态重新驱动，而服务端并不认为自己该发 sleep。
        #
        # 修法：每次有语音被门控静默忽略时，顺带核对一次唤醒会话是否已过期。
        # 过了就补发一条 sleep 让按钮复位。用 `_standby_notified` 做幂等标记：
        # 会话被唤醒词激活时清零，所以不会把正常对话态打回待机。
        if device_id.startswith('web-terminal') and not getattr(conn, '_standby_notified', False):
            try:
                from core.utils.wakeword_provider import get_wakeword_detector as _wp_get
                _wp = _wp_get()
                if _wp and not _wp.is_session_active():
                    conn._standby_notified = True
                    # [2026-10-20] 与 receiveAudioHandle 那份共用一个时间戳，
                    # 避免任一方刚发完、另一方又立刻重发一遍。
                    conn._standby_notified_ts = time.time()
                    await conn.websocket.send(
                        json.dumps({"type": "state", "state": "sleep"})
                    )
                    conn.logger.bind(tag=TAG).info(
                        "[idle_guard] 唤醒会话已过期，补发待机通知（前端对话按钮复位）"
                    )
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"[idle_guard] 待机通知下发失败: {e}")

        # 静默忽略，不发 TTS 提示
        # [2026-10-18 P1，主人批准] 运行观察：这句话是真的被丢了（主人侧无任何反馈），
        # 计数 + 留样本，报告里用来判断「它不理我」到底发生多少次。
        try:
            from core.utils import observation as _obs
            _obs.bump("wake_drop", sample=text)
        except Exception:
            pass
        conn.logger.bind(tag=TAG).info(f"[idle_guard] 会话未激活，静默忽略: {text[:20]}...")
        return False

    # ── 声纹滚动窗口（2026-10-08 主人裁决 B 方案）────────────────────────────
    # 主人实测：同一人声纹相似度只有 0.23，而 provider 自己就提示「建议 3-10 秒」——
    # 因为 handle_voice_stop 只拿「刚提交的这一小段」（日志里大量 0.9~1.8s）。
    # 这里把最近若干秒的语音累积成滚动窗口再提取声纹，分数明显更稳。
    #   · voiceprint_roll_window_seconds（默认 6s）：窗口总时长上限
    #   · voiceprint_roll_gap_seconds（默认 15s）：距上次语音超过它就视为新一轮
    #     （换人/新话题），重新起窗口，避免把上一个说话人的音频混进来造成误放行
    _VP_ROLL_BYTES_PER_SEC = 16000 * 2  # 16kHz / 16bit / 单声道

    def _voiceprint_roll_pcm(self, conn: "ConnectionHandler", current_pcm: bytes) -> bytes:
        """把当前这段语音并进滚动缓冲，返回用于声纹的音频（受时长上限约束）。"""
        try:
            max_seconds = float(conn.config.get("voiceprint_roll_window_seconds", 6.0) or 6.0)
        except (TypeError, ValueError):
            max_seconds = 6.0
        try:
            gap_seconds = float(conn.config.get("voiceprint_roll_gap_seconds", 15.0) or 15.0)
        except (TypeError, ValueError):
            gap_seconds = 15.0
        max_bytes = int(max_seconds * self._VP_ROLL_BYTES_PER_SEC)

        now = time.time()
        buf = getattr(conn, "_vp_roll_pcm", None)
        last_ts = getattr(conn, "_vp_roll_last_ts", 0.0)
        if buf is None or (now - last_ts) > gap_seconds:
            buf = bytearray()
            conn._vp_roll_pcm = buf
        conn._vp_roll_last_ts = now
        if current_pcm:
            buf.extend(current_pcm)
        if len(buf) > max_bytes:
            del buf[: len(buf) - max_bytes]
        return bytes(buf)

    async def handle_voice_stop(self, conn: "ConnectionHandler", asr_audio_task: List[bytes]):
        """并行处理ASR和声纹识别 - 与小智一致的过滤机制"""
        logger.bind(tag=TAG).info(f"handle_voice_stop 被调用: {len(asr_audio_task)} 个音频块")
        voice_stop_ts = time.time() * 1000
        logger.bind(tag=TAG).info(f"[耗时] handle_voice_stop 开始, ts={voice_stop_ts}")
        try:
            total_start_time = time.monotonic()

            # 准备音频数据
            if conn.audio_format == "pcm":
                pcm_data = asr_audio_task
            else:
                pcm_data = self.decode_opus(asr_audio_task)

            combined_pcm_data = b"".join(pcm_data)

            # 【调试已关闭 2026-09-09】原先每句保存 /tmp/debug_asr_input_*.wav，
            # 同步写盘拖慢响应速度且占磁盘，问题排查完成即关闭
            # 预先准备WAV数据
            wav_data = None
            if conn.voiceprint_provider and combined_pcm_data:
                # 2026-10-08：声纹改用滚动窗口（最近若干秒）而不是只拿这一小段——
                # 片段太短（0.9~1.8s）时同一人分数只有 0.2x，会被闸门误杀。
                _vp_pcm = self._voiceprint_roll_pcm(conn, combined_pcm_data)
                wav_data = self._pcm_to_wav(_vp_pcm)

            # 2026-10-04 主人裁决：去掉 JEV 预识别文本复用。
            # 原因：hold 之后音频仍在累积，但 _semantic_preview_text 是判停时刻
            # 的快照，不跟着更新。复用它等于「用旧文本冒充最终结果」——
            # 主人 2026-10-04 实测连喊多遍唤醒词，只识别出前几遍就被判停，
            # 漏掉的后来几遍直接丢失（LLM 因此回答"你怎么喊了我这么多遍"）。
            # 现在每次提交都用当次完整音频真识别，保证文本与音频绝对一致。
            # 代价：非流式 ASR 每次提交多跑一次本地识别（实测 0.01~0.15 秒）。
            #
            # 2026-10-05 A2 优化：在**可证明音频没变**的前提下恢复复用。
            # A1 并行预判在候选结束那一刻已经识别过这份音频，且 verdict=complete
            # 时 VAD 会在同一帧直接提交（不经过 hold），中间不会有新音频进来。
            # 用音频指纹（块数 + 总字节数）做硬校验：
            #   指纹一致 → 文本必然来自同一份音频，复用安全，省一次 ASR(~125ms)
            #   指纹不一致 / 无指纹 / 非 complete → 照旧真识别
            # 这样既拿到省一次识别的收益，又绝不使用过期快照。
            _reuse_text = None
            if self._probe_still_valid(conn, asr_audio_task):
                _reuse_text = str(getattr(conn, "_vad_probe_text", "") or "").strip()

            if _reuse_text:
                logger.bind(tag=TAG).info(
                    f"[判停] 复用预判文本（音频指纹一致）: {_reuse_text[:60]!r}"
                )
                asr_result = (_reuse_text, None)
                asr_task = None
            else:
                asr_task = self.speech_to_text_wrapper(
                    asr_audio_task, conn.session_id, conn.audio_format
                )

            if asr_task is not None and conn.voiceprint_provider and wav_data:
                voiceprint_task = conn.voiceprint_provider.identify_speaker(
                    wav_data, conn.session_id
                )
                # 并发等待两个结果
                asr_result, voiceprint_result = await asyncio.gather(
                    asr_task, voiceprint_task, return_exceptions=True
                )
            elif asr_task is not None:
                asr_result = await asr_task
                voiceprint_result = None
            else:
                # 复用了预判文本，没有新的 ASR 任务；声纹仍需照常跑
                voiceprint_result = None
                if conn.voiceprint_provider and wav_data:
                    try:
                        voiceprint_result = (
                            await conn.voiceprint_provider.identify_speaker(
                                wav_data, conn.session_id
                            )
                        )
                    except Exception as exc:
                        voiceprint_result = ""
                        logger.bind(tag=TAG).warning(f"声纹识别失败: {exc}")

            # 记录识别结果 - 检查是否为异常
            if isinstance(asr_result, Exception):
                logger.bind(tag=TAG).error(f"ASR识别失败: {asr_result}")
                raw_text = ""
            else:
                raw_text, _ = asr_result

            if isinstance(voiceprint_result, Exception):
                logger.bind(tag=TAG).error(f"声纹识别失败: {voiceprint_result}")
                speaker_name = ""
            else:
                speaker_name = voiceprint_result

            # 判断 ASR 结果类型
            if isinstance(raw_text, dict):
                # FunASR 返回的 dict 格式
                if speaker_name:
                    raw_text["speaker"] = speaker_name

                # 记录识别结果
                if raw_text.get("language"):
                    logger.bind(tag=TAG).info(f"识别语言: {raw_text['language']}")
                if raw_text.get("emotion"):
                    logger.bind(tag=TAG).info(f"识别情绪: {raw_text['emotion']}")
                if raw_text.get("content"):
                    logger.bind(tag=TAG).info(f"识别文本: {raw_text['content']}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"识别说话人: {speaker_name}")

                # 转换为 JSON 字符串用于下游
                enhanced_text = json.dumps(raw_text, ensure_ascii=False)
                content_for_length_check = raw_text.get("content", "")
            else:
                # 其他 ASR 返回的纯文本
                if raw_text:
                    logger.bind(tag=TAG).info(f"识别文本: {raw_text}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"识别说话人: {speaker_name}")

                # 构建包含说话人信息的JSON字符串
                enhanced_text = self._build_enhanced_text(raw_text, speaker_name)
                content_for_length_check = raw_text

            # 音乐声纹闸门：音乐播放期间，声纹不匹配家庭成员的识别结果
            # 判为歌词残留（AEC 消音不彻底漏进 ASR），直接丢弃不进对话
            from core.utils.music_auto_pause import should_gate_music_text
            # 2026-09-21 修复：传入识别文本，供控制词白名单放行（音乐控制指令不再被误杀）
            if should_gate_music_text(conn, speaker_name, content_for_length_check):
                logger.bind(tag=TAG).info(
                    f"[音乐闸门] 音乐播放中且声纹不匹配，丢弃识别结果: {content_for_length_check[:30]}"
                )
                # [2026-10-13 音乐不停对话] 旧「丢弃后 schedule_restore 安排音量恢复」
                # 已删除：降音量流程不存在了，无恢复对象。
                return

            # 唤醒词优先通道（2026-10-03 修订，主人裁决"说对唤醒词就唤醒"）：
            # 声纹全线闸门原先排在下方 idle_guard 唤醒词判定之前 → 未激活会话说
            # "你好伊娃"先被闸门当非家庭成员声音杀掉，永远唤不醒。
            # 声纹校验一度加在本通道内，但声纹识别偶发误判（同一句喊 5、6 遍
            # 才唤醒一次），故撤销——命中唤醒词即激活并放行本句；未命中唤醒词
            # 的文本仍由下方声纹全线闸门处理（已激活会话不受影响，闸门维持
            # "非家庭成员不听不答"）。
            bypass_gate = False
            if (getattr(conn, 'idle_guard', False)
                    and not getattr(conn, '_session_active', False)
                    and content_for_length_check):
                from core.utils.wakeword_provider import get_wakeword_detector
                _wp = get_wakeword_detector()
                _hit = bool(_wp and _wp.enabled and _wp.detect(content_for_length_check)[0])
                _used = content_for_length_check

                # [2026-10-06] 正式识别把唤醒词听丢时，回看同一段音频的 A1 预判文本。
                #
                # 主人实测（开发机，本地 SenseVoice）：
                #     预判  「你好伊娃。」  ✅ 带唤醒词
                #     正式  「你好。」      ❌ 唤醒词没了 → 唤不醒，说好几遍才中
                # 另一次：预判「你好，伊娃。」→ 正式「你好に。」（日文假名）
                # 三次里两次正式结果更差，唤醒成功率只有 1/3。
                #
                # 为什么会有两份：`_semantic_endpoint_status` 的 preview 与
                # `handle_voice_stop` 的正式识别听的是同一段话音，
                # 后者多了 500ms 确认窗口的尾巴。已有「复用预判文本」机制
                # （_vad_probe_audio_sig 指纹校验）在这条路径上永远不成立，
                # 因为块数必然不同，于是每次都重识别、并且只用后一次。
                #
                # 这里不去动 ASR 结果（正式文本仍是送去对话的那份），
                # 只在**唤醒词门控**上多看一份预判文本 —— 门控本来只决定
                # 「醒不唤醒」，不参与对话内容，且仅在会话未激活时生效，
                # 对已激活会话零影响。
                if not _hit:
                    _probe = str(getattr(conn, '_vad_probe_text', '') or '').strip()
                    if _probe and _wp and _wp.detect(_probe)[0]:
                        _hit = True
                        _used = _probe
                        logger.bind(tag=TAG).info(
                            f"[idle_guard] 正式文本未命中唤醒词，改用预判文本: {_probe[:30]}"
                        )

                if _hit:
                    conn._session_active = True
                    bypass_gate = True
                    # [2026-10-20 主人批准，Mini 实测 bug] 与 _idle_guard_allow 的
                    # 唤醒分支保持一致，补两件事：
                    # ①清掉「已通知待机」标记 —— 原先这条路径漏清，标记从上次待机
                    #   起永远是 True，_push_standby_if_session_expired 每次都在
                    #   「已通知过」处 return → 120 秒超时后再也不推 sleep，页面
                    #   永久停在星球/待机（Mini 10-20 17:05→20:34 卡 3.5 小时即此因）。
                    # ②启动 120 秒会话计时 —— 后续每轮由 chat_engine 的
                    #   _update_wake_greeting_state → extend_session() 续期。
                    if getattr(conn, '_standby_notified', False):
                        conn._standby_notified = False
                        conn._standby_notified_ts = 0.0
                    try:
                        if _wp:
                            _wp.activate_session()
                    except Exception:
                        pass
                    conn.logger.bind(tag=TAG).info(
                        f"[idle_guard] 唤醒词命中，激活会话: {_used[:30]}"
                    )
                    # [2026-10-13 音乐不停对话·第三段，主人批准方案 A] 旧「唤醒词
                    # 命中 → 立刻暂停音乐（触发点①）」已删除：音乐持续播放不暂停；
                    # 第一句话同样走原始音频喂 ASR（实测音乐下识别率不受影响）。

            # 声纹全线闸门（2026-09-23，主人裁决）：非家庭成员不听不答，无媒体播放限制。
            # 未匹配到已录入成员（含"未知说话人"）→ 丢弃；退出命令白名单放行。
            # 例外：未激活会话唤醒词命中句已在上方激活放行（bypass_gate），不再闸。
            from core.utils.voiceprint_gate import should_gate_voiceprint
            if not bypass_gate and should_gate_voiceprint(conn, speaker_name, content_for_length_check):
                # [2026-10-18 P3，主人批准] 运行观察：声纹闸门丢弃计数 + 样本
                # （高频，只进内存聚合，不逐条落库）。
                try:
                    from core.utils import observation as _obs
                    _obs.bump("voiceprint_drop", sample=content_for_length_check)
                except Exception:
                    pass
                logger.bind(tag=TAG).info(
                    f"[声纹闸门] 非家庭成员声音({speaker_name or '未知'})，丢弃不回复: {content_for_length_check[:30]}"
                )
                return

            # 性能监控
            total_time = time.monotonic() - total_start_time
            logger.bind(tag=TAG).debug(f"总处理耗时: {total_time:.3f}s")

            # 延迟监控：ASR 识别完成（最终文本就绪；是否被回声丢弃另行判断）
            if hasattr(conn, 'latency_monitor'):
                conn.latency_monitor.mark('asr_complete', text_length=len(content_for_length_check))

            # TTS 播放期间识别的结果很可能是回声，直接忽略（与小智一致）
            # 例外：退出命令必须放行——否则"退出/再见"在播报期间说出口会被当回声吞掉
            if conn.client_is_speaking:
                _spk_text_len, plain_speaking_text = remove_punctuation_and_length(
                    content_for_length_check
                )
                _is_exit_cmd = any(
                    plain_speaking_text == c or plain_speaking_text.startswith(c)
                    for c in getattr(conn, "cmd_exit", [])
                )
                if not _is_exit_cmd:
                    # 打断命令白名单（2026-09-23，主人裁决 A+B 方案，与数字人链路
                    # 同源 voiceprint_gate.bargein_words）：播报中说"停/等一下/别说了"
                    # 等 → 立即打断且不应答（abort 后直接丢弃本段，不走应答）。
                    from core.utils.voiceprint_gate import is_bargein_command
                    if is_bargein_command(conn, plain_speaking_text):
                        logger.bind(tag=TAG).info(
                            f"TTS 播放中，识别到打断命令，打断当前播报(不应答): {plain_speaking_text}"
                        )
                        from core.handle.abortHandle import handleAbortMessage
                        await handleAbortMessage(conn)
                        return
                    # 真实插话通道（2026-09-23，主人反馈"讲故事时插话完全没打断"）：
                    # 走到这里的声音已过上方声纹全线闸门（=家庭成员；闸门关/不可用时
                    # 不判别）。TTS 外放播报时，家庭成员的长句插话（≥4字）是真实
                    # 插话而非回声——先打断当前播报，再放行进入正常应答。
                    # 短促插话（"停/等下"）由 receiveAudioHandle 的 VAD 层
                    # 近场爆发门槛兜底，此处仍按回声丢弃，防回声循环。
                    # 2026-10-07 新增：自激回声判别（声纹低 + 能量低）
                    #
                    # 主人实测（2026-10-07 00:00）：助手自己的声音被麦克风录回后，
                    # ASR 会把它听成**完全不同的词**（「我去网上找找看哈」→「日志8人」），
                    # 文本相似度过滤（0.65 阈值）根本抓不到；它还会被当成「家庭成员插话」
                    # 打断播报并应答，把对话带偏。
                    #
                    # 但回声的**声纹相似度与音量都明显偏低**（同一段真实会话实测）：
                    #     回声：相似度 0.24~0.34，RMS 49~617
                    #     真人：相似度 0.36~0.66，RMS 800~3127
                    # 所以播报期间「声纹 < 0.35 且 能量 < 1000」判为回声直接丢弃。
                    # 注意：退出命令 / 打断命令已在上方放行，不受影响。
                    if self._looks_like_self_echo(conn, asr_audio_task, content_for_length_check):
                        return
                    if _spk_text_len >= 4:
                        logger.bind(tag=TAG).info(
                            f"TTS 播放中，但识别到家庭成员插话（{_spk_text_len}字），"
                            f"打断当前播报并应答: {content_for_length_check[:30]}"
                        )
                        from core.handle.abortHandle import handleAbortMessage
                        await handleAbortMessage(conn)
                    else:
                        logger.bind(tag=TAG).info(
                            f"TTS 播放中，忽略 ASR 识别结果（防止回声循环）: {content_for_length_check[:30]}"
                        )
                        return
                logger.bind(tag=TAG).info(
                    f"TTS 播放中，但识别到退出命令，放行处理: {plain_speaking_text}"
                )

            # 保持与小智一致的过滤机制：至少需要 3 个音频块（约180ms）
            MIN_AUDIO_BLOCKS = 3  # 与小智ESP32一致的阈值
            if len(asr_audio_task) < MIN_AUDIO_BLOCKS:
                logger.bind(tag=TAG).info(
                    f"音频片段过短({len(asr_audio_task)}个音频块)，忽略回声/噪音"
                )
                return

            # 检查文本长度
            text_len, _ = remove_punctuation_and_length(content_for_length_check)
            self.stop_ws_connection()

            # 免唤醒守卫：Web 终端常开推流，会话未激活时只放行唤醒词语音
            # 注意传纯文本（非 enhanced_text JSON），否则唤醒词拼音匹配会失效
            if not await self._idle_guard_allow(conn, content_for_length_check):
                return

            if text_len > 0:
                asr_done_ts = time.time() * 1000
                logger.bind(tag=TAG).info(f"[耗时] ASR 识别完成, ts={asr_done_ts}, 耗时_since_stop={asr_done_ts - voice_stop_ts:.0f}ms, text={content_for_length_check[:30]}")
                # 使用自定义模块进行上报
                await startToChat(conn, enhanced_text)
                audio_snapshot = asr_audio_task.copy()
                enqueue_asr_report(conn, enhanced_text, audio_snapshot)
            elif getattr(conn, "just_woken_up", False):
                # 用户刚唤醒，但 ASR 没有识别出文本，兜底播放一句短回复
                last_wakeup_time = getattr(conn, "last_wakeup_time", 0)
                time_since_wakeup = time.time() * 1000 - last_wakeup_time
                # 限制为唤醒后 5 秒内，避免误兜底
                if last_wakeup_time and time_since_wakeup <= 5000:
                    logger.bind(tag=TAG).info(
                        f"ASR 为空但处于唤醒状态({time_since_wakeup:.0f}ms)，兜底播放短回复"
                    )
                    await play_short_wakeup_reply(conn)
                else:
                    logger.bind(tag=TAG).info(
                        f"ASR 为空且唤醒已超时({time_since_wakeup:.0f}ms)，不兜底短回复"
                    )
        except Exception as e:
            logger.bind(tag=TAG).error(f"处理语音停止失败: {e}")
            import traceback

            logger.bind(tag=TAG).debug(f"异常详情: {traceback.format_exc()}")

    def _build_enhanced_text(self, text: str, speaker_name: Optional[str]) -> str:
        """构建包含说话人信息的文本（仅用于纯文本ASR）"""
        if speaker_name and speaker_name.strip():
            return json.dumps(
                {"speaker": speaker_name, "content": text}, ensure_ascii=False
            )
        else:
            return text

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """将PCM数据转换为WAV格式"""
        if len(pcm_data) == 0:
            logger.bind(tag=TAG).warning("PCM数据为空，无法转换WAV")
            return b""

        # 确保数据长度是偶数（16位音频）
        if len(pcm_data) % 2 != 0:
            pcm_data = pcm_data[:-1]

        # 创建WAV文件头
        wav_buffer = io.BytesIO()
        try:
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # 单声道
                wav_file.setsampwidth(2)  # 16位
                wav_file.setframerate(16000)  # 16kHz采样率
                wav_file.writeframes(pcm_data)

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            return wav_data
        except Exception as e:
            logger.bind(tag=TAG).error(f"WAV转换失败: {e}")
            return b""

    def stop_ws_connection(self):
        pass

    async def close(self):
        pass

    class AudioArtifacts(NamedTuple):
        pcm_frames: List[bytes]
        """PCM音频帧列表"""
        pcm_bytes: bytes
        """合并后的PCM音频字节数据"""
        file_path: Optional[str]
        """WAV文件路径"""
        temp_path: Optional[str]
        """临时WAV文件路径"""

    def get_current_artifacts(self) -> Optional["ASRProviderBase.AudioArtifacts"]:
        return self._current_artifacts

    def requires_file(self) -> bool:
        """是否需要文件输入"""
        return False

    def prefers_temp_file(self) -> bool:
        """是否优先使用临时文件"""
        return False

    def build_temp_file(self, pcm_bytes: bytes) -> Optional[str]:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_path = temp_file.name
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(pcm_bytes)
            return temp_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"临时音频文件生成失败: {e}")
            return None

    def save_audio_to_file(self, pcm_data: List[bytes], session_id: str) -> str:
        """PCM数据保存为WAV文件"""
        module_name = __name__.split(".")[-1]
        file_name = f"asr_{module_name}_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)

        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))

        return file_path

    async def speech_to_text_wrapper(
        self, opus_data: List[bytes], session_id: str, audio_format="opus"
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path = None
        temp_path = None
        try:
            if audio_format == "pcm":
                pcm_data = opus_data
            else:
                pcm_data = self.decode_opus(opus_data)
            combined_pcm_data = b"".join(pcm_data)

            # 诊断：检查 Opus 解码结果
            if audio_format != "pcm":
                expected_pcm = len(opus_data) * 1920  # 每帧 Opus 应解码为 1920 bytes
                logger.bind(tag=TAG).warning(f"[ASR-wrapper诊断] opus块数={len(opus_data)}, 解码PCM块数={len(pcm_data)}, 总PCM={len(combined_pcm_data)} bytes, 预期={expected_pcm} bytes")
                if len(combined_pcm_data) >= 2:
                    audio_int16 = np.frombuffer(combined_pcm_data, dtype=np.int16)
                    rms = np.sqrt(np.mean(audio_int16.astype(np.float64) ** 2))
                    max_val = np.max(np.abs(audio_int16))
                    logger.bind(tag=TAG).warning(f"[ASR-wrapper诊断] PCM: RMS={rms:.1f}, MAX={max_val}, samples={len(audio_int16)}, duration={len(audio_int16)/16000:.2f}s")

            free_space = shutil.disk_usage(self.output_dir).free
            if free_space < len(combined_pcm_data) * 2:
                raise OSError("磁盘空间不足")

            if self.requires_file() and self.prefers_temp_file():
                temp_path = self.build_temp_file(combined_pcm_data)

            if (hasattr(self, "delete_audio_file") and not self.delete_audio_file) or (
                self.requires_file() and not self.prefers_temp_file()
            ):
                file_path = self.save_audio_to_file(pcm_data, session_id)

            if len(combined_pcm_data) == 0:
                artifacts = None
            else:
                artifacts = ASRProviderBase.AudioArtifacts(
                    pcm_frames=pcm_data,
                    pcm_bytes=combined_pcm_data,
                    file_path=file_path,
                    temp_path=temp_path,
                )

            text, _ = await self.speech_to_text(
                opus_data, session_id, audio_format, artifacts
            )
            return text, file_path
        except OSError as e:
            logger.bind(tag=TAG).error(f"文件操作错误: {e}")
            return None, None
        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}")
            return None, None
        finally:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                if (
                    hasattr(self, "delete_audio_file")
                    and self.delete_audio_file
                    and file_path
                    and os.path.exists(file_path)
                ):
                    os.remove(file_path)
            except Exception as e:
                logger.bind(tag=TAG).error(f"文件清理失败: {e}")

    @abstractmethod
    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        audio_format="opus",
        artifacts: Optional[AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本

        :param opus_data: 输入的Opus音频数据
        :param session_id: 会话ID
        :param audio_format: 音频格式，默认"opus"
        :param artifacts: 音频工件，包含PCM数据、文件路径等
        :return: 识别结果文本和文件路径（如果有）
        """
        pass

    @staticmethod
    def decode_opus(opus_data: List[bytes]) -> List[bytes]:
        """将Opus音频数据解码为PCM数据"""
        decoder = None
        try:
            decoder = opuslib_next.Decoder(16000, 1)
            pcm_data = []
            buffer_size = 960  # 每次处理960个采样点 (60ms at 16kHz)

            for i, opus_packet in enumerate(opus_data):
                try:
                    if not opus_packet or len(opus_packet) == 0:
                        continue

                    pcm_frame = decoder.decode(opus_packet, buffer_size)
                    if pcm_frame and len(pcm_frame) > 0:
                        pcm_data.append(pcm_frame)

                except opuslib_next.OpusError as e:
                    logger.bind(tag=TAG).warning(f"Opus解码错误，跳过数据包 {i}: {e}")
                except Exception as e:
                    logger.bind(tag=TAG).error(f"音频处理错误，数据包 {i}: {e}")

            return pcm_data

        except Exception as e:
            logger.bind(tag=TAG).error(f"音频解码过程发生错误: {e}")
            return []
        finally:
            if decoder is not None:
                try:
                    del decoder
                except Exception as e:
                    logger.bind(tag=TAG).debug(f"释放decoder资源时出错: {e}")
