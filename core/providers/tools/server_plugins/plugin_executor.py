"""服务端插件工具执行器"""

import asyncio
import inspect
import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, Action, ActionResponse


# 同步函数里直接用 conn.loop.create_task 等「必须在事件循环线程里调」的工具，
# 不能丢进线程池。
_LOOP_BOUND_SYNC_TOOLS = {"play_music"}


def _obs_tool_call(conn, tool_name: str, t0: float, ok: bool, error: str = "") -> None:
    """运行观察（旁路，2026-10-18 P6）：记一次工具调用，**绝不影响主流程**。"""
    try:
        from core.utils import observation as _obs
        _obs.bump("tool_call" if ok else "tool_call_failed", sample=tool_name if not ok else None)
        _obs.note_tool(
            tool_name,
            latency_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
            ok=ok,
            device_id=getattr(conn, "device_id", "") or "",
            error=error,
        )
    except Exception:
        pass


class ServerPluginExecutor(ToolExecutor):
    """服务端插件工具执行器"""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端插件工具"""
        _t0 = time.monotonic()
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            _obs_tool_call(conn, tool_name, _t0, ok=False, error="not_found")
            return ActionResponse(
                action=Action.NOTFOUND, response=f"插件函数 {tool_name} 不存在"
            )

        try:
            # 根据工具类型决定如何调用
            call_conn = False
            if hasattr(func_item, "type"):
                # 4/5/3：SYSTEM_CTL、IOT_CTL、CHANGE_SYS_PROMPT 需要 conn 参数
                call_conn = func_item.type.code in [3, 4, 5]
            args = (conn,) if call_conn else ()

            if inspect.iscoroutinefunction(func_item.func):
                result = func_item.func(*args, **arguments)
            elif tool_name in _LOOP_BOUND_SYNC_TOOLS:
                result = func_item.func(*args, **arguments)
            else:
                # 2026-10-11：同步插件（requests 5~10s 超时的天气/HA/摄像头等）原先直接跑在
                # 事件循环上，整个连接的音频接收、TTS 发送、WS 消息全部冻住。
                # 放到线程里跑，事件循环不再被工具阻塞。
                result = await asyncio.to_thread(func_item.func, *args, **arguments)

            # 如果结果是协程，需要 await
            if inspect.iscoroutine(result):
                result = await result

            _obs_tool_call(conn, tool_name, _t0, ok=True)
            return result

        except Exception as e:
            _obs_tool_call(conn, tool_name, _t0, ok=False, error=str(e))
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有注册的服务端插件工具"""
        tools = {}

        # 获取必要的函数
        necessary_functions = ["handle_exit_intent", "get_lunar"]

        # 获取配置中的函数
        config_functions = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ].get("functions", [])

        # 转换为列表
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                config_functions = []

        # 合并所有需要的函数
        all_required_functions = list(set(necessary_functions + config_functions))

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item:
                # RAGflow 工具描述随知识库品类动态刷新（重建工具缓存前刷新）
                if func_name == "search_from_ragflow":
                    try:
                        from plugins_func.functions.search_from_ragflow import (
                            refresh_ragflow_tool_description,
                        )

                        refresh_ragflow_tool_description(self.conn, force=False)
                    except Exception:
                        pass

                # 从函数注册中获取描述
                fun_description = (
                    self.config.get("plugins", {})
                    .get(func_name, {})
                    .get("description", "")
                )
                if fun_description is not None and len(fun_description) > 0:
                    if "function" in func_item.description and isinstance(
                        func_item.description["function"], dict
                    ):
                        func_item.description["function"][
                            "description"
                        ] = fun_description
                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=func_item.description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端插件工具"""
        return tool_name in all_function_registry
