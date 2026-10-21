"""
WebSocket 连接处理器 — HomeBrain 核心

从原 1749 行精简为 ~400 行。
对话逻辑、组件初始化、消息路由、设备管理已拆分到 core/chat/ 子模块。
"""
import os
import sys
import copy
import json
import uuid
import time
import queue
import asyncio
import threading
import traceback
import subprocess
import websockets

from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor
from core.utils.dialogue import Dialogue
from plugins_func.loadplugins import auto_import_modules
from core.auth import AuthenticationError
from config.logger import setup_logging
from core.utils.prompt_manager import PromptManager
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType

# 拆分后的子模块
from core.chat.chat_engine import chat as _chat
from core.chat.initializer import (
    initialize_components,
    background_initialize,
)
from core.chat.device import auto_register_device, update_device_offline
from core.chat.router import route_message

TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


class ConnectionHandler:
    def __init__(
            self,
            config: Dict[str, Any],
            _vad,
            _asr,
            _llm,
            _memory,
            _intent,
            server=None,
    ):
        self.common_config = config
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        self.logger = setup_logging()
        self.server = server

        from core.utils.latency_monitor import get_monitor
        self.latency_monitor = get_monitor(self.session_id)

        self.need_bind = False
        self.bind_completed_event = asyncio.Event()
        self.bind_code = None
        self.last_bind_prompt_time = 0
        self.bind_prompt_interval = 60

        self.read_config_from_api = self.config.get("read_config_from_api", False)

        self.websocket: websockets.ServerConnection | None = None
        self.headers = None
        self.device_id = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.max_output_size = 0
        self.chat_history_conf = 0
        self.audio_format = "opus"
        self.sample_rate = 24000

        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"

        self.aec_enabled = False
        self.aec = None

        # Web 终端免唤醒守卫：声明 idle_guard 的连接（常开推流），
        # 会话未激活时只放行含唤醒词的语音
        self.idle_guard = False
        self._session_active = False

        self.continuous_conversation_mode = False
        self.continuous_input_channel = None
        self.continuous_output_channel = None
        self.continuous_sample_rate = 48000

        self.loop = None
        self.stop_event = threading.Event()
        # [2026-10-12 P0] max_workers 5 → 3。原因见 receiveAudioHandle.startToChat
        # 里的「根本原因」注释：chat() 是同步阻塞函数，abort 停不掉已发出的 LLM
        # 请求，旧线程要等云端返回才释放 worker。worker 越多只会让排队积压越多。
        # 现在的根治手段是「LLM 流感知新一轮已接管就主动断开」（openai.py 的
        # abort_probe），worker 能立即释放，所以不必压到 1（那会让插话响应变慢）。
        # 3 = 允许 1 个在跑 + 少量缓冲，又不至于把人手一句全塞进队列。
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="chat-")

        self.report_queue = queue.Queue()
        self.report_thread = None
        self.report_asr_enable = self.read_config_from_api
        self.report_tts_enable = self.read_config_from_api

        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        self.voiceprint_provider = None

        self.audio_rate_controller = None
        self.audio_flow_control = None

        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = []
        self.first_activity_time = 0.0
        self.last_activity_time = 0.0
        self.client_voice_stop = False
        self.last_is_voice = False

        self.asr_audio = []
        self.asr_audio_queue = queue.Queue()
        self.current_speaker = None

        self.dialogue = Dialogue()

        self.tool_call_stats = {
            'last_call_turn': -1,
            'consecutive_no_call': 0,
        }

        self.sentence_id = None
        self.tts_MessageText = ""

        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]

        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"
        # 组件初始化完成标志（initialize_components 末尾置 True），
        # 用于意图分析/对话前等待就绪，避免 detect 文本路径的初始化竞态
        self.components_ready = False

        self.timeout_seconds = (
                int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )
        self.timeout_task = None

        self.features = None
        self.conn_from_mqtt_gateway = False

        # 危险操作人确认闸门：插件通过 _ask_human 挂起等待网页端 confirm 回复
        self.pending_confirm_future = None

        self.prompt_manager = PromptManager(self.config, self.logger)

    # ─── WebSocket 生命周期 ───────────────────────────────

    async def handle_connection(self, ws: websockets.ServerConnection):
        try:
            self.loop = asyncio.get_running_loop()

            self.headers = dict(ws.request.headers)
            real_ip = self.headers.get("x-real-ip") or self.headers.get("x-forwarded-for")
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()
            else:
                self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            self.device_id = self.headers.get("device-id", None)
            self.client_id = self.headers.get("client-id", self.device_id)

            await auto_register_device(self)

            self.websocket = ws

            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("连接来自:MQTT网关")

            self.first_activity_time = time.time() * 1000
            self.last_activity_time = time.time() * 1000

            self.timeout_task = asyncio.create_task(self._check_timeout())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            self.sample_rate = self.welcome_msg["audio_params"]["sample_rate"]
            self.logger.bind(tag=TAG).info(f"配置输出音频采样率为: {self.sample_rate}")

            asyncio.create_task(background_initialize(self))

            try:
                async for message in self.websocket:
                    await route_message(self, message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("客户端断开连接")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"最终清理时出错: {final_error}")
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(f"强制关闭连接时出错: {close_error}")

    async def _save_and_close(self, ws):
        """收尾记忆并关闭连接。

        ⚠️⚠️ 2026-10-13 WP-3 改动（主人已批准改这个稳定模块）：

        原来这里是**把整段 `self.dialogue.dialogue` 再交给 save_memory 一遍**，
        而且是 `threading.Thread(daemon=True)` + 新建事件循环，没有任何完成确认：
        ① 与 chat_engine 的逐轮实时保存**重叠** → 现网 160 条记忆里 28 条完全重复；
        ② daemon 线程在进程退出时被直接杀死 → 那次保存静默丢失；
        ③ 整段历史一次性提交，很容易撞 LM Studio 的 2048 token 槽位上限。

        现在改成只调 provider 的 `flush_now()`：把**已经入队**的待抽取任务抽完
        （mem0 走落盘 outbox，msg_id 幂等），不重新提交历史。
        即使这里超时/失败，任务仍在队列里，下次连接或重启后会自动继续。
        """
        try:
            if self.memory:
                flush = getattr(self.memory, "flush_now", None)
                if flush is not None:
                    result = await flush(timeout=25.0)
                    self.logger.bind(tag=TAG).info(
                        f"连接收尾记忆 flush：{result}"
                    )
                else:
                    # 没有 flush_now 的 provider（如 openviking）：
                    # **什么都不做**。逐轮实时保存已经在 chat_engine 里做了，
                    # 这里再提交整段历史只会产生重复；
                    # 也不能调 clear_session()——openviking 的 clear_session 会
                    # **删掉服务端会话**，那是销毁数据不是收尾。
                    self.logger.bind(tag=TAG).debug(
                        "该记忆后端无 flush_now，收尾不重复提交历史"
                    )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"收尾记忆失败（任务仍在队列，重启后会恢复）: {e}")
        finally:
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(f"保存记忆后关闭连接失败: {close_error}")

    async def close(self, ws=None):
        """资源清理"""
        try:
            await update_device_offline(self)

            if hasattr(self, "vad") and self.vad and hasattr(self.vad, "release_conn_resources"):
                self.vad.release_conn_resources(self)

            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            # 唤醒仍挂起的人确认等待（视为取消/拒绝，避免插件协程永久悬挂）
            if self.pending_confirm_future and not self.pending_confirm_future.done():
                self.pending_confirm_future.cancel()
                try:
                    await self.pending_confirm_future
                except asyncio.CancelledError:
                    pass
                self.pending_confirm_future = None

            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(f"清理工具处理器时出错: {cleanup_error}")

            if self.stop_event:
                self.stop_event.set()

            self.clear_queues()

            try:
                if ws:
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            await ws.close()
                    except Exception:
                        pass
                elif self.websocket:
                    try:
                        if hasattr(self.websocket, "closed") and not self.websocket.closed:
                            await self.websocket.close()
                        elif hasattr(self.websocket, "state") and self.websocket.state.name != "CLOSED":
                            await self.websocket.close()
                        else:
                            await self.websocket.close()
                    except Exception:
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"关闭WebSocket连接时出错: {ws_error}")

            if self.tts:
                await self.tts.close()
            if self.asr:
                await self.asr.close()

            if self.executor:
                try:
                    self.executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(f"关闭线程池时出错: {executor_error}")
                self.executor = None
            self.logger.bind(tag=TAG).info("连接资源已释放")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"关闭连接时出错: {e}")
        finally:
            if self.stop_event:
                self.stop_event.set()

    # ─── 便捷方法（委托给子模块） ──────────────────────────

    def chat(self, query, depth=0):
        """对话 — 委托给 chat_engine"""
        return _chat(self, query, depth)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        self.dialogue.update_system_message(self.prompt)

    def chat_and_close(self, text):
        try:
            self.chat(text)
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug("清除服务端讲话状态")


    def clear_queues(self):
        """清空所有任务队列"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"开始清理: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )
            for q in [self.tts.tts_text_queue, self.tts.tts_audio_queue, self.report_queue]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            if hasattr(self, "audio_rate_controller") and self.audio_rate_controller:
                self.audio_rate_controller.reset()
                self.logger.bind(tag=TAG).debug("已重置音频流控器")
            self.logger.bind(tag=TAG).debug(
                f"清理结束: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_audio_states(self):
        """重置所有音频相关状态"""
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        for attr in ("_vad_state", "_vad_context", "_asr_voice_start_time", "_last_voice_time"):
            if hasattr(self, attr):
                try:
                    delattr(self, attr)
                except Exception:
                    pass
        self.asr_audio.clear()
        if hasattr(self, '_last_asr_time'):
            self._last_asr_time = 0
        self.logger.bind(tag=TAG).debug("All audio states reset (VAD will be re-initialized).")

    async def handle_restart(self, message):
        """处理服务器重启请求"""
        try:
            self.logger.bind(tag=TAG).info("收到服务器重启指令，准备执行...")
            await self.websocket.send(
                json.dumps({
                    "type": "server",
                    "status": "success",
                    "message": "服务器重启中...",
                    "content": {"action": "restart"},
                })
            )
            def restart_server():
                time.sleep(1)
                self.logger.bind(tag=TAG).info("执行服务器重启...")
                subprocess.Popen(
                    [sys.executable, "app.py"],
                    stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                    start_new_session=True,
                )
                os._exit(0)
            threading.Thread(target=restart_server, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重启失败: {str(e)}")
            await self.websocket.send(
                json.dumps({
                    "type": "server",
                    "status": "error",
                    "message": f"Restart failed: {str(e)}",
                    "content": {"action": "restart"},
                })
            )

    async def _check_timeout(self):
        """检查连接超时"""
        try:
            while not self.stop_event.is_set():
                last_activity_time = self.last_activity_time
                if self.need_bind:
                    last_activity_time = self.first_activity_time
                if last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if current_time - last_activity_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("连接超时，准备关闭")
                            self.stop_event.set()
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(f"超时关闭连接时出错: {close_error}")
                        break
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"超时检查任务出错: {e}")
        finally:
            self.logger.bind(tag=TAG).info("超时检查任务已退出")
