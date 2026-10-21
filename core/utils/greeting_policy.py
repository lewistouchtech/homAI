# -*- coding: utf-8 -*-
"""问候策略：防重复 + 频次感知 + 场景细分（T9 / T10）

主人 2026-09-26 的三条要求：
    1. 不要生硬——不要从文本库里挑模板来回换，要由大模型生成、像真人
    2. 不能重复——"这个人一天进出大门 20 次，每次都同一句问候，很烦、很过度"
    3. 要分场景——(a) 家人带朋友进来 = 私人社交；(b) 我或员工带人进来 = 参观/商务

对应设计：
    防重复   → 把"最近说过的话"作为**负例**注入 LLM 提示词，让它自己避开；
               不再依赖单纯的时间冷却（原 _FAMILY_COOLDOWN 保留为兜底）
    频次感知 → 按"今天第几次见 / 距上次多久"分三档：speak（完整）/
               minimal（极简）/ silent（静默）。核心是**该不该说比说什么更重要**
    场景细分 → 用"已知人脸 + 未知人脸在时间窗内共现"判别（最务实可行的手段），
               再按带人者的身份（家人 / 员工）分流语气与动作

数据来源：event_log（T1 的采集表）。这也是数据采集第一个直接反哺功能的例子。
"""
import sqlite3
import time
import calendar
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# ⚠️ 时区约定（2026-09-26 端到端实测踩坑；2026-09-29 补充修正）：
# event_log.created_at 用 SQLite 的 CURRENT_TIMESTAMP 写入，**存的是 UTC**，
# 不是本地时间。因此 Python 里解析要用 calendar.timegm()（按 UTC），
# 不能用 time.mktime()（按本地），否则"距上次打招呼"凭空多出 8 小时。
# 但"今天第几次"的"今天"必须按**本地日历日**算（UTC 直接比日期会让
# 东八区每天 00:00-08:00 的问候全部计入"昨天"，凌晨频次感知失真）。
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _local_midnight_epoch() -> float:
    """本地今天 00:00:00 对应的 epoch 秒。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _parse_utc_ts(raw: Any) -> Optional[float]:
    """把 event_log.created_at（UTC 文本）解析成 epoch 秒。失败返回 None。"""
    if not raw:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(raw)[:19], _TS_FMT)))
    except Exception:
        return None


# 三档动作
SPEAK = "speak"        # 完整问候（LLM 生成，带情境与称呼）
MINIMAL = "minimal"    # 极简应答（不打扰，如"回来啦"）
SILENT = "silent"      # 完全静默

# 极简话术池：短、不带情绪负担、不点名也不尴尬。
# 刻意做得很轻——频繁进出时任何"有内容"的话都会变成打扰。
_MINIMAL_LINES = ["回来啦", "又出门呀", "嗯，看到你了", "进进出出的，忙着呢"]

# 场景类型
SCENE_FAMILY_ONLY = "family_only"                  # 只有家人
SCENE_FAMILY_WITH_GUEST = "family_with_guest"      # (a) 家人带朋友 → 私人社交
SCENE_EMPLOYEE_WITH_GUEST = "employee_with_guest"  # (b) 员工带客人 → 参观/商务
SCENE_STRANGER = "stranger"                        # 只有陌生人 → 走安防

# 默认按"员工"处理的关系词（其余已登记成员视为家人）
_DEFAULT_EMPLOYEE_RELATIONS = ["员工", "同事", "职员", "staff"]


class GreetingPolicy:
    """问候策略判定器。无状态依赖注入，可独立单测。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        # 注入 LLM 提示词的"最近已说话术"条数（负例）
        self.recent_limit = int(cfg.get("recent_limit", 5))
        # 一天内完整问候上限，超过后转极简
        self.today_max_full = int(cfg.get("today_max_full", 3))
        # 距上次问候不足此秒数 → 至少降为极简
        self.minimal_after_seconds = float(cfg.get("minimal_after_seconds", 600))
        # 一天内超过此次数 → 静默
        self.silent_after_count = int(cfg.get("silent_after_count", 6))
        # 已知人脸与未知人脸共现判定窗（秒）
        self.cooccur_window = float(cfg.get("cooccur_window_seconds", 60))
        self.employee_relations = {
            str(r).strip() for r in
            (cfg.get("employee_relations") or _DEFAULT_EMPLOYEE_RELATIONS)
            if str(r).strip()
        }
        # 观测缓冲：[(ts, camera_id, name_key_or_None)]，None 表示未知人脸
        self._observations: List[tuple] = []

    # ------------------------------------------------------------------
    # 历史查询（读 event_log）
    # ------------------------------------------------------------------
    def _query(self, sql: str, args: tuple) -> List[tuple]:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except Exception as e:
            # 采集表缺失/损坏都不能影响问候本身
            logger.bind(tag=TAG).debug(f"[问候策略] 查询失败（按无历史处理）: {e}")
            return []

    def recent_greetings(self, call_name: str) -> List[str]:
        """最近对这个人说过的话（新→旧），作为 LLM 的"不要重复"负例。"""
        if not call_name:
            return []
        rows = self._query(
            "SELECT greeting_text FROM event_log "
            "WHERE label=? AND greeting_triggered=1 AND greeting_text!='' "
            "ORDER BY id DESC LIMIT ?", (call_name, self.recent_limit))
        return [r[0] for r in rows if r[0]]

    def today_count(self, call_name: str) -> int:
        """今天（按本地日历日）已经对这个人播报过几次。

        created_at 存的是 UTC，不能直接用 SQL 的 date(created_at)=date('now')
        比日期（那是 UTC 的"今天"）；先取回时间戳再与本地零点比大小。
        """
        if not call_name:
            return 0
        midnight = _local_midnight_epoch()
        rows = self._query(
            "SELECT created_at FROM event_log "
            "WHERE label=? AND greeting_triggered=1", (call_name,))
        n = 0
        for (ts,) in rows:
            e = _parse_utc_ts(ts)
            if e is not None and e >= midnight:
                n += 1
        return n

    def seconds_since_last(self, call_name: str) -> Optional[float]:
        """距上次对该人播报过了多少秒；从未播报返回 None。"""
        if not call_name:
            return None
        rows = self._query(
            "SELECT created_at FROM event_log "
            "WHERE label=? AND greeting_triggered=1 "
            "ORDER BY id DESC LIMIT 1", (call_name,))
        if not rows:
            return None
        ts = _parse_utc_ts(rows[0][0])
        if ts is None:
            return None
        return max(0.0, time.time() - ts)

    # ------------------------------------------------------------------
    # T9：频次感知判定
    # ------------------------------------------------------------------
    def decide(self, call_name: str) -> Dict[str, Any]:
        """决定这次该不该说、说多重。

        返回 {"action": speak|minimal|silent, "reason": str,
              "count_today": int, "secs_since_last": float|None,
              "recent": [str]}
        """
        recent = self.recent_greetings(call_name)
        count = self.today_count(call_name)
        since = self.seconds_since_last(call_name)

        if not self.enabled:
            action, reason = SPEAK, "policy_disabled"
        elif count >= self.silent_after_count:
            # 一天见太多次了，再说就是打扰
            action, reason = SILENT, f"today_count={count}>={self.silent_after_count}"
        elif since is not None and since < self.minimal_after_seconds:
            # 刚见过（比如 10 分钟内又回来）→ 极简，别每次都来一段完整问候
            action, reason = MINIMAL, f"secs_since_last={since:.0f}<{self.minimal_after_seconds:.0f}"
        elif count >= self.today_max_full:
            action, reason = MINIMAL, f"today_count={count}>={self.today_max_full}"
        else:
            action, reason = SPEAK, "normal"

        return {"action": action, "reason": reason, "count_today": count,
                "secs_since_last": since, "recent": recent}

    def minimal_line(self, call_name: str = "") -> str:
        """极简话术：轮转取，避免连续两次一模一样。"""
        if not _MINIMAL_LINES:
            return ""
        # 用"今天已说次数"做轮转下标，天然错开且不引入随机不确定性
        idx = self.today_count(call_name) % len(_MINIMAL_LINES)
        return _MINIMAL_LINES[idx]

    def build_llm_context(self, call_name: str) -> str:
        """给 LLM 的补充情境：今天第几次见、距上次多久、已经说过哪些话。

        主人明确要求"内容由大模型生成、不要从文本库挑"，所以这里**只给事实**，
        让模型自己把握分寸；已说话术作为负例，是防重复最有效的一招。
        """
        d = self.decide(call_name)
        parts = [f"今天是第 {d['count_today'] + 1} 次见到对方"]
        if d["secs_since_last"] is not None:
            mins = int(d["secs_since_last"] // 60)
            parts.append(f"距上次打招呼约 {mins} 分钟" if mins < 60
                         else f"距上次打招呼约 {mins // 60} 小时")
        if d["recent"]:
            quoted = "、".join(f"「{t}」" for t in d["recent"][:self.recent_limit])
            parts.append(f"已经说过这些话，**不要重复也不要换个说法表达同一个意思**：{quoted}")
        return "；".join(parts)

    # ------------------------------------------------------------------
    # T10：场景细分
    # ------------------------------------------------------------------
    def note_person(self, camera_id: str, name_key: Optional[str]):
        """记录一次人脸观测。name_key 为 None 表示"识别不出的陌生人"。"""
        self._observations.append((time.time(), camera_id or "", name_key))
        # 只保留窗口内数据，避免无限增长
        cutoff = time.time() - max(self.cooccur_window, 60) * 2
        self._observations = [o for o in self._observations if o[0] >= cutoff]

    def _recent_unknown(self, camera_id: str = "") -> bool:
        """窗口内是否出现过陌生人（同摄像头优先，其次全局）。"""
        now = time.time()
        cutoff = now - self.cooccur_window
        same_cam = any(ts >= cutoff and cam == camera_id and nk is None
                       for ts, cam, nk in self._observations)
        if same_cam:
            return True
        return any(ts >= cutoff and nk is None for ts, cam, nk in self._observations)

    def classify_scene(self, name_key: Optional[str], relation: str = "",
                       camera_id: str = "") -> str:
        """判别当前场景（主人要求的 (a)/(b) 区分）。

        判据是"已知人脸 + 未知人脸在时间窗内共现"：
            家人 + 陌生人 → family_with_guest   （私人社交，温暖、少打扰）
            员工 + 陌生人 → employee_with_guest （参观/商务，职业、静默联动）
            只有陌生人    → stranger            （走既有安防）
            只有家人/员工 → family_only
        """
        has_unknown = self._recent_unknown(camera_id)
        if not name_key:
            return SCENE_STRANGER if has_unknown else SCENE_STRANGER
        if not has_unknown:
            return SCENE_FAMILY_ONLY
        return (SCENE_EMPLOYEE_WITH_GUEST if self.is_employee(relation)
                else SCENE_FAMILY_WITH_GUEST)

    def is_employee(self, relation: str) -> bool:
        """按 relation 判断是否员工（未登记关系默认按家人处理，更保守）。"""
        r = str(relation or "").strip()
        return bool(r) and r in self.employee_relations

    def scene_hint(self, scene: str) -> str:
        """给 LLM 的场景说明（决定语气）。"""
        return {
            SCENE_FAMILY_WITH_GUEST: (
                "对方带了客人来家里（私人社交场合）。语气自然温暖，但**简短克制**，"
                "主人在客人面前，不要让主人尴尬，也不要追问客人是谁。"),
            SCENE_EMPLOYEE_WITH_GUEST: (
                "这是同事带客人来公司（参观考察或谈商务）。语气职业、得体、简洁，"
                "不要过度寒暄，不要打扰他们谈事。"),
            SCENE_FAMILY_ONLY: "对方独自回来，正常温暖问候即可。",
            SCENE_STRANGER: "识别不到身份，按访客处理，不要套近乎。",
        }.get(scene, "")
