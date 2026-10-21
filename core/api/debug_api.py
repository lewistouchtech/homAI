"""
调试模式 API（主人 2026-09-09 需求）
后台"调试"按钮 → 按功能模块勾选进入测试态，绕过时间/冷却/次数限制。
摄像头安防模块：强制视为深夜、绕过家人冷却、缩短判案等待、告警不设冷却。
状态存 data/debug_mode.json（无需重启，监听端每次事件时实时读取）。
"""
import json
from aiohttp import web
from pathlib import Path

DEBUG_FILE = Path(__file__).parent.parent.parent / "data" / "debug_mode.json"

# 可勾选的功能模块（将来其他功能在此扩展，前端自动跟随）
MODULES = {
    "camera": "摄像头安防（人脸识别欢迎/深夜告警/访客提示）",
}


def _read_state():
    try:
        with open(DEBUG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "modules": {}}


def _write_state(state):
    DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def get_debug_mode(request: web.Request) -> web.Response:
    """查询调试模式状态"""
    state = _read_state()
    return web.json_response({"code": 0, "data": {**state, "module_names": MODULES}})


async def set_debug_mode(request: web.Request) -> web.Response:
    """设置调试模式：{"enabled": bool, "modules": {"camera": bool, ...}}"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": 1, "message": "请求体不是合法 JSON"}, status=400)
    enabled = bool(body.get("enabled", False))
    modules = {k: bool(v) for k, v in (body.get("modules") or {}).items()}
    # 关闭总开关时清空模块勾选（避免残留状态）
    if not enabled:
        modules = {}
    state = {"enabled": enabled, "modules": modules}
    _write_state(state)
    on_list = [k for k, v in modules.items() if v]
    return web.json_response({
        "code": 0,
        "message": f"调试模式{'开启，测试模块: ' + '、'.join(on_list) if on_list else '关闭'}",
        "data": state,
    })
