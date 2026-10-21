"""MCP接入点处理器 - HTTP 协议版本"""

import json
import asyncio
from config.logger import setup_logging
from .mcp_endpoint_client import MCPEndpointClient

TAG = __name__
logger = setup_logging()


async def connect_mcp_endpoint(mcp_endpoint_url: str, conn=None) -> MCPEndpointClient:
    """连接到MCP接入点（HTTP协议）
    
    Args:
        mcp_endpoint_url: MCP 端点 URL，支持格式：
            - http://host:port/api/mcp
            - ws://host:port/api/mcp (自动转换为 http)
            - 带 ?token=xxx 参数
    """
    if not mcp_endpoint_url or "你的" in mcp_endpoint_url or mcp_endpoint_url == "null":
        return None

    try:
        # 解析 URL，提取 token 和转换协议
        url, token = _parse_mcp_url(mcp_endpoint_url)
        
        # 创建客户端
        mcp_client = MCPEndpointClient(conn)
        mcp_client.configure(url, token)

        # 发送初始化请求
        await send_mcp_endpoint_initialize(mcp_client)

        # 发送初始化完成通知
        await send_mcp_endpoint_notification(mcp_client, "notifications/initialized")

        # 获取工具列表
        await send_mcp_endpoint_tools_list(mcp_client)

        logger.bind(tag=TAG).info("MCP接入点连接成功（HTTP协议）")
        return mcp_client

    except Exception as e:
        logger.bind(tag=TAG).error(f"连接MCP接入点失败: {e}")
        return None


def _parse_mcp_url(url: str) -> tuple:
    """解析 MCP URL，提取 token 并转换协议
    
    Returns:
        (base_url, token) 元组
    """
    token = None
    
    # 提取 token 参数
    if "?" in url:
        base_part, query_part = url.split("?", 1)
        for param in query_part.split("&"):
            if param.startswith("token="):
                token = param[6:]
                break
        url = base_part
    
    # 转换 ws:// 为 http://
    if url.startswith("ws://"):
        url = "http://" + url[5:]
    elif url.startswith("wss://"):
        url = "https://" + url[6:]
    
    return url, token


async def send_mcp_endpoint_initialize(mcp_client: MCPEndpointClient):
    """发送MCP初始化请求"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "homebrain",
                "version": "1.0"
            }
        }
    }
    
    response = mcp_client.send_request(payload)
    
    if "result" in response:
        result = response["result"]
        server_info = result.get("serverInfo", {})
        logger.bind(tag=TAG).info(
            f"MCP接入点服务器信息: name={server_info.get('name')}, version={server_info.get('version')}"
        )
    else:
        logger.bind(tag=TAG).warning(f"MCP初始化响应异常: {response}")


async def send_mcp_endpoint_notification(mcp_client: MCPEndpointClient, method: str):
    """发送MCP通知（无响应）"""
    payload = {
        "jsonrpc": "2.0",
        "method": method
    }
    
    try:
        mcp_client.send_request(payload)
    except Exception as e:
        # 通知不需要响应，忽略错误
        logger.bind(tag=TAG).debug(f"MCP通知发送完成: {method}")


async def send_mcp_endpoint_tools_list(mcp_client: MCPEndpointClient):
    """获取MCP工具列表"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }
    
    response = mcp_client.send_request(payload)
    
    if "result" in response:
        result = response["result"]
        tools_data = result.get("tools", [])
        
        logger.bind(tag=TAG).info(f"MCP接入点支持的工具数量: {len(tools_data)}")
        
        for i, tool in enumerate(tools_data):
            if not isinstance(tool, dict):
                continue
            
            name = tool.get("name", "")
            description = tool.get("description", "")
            input_schema = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            
            if "inputSchema" in tool and isinstance(tool["inputSchema"], dict):
                schema = tool["inputSchema"]
                input_schema["type"] = schema.get("type", "object")
                input_schema["properties"] = schema.get("properties", {})
                input_schema["required"] = [
                    s for s in schema.get("required", []) if isinstance(s, str)
                ]
            
            new_tool = {
                "name": name,
                "description": description,
                "inputSchema": input_schema,
            }
            await mcp_client.add_tool(new_tool)
            logger.bind(tag=TAG).debug(f"MCP接入点工具 #{i+1}: {name}")
        
        # 替换工具描述中的工具名称
        for tool_data in mcp_client.tools.values():
            if "description" in tool_data:
                description = tool_data["description"]
                for sanitized_name, original_name in mcp_client.name_mapping.items():
                    description = description.replace(original_name, sanitized_name)
                tool_data["description"] = description
        
        await mcp_client.set_ready(True)
        logger.bind(tag=TAG).info(
            f"MCP接入点工具获取完成，共 {len(mcp_client.tools)} 个工具"
        )
        
        # 刷新工具缓存
        if (
            hasattr(mcp_client, "conn")
            and mcp_client.conn
            and hasattr(mcp_client.conn, "func_handler")
            and mcp_client.conn.func_handler
        ):
            mcp_client.conn.func_handler.tool_manager.refresh_tools()
            mcp_client.conn.func_handler.current_support_functions()
    else:
        logger.bind(tag=TAG).warning(f"MCP工具列表响应异常: {response}")


async def call_mcp_endpoint_tool(mcp_client: MCPEndpointClient, tool_name: str, arguments: str) -> str:
    """调用MCP工具
    
    Args:
        mcp_client: MCP 客户端
        tool_name: 工具名称（原始名称）
        arguments: JSON 格式的参数
    
    Returns:
        工具执行结果
    """
    # 获取原始工具名称
    original_name = mcp_client.name_mapping.get(tool_name, tool_name)
    
    # 解析参数
    try:
        args_dict = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        args_dict = {}
    
    # 构建请求
    msg_id = await mcp_client.get_next_id()
    payload = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {
            "name": original_name,
            "arguments": args_dict
        }
    }
    
    logger.bind(tag=TAG).info(f"调用MCP工具: {original_name}, 参数: {args_dict}")
    
    # 发送请求
    response = mcp_client.send_request(payload)
    
    # 处理响应
    if "result" in response:
        result = response["result"]
        # 提取文本内容
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "\n".join(texts)
        return json.dumps(result, ensure_ascii=False)
    elif "error" in response:
        error = response["error"]
        error_msg = error.get("message", "未知错误")
        logger.bind(tag=TAG).error(f"MCP工具调用失败: {error_msg}")
        raise Exception(f"MCP工具调用失败: {error_msg}")
    else:
        logger.bind(tag=TAG).warning(f"MCP工具调用响应异常: {response}")
        return json.dumps(response, ensure_ascii=False)
