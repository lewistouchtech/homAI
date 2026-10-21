# -*- coding: utf-8 -*-
"""Mac 原生应用窗口捕获引擎

用 screencapture + AppleScript 实现任意 Mac 应用窗口捕获，
输出 MJPEG 流供前端 <img> 标签嵌入显示。

架构：
  每个目标应用 → 一个后台 CaptureSession
  Session 启动时：获取窗口 bounds → 循环 screencapture → 写入临时文件
  HTTP 端点读临时文件 → 输出 multipart/x-mixed-replace MJPEG 流
"""
import asyncio
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from config.logger import setup_logging

logger = setup_logging()
TAG = "mac-capture"


@dataclass
class CaptureSession:
    """单个应用的窗口捕获会话"""
    app_name: str           # macOS 应用名（如 "Music", "Safari"）
    window_bounds: tuple = (0, 0, 800, 600)  # (left, top, right, bottom)
    fps: float = 3.0
    running: bool = False
    window_id: Optional[int] = None   # CGWindowNumber，screencapture -l 用
    _fail_count: int = 0              # 连续捕获失败次数（超限重查窗口 id）
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)
    frame_a: str = ""       # ping-pong buffer A
    frame_b: str = ""       # ping-pong buffer B
    _active_file: str = ""  # 当前最新帧文件路径
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_latest_frame(self) -> Optional[str]:
        """获取最新一帧的文件路径"""
        with self._lock:
            if self._active_file and os.path.exists(self._active_file):
                return self._active_file
        return None


