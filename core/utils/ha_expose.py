"""
HA 暴露实体同步
================

把 Home Assistant「暴露给语音助手(conversation)」的实体清单，作为 HomeBrain 唯一的设备边界。

设计原则（2026-09-17）：
- 设备范围由 HA 决定，不由 HomeBrain 硬编码。工程师在 HA 界面
  「设置 → 语音助手 → 暴露实体」里配置哪些设备可被语音控制。
- HA 里接了哪些（且暴露了哪些）设备，HomeBrain 就控哪些；HA 删掉设备，
  HomeBrain 自动跟着消失，不会残留死配置。
- 这取代了原先三套各自硬编码且互相漂移的清单：
  ha_control.ALLOWED_ENTITIES / smart_home_api.DEVICE_MAPPING / hass_init._CONTROLLABLE_DOMAINS

数据来源：
- WebSocket `homeassistant/expose_entity/list` → 暴露清单（entity_id）
- REST `GET /api/states`                     → friendly_name / state

注意：`expose_entity/list` 只有 WebSocket 接口，REST 不提供；因此本模块用
aiohttp 的 ws_connect 拿清单，再用 REST 补全名称与状态。
"""
import asyncio
import time

import aiohttp

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 缓存：5 分钟内不重复打 HA（与 hass_init 原有节奏一致）
EXPOSE_TTL = 300.0
_expose_cache = {"ts": 0.0, "entities": []}
_cache_lock = asyncio.Lock()

# domain → 中文分组名
DOMAIN_CN = {
    "light": "灯光", "switch": "开关", "cover": "窗帘/门窗", "fan": "风扇",
    "climate": "空调", "media_player": "媒体播放", "scene": "预设场景",
    "input_boolean": "状态开关", "humidifier": "加湿器", "vacuum": "扫地机",
    "lock": "门锁", "script": "脚本", "sensor": "传感器",
    "binary_sensor": "状态传感器", "camera": "摄像头",
}


def _to_ws_url(base_url: str) -> str:
    url = str(base_url).rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + "/api/websocket"
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):] + "/api/websocket"
    return "ws://" + url + "/api/websocket"


async def _ws_fetch_exposed_ids(base_url: str, api_key: str) -> set:
    """通过 WebSocket 拉取「暴露给 conversation 助手」的 entity_id 集合。"""
    ws_url = _to_ws_url(base_url)
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.ws_connect(ws_url) as ws:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"HA WebSocket 握手异常: {msg.get('type')}")

            await ws.send_json({"type": "auth", "access_token": api_key})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA WebSocket 认证失败: {msg.get('type')}")

            await ws.send_json({"id": 1, "type": "homeassistant/expose_entity/list"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if msg.get("success") is False:
                raise RuntimeError(f"expose_entity/list 调用失败: {msg.get('error')}")

            exposed = (msg.get("result") or {}).get("exposed_entities") or {}
            return {
                eid for eid, cfg in exposed.items()
                if isinstance(cfg, dict) and cfg.get("conversation")
            }


async def _rest_fetch_states(base_url: str, api_key: str) -> dict:
    """拉取 /api/states，返回 {entity_id: state_dict}。"""
    url = f"{str(base_url).rstrip('/')}/api/states"
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"/api/states 返回 HTTP {resp.status}")
            states = await resp.json()
    return {s.get("entity_id"): s for s in states if s.get("entity_id")}


async def fetch_exposed_entities(base_url: str, api_key: str, force: bool = False) -> list:
    """返回 HA 暴露给语音助手的设备清单（带 TTL 缓存）。

    返回 [{"entity_id","domain","name","state"}]（按 entity_id 排序）。
    失败时回退到上次缓存；无缓存则返回 []（调用方需把空清单视为"无可用设备"）。
    """
    now = time.time()
    if (not force and _expose_cache["entities"]
            and now - _expose_cache["ts"] < EXPOSE_TTL):
        return _expose_cache["entities"]

    if not base_url or not api_key:
        return _expose_cache["entities"]

    async with _cache_lock:
        # 双检：等锁期间可能已被其他协程刷新
        if (not force and _expose_cache["entities"]
                and time.time() - _expose_cache["ts"] < EXPOSE_TTL):
            return _expose_cache["entities"]

        try:
            exposed_ids = await _ws_fetch_exposed_ids(base_url, api_key)
            states = await _rest_fetch_states(base_url, api_key)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"从HA同步暴露实体失败: {e}，沿用旧清单")
            return _expose_cache["entities"]

        entities = []
        for eid in sorted(exposed_ids):
            st = states.get(eid)
            if st is None:
                continue  # 暴露了但实体不存在，跳过
            attrs = st.get("attributes") or {}
            entities.append({
                "entity_id": eid,
                "domain": eid.split(".")[0],
                "name": attrs.get("friendly_name") or eid,
                "state": st.get("state"),
            })

        _expose_cache["ts"] = time.time()
        _expose_cache["entities"] = entities
        logger.bind(tag=TAG).info(
            f"HA暴露实体已同步: {len(entities)} 个可用设备"
            f"（HA 共暴露 {len(exposed_ids)} 个）"
        )
        return entities


