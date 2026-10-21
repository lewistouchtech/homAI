"""
Morning Digest — 每日语音简报（2026-09-11，参考 OpenJarvis morning_digest）

数据全部来自现有设施：
- 天气：和风 API（plugins.get_weather 的 key/host/默认城市）
- 新闻：newsnow API（与 get_news_from_newsnow 同源）
- 日程：devices.db 的 family_members + member_schedules（今日）
- 家居状态：HA REST /api/states 聚合（plugins.home_assistant）
文案由现有 LLM（config LLM.UserDefinedLLM）生成，TTS 走 MOSS-Nano 8086（zh_3），
WebSocket 广播 type="announce" + audio_b64（网页终端播放；ESP32 端二期走 device_handlers）。
"""
import asyncio
import json
import sqlite3
import time
from datetime import date

import aiohttp

from config.logger import setup_logging
from core.utils.assistant_identity import get_name

TAG = __name__
logger = setup_logging()

_DB_PATH = None  # 延迟到 run_digest 时按 cwd 解析


def _db_path():
    import os
    return os.path.join(os.getcwd(), "data", "devices.db")


# ---------- 数据聚合 ----------

def _evaluate_apple_providers(provs_result) -> str:
    """纯函数：根据 MA providers 列表判断 Apple Music 是否异常，返回提醒文本（空串=正常）。"""
    apple = [
        p for p in (provs_result or [])
        if isinstance(p, dict) and p.get("domain") == "apple_music"
    ]
    if not apple:
        return "Apple Music 音乐源尚未配置，语音点歌功能不可用"
    bad = [
        p for p in apple
        if p.get("last_error") or p.get("status") not in ("loaded", None)
    ]
    if bad:
        names = "、".join(str(p.get("name") or p.get("instance_id") or "apple_music") for p in bad)
        return (
            f"{names} 音乐授权可能已过期，语音点歌会失败，"
            "需要重新配置 Apple Music"
        )
    return ""


