# -*- coding: utf-8 -*-
"""到访播报决策：该不该说、说多重（2026-10-13，主人确认的欢迎语分层方案）

方案文档：docs/plans/2026-10-13_欢迎语分层设计方案.md

核心原则：**先决定说不说，再决定说什么**；离开越久越值得说；出门默认不说；
拿不准一律少说。

⚠️ 关键技术约束（设计时反复推敲的结论）：
    主入口摄像头在室内朝门拍。人**进门时正对镜头**能识别出是谁，**出门时背对镜头**
    几乎认不出。所以"这个人什么时候出的门"通常是未知的。这里的做法是用
    **离开时长的下界**：本次进门之前，门口最近一次"有人出门"的时间 T_out（不管是谁）。
    任何此刻进门的人，之前一定在门外，且他出门的时间 ≤ T_out，所以
    他离开的时长 ≥ now - T_out。**这是严格的下界**，估小不估大 → 只会"少说"，不会
    把倒垃圾说成"欢迎回家"。代价：别人刚出门不久时，真正离开很久的人也会被判短；
    这是可以接受的保守误差。
"""
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 决策档位
SILENT = "silent"          # 不说
MINIMAL = "minimal"        # 一句极简（"回来啦"）
FULL = "full"              # 完整问候（大模型生成）
MORNING = "morning"        # 当天第一次见到的早安
NIGHT_LIGHT = "night_light"  # 深夜回来：更短更轻
SAFE_TRIP = "safe_trip"    # 学生上学出门："路上小心"

KIND_FAMILY = "family"
KIND_COLLEAGUE = "colleague"
WORK_OFFICE = "office"     # 楼内办公
WORK_FIELD = "field"       # 常外出

DEFAULTS = {
    "enabled": True,
    "family_short_minutes": 15,       # 家人离开不足此分钟 → 不说
    "family_long_hours": 2,           # 家人离开不少于此小时 → 完整问候
    "office_long_hours": 2,           # 楼内办公同事：离开 ≥ 此小时才说一句
    "field_long_hours": 3,            # 常外出同事：离开 ≥ 此小时完整问候
    "field_minimal_hours": 1,         # 常外出同事：离开 ≥ 此小时说一句
    "repeat_cooldown_minutes": 30,    # 极简问候同一人 N 分钟内不重复
    "group_window_seconds": 5,        # 多人同时进门的合并窗口
    "morning_before_hour": 11,        # 当天首次见面在此点之前 → 早安
    "night_start_hour": 22,
    "night_end_hour": 6,
    "school_trip_start": "06:30",     # 学生上学时段（首次出门说路上小心）
    "school_trip_end": "08:30",
    "retention_hours": 48,
}