class MacCaptureEngine:
    """Mac 窗口捕获引擎 - 管理所有活跃捕获会话"""

    def __init__(self):
        self._sessions: Dict[str, CaptureSession] = {}
        self._lock = threading.Lock()
        self._tmpdir = tempfile.mkdtemp(prefix="homebrain-capture-")
        logger.bind(tag=TAG).info(f"捕获引擎初始化完成，临时目录: {self._tmpdir}")

    def get_window_bounds(self, app_name: str) -> Optional[tuple]:
        """通过 AppleScript 获取应用首个窗口的 bounds (left, top, right, bottom)"""
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'tell application "{app_name}" to get bounds of window 1'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                # 兜底：Electron 类应用（微信等）不支持标准 bounds，走 System Events
                result2 = subprocess.run(
                    ["osascript", "-e",
                     f'tell application "System Events" to tell process "{app_name}" '
                     f'to get {{position, size}} of window 1'],
                    capture_output=True, text=True, timeout=5
                )
                if result2.returncode == 0:
                    vals = [int(x.strip()) for x in result2.stdout.strip().split(",")]
                    if len(vals) == 4:
                        x, y, w, h = vals
                        return (x, y, x + w, y + h)
                logger.bind(tag=TAG).warning(f"获取 {app_name} 窗口 bounds 失败: {result.stderr.strip()}")
                return None
            parts = [int(x.strip()) for x in result.stdout.strip().split(",")]
            if len(parts) == 4:
                return tuple(parts)
        except subprocess.TimeoutExpired:
            logger.bind(tag=TAG).warning(f"获取 {app_name} 窗口 bounds 超时")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"获取 {app_name} 窗口 bounds 异常: {e}")
        return None

    def activate_app(self, app_name: str):
        """把应用窗口激活到最前"""
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'],
                capture_output=True, timeout=3
            )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"activate {app_name} 失败: {e}")

    def is_app_running(self, app_name: str) -> bool:
        """检查应用是否在运行"""
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to (name of processes) contains "{app_name}"'],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip().lower() == "true"
        except Exception:
            return False

    def _resolve_window_id(self, app_name: str) -> Optional[int]:
        """用 swift 助手按 PID 解析应用主窗口的 CGWindowNumber。

        窗口 ID 捕获（screencapture -l）能拍到被其他窗口遮挡的窗口内容，
        这样应用可以在浏览器后面后台运行——区域捕获（-R）拍到的是屏幕上
        最上层的像素，窗口被看板浏览器盖住时会出现"嵌套递归"画面。
        注意 kCGWindowOwnerName 是本地化名称，必须按 PID 匹配。
        """
        helper = os.path.join(os.path.dirname(__file__), "..", "..",
                              "scripts", "find_window.swift")
        if not os.path.exists(helper):
            return None
        try:
            pid = subprocess.run(
                ["pgrep", "-x", app_name],
                capture_output=True, text=True, timeout=3
            ).stdout.strip().split("\n")[0]
            if not pid:
                return None
            result = subprocess.run(
                ["swift", helper, pid],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split("\n")[0])
        except Exception as e:
            logger.bind(tag=TAG).debug(f"解析 {app_name} 窗口 id 失败: {e}")
        return None

    def window_exists(self, app_name: str) -> bool:
        """应用是否有可捕获的窗口（bounds 或窗口 id 任一可读即可）"""
        if self.get_window_bounds(app_name):
            return True
        return self._resolve_window_id(app_name) is not None

    def start_capture(self, app_name: str, fps: float = 3.0) -> Optional[CaptureSession]:
        """启动对指定应用的窗口捕获"""
        with self._lock:
            if app_name in self._sessions and self._sessions[app_name].running:
                return self._sessions[app_name]

        # 检查应用是否运行
        if not self.is_app_running(app_name):
            logger.bind(tag=TAG).warning(f"{app_name} 未在运行")
            return None

        # 获取窗口 bounds（-R 兜底用）。不再 activate——应用应在浏览器后面后台运行，
        # 把窗口拉到最前会把看板盖住（用户明确要求浏览器始终最上层）
        time.sleep(0.3)
        bounds = self.get_window_bounds(app_name)

        # 解析窗口 ID（-l 捕获，支持后台/被遮挡窗口）
        window_id = self._resolve_window_id(app_name)
        if window_id:
            logger.bind(tag=TAG).info(f"{app_name} 窗口 id={window_id}（-l 窗口捕获）")
        elif not bounds:
            logger.bind(tag=TAG).warning(f"无法获取 {app_name} 窗口边界且未解析到窗口 id")
            return None
        else:
            logger.bind(tag=TAG).warning(f"{app_name} 未解析到窗口 id，退回区域捕获（-R）")

        left, top, right, bottom = bounds if bounds else (0, 0, 800, 600)
        w = right - left
        h = bottom - top
        if w <= 0 or h <= 0:
            w, h = 800, 600

        # 创建 session
        session = CaptureSession(
            app_name=app_name,
            window_bounds=bounds,
            fps=fps,
            window_id=window_id,
            frame_a=os.path.join(self._tmpdir, f"{app_name}_a.png"),
            frame_b=os.path.join(self._tmpdir, f"{app_name}_b.png"),
        )

        # 启动捕获线程
        session.running = True
        session._stop_event.clear()
        session._thread = threading.Thread(
            target=self._capture_loop, args=(session, left, top, w, h),
            daemon=True, name=f"capture-{app_name}"
        )
        session._thread.start()

        with self._lock:
            self._sessions[app_name] = session

        logger.bind(tag=TAG).info(f"开始捕获 {app_name} 窗口 ({w}x{h} @ {left},{top}) {fps}fps")
        return session

    def stop_capture(self, app_name: str):
        """停止指定应用的捕获"""
        with self._lock:
            session = self._sessions.get(app_name)
        if not session:
            return
        session.running = False
        session._stop_event.set()
        if session._thread and session._thread.is_alive():
            session._thread.join(timeout=3)
        with self._lock:
            self._sessions.pop(app_name, None)
        # 清理临时文件
        for f in [session.frame_a, session.frame_b]:
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        logger.bind(tag=TAG).info(f"停止捕获 {app_name}")

    def stop_all(self):
        """停止所有捕获"""
        with self._lock:
            names = list(self._sessions.keys())
        for name in names:
            self.stop_capture(name)

    def get_session(self, app_name: str) -> Optional[CaptureSession]:
        with self._lock:
            return self._sessions.get(app_name)

    def list_sessions(self) -> list:
        with self._lock:
            return [
                {"app": s.app_name, "running": s.running,
                 "bounds": s.window_bounds, "fps": s.fps}
                for s in self._sessions.values()
            ]

    def _capture_loop(self, session: CaptureSession, x: int, y: int, w: int, h: int):
        """后台捕获循环：交替写入 ping-pong buffer"""
        interval = 1.0 / session.fps
        region = f"{x},{y},{w},{h}"
        use_a = True

        while session.running and not session._stop_event.is_set():
            target = session.frame_a if use_a else session.frame_b
            try:
                if session.window_id:
                    # 窗口 ID 捕获：能拍到被遮挡的后台窗口（用户要求浏览器最上层）
                    result = subprocess.run(
                        ["screencapture", "-x", "-l", str(session.window_id), target],
                        capture_output=True, timeout=3
                    )
                else:
                    # 区域捕获兜底：只能拍到屏幕上可见的最上层像素
                    result = subprocess.run(
                        ["screencapture", "-x", "-R", region, target],
                        capture_output=True, timeout=3
                    )
                if result.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0:
                    with session._lock:
                        session._active_file = target
                    session._fail_count = 0
                else:
                    session._fail_count += 1
                    # 窗口可能被重建（id 变化）或锁屏，连续失败 6 次后重查窗口 id
                    if session.window_id and session._fail_count >= 6:
                        new_id = self._resolve_window_id(session.app_name)
                        if new_id:
                            session.window_id = new_id
                        session._fail_count = 0
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                logger.bind(tag=TAG).debug(f"捕获帧失败: {e}")

            use_a = not use_a
            session._stop_event.wait(interval)

        session.running = False


# 全局单例
_engine: Optional[MacCaptureEngine] = None


def get_capture_engine() -> MacCaptureEngine:
    global _engine
    if _engine is None:
        _engine = MacCaptureEngine()
    return _engine
