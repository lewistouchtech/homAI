import asyncio
import logging

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api_async
from core.auth import AuthManager, AuthenticationError
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update, check_tts_update
from core.api.config_api import set_websocket_server_instance

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.config_lock = asyncio.Lock()
        self.active_connections = set()  # 跟踪所有活跃的 WebSocket 连接
        self.connections_lock = asyncio.Lock()  # 保护 active_connections 的锁
        self._conn_seq = 0  # 连接注册序号，用于同设备多标签页时选出最新连接
        # 新增：设备ID到ConnectionHandler的映射（用于跨连接消息路由）
        self.device_handlers = {}  # device_id -> ConnectionHandler
        self.device_handlers_lock = asyncio.Lock()  # 保护 device_handlers 的锁

        # 设置 WebSocket 服务器实例，供 config_api 触发模块重载使用
        set_websocket_server_instance(self)

        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            "TTS" in self.config["selected_module"],
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    async def register_connection(self, websocket):
        """注册新的 WebSocket 连接"""
        async with self.connections_lock:
            self._conn_seq += 1
            websocket._homebrain_conn_seq = self._conn_seq
            self.active_connections.add(websocket)
            self.logger.bind(tag=TAG).info(f"连接已注册，当前活跃连接数: {len(self.active_connections)}")

    def get_stage_target_connections(self):
        """舞台类消息（嵌套/覆盖/相册等）的目标连接：每个设备只保留最新一条连接。

        同一浏览器开多个标签页会注册多条连接（device_id 相同），全部广播会让
        同一屏幕出现多个嵌套窗口。按注册序号取每个 device_id 最新的一条。
        """
        best = {}
        for ws in self.active_connections:
            handler = getattr(ws, "_homebrain_handler", None)
            device_id = getattr(handler, "device_id", None) or f"anon-{id(ws)}"
            seq = getattr(ws, "_homebrain_conn_seq", 0)
            cur = best.get(device_id)
            if cur is None or seq > getattr(cur, "_homebrain_conn_seq", 0):
                best[device_id] = ws
        return list(best.values())

    async def unregister_connection(self, websocket):
        """注销 WebSocket 连接"""
        async with self.connections_lock:
            self.active_connections.discard(websocket)
            self.logger.bind(tag=TAG).info(f"连接已注销，当前活跃连接数: {len(self.active_connections)}")

    async def register_device_handler(self, device_id: str, handler):
        """注册设备ID到ConnectionHandler的映射"""
        if device_id:
            async with self.device_handlers_lock:
                self.device_handlers[device_id] = handler
                self.logger.bind(tag=TAG).info(f"设备处理器已注册: device_id={device_id}, 当前注册设备数: {len(self.device_handlers)}")

    async def unregister_device_handler(self, device_id: str):
        """注销设备ID到ConnectionHandler的映射"""
        if device_id:
            async with self.device_handlers_lock:
                if device_id in self.device_handlers:
                    self.device_handlers.pop(device_id, None)
                    self.logger.bind(tag=TAG).info(f"设备处理器已注销: device_id={device_id}, 当前注册设备数: {len(self.device_handlers)}")

    async def get_device_handler(self, device_id: str):
        """根据设备ID获取ConnectionHandler"""
        if device_id:
            async with self.device_handlers_lock:
                return self.device_handlers.get(device_id)
        return None

    async def broadcast_message(self, message: str, exclude=None):
        """广播消息到所有活跃的 WebSocket 连接
        
        Args:
            message: 要广播的 JSON 字符串消息
            exclude: 要排除的连接（通常是发送音频的源连接）
        """
        async with self.connections_lock:
            connections = self.active_connections.copy()
        
        failed = []
        for ws in connections:
            if ws is exclude:
                continue
            try:
                await ws.send(message)
            except Exception as e:
                failed.append(ws)
                self.logger.bind(tag=TAG).debug(f"广播消息失败: {e}")
        
        # 清理失败的连接
        if failed:
            async with self.connections_lock:
                for ws in failed:
                    self.active_connections.discard(ws)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        # 创建服务器时禁用 TCP Nagle 算法
        import socket

        # [2026-10-14] 浏览器安全来源：https 页面不能连 ws://（混合内容会被浏览器拦掉），
        # 所以再起一个 wss 监听给网页端用。**原 9800 端口一个字不动**（ESP32 设备走它）。
        wss_ssl, wss_port = self._build_wss_ssl(server_config)

        async with websockets.serve(
            self._handle_connection, host, port,
            process_request=self._http_response,
            # 【修复】启用 ping 保持连接活跃，防止浏览器 WebSocket 超时断开
            # 30s ping 一次，60s 超时（兼容浏览器后台标签页省电模式，JS 节流导致 pong 延迟）
            ping_interval=30,
            ping_timeout=60,
            # 保持默认队列大小（16），避免阻塞
            # max_queue 默认为 16，足够处理实时音频
        ):
            if wss_ssl is None:
                await asyncio.Future()
            else:
                async with websockets.serve(
                    self._handle_connection, host, wss_port,
                    process_request=self._http_response,
                    ssl=wss_ssl,
                    ping_interval=30,
                    ping_timeout=60,
                ):
                    await asyncio.Future()

    def _build_wss_ssl(self, server_config):
        """[2026-10-14] 给网页端 wss 用的 SSLContext；不可用时返回 (None, None) 只记日志。

        证书与 https 端口共用一份（core/utils/tls_certs.py），缺失/损坏都不影响主链路。
        """
        from core.utils import tls_certs

        try:
            if not server_config.get("https_enabled", True):
                return None, None
            wss_port = int(server_config.get("wss_port", tls_certs.DEFAULT_WSS_PORT))
            if wss_port <= 0:
                return None, None
            tls_certs.ensure_self_signed()      # 不存在则生成（与 https 端口共用）
            ssl_context = tls_certs.ssl_context_for()
            if ssl_context is None:
                self.logger.bind(tag=TAG).error("证书读取失败，跳过 wss 监听（设备 ws 链路不受影响）")
                return None, None
            self.logger.bind(tag=TAG).info(f"WSS 监听启动在 wss://{server_config.get('ip', '0.0.0.0')}:{wss_port}")
            return ssl_context, wss_port
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"wss 监听启动失败（不影响设备 ws 链路）：{e}")
            return None, None

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # 尝试从 URL 的查询参数中获取 device-id
            from urllib.parse import parse_qs, urlparse

            # 从 WebSocket 请求中获取路径
            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send("端口正常，如需测试连接，请使用test_page.html")
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 先认证，后建立连接
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("认证失败")
            await websocket.close()
            return
        # 注册连接
        await self.register_connection(websocket)
        
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # 传入server实例
        )
        # J3：把 handler 挂到 websocket 上，供 bundle 热加载后刷新活跃连接的工具列表
        websocket._homebrain_handler = handler
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            # 注销连接
            await self.unregister_connection(websocket)
            # 强制关闭连接（如果还没有关闭的话）
            try:
                # 安全地检查WebSocket状态并关闭
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # 如果没有closed属性，直接尝试关闭
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _http_response(self, websocket, request_headers):
        # 检查是否为 WebSocket 升级请求
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            # 如果是 WebSocket 请求，返回 None 允许握手继续
            return None
        else:
            # 如果是普通 HTTP 请求，返回 "server is running"
            return websocket.respond(200, "Server is running\n")

    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                # 重新获取配置（优先从本地 config.yaml 读取最新配置）
                from config.config_loader import read_config, merge_configs
                import os
                
                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                config_path = os.path.join(project_dir, "config.yaml")
                
                # 读取最新本地配置
                if os.path.exists(config_path):
                    try:
                        new_config = read_config(config_path)
                        self.logger.bind(tag=TAG).info(f"从 {config_path} 重新加载配置")
                    except Exception as e:
                        self.logger.bind(tag=TAG).warning(f"读取本地配置失败: {e}，使用内存配置")
                        new_config = self.config.copy()
                else:
                    new_config = self.config.copy()

                # 合并运行时配置 data/.config.yaml（与启动时 load_config 行为一致，
                # 否则 selected_module 指向运行时独有的 provider 时会 KeyError）
                custom_config_path = os.path.join(project_dir, "data", ".config.yaml")
                if os.path.exists(custom_config_path):
                    try:
                        custom_config = read_config(custom_config_path)
                        if custom_config:
                            new_config = merge_configs(new_config, custom_config)
                            self.logger.bind(tag=TAG).info(f"已合并运行时配置 {custom_config_path}")
                    except Exception as e:
                        self.logger.bind(tag=TAG).warning(f"合并运行时配置失败: {e}")
                
                # 检查 VAD、ASR、TTS 类型是否需要更新
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                update_tts = check_tts_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查组件类型是否需要更新: VAD={update_vad}, ASR={update_asr}, TTS={update_tts}"
                )

                # 更新配置
                self.config = new_config

                # 重新初始化组件
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    update_tts,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # 更新组件实例
                if "vad" in modules:
                    self._vad = modules["vad"]
                if "asr" in modules:
                    self._asr = modules["asr"]
                if "tts" in modules:
                    self._tts = modules["tts"]
                if "llm" in modules:
                    self._llm = modules["llm"]
                if "intent" in modules:
                    self._intent = modules["intent"]
                if "memory" in modules:
                    self._memory = modules["memory"]
                self.logger.bind(tag=TAG).info(f"更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            # 2026-10-05：把失败原因存到属性上，供上层透出给后台。
            # 以前只 return False，后台只能显示「组件热更新失败（建议重启服务）」，
            # 主人完全看不出为什么——而重启往往并不能解决（如 provider 缺参数）。
            self.last_update_error = f"{type(e).__name__}: {e}"
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        # 先认证，后建立连接
        if self.auth_enable:
            headers = dict(websocket.request.headers)
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                # 如果属于白名单内的设备，不校验token，直接放行
                return
            else:
                # 否则校验token
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    token = token[7:]  # 移除'Bearer '前缀
                else:
                    raise AuthenticationError("Missing or invalid Authorization header")
                # 进行认证
                auth_success = self.auth.verify_token(
                    token, client_id=client_id, username=device_id
                )
                if not auth_success:
                    raise AuthenticationError("Invalid token")
