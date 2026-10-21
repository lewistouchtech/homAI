"""吃药提醒调度器（2026-10-11）

按 medications 表的服药时间到点播报：
- 每 30 秒扫一次，匹配 HH:MM（±1 分钟内只触发一次）
- 话术包含药名、剂量、饭前/饭后
- 广播格式与安防一致：{"type": "medication_reminder", text, audio_b64}
  前端收到后弹条 + 播语音（数字星球/LED 各自渲染）
- 失败绝不影响主服务
"""
import asyncio
import json
import logging

import aiohttp

from core.api.medication_api import list_medications

logger = logging.getLogger(__name__)

_scan_interval = 30  # 秒
_reminded_today = set()  # {(med_id, HH:MM, YYYY-MM-DD)}


def _meal_txt(m):
    mr = m.get('meal_relation') or '任意'
    mins = m.get('meal_minutes') or 0
    if mr == '任意' or mr == '随餐':
        return mr
    return f"{mr}{mins}分钟" if mins else mr


async def _tts_base64(text, tts_url):
    """调本地 TTS 服务合成语音。失败返回 None（降级纯文本广播）。"""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(tts_url, json={"text": text}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    audio = data.get("audio") or data.get("audio_b64")
                    return audio
    except Exception:
        pass
    return None


async def _remind_once(server, tts_url):
    from datetime import datetime
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    hm = now.strftime('%H:%M')

    meds = list_medications(include_inactive=False)
    fired = []
    for m in meds:
        if not m.get('remind'):
            continue
        for t in (m.get('times') or []):
            if t != hm:
                continue
            key = (m['id'], t, today)
            if key in _reminded_today:
                continue
            _reminded_today.add(key)
            dose = f" {m['dose']}" if m.get('dose') else ''
            meal = _meal_txt(m)
            text = (f"主人，现在{t}，该吃{m['name']}{dose}了，{meal}服用。"
                    f"记得按时吃药，不要自行停药哦。")
            audio_b64 = await _tts_base64(text, tts_url)
            payload = {"type": "medication_reminder",
                       "medication": m['name'], "text": text}
            if audio_b64:
                payload["audio_b64"] = audio_b64
            body = json.dumps(payload, ensure_ascii=False)
            async with server.connections_lock:
                conns = list(server.active_connections)
            sent = 0
            for ws in conns:
                try:
                    await ws.send(body)
                    sent += 1
                except Exception:
                    pass
            logger.info(f"[用药提醒] 已播报：{text}（送达 {sent} 个连接）")
            fired.append(m['name'])
    # 内存防重集合每天清理一次（防无限增长）
    if now.strftime('%H:%M') == '00:00':
        _reminded_today.clear()
    return fired


async def medication_reminder_loop(server, config, interval=_scan_interval):
    """后台循环。config 用于读 TTS 地址。"""
    tts_url = "http://127.0.0.1:8086/api/tts"
    try:
        sec = (config or {}).get('security') or {}
        tts_url = str(sec.get('tts_url') or tts_url)
    except Exception:
        pass
    logger.info(f"[用药提醒] 调度器已启动（间隔 {interval}s，TTS: {tts_url}）")
    while True:
        try:
            await _remind_once(server, tts_url)
        except Exception as e:
            logger.warning(f"[用药提醒] 本轮扫描失败（不影响主服务）: {e}")
        await asyncio.sleep(interval)
