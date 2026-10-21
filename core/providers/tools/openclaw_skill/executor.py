"""OpenClaw技能执行器 - 将技能说明注入到模型上下文"""

import subprocess
import os
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import Action, ActionResponse
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class OpenClawSkillExecutor(ToolExecutor):
    """OpenClaw技能执行器 - 将技能说明作为工具定义注入"""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config
        self._cached_tools: Optional[Dict[str, ToolDefinition]] = None
        self.logger = setup_logging()

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行OpenClaw技能"""
        self.logger.bind(tag=TAG).info(f"请求执行OpenClaw技能: {tool_name}, 参数: {arguments}")

        # 检查是否是动态生成的 HA 工具
        tools = self.get_tools()
        tool_def = tools.get(tool_name)
        if tool_def and tool_def.parameters and "ha_command" in tool_def.parameters:
            # 这是 HA 工具，转换为 mcporter 命令
            ha_command = tool_def.parameters["ha_command"]
            return await self._run_mcporter_command(ha_command, arguments)

        # 检查技能是否存在
        skill_path = os.path.expanduser(f"~/.openclaw/skills/{tool_name}")
        if not os.path.exists(skill_path):
            return ActionResponse(
                action=Action.NOTFOUND,
                response=f"技能 {tool_name} 不存在"
            )

        # 对 mcp-hass 技能实际执行 mcporter 命令（降级处理）
        if tool_name == "mcp-hass":
            return await self._execute_mcp_hass(arguments)

        # 读取技能说明
        skill_md_path = os.path.join(skill_path, "SKILL.md")
        if os.path.exists(skill_md_path):
            with open(skill_md_path, 'r') as f:
                skill_content = f.read()

            return ActionResponse(
                action=Action.REQLLM,
                result=f"技能 {tool_name} 的使用说明:\n{skill_content}\n\n用户请求参数: {arguments}",
                response=f"请根据技能说明执行 {tool_name} 技能"
            )

        return ActionResponse(
            action=Action.ERROR,
            response=f"技能 {tool_name} 缺少 SKILL.md 文件"
        )

    async def _execute_mcp_hass(self, arguments: Dict[str, Any]) -> ActionResponse:
        """实际执行 mcp-hass 技能的 mcporter 命令
        
        支持两种参数格式：
        1. 直接传递 mcporter 命令：{"command": "home-assistant.HassTurnOn", "args": {"name": "玩具灯"}}
        2. 自然语言查询：{"query": "开灯"} - 需要 LLM 先解析意图
        """
        # 检查是否有直接的命令参数
        command = arguments.get("command")
        args = arguments.get("args", {})
        
        if command:
            # 直接执行 mcporter 命令
            return await self._run_mcporter_command(command, args)
        
        # 自然语言查询 - 尝试解析常见意图
        query = arguments.get("query", "")
        if not query:
            return ActionResponse(
                action=Action.ERROR,
                response="缺少 query 或 command 参数"
            )
        
        # 简单的意图解析（可以根据需要扩展）
        return await self._parse_and_execute_intent(query)
    
    async def _parse_and_execute_intent(self, query: str) -> ActionResponse:
        """解析自然语言意图并执行对应的 HA 操作"""
        query_lower = query.lower()
        
        # 开灯相关
        if any(kw in query for kw in ["开灯", "打开灯", "亮灯", "开"]):
            # 提取设备名称（简单实现，可以改进）
            device_name = self._extract_device_name(query, ["灯", "台灯", "玩具灯", "吸顶灯"])
            if device_name:
                return await self._run_mcporter_command("home-assistant.HassTurnOn", {"name": device_name})
            else:
                return await self._run_mcporter_command("home-assistant.HassTurnOn", {"domain": ["light"]})
        
        # 关灯相关
        if any(kw in query for kw in ["关灯", "关闭灯", "熄灯", "关"]):
            device_name = self._extract_device_name(query, ["灯", "台灯", "玩具灯", "吸顶灯"])
            if device_name:
                return await self._run_mcporter_command("home-assistant.HassTurnOff", {"name": device_name})
            else:
                return await self._run_mcporter_command("home-assistant.HassTurnOff", {"domain": ["light"]})
        
        # 获取状态
        if any(kw in query for kw in ["状态", "信息", "怎么样", "如何"]):
            return await self._run_mcporter_command("home-assistant.GetLiveContext", {})
        
        return ActionResponse(
            action=Action.ERROR,
            response=f"无法解析意图: {query}。支持的命令: 开灯/关灯/获取状态"
        )
    
    def _extract_device_name(self, query: str, known_devices: list) -> Optional[str]:
        """从查询中提取设备名称"""
        for device in known_devices:
            if device in query:
                return device
        return None
    
    async def _run_mcporter_command(self, command: str, args: Dict[str, Any]) -> ActionResponse:
        """执行 mcporter 命令"""
        # 构建 mcporter 命令
        # 格式: mcporter call <command> key1: value1 key2: value2
        cmd_parts = ["mcporter", "call", command]
        
        for key, value in args.items():
            if isinstance(value, bool):
                cmd_parts.append(f"{key}: {str(value).lower()}")
            elif isinstance(value, list):
                # 数组参数 - 需要加引号防止 shell 解析出错
                arr_str = "[" + ", ".join(f'"{v}"' for v in value) + "]"
                cmd_parts.append(f"{key}: '{arr_str}'")
            elif isinstance(value, (int, float)):
                cmd_parts.append(f"{key}: {value}")
            else:
                # 字符串参数 - 不加引号，mcporter 会正确处理
                cmd_parts.append(f"{key}: {value}")
        
        mcporter_cmd = " ".join(cmd_parts)
        self.logger.bind(tag=TAG).info(f"执行 mcporter 命令: {mcporter_cmd}")
        
        try:
            # 确保环境变量已设置
            env = os.environ.copy()
            if not env.get("HASS_BASE_URL"):
                env["HASS_BASE_URL"] = 'YOUR_HA_BASE_URL'
            if not env.get("HASS_ACCESS_TOKEN"):
                # 从 config 中获取 token
                hass_config = self.config.get("plugins", {}).get("home_assistant", {})
                token = hass_config.get("api_key", "")
                if token and token != "你的home assistant api访问令牌":
                    env["HASS_ACCESS_TOKEN"] = token
            
            result = subprocess.run(
                mcporter_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=15,
                env=env
            )

            self.logger.bind(tag=TAG).info(f"mcporter 执行结果: returncode={result.returncode}, stdout={result.stdout[:200]}, stderr={result.stderr[:200]}")

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip() or "命令执行失败"
                return ActionResponse(
                    action=Action.ERROR,
                    response=f"HomeAssistant 控制失败: {error_msg}"
                )

            # 解析 JSON 结果，提取关键信息
            response_text = self._parse_ha_response(result.stdout.strip())
            
            return ActionResponse(
                action=Action.RESPONSE,
                response=response_text
            )

        except subprocess.TimeoutExpired:
            return ActionResponse(
                action=Action.ERROR,
                response="HomeAssistant 控制超时"
            )
        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=f"执行 mcporter 命令失败: {str(e)}"
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有OpenClaw技能，并读取其描述"""
        if self._cached_tools is not None:
            return self._cached_tools

        tools = {}
        
        # 1. 尝试从 HA 同步设备列表，生成动态工具
        ha_tools = self._generate_ha_tools()
        if ha_tools:
            tools.update(ha_tools)
            self.logger.bind(tag=TAG).info(f"从 HA 同步了 {len(ha_tools)} 个工具")
        else:
            # 降级：不注册假的 mcp-hass 工具——真实 HA 控制走 MCP 接入点
            # （此前降级注册的静态 mcp-hass 依赖不存在的 mcporter，
            #   LLM 选中后只会得到"技能 mcp-hass 不存在"，误导用户）
            self.logger.bind(tag=TAG).warning(
                "HA 工具同步失败，跳过降级 mcp-hass 注册（HA 控制由 MCP 接入点承担）"
            )
        
        # 2. 从数据库读取控制台已启用的技能
        try:
            import sqlite3
            db_path = os.path.expanduser("~/.openclaw/projects/homebrain/server/homebrain-server/data/devices.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT skill_id, config FROM skills_config WHERE enabled = 1 AND deleted = 0")
                    rows = cursor.fetchall()
                finally:
                    conn.close()

                for row in rows:
                    skill_id, config_str = row
                    # 跳过 mcp-hass，已经动态生成
                    if skill_id == "mcp-hass":
                        continue

                    # 从 SKILL.md 或 config 中提取描述
                    description_text = f"控制台技能: {skill_id}"
                    skill_path = os.path.expanduser(f"~/.openclaw/skills/{skill_id}")
                    skill_md_path = os.path.join(skill_path, "SKILL.md")

                    if os.path.exists(skill_md_path):
                        with open(skill_md_path, 'r') as f:
                            for line in f:
                                if line.startswith('description:'):
                                    description_text = line.split(':', 1)[1].strip()
                                    break

                    tools[skill_id] = ToolDefinition(
                        name=skill_id,
                        description={
                            "type": "function",
                            "function": {
                                "name": skill_id,
                                "description": description_text,
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string", "description": "查询参数"}
                                    },
                                    "required": []
                                }
                            }
                        },
                        tool_type=ToolType.OPENCLAW_SKILL,
                        parameters={}
                    )

                self.logger.bind(tag=TAG).info(f"从数据库加载了 {len(rows)} 个控制台技能")
            else:
                self.logger.bind(tag=TAG).warning(f"数据库文件不存在: {db_path}")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"从数据库加载控制台技能失败: {e}")

        self.logger.bind(tag=TAG).info(f"总共加载了 {len(tools)} 个工具: {list(tools.keys())}")
        self._cached_tools = tools
        return tools

    def _generate_ha_tools(self) -> Optional[Dict[str, ToolDefinition]]:
        """从 HA 同步设备列表，生成动态工具定义"""
        try:
            # 调用 mcporter 获取设备列表
            env = os.environ.copy()
            if not env.get("HASS_BASE_URL"):
                env["HASS_BASE_URL"] = 'YOUR_HA_BASE_URL'
            if not env.get("HASS_ACCESS_TOKEN"):
                hass_config = self.config.get("plugins", {}).get("home_assistant", {})
                token = hass_config.get("api_key", "")
                if token and token != "你的home assistant api访问令牌":
                    env["HASS_ACCESS_TOKEN"] = token
            
            result = subprocess.run(
                ["mcporter", "call", "home-assistant.GetLiveContext"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env
            )
            
            if result.returncode != 0:
                self.logger.bind(tag=TAG).warning(f"获取 HA 设备列表失败: {result.stderr}")
                return None
            
            # 解析设备列表
            import json
            data = json.loads(result.stdout)
            devices_text = data.get("result", "")
            
            # 提取设备信息
            devices = self._parse_devices(devices_text)
            if not devices:
                self.logger.bind(tag=TAG).warning("HA 设备列表为空")
                return None
            
            # 按类型分组设备
            lights = [n for n, d in devices.items() if d.get("domain") == "light"]
            covers = [n for n, d in devices.items() if d.get("domain") == "cover"]
            switches = [n for n, d in devices.items() if d.get("domain") == "switch"]
            sensors = [n for n, d in devices.items() if d.get("domain") == "sensor"]
            
            # 生成工具描述
            tools = {}
            
            # HassTurnOn - 开灯/开设备
            turn_on_desc = "打开设备。"
            if lights:
                turn_on_desc += f"灯具(可用name或domain=['light']): {', '.join(lights[:10])}。"
            if covers:
                turn_on_desc += f"窗帘(可用name或domain=['cover']): {', '.join(covers[:5])}。"
            turn_on_desc += "回复要简洁，如'已开灯'。"
            
            tools["HassTurnOn"] = ToolDefinition(
                name="HassTurnOn",
                description={
                    "type": "function",
                    "function": {
                        "name": "HassTurnOn",
                        "description": turn_on_desc,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "设备名称，如'射灯一'"},
                                "area": {"type": "string", "description": "区域名称"},
                                "domain": {"type": "array", "items": {"type": "string"}, "description": "设备类型，如['light']控制所有灯"}
                            },
                            "required": []
                        }
                    }
                },
                tool_type=ToolType.OPENCLAW_SKILL,
                parameters={"ha_command": "home-assistant.HassTurnOn"}
            )
            
            # HassTurnOff - 关灯/关设备
            turn_off_desc = "关闭设备。"
            if lights:
                turn_off_desc += f"灯具(可用name或domain=['light']): {', '.join(lights[:10])}。"
            if covers:
                turn_off_desc += f"窗帘(可用name或domain=['cover']): {', '.join(covers[:5])}。"
            turn_off_desc += "回复要简洁，如'已关灯'。"
            
            tools["HassTurnOff"] = ToolDefinition(
                name="HassTurnOff",
                description={
                    "type": "function",
                    "function": {
                        "name": "HassTurnOff",
                        "description": turn_off_desc,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "设备名称，如'射灯一'"},
                                "area": {"type": "string", "description": "区域名称"},
                                "domain": {"type": "array", "items": {"type": "string"}, "description": "设备类型，如['light']控制所有灯"}
                            },
                            "required": []
                        }
                    }
                },
                tool_type=ToolType.OPENCLAW_SKILL,
                parameters={"ha_command": "home-assistant.HassTurnOff"}
            )
            
            # HassLightSet - 设置灯光
            if lights:
                light_set_desc = f"设置灯光。支持：亮度(0-100)、颜色(red/blue/green等)、色温(2000-6500K，2700K暖黄/4000K自然/6500K冷白)。灯具: {', '.join(lights[:10])}。回复简洁。"
                tools["HassLightSet"] = ToolDefinition(
                    name="HassLightSet",
                    description={
                        "type": "function",
                        "function": {
                            "name": "HassLightSet",
                            "description": light_set_desc,
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "灯具名称，如'射灯一'，或用domain:['light']控制所有灯"},
                                    "brightness": {"type": "integer", "description": "亮度 0-100"},
                                    "color": {"type": "string", "description": "颜色名称，如'red','blue','warm_yellow'"},
                                    "temperature": {"type": "integer", "description": "色温(K)，2700=暖黄，4000=自然白，6500=冷白"}
                                },
                                "required": []
                            }
                        }
                    },
                    tool_type=ToolType.OPENCLAW_SKILL,
                    parameters={"ha_command": "home-assistant.HassLightSet"}
                )
            
            self.logger.bind(tag=TAG).info(f"HA 设备同步成功: {len(devices)} 个设备")
            return tools
            
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"生成 HA 工具失败: {e}")
            return None

    def _parse_ha_response(self, response_text: str) -> str:
        """解析 HA 的 JSON 响应，返回简洁的文本"""
        import json
        try:
            data = json.loads(response_text)
            
            # 检查是否是 action_done 类型的响应
            if data.get("response_type") == "action_done":
                result_data = data.get("data", {})
                success_list = result_data.get("success", [])
                failed_list = result_data.get("failed", [])
                
                if success_list and not failed_list:
                    # 全部成功
                    names = [item.get("name", "") for item in success_list]
                    if len(names) == 1:
                        return f"{names[0]}已操作"
                    else:
                        return f"{len(names)}个设备已操作"
                elif failed_list and not success_list:
                    # 全部失败
                    names = [item.get("name", "") for item in failed_list]
                    return f"设备{','.join(names)}未找到"
                else:
                    # 部分成功部分失败
                    return f"{len(success_list)}个成功，{len(failed_list)}个失败"
            
            # 其他类型的响应，返回默认成功消息
            return "操作已完成"
            
        except json.JSONDecodeError:
            # [2026-10-13 工具错误人话化修复] 非 JSON 不一定是无害文本：
            # mcporter CLI 会把错误打在 stdout 且 returncode 仍为 0，
            # 例如 "Error calling tool: <MatchFailedError result=MatchTargetsResult(..."，
            # 此前这段 906 字 Python repr 会被当成功回复直接进 TTS 念出（主人实测踩坑）。
            # 技术错误文本 → 原文进日志，对外换成一句人话；普通短文本照旧透传。
            text = (response_text or "").strip()
            if not text:
                return "操作已成功执行"
            if self._looks_like_technical_error(text):
                self.logger.bind(tag=TAG).error(
                    f"[HA 控制失败] mcporter stdout 原文（前500字）: {text[:500]}"
                )
                return "控制没有成功，没有找到目标设备或者设备没有响应"
            return text

    # [2026-10-13 工具错误人话化修复] mcporter/HA 技术错误输出的特征词
    _TECH_ERROR_HINTS = (
        "Error calling tool",
        "Traceback",
        "MatchFailedError",
        "no_match_reason",
        "Exception",
        "Error:",
        "error:",
        "result=",
    )

    def _looks_like_technical_error(self, text: str) -> bool:
        """判断文本是否为技术错误输出（不应播报给用户的内容）"""
        if any(hint in text for hint in self._TECH_ERROR_HINTS):
            return True
        # 非 JSON 且英文字符占一半以上，基本是报错堆栈/repr；正常业务提示是中文
        if len(text) >= 4:
            ascii_ratio = sum(1 for c in text if c.isascii()) / len(text)
            if ascii_ratio > 0.5:
                return True
        return False

    def _parse_devices(self, text: str) -> Dict[str, Dict]:
        """解析 HA 设备列表文本"""
        devices = {}
        current_name = None
        current_domain = None
        current_area = None
        
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('- names:'):
                if current_name:
                    devices[current_name] = {"domain": current_domain, "area": current_area}
                current_name = line.replace('- names:', '').strip()
                current_domain = None
                current_area = None
            elif line.startswith('domain:'):
                current_domain = line.replace('domain:', '').strip().strip("'")
            elif line.startswith('areas:'):
                current_area = line.replace('areas:', '').strip()
        
        if current_name:
            devices[current_name] = {"domain": current_domain, "area": current_area}
        
        return devices

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的OpenClaw技能"""
        tools = self.get_tools()
        return tool_name in tools

    def refresh_tools(self):
        """刷新技能缓存"""
        self._cached_tools = None
        self.logger.bind(tag=TAG).debug("OpenClaw技能缓存已刷新")