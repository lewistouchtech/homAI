# -*- coding: utf-8 -*-
"""批量设备控制插件（社区版）

一次调用控制多个 Home Assistant 设备。写法与 `ha_control` 一致：
每条指令写明设备实体 ID 与操作。

社区版实现直接走标准 HA REST API，**不依赖商业版的语义寻址引擎**。

> 想用「一句话控制一整间屋子的设备」这类语义寻址能力，请使用商业版。
"""
from typing import Any, Dict, List

from plugins_func.functions.ha_control import (
    HEADERS,
    HASS_ACCESS_TOKEN,
    HASS_BASE_URL,
    _HTTP_TIMEOUT,
    _friendly,
    _not_configured,
    _resolve_action,
    _short,
    ha_call_service,
)

BATCH_CONTROL_FUNC_DESC = {
    "type": "function",
    "function": {
        "name": "batch_device_control",
        "description": (
            "批量控制智能家居设备，一次调用多个设备。"
            "写法同 ha_control 的 commands：每条给 entity_id 和 action。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "description": "要批量执行的操作列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "description": "设备实体 ID，如 light.living_room"},
                            "action": {"type": "string", "description": "操作：开 / 关 / 播放 / 停止"},
                            "params": {"type": "object", "description": "额外参数"},
                        },
                        "required": ["entity_id", "action"],
                    },
                },
            },
            "required": ["commands"],
        },
    },
}


async def batch_device_control_async(commands: List[Dict[str, Any]]) -> str:
    """批量执行设备操作，返回人话播报文本"""
    if not commands:
        return "没有要执行的设备操作"
    tip = _not_configured()
    if tip:
        return tip

    done: List[str] = []
    failed: List[str] = []
    for cmd in commands:
        eid = str(cmd.get("entity_id") or "").strip()
        action = str(cmd.get("action") or "").strip()
        params = cmd.get("params") or {}
        if not eid:
            failed.append("有个设备没说是哪个")
            continue
        domain = eid.split(".")[0] if "." in eid else "switch"
        label = _friendly(eid)
        svc_domain, svc = _resolve_action(domain, action)
        res = await ha_call_service(svc_domain, svc, {"entity_id": eid, **params})
        if res.get("success"):
            done.append(f"{label}已{'打开' if svc.endswith('turn_on') else '调整'}")
        else:
            failed.append(f"{label}操作失败")

    parts = []
    if done:
        parts.append("，".join(done))
    if failed:
        parts.append("；".join(failed) + "失败了")
    return _short("。".join(parts) if parts else "没有执行任何操作")


batch_device_control_func = BATCH_CONTROL_FUNC_DESC
