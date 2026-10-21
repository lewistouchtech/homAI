"""MCP接入点客户端定义 - HTTP 协议版本"""

import asyncio
import requests
from concurrent.futures import Future
from core.utils.util import sanitize_tool_name
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class MCPEndpointClient:
    """MCP接入点客户端，用于管理MCP接入点状态和工具（HTTP协议）"""

    def __init__(self, conn=None):
        self.conn = conn
        self.tools = {}  # sanitized_name -> tool_data
        self.name_mapping = {}
        self.ready = False
        self.call_results = {}  # To store Futures for tool call responses
        self.next_id = 1
        self.lock = asyncio.Lock()
        self._cached_available_tools = None  # Cache for get_available_tools
        
        # HTTP 配置
        self.base_url = None
        self.headers = None

    def configure(self, url: str, token: str = None):
        """配置 HTTP MCP 端点
        
        Args:
            url: MCP 端点 URL，如 http://YOUR_HA_HOST:8123/api/mcp
            token: Bearer token（可选）
        """
        self.base_url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        
        logger.bind(tag=TAG).info(f"MCP HTTP 客户端配置完成: {url}")

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def get_available_tools(self) -> list:
        # Check if the cache is valid
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        # If cache is not valid, regenerate the list
        result = []
        for tool_name, tool_data in self.tools.items():
            function_def = {
                "name": tool_name,
                "description": tool_data["description"],
                "parameters": {
                    "type": tool_data["inputSchema"].get("type", "object"),
                    "properties": tool_data["inputSchema"].get("properties", {}),
                    "required": tool_data["inputSchema"].get("required", []),
                },
            }
            result.append({"type": "function", "function": function_def})

        self._cached_available_tools = result  # Store the generated list in cache
        return result

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    async def add_tool(self, tool_data: dict):
        async with self.lock:
            sanitized_name = sanitize_tool_name(tool_data["name"])
            self.tools[sanitized_name] = tool_data
            self.name_mapping[sanitized_name] = tool_data["name"]
            self._cached_available_tools = (
                None  # Invalidate the cache when a tool is added
            )

    async def get_next_id(self) -> int:
        async with self.lock:
            current_id = self.next_id
            self.next_id += 1
            return current_id

    async def register_call_result_future(self, id: int, future: Future):
        async with self.lock:
            self.call_results[id] = future

    async def resolve_call_result(self, id: int, result: any):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_result(result)

    async def reject_call_result(self, id: int, exception: Exception):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_exception(exception)

    async def cleanup_call_result(self, id: int):
        async with self.lock:
            if id in self.call_results:
                self.call_results.pop(id)

    def send_request(self, payload: dict) -> dict:
        """发送 HTTP POST 请求到 MCP 端点"""
        if not self.base_url:
            raise RuntimeError("MCP 客户端未配置，请先调用 configure()")
        
        try:
            # trust_env=False：不读系统/环境代理。MCP 接入点是内网地址
            # （192.168.x.x），走本机代理（Clash 等）会被错误转发导致 502。
            response = requests.post(
                self.base_url,
                json=payload,
                headers=self.headers,
                timeout=30,
                proxies={"http": None, "https": None},
            )
            response.raise_for_status()
            # 202（通知）或空 body 时无 JSON 内容，返回 None（notification 调用方不使用返回值）
            if response.status_code == 202 or not response.text.strip():
                return None
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.bind(tag=TAG).error(f"MCP HTTP 请求失败: {e}")
            raise

    async def close(self):
        """关闭客户端（HTTP 无需特殊处理）"""
        pass