def _hhmm_to_min(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _day(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


class ArrivalPolicy:
    """内存状态 + 纯决策。状态可由 event_log 的 passage 记录恢复。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        c = dict(DEFAULTS)
        c.update(cfg or {})
        self.cfg = c
        self.enabled = bool(c["enabled"])
        self.exits: List[Tuple[float, Optional[str]]] = []   # (时间, 人名或 None)
        self.last_in: Dict[str, float] = {}
        self.last_out: Dict[str, float] = {}
        self.spoken: Dict[str, Tuple[float, str]] = {}

    # ---------- 状态 ----------
    def note_passage(self, ts: float, direction: str, name: Optional[str] = None):
        """登记一次进/出门。name 为 None 表示没认出是谁。"""
        if direction == "out":
            self.exits.append((ts, name))
            if name:
                self.last_out[name] = ts
        elif direction == "in" and name:
            self.last_in[name] = ts
        cutoff = ts - float(self.cfg["retention_hours"]) * 3600
        if len(self.exits) > 2000 or (self.exits and self.exits[0][0] < cutoff):
            self.exits = [e for e in self.exits if e[0] >= cutoff]

    def note_spoken(self, name: str, ts: float, tier: str):
        self.spoken[name] = (ts, tier)

    def load_history(self, rows: Iterable[Tuple[float, str, Optional[str]]]):
        """rows: (ts, direction, name|None)，按时间升序"""
        for ts, direction, name in rows:
            self.note_passage(ts, direction, name or None)

    # ---------- 查询 ----------
    def _latest_exit_before(self, t: float) -> Optional[float]:
        cands = [ts for ts, _ in self.exits if ts <= t - 1]
        return max(cands) if cands else None

    def _exit_after(self, since: float, t: float) -> bool:
        return any(since < ts <= t - 1 for ts, _ in self.exits)

    def is_night(self, hour: int) -> bool:
        s, e = int(self.cfg["night_start_hour"]), int(self.cfg["night_end_hour"])
        return hour >= s or hour < e

    # ---------- 决策 ----------
    def decide(self, name: str, kind: str, direction: str, t: Optional[float] = None,
               work_mode: str = WORK_OFFICE, school_kid: bool = False) -> Dict[str, Any]:
        """返回 {tier, reason, away_lb(秒或None), first_today, hour}。

        必须在 note_passage 登记本次进门**之前**调用（需要看"上一次"状态）。
        """
        t = time.time() if t is None else t
        lt = time.localtime(t)
        hour = lt.tm_hour
        c = self.cfg

        def out(tier, reason, lb=None, first=False):
            return {"tier": tier, "reason": reason, "away_lb": lb,
                    "first_today": first, "hour": hour}

        if not self.enabled:
            return out(FULL, "policy_disabled")
        if direction not in ("in", "out"):
            return out(SILENT, "direction_unknown")

        if direction == "out":
            if kind == KIND_FAMILY and school_kid:
                mins = lt.tm_hour * 60 + lt.tm_min
                lo, hi = _hhmm_to_min(c["school_trip_start"]), _hhmm_to_min(c["school_trip_end"])
                lo_ts = self.last_out.get(name)
                already = lo_ts is not None and _day(lo_ts) == _day(t)
                if lo <= mins <= hi and not already:
                    return out(SAFE_TRIP, "school_first_out")
            return out(SILENT, "exit_silent")

        # ---- 进门 ----
        last_in = self.last_in.get(name)
        first_today = last_in is None or _day(last_in) != _day(t)
        latest_exit = self._latest_exit_before(t)
        lb = (t - latest_exit) if latest_exit is not None else None

        if not first_today and last_in is not None and not self._exit_after(last_in, t):
            # 上次进门后门口没有任何人出过门 → 他其实一直在里面，只是从别处经过
            return out(SILENT, "no_exit_since_last_in", lb, first_today)

        if kind == KIND_FAMILY:
            short_s = float(c["family_short_minutes"]) * 60
            long_s = float(c["family_long_hours"]) * 3600
            if lb is None:
                tier, reason = (FULL, "first_today_no_exit_record") if first_today \
                    else (SILENT, "no_exit_record")
            elif lb < short_s:
                tier, reason = SILENT, "short_absence"
            elif lb < long_s:
                tier, reason = MINIMAL, "medium_absence"
            else:
                tier, reason = FULL, "long_absence"
            if tier == FULL and self.is_night(hour):
                tier, reason = NIGHT_LIGHT, reason + "+night"
        else:
            morning = first_today and hour < int(c["morning_before_hour"])
            if morning:
                tier, reason = MORNING, "colleague_first_today"
            elif work_mode == WORK_FIELD:
                full_s = float(c["field_long_hours"]) * 3600
                min_s = float(c["field_minimal_hours"]) * 3600
                if lb is None:
                    tier, reason = (MINIMAL, "first_today_no_exit_record") if first_today \
                        else (SILENT, "no_exit_record")
                elif lb >= full_s:
                    tier, reason = FULL, "field_long_absence"
                elif lb >= min_s:
                    tier, reason = MINIMAL, "field_medium_absence"
                else:
                    tier, reason = SILENT, "field_short_absence"
            else:
                long_s = float(c["office_long_hours"]) * 3600
                if lb is None:
                    tier, reason = (MINIMAL, "first_today_no_exit_record") if first_today \
                        else (SILENT, "no_exit_record")
                elif lb >= long_s:
                    tier, reason = MINIMAL, "office_long_absence"
                else:
                    tier, reason = SILENT, "office_short_absence"

        if tier == MINIMAL:
            sp = self.spoken.get(name)
            if sp and t - sp[0] < float(c["repeat_cooldown_minutes"]) * 60:
                return out(SILENT, "repeat_cooldown", lb, first_today)
        return out(tier, reason, lb, first_today)
