# -*- coding: utf-8 -*-
"""人脸录入 API：控制台"家庭成员"处注册人脸到 Frigate 原生人脸识别

链路：控制台"录入人脸"弹窗 → 本 API（代理，规避 Frigate HTTPS 自签跨域）→ Frigate
注册方式（POST /api/face/register）：
  - image:           前端电脑摄像头拍照（base64 dataURL）→ 直传注册
  - source="camera": 抓监控实时帧（备用）
  - event_id:        Frigate 事件快照直接注册
其余：实时帧/事件列表/事件快照/已注册名单代理，前端统一走本模块。
"""
import aiohttp
import json
import ssl
import time
from pathlib import Path

import yaml
from aiohttp import web

from config.logger import setup_logging
from config.config_loader import load_yaml_expanded

logger = setup_logging()
TAG = "face_api"

# 项目根：server/homebrain-server/core/api/face_api.py → 上溯 4 级
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_TRAIN_DIR = _PROJECT_ROOT / "integrations/frigate/media/clips/faces/train"
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE  # Frigate HTTPS 自签

# 人脸库端点要求 admin 角色（remote-role 头，角色须在 Frigate auth.roles 定义）
_FRIGATE_HEADERS = {"remote-user": "homebrain", "remote-role": "admin"}


def _frigate_cfg():
    """读 config.yaml plugins.face_register（缺省回退本机 Frigate）"""
    try:
        # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
        cfg = load_yaml_expanded(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        sec = (cfg.get("plugins") or {}).get("face_register") or {}
        # 默认走 Frigate 内部 5000 端口（自动 admin 角色；8971 经 nginx 会剥 remote-* 头）
        return sec.get("api_url", "http://localhost:5000"), sec.get("camera", "cam_1")
    except Exception:
        return "http://localhost:5000", "cam_1"


async def _frigate_get(path: str):
    """代理 GET Frigate，返回 (status, body_bytes, content_type)"""
    api, _ = _frigate_cfg()
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{api}{path}", ssl=_SSL_CTX, headers=_FRIGATE_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status, await r.read(), r.headers.get("Content-Type", "application/json")


async def handle_face_frame(request):
    """GET /api/face/frame → 摄像头实时帧（latest.jpg）"""
    try:
        _, camera = _frigate_cfg()
        status, body, ctype = await _frigate_get(f"/api/{camera}/latest.jpg")
        if status != 200:
            return web.json_response({"success": False, "message": f"Frigate 返回 {status}"}, status=502)
        return web.Response(body=body, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[人脸] 实时帧获取失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=502)


async def handle_face_events(request):
    """GET /api/face/events → 最近事件列表（仅含已抓拍快照的，供选图注册）"""
    try:
        status, body, _ = await _frigate_get("/api/events?limit=15")
        events = json.loads(body) if status == 200 else []
        out = [{"id": e.get("id"), "camera": e.get("camera"),
                "label": e.get("label"),
                "start_time": e.get("start_time"),
                "has_snapshot": e.get("has_snapshot", False)}
               for e in events if e.get("has_snapshot")]
        return web.json_response({"success": True, "events": out[:12]})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[人脸] 事件列表失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=502)


async def handle_face_snapshot(request):
    """GET /api/face/snapshot/{event_id} → 事件快照图"""
    try:
        event_id = request.match_info["event_id"]
        status, body, _ = await _frigate_get(f"/api/events/{event_id}/snapshot.jpg")
        if status != 200:
            return web.json_response({"success": False, "message": f"Frigate 返回 {status}"}, status=502)
        return web.Response(body=body, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[人脸] 快照获取失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=502)


async def handle_face_list(request):
    """GET /api/face/list → 已注册人脸名单（GET /faces）"""
    try:
        status, body, _ = await _frigate_get("/api/faces")
        faces = json.loads(body) if status == 200 else {}
        faces.pop("train", None)  # 内部训练目录不对客户端展示
        return web.json_response({"success": True, "faces": faces})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[人脸] 名单获取失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=502)


async def _register_upload(name: str, img: bytes):
    """调 Frigate POST /api/faces/{name}/register（multipart 上传，Frigate 自行人脸检测）
    返回 (ok, message)"""
    api, _ = _frigate_cfg()
    async with aiohttp.ClientSession() as s:
        form = aiohttp.FormData()
        form.add_field("file", img, filename="face.jpg", content_type="image/jpeg")
        async with s.post(f"{api}/api/faces/{name}/register",
                          data=form, ssl=_SSL_CTX, headers=_FRIGATE_HEADERS,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            try:
                data = await r.json()
            except Exception:
                data = {"message": await r.text()}
            ok = r.status == 200 and data.get("success", True)
            msg = data.get("message") or ("" if ok else f"HTTP {r.status}")
            return ok, msg


async def handle_face_register(request):
    """POST /api/face/register  body: {name, image:"data:image/jpeg;base64,..."} 或
    {name, source:"camera"} / {name, event_id}
    三种方式最终都把图片直传 Frigate register（Frigate 自行检测并入库）"""
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            return web.json_response({"success": False, "message": "缺少姓名"}, status=400)

        data_url = body.get("image")
        if data_url:
            # 前端电脑摄像头拍照：base64 dataURL 直传
            import base64
            try:
                img = base64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                return web.json_response({"success": False, "message": "图片数据解析失败"}, status=400)
        elif body.get("event_id"):
            status, img, _ = await _frigate_get(
                f"/api/events/{body['event_id']}/snapshot.jpg")
            if status != 200:
                return web.json_response({"success": False, "message": "获取事件快照失败"}, status=502)
        else:
            _, camera = _frigate_cfg()
            status, img, _ = await _frigate_get(f"/api/{camera}/latest.jpg")
            if status != 200:
                return web.json_response({"success": False, "message": "抓取摄像头画面失败"}, status=502)
        ok, msg = await _register_upload(name, img)
        logger.bind(tag=TAG).info(f"[人脸] 注册 {name}: {'成功' if ok else f'失败 {msg}'}")
        return web.json_response({"success": ok, "message": msg})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[人脸] 注册异常: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def handle_camera_snapshot(request):
    """GET /api/frigate/{camera_id}/latest.jpg → 指定摄像头实时快照（8003 同源代理，供看板弹窗）"""
    camera = request.match_info["camera_id"]
    try:
        status, body, _ = await _frigate_get(f"/api/{camera}/latest.jpg")
        if status != 200:
            return web.json_response({"success": False, "message": f"Frigate 返回 {status}"}, status=502)
        return web.Response(body=body, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[快照] {camera} 获取失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=502)


def create_face_api_routes():
    return [
        web.get("/api/face/frame", handle_face_frame),
        web.get("/api/face/events", handle_face_events),
        web.get("/api/face/snapshot/{event_id}", handle_face_snapshot),
        web.get("/api/face/list", handle_face_list),
        web.post("/api/face/register", handle_face_register),
        web.get("/api/frigate/{camera_id}/latest.jpg", handle_camera_snapshot),
    ]
