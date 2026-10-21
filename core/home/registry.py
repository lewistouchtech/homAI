"""家居注册表：HA 暴露实体 + 区域/楼层/别名 + 实时状态

数据来源（一条 WebSocket 全部取到）：
  homeassistant/expose_entity/list   → 暴露给语音助手的实体（唯一设备边界）
  config/{floor,area,entity,device}_registry/list → 房间/楼层/别名/设备类型
  get_states + subscribe_events(state_changed)   → 状态，之后由事件实时更新

后台保持一条长连接（断线指数退避重连）；连不上时退化为 30 秒 TTL 的一次性拉取，
所以任何时候 `ensure_ready()` 都能给出「尽量新」的快照，不会因为订阅挂了就瘫痪。
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from config.logger import setup_logging
from core.home.config import get_ha_config

TAG = __name__
logger = setup_logging()

# 不可用/未接入的状态：不能当作「设备在线」
OFFLINE_STATES = ("unavailable", "unknown", "unreachable")
# 场景/按钮这类「没有持续状态」的实体，HA 里没触发过时状态就是 unknown——那是正常的，不是没接入
STATELESS_DOMAINS = ("scene", "button", "input_button")
_FALLBACK_TTL = 30.0
_FULL_RELOAD_SECONDS = 300.0
_REGISTRY_EVENTS = (
    "entity_registry_updated", "device_registry_updated",
    "area_registry_updated", "floor_registry_updated",
)


@dataclass
class Entity:
    entity_id: str
    name: str = ""
    state: str = "unknown"
    attrs: Dict[str, Any] = field(default_factory=dict)
    area: str = ""
    floor: str = ""
    area_aliases: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    device_class: str = ""

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def online(self) -> bool:
        s = str(self.state).lower()
        if s == "unknown" and self.domain in STATELESS_DOMAINS:
            return True
        return s not in OFFLINE_STATES

    @property
    def placeholder(self) -> bool:
        """「unknown」= 创明/杜亚等尚未接入的占位实体，当它不存在"""
        return str(self.state).lower() == "unknown" and self.domain not in STATELESS_DOMAINS


def _to_ws_url(base_url: str) -> str:
    u = base_url.rstrip("/")
    u = "wss://" + u[8:] if u.startswith("https://") else "ws://" + u[7:] if u.startswith("http://") else "ws://" + u
    return u + "/api/websocket"


class HomeRegistry:
    def __init__(self):
        self.entities: Dict[str, Entity] = {}
        self.areas: List[Dict[str, Any]] = []
        self.loaded_at = 0.0
        self.live = False                      # 订阅连接是否在线
        self._task: Optional[asyncio.Task] = None
        self._task_loop = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._msg_id = 0
        self._dirty = False

    # ---------- 对外 ----------
    def get(self, entity_id: str) -> Optional[Entity]:
        return self.entities.get(entity_id)

    def all(self) -> List[Entity]:
        return list(self.entities.values())

    def set_entities(self, entities: List[Entity], areas: Optional[List[dict]] = None) -> None:
        """测试/离线夹具用：直接灌入快照"""
        self.entities = {e.entity_id: e for e in entities}
        self.areas = areas or []
        self.loaded_at = time.time()

    async def ensure_ready(self, force: bool = False) -> bool:
        """保证快照可用。返回是否有数据。"""
        loop = asyncio.get_running_loop()
        self._maybe_start_watch(loop)
        fresh = self.entities and self.live and not force
        if fresh and not self._dirty:
            return True
        if not force and self.entities and time.time() - self.loaded_at < _FALLBACK_TTL:
            return True
        lock = self._get_lock(loop)
        async with lock:
            if not force and self.entities and time.time() - self.loaded_at < _FALLBACK_TTL:
                return True
            try:
                await self._load_once()
            except Exception as e:
                logger.bind(tag=TAG).warning(f"家居注册表刷新失败: {e}，沿用旧快照")
        return bool(self.entities)

    async def live_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """直接问 HA 要某个实体此刻的状态对象（执行前/后校验用）；失败返回 None"""
        try:
            base, token = get_ha_config()
            async with aiohttp.ClientSession(trust_env=False) as s:
                async with s.get(f"{base}/api/states/{entity_id}",
                                 headers={"Authorization": f"Bearer {token}"},
                                 timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status != 200:
                        return None
                    st = await r.json()
            self._apply_state(st)
            return st
        except Exception:
            return None

    # ---------- 内部 ----------
    def _get_lock(self, loop) -> asyncio.Lock:
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _maybe_start_watch(self, loop) -> None:
        if self._task is not None and not self._task.done() and self._task_loop is loop:
            return
        if self._task is not None and not self._task.done() and self._task_loop is not loop:
            return  # 另一个循环里已有订阅，不抢
        try:
            self._task = loop.create_task(self._watch_forever())
            self._task_loop = loop
        except Exception:
            self._task = None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _call(self, ws, msg_type: str, **extra):
        mid = self._next_id()
        await ws.send_json({"id": mid, "type": msg_type, **extra})
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if msg.get("id") == mid and msg.get("type") == "result":
                if msg.get("success") is False:
                    raise RuntimeError(f"{msg_type} 失败: {msg.get('error')}")
                return msg.get("result")
            self._handle_event(msg)

    async def _connect(self, session):
        base, token = get_ha_config()
        ws = await session.ws_connect(_to_ws_url(base), heartbeat=30)
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if first.get("type") != "auth_required":
            raise RuntimeError(f"HA WebSocket 握手异常: {first.get('type')}")
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA WebSocket 认证失败: {auth.get('type')}")
        # 注意：不能在这里把 _msg_id 清零。计数器全局共享，force 刷新会另开一条连接，
        # 清零会让常驻订阅连接下次发消息的 id 变小 → HA 报 id_reuse 踢掉订阅（2026-10-13 实测）。
        # HA 只要求单条连接内递增，全局单调递增天然满足。
        return ws

    async def _load_all(self, ws) -> None:
        exposed = (await self._call(ws, "homeassistant/expose_entity/list") or {}).get("exposed_entities") or {}
        floors = await self._call(ws, "config/floor_registry/list") or []
        areas = await self._call(ws, "config/area_registry/list") or []
        ent_reg = await self._call(ws, "config/entity_registry/list") or []
        dev_reg = await self._call(ws, "config/device_registry/list") or []
        states = await self._call(ws, "get_states") or []
        self._build(exposed, floors, areas, ent_reg, dev_reg, states)

    def _build(self, exposed, floors, areas, ent_reg, dev_reg, states) -> None:
        floor_name = {f.get("floor_id"): f.get("name") or "" for f in floors}
        area_info = {}
        for a in areas:
            aid = a.get("area_id")
            if aid:
                area_info[aid] = {"area_id": aid, "name": a.get("name") or "",
                                  "floor": floor_name.get(a.get("floor_id")) or "",
                                  "aliases": list(a.get("aliases") or [])}
        dev_area = {d.get("id"): d.get("area_id") for d in dev_reg if d.get("id")}
        reg = {e.get("entity_id"): e for e in ent_reg if e.get("entity_id")}
        st_map = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
        ids = {eid for eid, cfg in exposed.items() if isinstance(cfg, dict) and cfg.get("conversation")}
        out: Dict[str, Entity] = {}
        for eid in sorted(ids):
            st = st_map.get(eid) or {}
            attrs = st.get("attributes") or {}
            r = reg.get(eid) or {}
            info = area_info.get(r.get("area_id") or dev_area.get(r.get("device_id"))) or {}
            name = str(attrs.get("friendly_name") or r.get("name") or r.get("original_name") or "").strip()
            out[eid] = Entity(
                entity_id=eid, name=name or eid,
                state=str(st.get("state", "unknown")), attrs=dict(attrs),
                area=info.get("name", ""), floor=info.get("floor", ""),
                area_aliases=list(info.get("aliases") or []),
                aliases=[str(x) for x in (r.get("aliases") or []) if x],
                device_class=str(attrs.get("device_class") or r.get("device_class")
                                 or r.get("original_device_class") or ""),
            )
        self.entities = out
        self.areas = list(area_info.values())
        self.loaded_at = time.time()
        self._dirty = False
        logger.bind(tag=TAG).info(f"家居注册表已加载: {len(out)} 个实体 / {len(self.areas)} 个区域")

    async def _load_once(self) -> None:
        async with aiohttp.ClientSession(trust_env=False) as session:
            ws = await self._connect(session)
            try:
                await self._load_all(ws)
            finally:
                await ws.close()

    def _apply_state(self, st: Dict[str, Any]) -> None:
        eid = (st or {}).get("entity_id")
        ent = self.entities.get(eid)
        if ent is None:
            return
        ent.state = str(st.get("state", ent.state))
        attrs = st.get("attributes")
        if isinstance(attrs, dict):
            ent.attrs = dict(attrs)
            if attrs.get("friendly_name"):
                ent.name = str(attrs["friendly_name"])

    def _handle_event(self, msg: Dict[str, Any]) -> None:
        if msg.get("type") != "event":
            return
        ev = msg.get("event") or {}
        et = ev.get("event_type")
        if et == "state_changed":
            new = (ev.get("data") or {}).get("new_state")
            if new:
                self._apply_state(new)
        elif et in _REGISTRY_EVENTS:
            self._dirty = True

    async def _watch_forever(self) -> None:
        backoff = 2.0
        while True:
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    ws = await self._connect(session)
                    try:
                        await self._load_all(ws)
                        for et in ("state_changed",) + _REGISTRY_EVENTS:
                            await self._call(ws, "subscribe_events", event_type=et)
                        self.live = True
                        backoff = 2.0
                        logger.bind(tag=TAG).info("家居状态订阅已建立（实时）")
                        while True:
                            try:
                                msg = await asyncio.wait_for(ws.receive_json(), timeout=_FULL_RELOAD_SECONDS)
                            except asyncio.TimeoutError:
                                self._dirty = True
                                msg = None
                            if msg is not None:
                                self._handle_event(msg)
                            if self._dirty:
                                await asyncio.sleep(1.0)        # 防抖：合并一阵连续的注册表变更
                                await self._load_all(ws)
                    finally:
                        self.live = False
                        await ws.close()
            except asyncio.CancelledError:
                self.live = False
                raise
            except Exception as e:
                self.live = False
                logger.bind(tag=TAG).warning(f"家居状态订阅断开: {e}，{backoff:.0f}s 后重连")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.live = False


_registry = HomeRegistry()


def get_registry() -> HomeRegistry:
    return _registry
