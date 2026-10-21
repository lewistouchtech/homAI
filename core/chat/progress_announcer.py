"""
进度插播组件（v3.0 第一波 P3）

C 类长任务执行时，按里程碑或定时心跳插播进度，避免用户感知"死机"。
"""
import asyncio
import logging

TAG = __name__

# [2026-10-11 C2] 心跳最多插播 2 次（主人原话：「最多 2 次，工具返回或被打断即取消」）
_MAX_ANNOUNCE = 2


class ProgressAnnouncer:
    """轻量进度插播器

    用法：
        announcer = ProgressAnnouncer(conn, interval=25)
        announcer.start()
        # 任务执行中...
        announcer.milestone("客厅灯和空调都关了，还剩书房和卧室两个。")
        # 任务完成
        announcer.stop()
    """

    def __init__(self, conn, interval: int = 25, heartbeat_text: str = "还在弄呢，再等我一下"):
        self.conn = conn
        self.interval = interval
        self.heartbeat_text = heartbeat_text
        self._task = None
        self._running = False
        self._announced = 0          # [C2] 已插播次数，最多 2 次，别变成话痨
        self._last_announce_time = 0
        self._logger = getattr(conn, 'logger', logging)

    def start(self):
        """启动心跳定时任务。

        [2026-10-11 C2 修复] 原来用 `asyncio.create_task`，但调用方
        （`chat_engine` 的工作线程）**不在事件循环里** → 必然抛
        "no running event loop" → 整个 ProgressAnnouncer 从来没启动过
        （日志实证：垫场句播了 667 次，进度插播器启动 0 次）。
        改为 `run_coroutine_threadsafe` 投递到主循环。同步返回，不阻塞调用线程。
        """
        if self._running:
            return
        self._running = True
        loop = getattr(self.conn, "loop", None)
        if loop is not None:
            try:
                self._task = asyncio.run_coroutine_threadsafe(self._heartbeat_loop(), loop)
            except Exception as e:
                self._running = False
                self._logger.bind(tag=TAG).warning(f"进度插播器启动失败（无事件循环）: {e}")
                return
        else:
            # 无 conn.loop（如单测或未起服务的场景）：退回在当前线程建 loop 不可行，
            # 直接放弃，绝不让异常冒到调用方。
            self._running = False
            self._logger.bind(tag=TAG).warning("进度插播器启动失败：conn 无事件循环")
            return
        self._logger.bind(tag=TAG).info(f"进度插播器已启动（心跳间隔 {self.interval}s）")

    def stop(self):
        """停止心跳"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._logger.bind(tag=TAG).info("进度插播器已停止")

    async def milestone(self, text: str):
        """里程碑插播（立即出声）"""
        await self._announce(text)

    async def _heartbeat_loop(self):
        """定时心跳循环。

        [C2] 原来 `_get_tool_progress()` 是空实现恒返回 None → 就算循环跑起来
        一次都不会播（第二个「从未生效」的原因）。现在兜底播固定心跳文案，
        并限制最多 `_MAX_ANNOUNCE` 次。
        """
        while self._running:
            await asyncio.sleep(self.interval)
            if not self._running:
                break
            if self._announced >= self._MAX_ANNOUNCE:
                self._running = False
                break
            try:
                progress = await self._get_tool_progress()
                if not progress:
                    progress = self.heartbeat_text  # 空实现 → 播兜底文案
                if progress and progress != self._last_announce_time:
                    await self._announce(progress)
                    self._last_announce_time = progress
            except Exception as e:
                self._logger.bind(tag=TAG).warning(f"心跳进度查询失败: {e}")

    async def _get_tool_progress(self) -> str | None:
        """查询当前执行工具的进度（预留，工具可选实现 get_progress）"""
        return None

    async def _announce(self, text: str):
        """实际播报"""
        try:
            from core.handle.sendAudioHandle import send_tts_message
            await send_tts_message(self.conn, "sentence_start", text)
            await send_tts_message(self.conn, "stop", None)
            self._announced += 1
            self._logger.bind(tag=TAG).info(f"[进度插播] {text}")
        except Exception as e:
            self._logger.bind(tag=TAG).warning(f"进度插播失败: {e}")
