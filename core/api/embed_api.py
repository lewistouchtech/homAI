# -*- coding: utf-8 -*-
"""Mac 应用嵌入 - HTTP API 端点

提供：
  GET  /api/mac-embed/stream/{app_name}  → MJPEG 视频流
  POST /api/mac-embed/start              → 启动捕获
  POST /api/mac-embed/stop               → 停止捕获
  GET  /api/mac-embed/sessions           → 列出活跃会话
"""
import asyncio
import json
import time
from aiohttp import web

from config.logger import setup_logging
from core.api.mac_capture import get_capture_engine
from core.api.stage_bounds import get_stage_bounds, set_stage_bounds

logger = setup_logging()
TAG = "mac-embed-api"


async def handle_mjpeg_stream(request):
    """MJPEG 流端点 - 前端 <img> 直接引用"""
    app_name = request.match_info.get("app_name", "")
    if not app_name:
        return web.json_response({"error": "缺少 app_name"}, status=400)

    engine = get_capture_engine()
    session = engine.get_session(app_name)

    # 如果还没启动，自动启动
    if not session or not session.running:
        session = engine.start_capture(app_name)
    if not session:
        return web.json_response({"error": f"无法捕获 {app_name}，请确认应用正在运行"}, status=404)

    resp = web.StreamResponse()
    resp.content_type = "multipart/x-mixed-replace; boundary=frame"
    resp.headers["Cache-Control"] = "no-cache, no-store"
    resp.headers["X-Accel-Buffering"] = "no"  # 禁用 nginx 缓冲
    await resp.prepare(request)

    try:
        while session.running:
            frame_path = session.get_latest_frame()
            if frame_path:
                try:
                    with open(frame_path, "rb") as f:
                        frame_data = f.read()
                    if frame_data:
                        await resp.write(
                            b"--frame\r\n"
                            b"Content-Type: image/png\r\n"
                            b"Content-Length: " + str(len(frame_data)).encode() + b"\r\n"
                            b"\r\n" + frame_data + b"\r\n"
                        )
                except (FileNotFoundError, PermissionError):
                    pass
            await asyncio.sleep(1.0 / max(session.fps, 1))
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # 客户端断开

    return resp


async def handle_start_capture(request):
    """启动捕获"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    app_name = data.get("app_name", "")
    fps = data.get("fps", 3)
    if not app_name:
        return web.json_response({"error": "缺少 app_name"}, status=400)

    engine = get_capture_engine()
    session = engine.start_capture(app_name, fps=fps)
    if not session:
        return web.json_response({"success": False, "error": f"无法启动 {app_name} 的捕获"}, status=400)

    return web.json_response({
        "success": True,
        "app": session.app_name,
        "bounds": session.window_bounds,
        "fps": session.fps,
        "stream_url": f"/api/mac-embed/stream/{app_name}",
    })


async def handle_stop_capture(request):
    """停止捕获"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    app_name = data.get("app_name", "")
    if not app_name:
        engine = get_capture_engine()
        engine.stop_all()
        return web.json_response({"success": True, "message": "已停止所有捕获"})

    engine = get_capture_engine()
    engine.stop_capture(app_name)
    return web.json_response({"success": True, "message": f"已停止 {app_name}"})


async def handle_list_sessions(request):
    """列出活跃会话"""
    engine = get_capture_engine()
    return web.json_response({"sessions": engine.list_sessions()})


async def handle_report_stage_bounds(request):
    """前端报告舞台区域的真实屏幕坐标"""
    try:
        data = await request.json()
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        width = int(data.get("width", 800))
        height = int(data.get("height", 600))
        set_stage_bounds(x, y, width, height)
        logger.bind(tag=TAG).info(f"[舞台坐标] 前端报告: x={x} y={y} w={width} h={height}")
        return web.json_response({"success": True, "bounds": get_stage_bounds()})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_get_stage_bounds(request):
    """获取当前舞台坐标"""
    return web.json_response(get_stage_bounds())


def setup_mac_embed_api_routes():
    """返回 Mac 嵌入相关路由列表（用于 add_routes）"""
    return [
        web.get("/api/mac-embed/stream/{app_name}", handle_mjpeg_stream),
        web.post("/api/mac-embed/start", handle_start_capture),
        web.post("/api/mac-embed/stop", handle_stop_capture),
        web.get("/api/mac-embed/sessions", handle_list_sessions),
        web.post("/api/mac-embed/stage-bounds", handle_report_stage_bounds),
        web.get("/api/mac-embed/stage-bounds", handle_get_stage_bounds),
    ]
