"""
HomeBrain 内部调度器（替代已弃用的 OpenClaw cron，2026-09-11）

- asyncio 协程，每 30s 扫描一次任务表
- 任务持久化 data/scheduler_tasks.json
- 任务格式：
  {
    "id": "uuid8",
    "name": "任务名",
    "kind": "reminder" | "morning_digest",
    "text": "提醒文本（kind=reminder 时播报内容）",
    "schedule": {"kind": "at", "at": "ISO时间"}           # 一次性
              | {"kind": "daily", "time": "08:00"}        # 每天
              | {"kind": "weekly", "time": "08:00", "weekday": 0}   # 每周(0=周一)
              | {"kind": "workdays", "time": "08:00"},    # 工作日
    "delete_after_run": true/false,
    "last_fired": "2026-09-11T08:00:00" 或 null
  }
- 触发回调由外部注入：scheduler.set_fire_handler(async fn(task))
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "scheduler_tasks.json",
)


class Scheduler:
    def __init__(self):
        self.tasks = []
        self._fire_handler = None
        self._task = None
        self._lock = asyncio.Lock()
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            if os.path.exists(_DATA_PATH):
                with open(_DATA_PATH, "r", encoding="utf-8") as f:
                    self.tasks = json.load(f)
                logger.bind(tag=TAG).info(f"[调度器] 加载任务 {len(self.tasks)} 个")
        except Exception as e:
            logger.bind(tag=TAG).error(f"[调度器] 任务加载失败: {e}")
            self.tasks = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_DATA_PATH), exist_ok=True)
            with open(_DATA_PATH, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.bind(tag=TAG).error(f"[调度器] 任务保存失败: {e}")

    # ---------- 外部接口 ----------
    def set_fire_handler(self, handler):
        """注入触发回调：async def handler(task: dict)"""
        self._fire_handler = handler

    def add_task(self, name, schedule, kind="reminder", text="", delete_after_run=False):
        task = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "kind": kind,
            "text": text,
            "schedule": schedule,
            "delete_after_run": bool(delete_after_run),
            "last_fired": None,
        }
        self.tasks.append(task)
        self._save()
        logger.bind(tag=TAG).info(f"[调度器] 新增任务 {name} ({task['id']}) {schedule}")
        return task

    def list_tasks(self):
        return list(self.tasks)

    def remove_task(self, task_id):
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        changed = len(self.tasks) != before
        if changed:
            self._save()
        return changed

    # ---------- 调度核心 ----------
    def _should_fire(self, task, now: datetime) -> bool:
        sch = task.get("schedule") or {}
        kind = sch.get("kind")
        last = task.get("last_fired")
        last_dt = None
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
            except Exception:
                last_dt = None

        if kind == "at":
            try:
                at = datetime.fromisoformat(sch["at"])
            except Exception:
                return False
            return now >= at and last_dt is None

        # 周期任务：今天的触发时刻
        time_str = sch.get("time", "08:00")
        try:
            hh, mm = int(time_str.split(":")[0]), int(time_str.split(":")[1])
        except Exception:
            return False
        fire_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < fire_at:
            return False
        # 触发窗口 60 分钟：超过则等下个周期（避免服务重启后补播已过时的简报）
        if now > fire_at + timedelta(minutes=60):
            return False
        # 今天已触发过就不再触发
        if last_dt and last_dt.date() == now.date() and kind in ("daily", "workdays"):
            return False
        if kind == "daily":
            return True
        if kind == "workdays":
            return now.weekday() < 5
        if kind == "weekly":
            return now.weekday() == int(sch.get("weekday", 0)) and (
                not last_dt or last_dt.date() != now.date()
            )
        return False

    async def _tick(self):
        now = datetime.now()
        fired = []
        for task in list(self.tasks):
            try:
                if self._should_fire(task, now):
                    fired.append(task)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[调度器] 判定失败 {task.get('name')}: {e}")
        for task in fired:
            task["last_fired"] = now.isoformat()
            logger.bind(tag=TAG).info(f"[调度器] 触发: {task['name']} ({task['kind']})")
            if self._fire_handler:
                try:
                    await self._fire_handler(task)
                except Exception as e:
                    logger.bind(tag=TAG).error(f"[调度器] 执行失败 {task['name']}: {e}")
            if task.get("delete_after_run") or task.get("schedule", {}).get("kind") == "at":
                self.tasks = [t for t in self.tasks if t["id"] != task["id"]]
        if fired:
            self._save()

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.bind(tag=TAG).error(f"[调度器] tick 异常: {e}")
            await asyncio.sleep(30)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.bind(tag=TAG).info("[调度器] 已启动（30s 扫描）")

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# 全局单例
scheduler = Scheduler()