async def _get_apple_music_alert(config) -> str:
    """检查 Music Assistant 的 Apple Music provider 状态。

    授权令牌过期时 MA 会把 provider 标记为 error/last_error，
    简报里要一直提醒直到主人处理完毕。任何连接/认证失败都静默跳过
    （返回空串），不能因为音乐服务检查把整条简报打断。
    """
    ma_cfg = config.get("music_assistant") or {}
    if not ma_cfg.get("enabled", False):
        return ""
    host = str(ma_cfg.get("host", "")).strip()
    if not host:
        return ""
    port = int(ma_cfg.get("port", 8095))
    username = str(ma_cfg.get("username", ""))
    password = str(ma_cfg.get("password", ""))

    try:
        import uuid

        import websockets

        async with websockets.connect(
            f"ws://{host}:{port}/ws", open_timeout=8
        ) as ws:

            async def _send(command, args=None):
                mid = str(uuid.uuid4())
                await ws.send(json.dumps({"message_id": mid, "command": command, "args": args or {}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=12)
                    if not raw:
                        continue
                    resp = json.loads(raw)
                    if resp.get("message_id") == mid:
                        return resp

            login = await _send("auth/login", {"username": username, "password": password})
            access_token = (login.get("result") or {}).get("access_token")
            if not access_token:
                logger.bind(tag=TAG).warning(f"[简报] MA 登录失败: {login.get('details') or login.get('result')}")
                return ""
            await _send("auth", {"token": access_token})

            provs = await _send("config/providers")
            return _evaluate_apple_providers(provs.get("result"))
    except Exception as e:
        logger.bind(tag=TAG).warning(
            f"[简报] Apple Music 状态检查失败（本次跳过）: {type(e).__name__} {e}"
        )
        return ""

async def _get_weather(config) -> str:
    """和风天气：实时 + 今日预报。失败返回空串。"""
    try:
        wc = (config.get("plugins") or {}).get("get_weather") or {}
        api_host = wc.get("api_host", "n43aaqvnh7.re.qweatherapi.com")
        api_key = wc.get("api_key", "")
        location = wc.get("default_location", "顺义")
        if not api_key:
            return ""
        async with aiohttp.ClientSession() as s:
            # 城市 lookup
            async with s.get(
                f"https://{api_host}/geo/v2/city/lookup",
                params={"key": api_key, "location": location, "lang": "zh"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                geo = await r.json()
            locs = (geo or {}).get("location") or []
            if not locs:
                return ""
            loc_id = locs[0].get("id")
            city = locs[0].get("name", location)
            # 实时
            async with s.get(
                f"https://{api_host}/v7/weather/now",
                params={"key": api_key, "location": loc_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                now = (await r.json()).get("now") or {}
            # 今日预报
            async with s.get(
                f"https://{api_host}/v7/weather/3d",
                params={"key": api_key, "location": loc_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                daily = ((await r.json()).get("daily") or [{}])[0]
        return (
            f"{city}现在{now.get('text', '未知')}{now.get('temp', '?')}°C，"
            f"今日{daily.get('textDay', '')}，{daily.get('tempMin', '?')}~{daily.get('tempMax', '?')}°C"
        )
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] 天气获取失败: {e}")
        return ""


async def _get_news(config) -> list:
    """newsnow 头条（微博热搜 source=weibo）。失败返回 []。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(
                "https://newsnow.busiyi.world/api/s?id=weibo",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        items = (data or {}).get("items") or []
        return [it.get("title", "") for it in items[:5] if it.get("title")]
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] 新闻获取失败: {e}")
        return []


def _get_schedules() -> list:
    """今日日程（全部成员）。失败返回 []。"""
    try:
        conn = sqlite3.connect(_db_path(), timeout=10)
        try:
            cur = conn.cursor()
            today = str(date.today())
            cur.execute(
                """
                SELECT m.name, s.title, s.time FROM member_schedules s
                JOIN family_members m ON m.id = s.member_id
                WHERE s.date = ? ORDER BY s.time
                """,
                (today,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [f"{name} {tm or ''} {title}" for name, title, tm in rows]
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] 日程获取失败: {e}")
        return []


async def _get_home_status(config) -> str:
    """HA 家居状态：只汇报异常（离线/故障）设备，正常设备不汇报（主人 2026-09-11 要求）。"""
    try:
        ha = (config.get("plugins") or {}).get("home_assistant") or {}
        base = str(ha.get("base_url", "")).rstrip("/")
        token = ha.get("api_key", "")
        if not base or not token:
            return ""
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{base}/api/states",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                states = await r.json()
        # 可控域才纳入巡检（传感器类只读不报错）
        _DOMAINS = ("light.", "switch.", "cover.", "lock.", "fan.", "climate.", "media_player.", "vacuum.")
        bad = [
            e.get("attributes", {}).get("friendly_name") or e["entity_id"]
            for e in states
            if e["entity_id"].startswith(_DOMAINS) and e.get("state") == "unavailable"
        ]
        if not bad:
            return ""  # 无异常 = 不汇报
        if len(bad) <= 5:
            return "离线设备：" + "、".join(bad)
        return f"有 {len(bad)} 个设备离线，包括：" + "、".join(bad[:5]) + " 等"
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] 家居状态获取失败: {e}")
        return ""


async def _get_stranger_events(config) -> str:
    """过去 24 小时 Frigate 陌生人事件汇总（排除家人）。失败/无事件返回空串。"""
    try:
        sec = config.get("security") or {}
        frigate_api = str(sec.get("frigate_api", "")).rstrip("/")
        if not frigate_api:
            return ""
        family = {str(n).strip().lower() for n in (sec.get("family_names") or [])}
        # 也并入 devices.db 家人名单（与 security_listener 同源）
        try:
            conn = sqlite3.connect(_db_path(), timeout=2)
            try:
                rows = conn.execute(
                    "SELECT name FROM family_members WHERE name IS NOT NULL AND name != ''"
                ).fetchall()
                family |= {str(r[0]).strip().lower() for r in rows if r[0]}
            finally:
                conn.close()
        except Exception:
            pass
        after = str(int(time.time()) - 86400)
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{frigate_api}/api/events",
                params={"after": after, "label": "person", "limit": "200"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
        strangers = [
            e for e in data
            if str((e.get("sub_label") or "")).strip().lower() not in family
        ]
        if not strangers:
            return ""
        cam_names = {str(k): str(v) for k, v in (sec.get("camera_names") or {}).items()}
        counts = {}
        for e in strangers:
            cam = cam_names.get(str(e.get("camera", "")), str(e.get("camera", "")))
            counts[cam] = counts.get(cam, 0) + 1
        parts = [f"{cam}{n}次" for cam, n in counts.items()]
        return "过去 24 小时陌生人活动：" + "、".join(parts)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] 陌生人事件获取失败: {e}")
        return ""


# ---------- 文案生成 ----------

async def _compose_script(config, weather, news, schedules, home_status) -> str:
    """LLM 生成播报文案；失败降级为模板拼接。"""
    today = date.today().strftime("%Y年%m月%d日")
    weekday = "一二三四五六日"[date.today().weekday()]
    fallback_parts = [f"早上好，今天是{today}，星期{weekday}。"]
    if weather:
        fallback_parts.append(f"天气：{weather}。")
    if schedules:
        fallback_parts.append("今日日程：" + "；".join(schedules) + "。")
    if news:
        fallback_parts.append("新闻热点：" + "；".join(news[:3]) + "。")
    if home_status:
        fallback_parts.append(f"家居状态：{home_status}。")
    fallback = "".join(fallback_parts)

    try:
        # 2026-10-17：原写死读 LLM.UserDefinedLLM（与「豆包大模型」配置完全重复，
        # 已作为僵尸条目删除）。改为读当前 selected_module.LLM 指向的条目，
        # 这样切换大模型时晨报自动跟着走，不必再维护一份重复配置。
        llm_section = config.get("LLM") or {}
        current = str((config.get("selected_module") or {}).get("LLM") or "").strip()
        llm_cfg = llm_section.get(current) or llm_section.get("豆包大模型") or {}
        base = str(llm_cfg.get("base_url") or llm_cfg.get("url") or "").rstrip("/")
        key = str(llm_cfg.get("api_key") or "")
        model = str(llm_cfg.get("model_name") or llm_cfg.get("model") or "").strip()
        if not (base and key and model):
            return fallback
        prompt = (
            f"你是家庭AI管家{get_name()}。根据以下信息生成一段早安播报，口语化、有温度、不超过150字，"
            "不要列点、不要emoji、直接输出播报文本。\n\n"
            f"日期：{today} 星期{weekday}\n"
            f"天气：{weather or '未知'}\n"
            f"今日日程：{'；'.join(schedules) if schedules else '无'}\n"
            f"新闻热点：{'；'.join(news[:3]) if news else '无'}\n"
            + (f"需要注意的异常设备：{home_status}\n" if home_status else "")
        )
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 800, "temperature": 0.7,
                      "enable_thinking": False},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                raw = await r.text()
        try:
            data = json.loads(raw)
        except Exception:
            logger.bind(tag=TAG).warning(f"[简报] LLM 响应非 JSON: {raw[:200]}")
            return fallback
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            logger.bind(tag=TAG).warning(f"[简报] LLM 返回空文案: {raw[:200]}")
        return text or fallback
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] LLM 文案失败，降级模板: {type(e).__name__} {e!r}")
        return fallback


# ---------- 播报 ----------

async def _tts_base64(text: str, tts_url: str):
    """MOSS-Nano 8086 合成 → base64(wav)"""
    try:
        import base64
        async with aiohttp.ClientSession() as s:
            async with s.post(
                tts_url,
                json={"text": text, "voice": "zh_3", "speed": 1.0, "volume": 5.0},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    return None
                return base64.b64encode(await r.read()).decode()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[简报] TTS 合成失败: {e}")
        return None


async def run_digest(server, config):
    """执行一次简报：聚合 → 文案 → TTS → 广播。返回文案文本。"""
    md_cfg = config.get("morning_digest") or {}
    tts_url = str(md_cfg.get("tts_url", "http://127.0.0.1:8086/api/tts"))

    weather, news = await _get_weather(config), await _get_news(config)
    schedules = _get_schedules()
    home_status = await _get_home_status(config)
    apple_alert = await _get_apple_music_alert(config)
    script = await _compose_script(config, weather, news, schedules, home_status)
    # 服务异常提醒必须原样保留，不交给 LLM 改写，防止被省略
    if apple_alert:
        script = f"{script} 最后提醒一下：{apple_alert}。"
    logger.bind(tag=TAG).info(f"[简报] 文案: {script[:60]}...")

    audio_b64 = await _tts_base64(script, tts_url)
    payload = {"type": "announce", "kind": "morning_digest", "text": script,
               "ts": int(time.time())}
    if audio_b64:
        payload["audio_b64"] = audio_b64
    body = json.dumps(payload, ensure_ascii=False)

    sent = 0
    if server is not None:
        async with server.connections_lock:
            conns = list(server.active_connections)
        for ws in conns:
            try:
                await ws.send(body)
                sent += 1
            except Exception:
                pass
    logger.bind(tag=TAG).info(f"[简报] 广播完成，送达 {sent} 个连接（{'含语音' if audio_b64 else '仅文本'}）")
    return script


def setup_morning_digest(server, config, scheduler):
    """注册 morning_digest 定时任务 + 调度器触发回调。"""
    md_cfg = config.get("morning_digest") or {}
    if not md_cfg.get("enabled", False):
        logger.bind(tag=TAG).info("[简报] morning_digest 未启用（config morning_digest.enabled=false）")
        return

    time_str = str(md_cfg.get("time", "07:30"))
    schedule = {"kind": "daily", "time": time_str}

    # 幂等注册：已有同名每日任务则跳过
    existing = [t for t in scheduler.list_tasks()
                if t.get("kind") == "morning_digest" and t.get("schedule", {}).get("kind") == "daily"]
    if not existing:
        scheduler.add_task("每日早安简报", schedule, kind="morning_digest")

    async def on_fire(task):
        if task.get("kind") == "morning_digest":
            await run_digest(server, config)
        elif task.get("kind") == "reminder":
            # 提醒类：TTS 播报提醒文本
            text = task.get("text") or task.get("name", "")
            tts_url_local = str(md_cfg.get("tts_url", "http://127.0.0.1:8086/api/tts"))
            audio_b64 = await _tts_base64(f"提醒：{text}", tts_url_local)
            payload = {"type": "announce", "kind": "reminder", "text": f"提醒：{text}",
                       "ts": int(time.time())}
            if audio_b64:
                payload["audio_b64"] = audio_b64
            body = json.dumps(payload, ensure_ascii=False)
            if server is not None:
                async with server.connections_lock:
                    conns = list(server.active_connections)
                for ws in conns:
                    try:
                        await ws.send(body)
                    except Exception:
                        pass
                logger.bind(tag=TAG).info(f"[调度器] 提醒已播报: {text}")

    # 叠加回调（reminder 插件也用同一个 scheduler，不覆盖已有 handler）
    prev_handler = scheduler._fire_handler
    if prev_handler is None:
        scheduler.set_fire_handler(on_fire)
    else:
        async def chained(task):
            await prev_handler(task)
            await on_fire(task)
        scheduler.set_fire_handler(chained)

    logger.bind(tag=TAG).info(f"[简报] 已注册每日 {time_str} 早安简报")
