# -*- coding: utf-8 -*-
"""车牌绑定 API：后台维护"车牌 ↔ 车主"，支撑车牌三段式反馈

设计沿用 face_api 的自包含路由模式（create_plate_api_routes()），
不改动 family_api，降低对既有接口的影响面。

接口：
    GET    /api/plate/bindings            列出全部绑定
    POST   /api/plate/bindings            新增/更新绑定
    DELETE /api/plate/bindings?plate=&member_id=   解绑（member_id 省略则解绑该车牌全部）
    GET    /api/plate/candidates?plate=   查候选驾驶员（调试/演示用）
"""
from aiohttp import web

from config.logger import setup_logging
from core.utils import plate_bindings

logger = setup_logging()
TAG = "plate_api"


async def handle_list_bindings(request):
    """GET /api/plate/bindings → 全部绑定"""
    try:
        rows = plate_bindings.list_all()
        return web.json_response({"success": True, "bindings": rows,
                                  "count": len(rows)})
    except Exception as e:
        logger.bind(tag=TAG).error(f"[车牌] 列表失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def handle_bind(request):
    """POST /api/plate/bindings → 绑定车牌到成员

    body: {"plate": "京A12345", "member_id": 15,
           "is_primary": true, "label": "家里唯一的车"}
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "请求体不是合法 JSON"},
                                 status=400)

    plate = str(data.get("plate") or "").strip()
    if not plate:
        return web.json_response({"success": False, "message": "plate 不能为空"},
                                 status=400)

    member_id = data.get("member_id")
    if member_id is not None:
        try:
            member_id = int(member_id)
        except (TypeError, ValueError):
            return web.json_response(
                {"success": False, "message": "member_id 必须是整数"}, status=400)

    result = plate_bindings.bind(
        plate, member_id,
        is_primary=bool(data.get("is_primary", False)),
        label=str(data.get("label") or ""),
    )
    if not result.get("success"):
        return web.json_response(result, status=400)
    logger.bind(tag=TAG).info(
        f"[车牌] 绑定 {result['plate']} → member_id={member_id} "
        f"主驾={result['is_primary']}")
    return web.json_response(result)


async def handle_unbind(request):
    """DELETE /api/plate/bindings?plate=京A12345[&member_id=15]"""
    plate = request.query.get("plate", "").strip()
    if not plate:
        return web.json_response({"success": False, "message": "plate 不能为空"},
                                 status=400)
    raw_mid = request.query.get("member_id")
    member_id = None
    if raw_mid:
        try:
            member_id = int(raw_mid)
        except ValueError:
            return web.json_response(
                {"success": False, "message": "member_id 必须是整数"}, status=400)

    result = plate_bindings.unbind(plate, member_id)
    if not result.get("success"):
        return web.json_response(result, status=400)
    logger.bind(tag=TAG).info(
        f"[车牌] 解绑 {plate}"
        f"{'（全部）' if member_id is None else f' member_id={member_id}'}"
        f"，移除 {result.get('removed')} 条")
    return web.json_response(result)


async def handle_candidates(request):
    """GET /api/plate/candidates?plate=京A12345

    返回该车牌的候选驾驶员列表。**空列表表示访客车**（未登记），
    调用方不应据此认定任何身份——这是"车牌≠身份"原则的接口体现。
    """
    plate = request.query.get("plate", "").strip()
    if not plate:
        return web.json_response({"success": False, "message": "plate 不能为空"},
                                 status=400)
    try:
        cands = plate_bindings.get_candidates(plate)
        return web.json_response({
            "success": True,
            "plate": plate_bindings.normalize_plate(plate),
            "known": len(cands) > 0,
            "candidates": cands,
            # 一车多人时单靠车牌无法定人，必须由人脸/声纹确认
            "ambiguous": len(cands) > 1,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"[车牌] 查候选失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


def create_plate_api_routes():
    return [
        web.get("/api/plate/bindings", handle_list_bindings),
        web.post("/api/plate/bindings", handle_bind),
        web.delete("/api/plate/bindings", handle_unbind),
        web.get("/api/plate/candidates", handle_candidates),
    ]
