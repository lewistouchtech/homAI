"""
批量设备控制插件（core/home 引擎适配层，2026-10-13 重构）

一次调用控制多个设备。和 ha_control 的 commands 同一套寻址：只写中文设备名/房间/类型，
不要写 entity_id（大模型看不到真实编号，写了只会编错）。老调用方仍可传 entity_id，照样认。

原来的问题：成功与否按「ha_control 有没有返回文本」判断，设备离线也算成功（「成功控制 2/2」）。
现在直接用引擎的真实结果：离线/没接入/没确认到位都如实说。
"""
from typing import Any, Dict, List

print("[批量控制] 模块已加载: batch_device_control")
from plugins_func.register import ToolType, Action, ActionResponse, register_function
from plugins_func.functions.ha_control import ha_control_func
from core.home import engine

_ITEMS = ha_control_func.description["function"]["parameters"]["properties"]["commands"]["items"]

BATCH_DEVICE_CONTROL_DESC = {
    "type": "function",
    "function": {
        "name": "batch_device_control",
        "description": (
            "批量控制智能家居设备，一次调用多个设备。写法同 ha_control 的 commands："
            "只写中文设备名、房间、类型，不要写 entity_id。"
            "按心情/氛围组合场景请用 run_ai_scene 的 mood，不要用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "设备操作列表，每项一个动作。",
                    "items": _ITEMS,
                }
            },
            "required": ["operations"],
        },
    },
}


def _normalize(op: Dict[str, Any]) -> Dict[str, Any]:
    """兼容老字段：on/off、brightness(0-255)、color_temp(K)、position"""
    op = dict(op or {})
    act = str(op.get("action") or "turn_on").lower()
    if act == "turn_on" and op.get("brightness") is not None and op.get("value") is None:
        op["action"], op["value"] = "set_brightness", round(float(op.pop("brightness")) / 255 * 100)
    elif act == "turn_on" and op.get("color_temp") is not None and op.get("value") is None:
        op["action"], op["value"] = "set_color_temp", op.pop("color_temp")
    elif act == "turn_on" and op.get("position") is not None and op.get("value") is None:
        op["action"], op["value"] = "set_position", op.pop("position")
    return op


@register_function("batch_device_control", BATCH_DEVICE_CONTROL_DESC, ToolType.SYSTEM_CTL)
async def batch_device_control(conn, operations: List[Dict]) -> ActionResponse:
    """批量控制：整批交给引擎，按真实结果播报"""
    try:
        if not operations:
            return ActionResponse(Action.REQLLM, response="未提供设备操作列表")
        res = await engine.run(conn, commands=[_normalize(o) for o in operations])
        return ActionResponse(Action.RESPONSE, response=res.spoken[:300])
    except Exception as e:
        return ActionResponse(Action.REQLLM, response=f"批量控制失败: {e}")
