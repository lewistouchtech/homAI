# -*- coding: utf-8 -*-
"""控制台「页面状态」API（localStorage 的服务器版，2026-10-14）

    GET    /api/console/state           → 一次拉全部（页面初始化用）
    POST   /api/console/state/{key}     → 覆盖写，body: {"value": <任意 JSON>}
    DELETE /api/console/state/{key}     → 删除

为什么需要（主人 2026-10-14）：管理台同时能用 http://<IP>:8003 与 https://<IP>:8443
两个地址打开，而 localStorage 按来源隔离 → 同一个后台页面在两个地址下状态不一致。
统一放服务器后，从哪个地址进去看到的都一样。

存储实现见 core/utils/console_ui_state.py。
"""

from aiohttp import web

from core.utils import console_ui_state


async def handle_load_all(request):
    return web.json_response({'success': True, 'states': console_ui_state.load_all()})


async def handle_save(request):
    key = request.match_info.get('key', '')
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'success': False, 'message': '请求体不是合法 JSON'}, status=400)
    ok, message = console_ui_state.save(key, body.get('value'))
    return web.json_response({'success': ok, 'message': message}, status=200 if ok else 400)


async def handle_delete(request):
    key = request.match_info.get('key', '')
    ok, message = console_ui_state.delete(key)
    return web.json_response({'success': ok, 'message': message}, status=200 if ok else 400)


def create_console_state_routes():
    return [
        web.get('/api/console/state', handle_load_all),
        web.post('/api/console/state/{key}', handle_save),
        web.delete('/api/console/state/{key}', handle_delete),
    ]