def get_cached_entities() -> list:
    """同步读取缓存（不发起请求），供无法 await 的场景兜底。"""
    return _expose_cache["entities"]


def clear_cache():
    """清空缓存（配置变更/测试用）。"""
    _expose_cache["ts"] = 0.0
    _expose_cache["entities"] = []


# ==================== 楼层/区域归属（2026-09-29） ====================
# 用途：给暴露实体标注「楼层 / 房间」，供前端按楼层-房间分组渲染设备卡片。
# 数据源全部来自 HA 注册表（只读）：
#   config/floor_registry/list → 楼层
#   config/area_registry/list  → 区域(房间)，含 floor_id
#   config/entity_registry/list → 实体（area_id / device_id）
#   config/device_registry/list → 设备（area_id）
# 归属链：entity.area_id 优先，其次 entity.device.area_id → area → floor。
# 注意：HA WebSocket 的消息 id 必须递增（复用 id 会报 id_reuse）。

_area_cache = {"ts": 0.0, "map": {}, "areas": []}
_AREA_TTL = 300.0
_area_lock = asyncio.Lock()


async def _ws_call(ws, counter: list, msg_type: str):
    """在一条 HA WebSocket 连接上发一次请求并等它的响应（id 递增）。"""
    counter[0] += 1
    my_id = counter[0]
    await ws.send_json({"id": my_id, "type": msg_type})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if msg.get("id") == my_id:
            if msg.get("success") is False:
                raise RuntimeError(f"{msg_type} 调用失败: {msg.get('error')}")
            return msg.get("result")


async def fetch_area_floor_map(base_url: str, api_key: str, force: bool = False) -> dict:
    """返回 {entity_id: {"room": 区域名, "floor": 楼层名}}（TTL 缓存 5 分钟）。

    关联不上的实体不出现在结果里（调用方自行兜底为「未分配」）。
    失败时回退上次缓存；无缓存则返回 {}。
    """
    now = time.time()
    if not force and _area_cache["map"] and now - _area_cache["ts"] < _AREA_TTL:
        return _area_cache["map"]
    if not base_url or not api_key:
        return _area_cache["map"]

    async with _area_lock:
        if not force and _area_cache["map"] and time.time() - _area_cache["ts"] < _AREA_TTL:
            return _area_cache["map"]
        try:
            ws_url = _to_ws_url(base_url)
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.ws_connect(ws_url) as ws:
                    await asyncio.wait_for(ws.receive_json(), timeout=10)
                    await ws.send_json({"type": "auth", "access_token": api_key})
                    auth = await asyncio.wait_for(ws.receive_json(), timeout=10)
                    if auth.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WebSocket 认证失败: {auth.get('type')}")

                    counter = [0]
                    floors = await _ws_call(ws, counter, "config/floor_registry/list") or []
                    areas = await _ws_call(ws, counter, "config/area_registry/list") or []
                    entities = await _ws_call(ws, counter, "config/entity_registry/list") or []
                    devices = await _ws_call(ws, counter, "config/device_registry/list") or []

            floor_name = {f.get("floor_id"): f.get("name") for f in floors}
            # area_id -> {"room": 区域名, "floor": 楼层名}
            area_info = {}
            for a in areas:
                aid = a.get("area_id")
                if not aid:
                    continue
                fid = a.get("floor_id")
                area_info[aid] = {
                    "room": a.get("name") or "",
                    "floor": floor_name.get(fid) or "",
                }

            dev_area = {d.get("id"): d.get("area_id") for d in devices if d.get("id")}
            result = {}
            for e in entities:
                eid = e.get("entity_id")
                if not eid:
                    continue
                aid = e.get("area_id") or dev_area.get(e.get("device_id"))
                info = area_info.get(aid)
                if info:
                    result[eid] = info

            _area_cache["ts"] = time.time()
            _area_cache["map"] = result
            _area_cache["areas"] = list(area_info.values())
            logger.bind(tag=TAG).info(
                f"HA楼层/区域已同步: {len(areas)} 区域 / {len(floors)} 楼层, "
                f"{len(result)} 个实体有归属"
            )
            return result
        except Exception as e:
            logger.bind(tag=TAG).warning(f"同步HA楼层/区域失败: {e}，沿用旧映射")
            return _area_cache["map"]


def get_cached_areas() -> list:
    """同步读取 HA 区域列表缓存 [{"room": 区域名, "floor": 楼层名}]（不发起请求）。"""
    return _area_cache["areas"]
