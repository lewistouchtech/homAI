"""
智能家居设备状态API
从HomeAssistant获取实时设备状态，供看板和控制台使用

设备边界（2026-09-17 起）：
    与 ha_control 同源 —— 由 HA「暴露给语音助手(conversation)」的实体清单决定。
    原先 15 条硬编码 DEVICE_MAPPING 已删除：里面既有已不存在的
    switch.san_ceng_quan_guan（死配置），又漏掉了 HA 里可用的 67 个窗帘。
"""
import aiohttp
import asyncio
import time
from aiohttp import web

# HA 配置与控制逻辑统一来自 ha_control（含设备边界校验、service 解析、状态码校验）
from plugins_func.functions.ha_control import (
    HASS_BASE_URL, HASS_ACCESS_TOKEN, ha_turn_on, ha_turn_off,
)
from core.utils.ha_expose import fetch_exposed_entities, fetch_area_floor_map, get_cached_areas

HEADERS = {
    "Authorization": f"Bearer {HASS_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

# 全局缓存：避免短时间内重复请求 HomeAssistant
_summary_cache = {
    "data": None,
    "timestamp": 0,
    "lock": None,  # 用于请求合并
}
CACHE_TTL = 5  # 缓存5秒

# HA domain → 看板设备类型
_DOMAIN_TO_TYPE = {
    "light": "light",
    "cover": "curtain",
    "switch": "switch",
    "media_player": "media_player",
    "sensor": "sensor",
    "climate": "climate",
    "fan": "fan",
    "lock": "lock",
    "vacuum": "vacuum",
    "scene": "scene",
    "script": "script",
}

# 传感器单位推断（HA friendly_name 已提供中文名，这里只补单位）
_SENSOR_UNIT_HINTS = (
    ("wen_du", "°C"), ("temperature", "°C"),
    ("shi_du", "%"), ("humidity", "%"),
    ("dian_chi", "%"), ("battery", "%"),
)


def build_device_mapping(entities: list) -> dict:
    """由 HA 暴露清单派生看板设备映射。

    2026-09-17：取代原先 15 条硬编码 DEVICE_MAPPING。
    """
    mapping = {}
    for e in entities or []:
        eid = e.get("entity_id", "")
        if not eid:
            continue
        domain = e.get("domain") or eid.split(".")[0]
        entry = {
            "type": _DOMAIN_TO_TYPE.get(domain, "unknown"),
            "name": e.get("name") or eid,
        }
        if entry["type"] == "sensor":
            unit = ""
            for key, u in _SENSOR_UNIT_HINTS:
                if key in eid:
                    unit = u
                    break
            entry["unit"] = unit
        mapping[eid] = entry
    return mapping


async def get_device_mapping() -> dict:
    """获取当前设备映射（走 ha_expose 的 TTL 缓存，不会每句话都打 HA）。

    2026-09-29：附加 HA 楼层/区域归属，供前端按「楼层→房间」分组渲染。
    区域查不到的设备 room 保持缺省（前端兜底为「未分配」）。
    """
    entities = await fetch_exposed_entities(HASS_BASE_URL, HASS_ACCESS_TOKEN)
    mapping = build_device_mapping(entities)
    try:
        area_map = await fetch_area_floor_map(HASS_BASE_URL, HASS_ACCESS_TOKEN)
    except Exception:
        area_map = {}
    for eid, info in area_map.items():
        if eid in mapping and info.get("room"):
            mapping[eid]["room"] = info["room"]
            if info.get("floor"):
                mapping[eid]["floor"] = info["floor"]

    # 关键词兜底：HA 实体/设备未设区域时，若设备名里直接带区域名（如「客厅手动窗帘」
    # 含「客厅」），按 HA 现有区域名归组。只匹配 HA 真实存在的区域，不虚构房间。
    try:
        areas = get_cached_areas()
    except Exception:
        areas = []
    if areas:
        for eid, entry in mapping.items():
            if entry.get("room"):
                continue
            label = f"{entry.get('name','')} {eid}"
            for a in areas:
                room = a.get("room") or ""
                if len(room) >= 2 and room in label:
                    entry["room"] = room
                    if a.get("floor"):
                        entry["floor"] = a["floor"]
                    break
    return mapping


async def fetch_ha_state(entity_id: str) -> dict:
    """获取单个设备状态"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HASS_BASE_URL}/api/states/{entity_id}",
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    return await response.json()
                return {"error": f"HTTP {response.status}"}
    except Exception as e:
        return {"error": str(e)}


async def fetch_all_ha_states() -> list:
    """获取 HA 暴露给语音助手的设备状态（设备边界与 ha_control 同源）。

    2026-09-17：边界改为 HA 暴露清单（不再用硬编码 ALLOWED_ENTITIES）。
    HA 不可达时 ha_expose 回退到上次缓存；无缓存则返回空（不再 fail-open 返回全量）。
    """
    entities = await fetch_exposed_entities(HASS_BASE_URL, HASS_ACCESS_TOKEN)
    wanted = {e["entity_id"] for e in entities}
    if not wanted:
        return []
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(
                f"{HASS_BASE_URL}/api/states",
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    return []
                all_states = await response.json()
                return [s for s in all_states if s.get("entity_id") in wanted]
    except Exception as e:
        print(f"[SmartHome API] 获取HA状态失败: {e}")
        return []


def parse_device_status(entity_id: str, ha_state: dict, device_mapping: dict = None) -> dict:
    """解析设备状态（device_mapping 由 HA 暴露清单动态派生）"""
    if not ha_state or "error" in ha_state:
        return {"error": "无法获取状态"}

    mapping = (device_mapping or {}).get(entity_id, {})
    device_type = mapping.get("type", "unknown")
    state = ha_state.get("state", "unknown")
    attributes = ha_state.get("attributes", {})

    result = {
        "entity_id": entity_id,
        "name": mapping.get("name", attributes.get("friendly_name", entity_id)),
        # 前端看板按 domain 分组统计（light/cover/climate/media_player...），
        # 2026-09-29 修复：之前只给 type 不给 domain，分组全部落空、数量显示错误
        "domain": entity_id.split(".", 1)[0],
        "type": device_type,
        "room": mapping.get("room", "未知"),
        # 楼层（HA area→floor 归属），没有归属时为空串，前端兜底「未分配」
        "floor": mapping.get("floor", ""),
        "state": state,
        "last_updated": ha_state.get("last_updated"),
    }

    # 根据设备类型添加特定字段
    if device_type == "light":
        result["on"] = state == "on"
        result["brightness"] = attributes.get("brightness", 0)
        result["color_temp"] = attributes.get("color_temp")
        result["supported_features"] = attributes.get("supported_features", 0)

    elif device_type == "curtain":
        result["position"] = attributes.get("current_position", 0)
        result["on"] = state == "open"
        result["state_text"] = "已开启" if state == "open" else "已关闭" if state == "closed" else "停止"

    elif device_type == "switch":
        result["on"] = state == "on"

    elif device_type == "sensor":
        result["value"] = state
        result["unit"] = mapping.get("unit", attributes.get("unit_of_measurement", ""))

    elif device_type == "weather":
        result["temperature"] = attributes.get("temperature")
        result["humidity"] = attributes.get("humidity")
        result["condition"] = attributes.get("condition", state)

    return result


# 安防设备实体 ID 模式匹配
SECURITY_PATTERNS = {
    "门锁": ["lock.", "binary_sensor.door", "binary_sensor.lock"],
    "门窗磁": ["binary_sensor.door_window", "binary_sensor.contact", "binary_sensor.window"],
    "人体感应": ["binary_sensor.motion", "binary_sensor.occupancy", "binary_sensor.pir"],
    "烟雾": ["binary_sensor.smoke", "sensor.smoke", "binary_sensor.fire"],
    "漏水": ["binary_sensor.water", "binary_sensor.leak", "sensor.water_leak"],
    "燃气": ["binary_sensor.gas", "sensor.gas", "binary_sensor.co"],
}

# 家电设备实体 ID 模式匹配
APPLIANCE_PATTERNS = {
    "洗衣机": ["switch.washer", "sensor.washer", "binary_sensor.washer"],
    "冰箱": ["sensor.fridge", "sensor.refrigerator", "climate.fridge"],
    "扫地机": ["vacuum.", "sensor.vacuum", "binary_sensor.vacuum"],
    "烤箱": ["switch.oven", "sensor.oven", "climate.oven"],
    "空调": ["climate.", "switch.ac", "sensor.air_conditioner"],
    "电视": ["media_player.tv", "switch.tv", "media_player.television"],
    "新风": ["fan.fresh", "switch.fresh_air", "sensor.fresh_air"],
    "净化器": ["fan.air_purifier", "switch.purifier", "sensor.air_purifier"],
}


def match_entity_patterns(entity_id: str, patterns: list) -> bool:
    """检查实体 ID 是否匹配模式列表"""
    eid_lower = entity_id.lower()
    for pattern in patterns:
        if pattern.lower() in eid_lower:
            return True
    return False


def classify_security_device(entity_id: str, state: str, attributes: dict) -> dict:
    """分类安防设备并返回状态"""
    eid_lower = entity_id.lower()
    state_lower = state.lower()

    for name, patterns in SECURITY_PATTERNS.items():
        if match_entity_patterns(entity_id, patterns):
            # 判断状态
            if name == "门锁":
                is_locked = state_lower in ["locked", "on", "closed"]
                return {"name": name, "status": "已锁定" if is_locked else "未锁定", "ok": is_locked, "entity_id": entity_id, "state": state}
            elif name == "门窗磁":
                is_closed = state_lower in ["off", "closed", "normal"]
                return {"name": name, "status": "正常" if is_closed else "开启", "ok": is_closed, "entity_id": entity_id, "state": state}
            elif name == "人体感应":
                has_motion = state_lower in ["on", "detected", "motion"]
                return {"name": name, "status": "有人" if has_motion else "无人", "ok": not has_motion, "entity_id": entity_id, "state": state}
            elif name == "烟雾":
                has_smoke = state_lower in ["on", "detected", "alarm"]
                return {"name": name, "status": "正常" if not has_smoke else "报警", "ok": not has_smoke, "entity_id": entity_id, "state": state}
            elif name == "漏水":
                has_leak = state_lower in ["on", "detected", "wet"]
                return {"name": name, "status": "正常" if not has_leak else "漏水", "ok": not has_leak, "entity_id": entity_id, "state": state}
            elif name == "燃气":
                has_gas = state_lower in ["on", "detected", "alarm"]
                return {"name": name, "status": "正常" if not has_gas else "报警", "ok": not has_gas, "entity_id": entity_id, "state": state}

    return None


def classify_appliance_device(entity_id: str, state: str, attributes: dict) -> dict:
    """分类家电设备并返回状态"""
    eid_lower = entity_id.lower()
    state_lower = state.lower()

    for name, patterns in APPLIANCE_PATTERNS.items():
        if match_entity_patterns(entity_id, patterns):
            if name == "洗衣机":
                is_running = state_lower in ["on", "running", "washing"]
                return {"name": name, "status": "运行中" if is_running else "空闲", "entity_id": entity_id, "state": state}
            elif name == "冰箱":
                return {"name": name, "status": f"{state}°" if state.replace('.','').replace('-','').isdigit() else "正常", "entity_id": entity_id, "state": state}
            elif name == "扫地机":
                if state_lower in ["cleaning", "on", "running"]:
                    return {"name": name, "status": "清扫中", "entity_id": entity_id, "state": state}
                elif state_lower in ["docked", "off", "idle"]:
                    return {"name": name, "status": "充电中", "entity_id": entity_id, "state": state}
                return {"name": name, "status": state, "entity_id": entity_id, "state": state}
            elif name == "烤箱":
                is_on = state_lower in ["on", "heating", "cooking"]
                return {"name": name, "status": "运行中" if is_on else "空闲", "entity_id": entity_id, "state": state}
            elif name == "空调":
                is_on = state_lower not in ["off", "unavailable", "unknown"]
                return {"name": name, "status": f"开启 · {state}°" if is_on and state.replace('.','').replace('-','').isdigit() else ("开启" if is_on else "关闭"), "entity_id": entity_id, "state": state}
            elif name == "电视":
                is_on = state_lower in ["on", "playing", "paused"]
                return {"name": name, "status": "开启" if is_on else "关闭", "entity_id": entity_id, "state": state}
            elif name == "新风":
                is_on = state_lower in ["on", "running"]
                return {"name": name, "status": "运行中" if is_on else "关闭", "entity_id": entity_id, "state": state}
            elif name == "净化器":
                is_on = state_lower in ["on", "running"]
                return {"name": name, "status": "运行中" if is_on else "关闭", "entity_id": entity_id, "state": state}

    return None


async def get_smart_home_summary() -> dict:
    """获取智能家居概览 - 带缓存和请求合并"""
    global _summary_cache

    # 1. 检查缓存是否有效
    current_time = time.time()
    if _summary_cache["data"] and (current_time - _summary_cache["timestamp"]) < CACHE_TTL:
        return _summary_cache["data"]

    # 2. 请求合并：如果已有请求在进行，等待它完成
    if _summary_cache["lock"] is not None:
        try:
            # 等待正在进行的请求完成（最多等待10秒）
            await asyncio.wait_for(_summary_cache["lock"], timeout=10)
            # 请求完成后，再次检查缓存
            if _summary_cache["data"]:
                return _summary_cache["data"]
        except asyncio.TimeoutError:
            pass  # 超时，继续发起新请求

    # 3. 创建新的锁，发起新请求
    lock = asyncio.Future()
    _summary_cache["lock"] = lock

    try:
        # 实际获取数据
        result = await _fetch_smart_home_summary_internal()

        # 更新缓存
        _summary_cache["data"] = result
        _summary_cache["timestamp"] = current_time

        return result
    finally:
        # 释放锁
        if not lock.done():
            lock.set_result(True)
        if _summary_cache["lock"] is lock:
            _summary_cache["lock"] = None


async def _fetch_smart_home_summary_internal() -> dict:
    """内部函数：实际获取智能家居概览数据"""
    all_states = await fetch_all_ha_states()
    device_mapping = await get_device_mapping()

    # 过滤我们关心的设备
    devices = []
    light_on_count = 0
    light_total = 0
    curtain_open_count = 0
    curtain_total = 0
    sensor_data = {}
    security_devices = []
    appliance_devices = []

    for state in all_states:
        entity_id = state.get("entity_id", "")
        if entity_id in device_mapping:
            parsed = parse_device_status(entity_id, state, device_mapping)
            devices.append(parsed)

            # 统计灯光
            if parsed["type"] == "light":
                light_total += 1
                if parsed.get("on"):
                    light_on_count += 1

            # 统计窗帘
            elif parsed["type"] == "curtain":
                curtain_total += 1
                if parsed.get("on"):
                    curtain_open_count += 1

            # 收集传感器数据
            elif parsed["type"] == "sensor":
                sensor_data[parsed["name"]] = {
                    "value": parsed.get("value"),
                    "unit": parsed.get("unit", "")
                }

        # 扫描安防设备（不在 DEVICE_MAPPING 中的也扫描）
        sec = classify_security_device(entity_id, state.get("state", "unknown"), state.get("attributes", {}))
        if sec:
            security_devices.append(sec)

        # 扫描家电设备（不在 DEVICE_MAPPING 中的也扫描）
        app = classify_appliance_device(entity_id, state.get("state", "unknown"), state.get("attributes", {}))
        if app:
            appliance_devices.append(app)

    # 获取温湿度
    temp = sensor_data.get("温度", {}).get("value", "--")
    humidity = sensor_data.get("湿度", {}).get("value", "--")

    return {
        "success": True,
        "timestamp": asyncio.get_event_loop().time(),
        "summary": {
            "lights": {
                "on": light_on_count,
                "total": light_total,
                "text": f"{light_on_count}/{light_total} 开启"
            },
            "curtains": {
                "open": curtain_open_count,
                "total": curtain_total,
                "text": f"{curtain_open_count}/{curtain_total} 打开"
            },
            "environment": {
                "temperature": temp,
                "humidity": humidity,
                "text": f"{temp}° · {humidity}%"
            },
            "security": security_devices,
            "appliances": appliance_devices
        },
        "devices": devices
    }


# HTTP 处理函数
async def handle_smart_home_status(request):
    """处理 /api/smart-home/status 请求"""
    try:
        summary = await get_smart_home_summary()
        return web.json_response(summary)
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def handle_smart_home_devices(request):
    """处理 /api/smart-home/devices 请求 - 获取所有设备列表"""
    try:
        summary = await get_smart_home_summary()
        return web.json_response({
            "success": True,
            "devices": summary.get("devices", [])
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def handle_device_control(request):
    """处理 /api/smart-home/control 请求 - 控制设备。

    2026-09-17：控制逻辑统一委托给 ha_control —— 设备边界校验（HA 暴露清单）、
    domain→service 解析、HTTP 状态码校验都只有一份实现，避免与对话链路不一致。
    原先这里维护了第二份 domain→service 硬编码分支，且对不存在/离线设备返回假成功。
    """
    try:
        data = await request.json()
        entity_id = data.get("entity_id")
        action = data.get("action")  # turn_on, turn_off, toggle, set_position
        params = data.get("params", {}) or {}

        if not entity_id:
            return web.json_response({
                "success": False,
                "error": "缺少 entity_id"
            }, status=400)

        if action in ("turn_on", "toggle"):
            result = await ha_turn_on(entity_id, **params)
        elif action == "turn_off":
            result = await ha_turn_off(entity_id, **params)
        elif action == "set_position":
            result = await ha_turn_on(entity_id, position=params.get("position", 0))
        else:
            return web.json_response({
                "success": False,
                "error": f"不支持的动作: {action}"
            }, status=400)

        if isinstance(result, dict) and result.get("error"):
            return web.json_response({
                "success": False,
                "error": result["error"]
            }, status=502)

        return web.json_response({
            "success": True,
            "message": f"设备 {action} 操作成功"
        })

    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
