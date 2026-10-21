"""上一次控制记忆 + 待确认动作（按终端 device_id，内存态）

用途：
- 「再亮一点」「还是不行」「刚才那个」「恢复刚才的样子」→ 要知道上次控制了谁、控制前什么样
- 门锁等需确认的动作：先问、记下，用户口头确认后才执行（必须有对应的待确认才认「确认」）
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LAST_TTL = 600.0
PENDING_TTL = 60.0
SCENE_TTL = 120.0            # 等用户回答「要不要存成场景」
APPLIED_TTL = 1800.0         # 刚调好的氛围，用户随时可以说「把这个存成场景」


@dataclass
class LastControl:
    ts: float
    entity_ids: List[str]
    action: str
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # 控制前状态快照


@dataclass
class Pending:
    ts: float
    entity_ids: List[str]
    action: str
    value: Any = None
    speaker: str = ""


@dataclass
class ScenePlan:
    """大模型按心情组合出来、等用户确认的场景方案（已做过寻址/可用性/安全预演）"""
    ts: float
    name: str
    mood: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)     # {"cmd": 原始指令, "entity_ids": [...], "action":, "value":}
    music: str = ""
    speaker: str = ""
    kind: str = "applied"            # applied=刚调好（可主动存） | save=刚问过要不要存、等回答


class SessionStore:
    def __init__(self):
        self._last: Dict[str, LastControl] = {}
        self._pending: Dict[str, Pending] = {}
        self._scene: Dict[str, ScenePlan] = {}

    # ---- 场景方案（心情场景：建议 → 确认 → 执行 → 可选保存）----
    def set_scene(self, device_id: str, plan: ScenePlan) -> None:
        if device_id:
            self._scene[device_id] = plan

    def scene(self, device_id: str) -> Optional[ScenePlan]:
        p = self._scene.get(device_id)
        if p and time.time() - p.ts <= (SCENE_TTL if p.kind == "save" else APPLIED_TTL):
            return p
        self._scene.pop(device_id, None)
        return None

    def drop_scene(self, device_id: str) -> None:
        self._scene.pop(device_id, None)

    def remember(self, device_id: str, entity_ids: List[str], action: str,
                 snapshots: Dict[str, Dict[str, Any]]) -> None:
        if device_id and entity_ids:
            if not snapshots:
                # 这次没有任何可撤销的快照（离线/失败/撤销本身）：沿用上一次的，别把它冲掉
                prev = self.last(device_id)
                snapshots = prev.snapshots if prev else {}
            self._last[device_id] = LastControl(time.time(), list(entity_ids), action, dict(snapshots))

    def drop_snapshots(self, device_id: str) -> None:
        lc = self._last.get(device_id)
        if lc:
            lc.snapshots = {}

    def last(self, device_id: str) -> Optional[LastControl]:
        lc = self._last.get(device_id)
        if lc and time.time() - lc.ts <= LAST_TTL:
            return lc
        self._last.pop(device_id, None)
        return None

    def set_pending(self, device_id: str, entity_ids: List[str], action: str, value=None,
                    speaker: str = "") -> None:
        self._pending[device_id] = Pending(time.time(), sorted(entity_ids), action, value, speaker)

    def take_pending(self, device_id: str, entity_ids: List[str], action: str) -> Optional[Pending]:
        """取出并清除与本次一致的待确认；不一致/过期返回 None（不清除一致性之外的）"""
        p = self._pending.get(device_id)
        if not p or time.time() - p.ts > PENDING_TTL:
            self._pending.pop(device_id, None)
            return None
        if p.action != action or p.entity_ids != sorted(entity_ids):
            return None
        self._pending.pop(device_id, None)
        return p

    def clear(self):
        self._last.clear()
        self._pending.clear()
        self._scene.clear()


_store = SessionStore()


def get_session_store() -> SessionStore:
    return _store
