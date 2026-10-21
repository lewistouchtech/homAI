import json
import gzip
import time
import uuid
import asyncio
import websockets
import opuslib_next
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# [2026-10-06] 空结果的处理时限（秒）。替代原来那个 `duration > 2000` 的魔法阈值。
#
# 背景：原判据是「云端已收 2 秒音频却还没有文本 → 判定识别失败」。
# 但火山 bigmodel_nostream 端点自带服务端 VAD，是它决定何时吐最终结果，
# 实测 2.6 秒音频时 duration=2020ms 就被放弃，而火山在 2675ms 才给出文本 ——
# 中间 655ms 的结果被白白丢掉，表现为「唤醒唤不醒、长句必败、短句偶尔成功」。
#
# 现在改成两个跟「真实状态」有关的时限，不再猜语音有多长：
#   _EMPTY_GRACE_SECONDS 客户端已发 listen stop（明确说完了）后，还允许云端收尾多久
#   _STALL_GUARD_SECONDS  兜底上限，防止任何情况下无限等待
_EMPTY_GRACE_SECONDS = 1.5
_STALL_GUARD_SECONDS = 15.0


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.decoder = opuslib_next.Decoder(16000, 1)
        # 按连接隔离会话状态（2026-09-14 修复）：
        # 原实现把 asr_ws/is_processing/text 挂在 provider 上，而 provider 是全局单例，
        # 两个设备同时说话会互相串流（实测网页终端+测试连接交叉收到对方识别结果）。
        # key = conn.session_id，value = {"ws","task","processing","text"}
        self._sessions = {}
        self._current_sid = None

        # 配置参数
        self.appid = str(config.get("appid") or "")
        self.cluster = config.get("cluster")
        self.access_token = config.get("access_token")
        # 新版方舟 API Key 认证（只需 api_key + resource_id，无需 appid/token）
        self.api_key = config.get("api_key")
        self.resource_id = config.get("resource_id", "volc.bigasr.sauc.duration")
        self.boosting_table_name = config.get("boosting_table_name", "")
        self.correct_table_name = config.get("correct_table_name", "")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        # 火山引擎ASR配置
        enable_multilingual = config.get("enable_multilingual", False)
        self.enable_multilingual = (
            False if str(enable_multilingual).lower() == "false" else True
        )
        # ws_url 可配（2026-09-28）：Agent Plan 套餐走 /api/v3/plan/sauc/... 端点，
        # 用套餐 key；不传则保持旧行为（普通端点 + 平台 key）。
        _configured_url = str(config.get("ws_url") or "").strip()
        if _configured_url:
            self.ws_url = _configured_url
        elif self.enable_multilingual:
            self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
        else:
            self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        self.uid = config.get("uid", "streaming_asr_service")
        self.workflow = config.get(
            "workflow", "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate"
        )
        self.result_type = config.get("result_type", "single")
        self.format = config.get("format", "pcm")
        self.codec = config.get("codec", "pcm")
        self.rate = config.get("sample_rate", 16000)
        # language参数仅在多语种模式(bigmodel_nostream)下有效
        self.language = config.get("language") if self.enable_multilingual else None
        self.bits = config.get("bits", 16)
        self.channel = config.get("channel", 1)
        self.auth_method = config.get("auth_method", "token")
        self.secret = config.get("secret", "access_secret")
        end_window_size = config.get("end_window_size")
        self.end_window_size = int(end_window_size) if end_window_size else 200

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    def _audio_to_pcm(self, conn: "ConnectionHandler", audio: bytes) -> bytes:
        """把音频帧统一转成 16k PCM 再发给火山。
        网页终端等 PCM 客户端直接透传；ESP32 等 opus 客户端需解码。"""
        if getattr(conn, "audio_format", "opus") == "pcm":
            return audio
        return self.decoder.decode(audio, 960)

    def _sess(self, conn: "ConnectionHandler") -> dict:
        """取当前连接的隔离会话状态（ws/task/processing/text）"""
        s = self._sessions.get(conn.session_id)
        if s is None:
            s = {
                "ws": None,
                "task": None,
                "processing": False,
                "text": "",
                # [2026-10-06] 空结果判定的计时起点/最近回包时刻，新连接要重置
                "_first_empty_ts": None,
                "_last_pkg_ts": None,
                # 连接代际（2026-10-11 A3）：每次新建 ws +1。转发任务收尾时据此判断
                # 「我还是当前这一轮吗」——旧任务晚收尾不得复位新连接已累积的音频/VAD 状态。
                "gen": 0,
            }
            self._sessions[conn.session_id] = s
        return s

    async def receive_audio(self, conn: "ConnectionHandler", audio, audio_have_voice):
        # 先调用父类方法处理基础逻辑
        await super().receive_audio(conn, audio, audio_have_voice)

        sess = self._sess(conn)

        # 如果本次有声音，且之前没有建立连接
        if audio_have_voice and sess["ws"] is None and not sess["processing"]:
            try:
                sess["processing"] = True
                sess.pop("_last_definite", None)  # 新连接重置去重状态
                # [2026-10-06] 新连接重置空结果计时，否则会沿用上一轮的起点立刻触发放弃
                sess["_first_empty_ts"] = None
                sess["_last_pkg_ts"] = None
                # [2026-10-11 A3·E1] 新连接必须清上一句的滞留文本。
                # 原缺陷：上一句 VAD 提交(B)先于云端 definite(A) 到达时，B 已把 asr_audio 取空，
                # A 到达时 len(audio_data) > 15 不成立 → 文本永久滞留在 sess["text"]；
                # 新连接创建时不清 → 下一句拿到的是上一句的旧文本（体感「要喊两遍」）。
                sess["text"] = ""
                sess["gen"] = sess.get("gen", 0) + 1
                # 建立新的WebSocket连接
                if self.api_key:
                    headers = self.apikey_auth()
                elif self.auth_method == "token":
                    headers = self.token_auth()
                else:
                    headers = None
                logger.bind(tag=TAG).info(f"正在连接ASR服务，headers: {headers}")

                sess["ws"] = await websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    max_size=1000000000,
                    # [2026-10-11 A3·E3] 原来 open 无超时、ping_interval=None、close_timeout=10。
                    # 每轮对话都新建一次 WS → N 轮就是 N 次「挂住」的机会；一次挂住会冻结整条
                    # 串行音频泵（PCM 路径还会冻结该连接整个消息循环），表现为「链路直接断、
                    # 日志一片寂静」。这里全部加上界，并开启 keepalive 让死连接能被检出。
                    open_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                )

                # 发送初始化请求
                request_params = self.construct_request(str(uuid.uuid4()))
                try:
                    payload_bytes = str.encode(json.dumps(request_params))
                    payload_bytes = gzip.compress(payload_bytes)
                    full_client_request = self.generate_header()
                    full_client_request.extend((len(payload_bytes)).to_bytes(4, "big"))
                    full_client_request.extend(payload_bytes)

                    logger.bind(tag=TAG).info(f"发送初始化请求: {request_params}")
                    await sess["ws"].send(full_client_request)

                    # 等待初始化响应（[2026-10-11 A3·E3] 必须有超时，否则云端不回就永久挂住）
                    init_res = await asyncio.wait_for(sess["ws"].recv(), timeout=5)
                    result = self.parse_response(init_res)
                    logger.bind(tag=TAG).info(f"收到初始化响应: {result}")

                    # 检查初始化响应
                    if "code" in result and result["code"] != 1000:
                        error_msg = f"ASR服务初始化失败: {result.get('payload_msg', {}).get('error', '未知错误')}"
                        logger.bind(tag=TAG).error(error_msg)
                        raise Exception(error_msg)

                except Exception as e:
                    logger.bind(tag=TAG).error(f"发送初始化请求失败: {str(e)}")
                    if hasattr(e, "__cause__") and e.__cause__:
                        logger.bind(tag=TAG).error(f"错误原因: {str(e.__cause__)}")
                    raise e

                # 启动接收ASR结果的异步任务
                sess["task"] = asyncio.create_task(self._forward_asr_results(conn, sess))

                # 发送缓存的音频数据
                if conn.asr_audio and len(conn.asr_audio) > 0:
                    for cached_audio in conn.asr_audio[-10:]:
                        try:
                            pcm_frame = self._audio_to_pcm(conn, cached_audio)
                            payload = gzip.compress(pcm_frame)
                            audio_request = bytearray(
                                self.generate_audio_default_header()
                            )
                            audio_request.extend(len(payload).to_bytes(4, "big"))
                            audio_request.extend(payload)
                            await sess["ws"].send(audio_request)
                        except Exception as e:
                            logger.bind(tag=TAG).info(
                                f"发送缓存音频数据时发生错误: {e}"
                            )

            except Exception as e:
                logger.bind(tag=TAG).error(f"建立ASR连接失败: {str(e)}")
                if hasattr(e, "__cause__") and e.__cause__:
                    logger.bind(tag=TAG).error(f"错误原因: {str(e.__cause__)}")
                if sess["ws"]:
                    await sess["ws"].close()
                    sess["ws"] = None
                sess["processing"] = False
                return

        # 发送当前音频数据
        if sess["ws"] and sess["processing"]:
            try:
                pcm_frame = self._audio_to_pcm(conn, audio)
                payload = gzip.compress(pcm_frame)
                audio_request = bytearray(self.generate_audio_default_header())
                audio_request.extend(len(payload).to_bytes(4, "big"))
                audio_request.extend(payload)
                await sess["ws"].send(audio_request)
            except Exception as e:
                logger.bind(tag=TAG).info(f"发送音频数据时发生错误: {e}")

    async def _forward_asr_results(self, conn: "ConnectionHandler", sess: dict):
        self._current_sid = conn.session_id
        my_ws = sess["ws"]  # 本任务绑定的 ws；会话重连后 sess 里会被替换
        my_gen = sess.get("gen", 0)  # 本任务绑定的连接代际
        try:
            while sess["ws"] is my_ws and not conn.stop_event.is_set():
                # 获取当前连接的音频数据
                audio_data = conn.asr_audio
                try:
                    response = await sess["ws"].recv()
                    result = self.parse_response(response)
                    logger.bind(tag=TAG).debug(f"收到ASR结果: {result}")

                    if "payload_msg" in result:
                        payload = result["payload_msg"]
                        # 检查是否是错误码1013（无有效语音）
                        if "code" in payload and payload["code"] == 1013:
                            # 静默处理，不记录错误日志
                            continue

                        if "result" in payload:
                            utterances = payload["result"].get("utterances", [])
                            # 空文本的处理：只在**确实等不到结果**时才放弃
                            #
                            # [2026-10-06] 这里原来有一个 `duration > 2000` 的魔法阈值，
                            # 是 2026-06-14 baseline 快照带进来的硬编码（0cde234），
                            # 从来没做成配置项。它是本项目最大的一个语音故障源：
                            #   ① 火山 bigmodel_nostream 端点**自带服务端 VAD**
                            #      （workflow 里有 vad），靠它决定什么时候吐最终结果；
                            #   ② 但这个阈值在火山 VAD 还没判完时就先放弃了 ——
                            #      实测 2.6 秒音频：duration=2020ms 时这里放弃，
                            #      而火山在 2675ms 才返回 definite 文本
                            #      「你好，伊娃，现在几点了？停！」，中间 655ms 被丢掉；
                            #   ③ 后果：真人说话超过 2 秒 → sess["text"] 被清空 →
                            #      唤醒词拿空串去匹配 → 唤不醒、无响应；
                            #      短句偶尔成功、长句必败，表现为「时好时坏」。
                            #
                            # 实测（tmp/probe_doubao_packets.py）：
                            #   0.98 秒音频 → 文本在 duration=1065ms 出现，不触发放弃 ✅
                            #   2.6  秒音频 → duration=2020ms 触发放弃 ❌
                            #
                            # 正确判据不是「猜语音有多长」，而是「客户端明确说完了」
                            # （listen stop → conn.client_voice_stop），
                            # 给云端 VAD 一点收尾时间仍无结果，才判定本轮失败。
                            if (
                                not self.enable_multilingual
                                and not utterances
                                and not payload["result"].get("text")
                                and conn.client_listen_mode != "manual"
                            ):
                                # 首次进入空结果状态时记下起点
                                now = time.time()
                                first_ts = sess.setdefault("_first_empty_ts", now)
                                last_ts = sess["_last_pkg_ts"] = now
                                waited = now - first_ts
                                # ① 客户端已结束本轮语音，且云端静默超过宽限期 → 放弃
                                # ② 兜底：等太久也放弃，绝不无限等
                                stalled = waited >= _STALL_GUARD_SECONDS or (
                                    conn.client_voice_stop
                                    and (now - last_ts) >= _EMPTY_GRACE_SECONDS
                                )
                                if stalled:
                                    logger.bind(tag=TAG).error(
                                        f"识别文本：空（已等待 {waited:.1f}s，"
                                        f"client_voice_stop={conn.client_voice_stop}）"
                                    )
                                    sess["text"] = ""
                                    if len(audio_data) > 15:  # 确保有足够音频数据
                                        await self.handle_voice_stop(conn, audio_data)
                                    break


                            # 专门处理没有文本的识别结果（手动模式下可能已经识别完成但是没松按键）
                            elif not payload["result"].get("text") and not utterances:
                                # 多语种模式会持续返回空文本，直到最后返回完整结果，所以需要排除
                                if self.enable_multilingual:
                                    continue

                                if conn.client_listen_mode == "manual" and conn.client_voice_stop and len(audio_data) > 15:
                                    logger.bind(tag=TAG).debug("消息结束收到停止信号，触发处理")
                                    await self.handle_voice_stop(conn, audio_data)
                                    break

                            for utterance in utterances:
                                if utterance.get("definite", False):
                                    current_text = utterance["text"]
                                    # 去重：prefetch 预取包与正式包会对同一句都返回
                                    # definite=true，不去重会触发两次 handle_voice_stop
                                    # （双份 LLM/双份 TTS，2026-09-14 实测）。
                                    # 判重键=(文本,end_time)：预取/正式包两者相同；
                                    # 用户重复说同一句时 end_time 不同，不误杀。
                                    dedup_key = (current_text, utterance.get("end_time"))
                                    if dedup_key == sess.get("_last_definite"):
                                        logger.bind(tag=TAG).info(
                                            f"跳过重复definite: {current_text}"
                                        )
                                        break
                                    sess["_last_definite"] = dedup_key
                                    logger.bind(tag=TAG).info(
                                        f"识别到文本: {current_text}"
                                    )

                                    # 手动模式下累积识别结果
                                    if conn.client_listen_mode == "manual":
                                        if sess["text"]:
                                            sess["text"] += current_text
                                        else:
                                            sess["text"] = current_text

                                        # 在接收消息中途时收到停止信号
                                        if conn.client_voice_stop and len(audio_data) > 0:
                                            logger.bind(tag=TAG).debug("消息中途收到停止信号，触发处理")
                                            await self.handle_voice_stop(conn, audio_data)
                                        break
                                    else:
                                        # 自动模式下直接覆盖
                                        sess["text"] = current_text
                                        if len(audio_data) > 15:  # 确保有足够音频数据
                                            self._current_sid = conn.session_id
                                            await self.handle_voice_stop(
                                                conn, audio_data
                                            )
                                    break
                        elif "error" in payload:
                            error_msg = payload.get("error", "未知错误")
                            logger.bind(tag=TAG).error(f"ASR服务返回错误: {error_msg}")
                            break

                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("ASR服务连接已关闭")
                    if sess.get("ws") is my_ws:
                        sess["processing"] = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"处理ASR结果时发生错误: {str(e)}")
                    if hasattr(e, "__cause__") and e.__cause__:
                        logger.bind(tag=TAG).error(f"错误原因: {str(e.__cause__)}")
                    if sess.get("ws") is my_ws:
                        sess["processing"] = False
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"ASR结果转发任务发生错误: {str(e)}")
            if hasattr(e, "__cause__") and e.__cause__:
                logger.bind(tag=TAG).error(f"错误原因: {str(e.__cause__)}")
        finally:
            # 只关闭本任务的 ws；若该会话已重连（ws 被替换）则不得动新连接——
            # 否则旧任务的 finally 会掐死同会话的新 ws（2026-09-14 实测此竞态丢识别）
            if my_ws is not None:
                try:
                    await my_ws.close()
                except Exception:
                    pass
            if sess.get("ws") is my_ws:
                sess["ws"] = None
                sess["processing"] = False
            # [2026-10-11 A3·E2] 原来无条件 reset_audio_states()。stop_ws_connection() 会先把 ws
            # 置空，新连接可在旧任务收尾前建立；旧任务晚收尾就会抹掉下一句已累积的音频和 VAD 状态
            # ——连续对话丢音频。改为按代际校验：只有「没有新连接建立过」才复位。
            if sess.get("gen", 0) == my_gen:
                conn.reset_audio_states()
            else:
                logger.bind(tag=TAG).info(
                    f"ASR结果转发任务收尾：会话已重建连接(gen {my_gen}→{sess.get('gen', 0)})，"
                    "不复位音频状态"
                )

    def stop_ws_connection(self):
        # base 在本连接 ASR 完成后调用（无 conn 参数），只关闭触发链路的会话，
        # 避免误关其他正在识别的连接
        s = self._sessions.get(self._current_sid)
        if s and s["ws"]:
            asyncio.create_task(s["ws"].close())
            s["ws"] = None
        if s:
            s["processing"] = False

    async def _send_stop_request(self):
        """发送最后一个音频帧以通知服务器结束"""
        s = self._sessions.get(self._current_sid)
        if s and s["ws"]:
            try:
                # 发送结束标记的音频帧（gzip压缩的空数据）
                empty_payload = gzip.compress(b"")
                last_audio_request = bytearray(
                    self.generate_last_audio_default_header()
                )
                last_audio_request.extend(len(empty_payload).to_bytes(4, "big"))
                last_audio_request.extend(empty_payload)
                await s["ws"].send(last_audio_request)
                logger.bind(tag=TAG).debug("已发送结束音频帧")
            except Exception as e:
                logger.bind(tag=TAG).debug(f"发送结束音频帧时出错: {e}")

    def construct_request(self, reqid):
        req = {
            "app": {
                "appid": self.appid,
                "cluster": self.cluster,
                "token": self.access_token,
            },
            "user": {"uid": self.uid},
            "request": {
                "reqid": reqid,
                "workflow": self.workflow,
                "show_utterances": True,
                "result_type": self.result_type,
                "sequence": 1,
                "boosting_table_name": self.boosting_table_name,
                "correct_table_name": self.correct_table_name,
                "end_window_size": self.end_window_size,
            },
            "audio": {
                "format": self.format,
                "codec": self.codec,
                "rate": self.rate,
                "bits": self.bits,
                "channel": self.channel,
                "sample_rate": self.rate,
            },
        }

        # language参数仅在多语种模式下添加
        if self.enable_multilingual and self.language:
            req["audio"]["language"] = self.language

        logger.bind(tag=TAG).debug(
            f"构造请求参数: {json.dumps(req, ensure_ascii=False)}"
        )
        return req

    def token_auth(self):
        return {
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": "volc.bigasr.sauc.duration",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def apikey_auth(self):
        """新版方舟 API Key 认证：只需 X-Api-Key + X-Api-Resource-Id"""
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def generate_header(
        self,
        version=0x01,
        message_type=0x01,
        message_type_specific_flags=0x00,
        serial_method=0x01,
        compression_type=0x01,
        reserved_data=0x00,
        extension_header: bytes = b"",
    ):
        header = bytearray()
        header_size = int(len(extension_header) / 4) + 1
        header.append((version << 4) | header_size)
        header.append((message_type << 4) | message_type_specific_flags)
        header.append((serial_method << 4) | compression_type)
        header.append(reserved_data)
        header.extend(extension_header)
        return header

    def generate_audio_default_header(self):
        return self.generate_header(
            version=0x01,
            message_type=0x02,
            message_type_specific_flags=0x00,
            serial_method=0x01,
            compression_type=0x01,
        )

    def generate_last_audio_default_header(self):
        return self.generate_header(
            version=0x01,
            message_type=0x02,
            message_type_specific_flags=0x02,
            serial_method=0x01,
            compression_type=0x01,
        )

    def parse_response(self, res: bytes) -> dict:
        try:
            # 检查响应长度
            if len(res) < 4:
                logger.bind(tag=TAG).error(f"响应数据长度不足: {len(res)}")
                return {"error": "响应数据长度不足"}

            # 获取消息头
            header = res[:4]
            message_type = header[1] >> 4

            # 如果是错误响应
            if message_type == 0x0F:  # SERVER_ERROR_RESPONSE
                code = int.from_bytes(res[4:8], "big", signed=False)
                msg_length = int.from_bytes(res[8:12], "big", signed=False)
                error_msg = json.loads(res[12:].decode("utf-8"))
                return {
                    "code": code,
                    "msg_length": msg_length,
                    "payload_msg": error_msg,
                }

            # 获取JSON数据（跳过12字节头部）
            try:
                json_data = res[12:].decode("utf-8")
                result = json.loads(json_data)
                logger.bind(tag=TAG).debug(f"成功解析JSON响应: {result}")
                return {"payload_msg": result}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.bind(tag=TAG).error(f"JSON解析失败: {str(e)}")
                logger.bind(tag=TAG).error(f"原始数据: {res}")
                raise

        except Exception as e:
            logger.bind(tag=TAG).error(f"解析响应失败: {str(e)}")
            logger.bind(tag=TAG).error(f"原始响应数据: {res.hex()}")
            raise

    async def speech_to_text(self, opus_data, session_id, audio_format, artifacts=None):
        s = self._sessions.get(session_id)
        result = s["text"] if s else ""
        if s:
            s["text"] = ""  # 清空text
        return result, None

    async def close(self):
        """资源清理（某连接断开时触发；provider 是全局单例，不能误伤其他连接）。

        只清扫已结束的会话：活动会话的转发任务自己会监听 conn.stop_event 并退出，
        无需在这里取消其任务（取消了反而会掐断其他正在识别的连接）。
        """
        for sid, s in list(self._sessions.items()):
            task_done = s["task"] is None or s["task"].done()
            try:
                ws_dead = s["ws"] is None or s["ws"].closed
            except Exception:
                ws_dead = True
            if task_done and ws_dead:
                self._sessions.pop(sid, None)
            elif task_done and not ws_dead:
                try:
                    await s["ws"].close()
                except Exception:
                    pass
                s["ws"] = None
                s["processing"] = False
        # 注意：不能置空 decoder——任何连接断开都会调用本方法，
        # 置空后所有连接永远识别失败（2026-09-14 实测：
        # 'NoneType' object has no attribute 'decode'）
