# -*- coding: utf-8 -*-
"""Frigate 安防事件监听：MQTT 订阅 frigate/events → 广播 security 消息给所有前端连接

事件链路：Frigate 检测(person/car等) → MQTT frigate/events → 本监听 →
WebSocket 广播 {"type":"security","level","source","text"} → 前端提示卡+字幕
"""
import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from config.logger import setup_logging

logger = setup_logging()
TAG = __name__

# 同一摄像头+目标类型的事件冷却（秒），防止静止目标反复 enter 刷屏
_EVENT_COOLDOWN = 60

# 家人欢迎防骚扰：同一人冷却（秒），反复经过不反复欢迎
_FAMILY_COOLDOWN = 600

# 目标类型中文映射
LABEL_CN = {
    "person": "有人",
    "car": "有车辆",
    "cat": "有猫",
    "dog": "有狗",
    "bird": "有鸟",
}


def _is_night(now_hour: int, night_start: int, night_end: int) -> bool:
    """跨零点时段判断（如 22~7 表示晚10点到早7点）"""
    if night_start <= night_end:
        return night_start <= now_hour < night_end
    return now_hour >= night_start or now_hour < night_end


def _collect_event(event_type: str, **kwargs):
    """旁路采集：把事件落到 event_log（统计用）。

    2026-09-26 主人确认：这是**额外的、只为统计**的需求，
    因此**任何失败都只记日志、绝不影响安防主链路**。
    """
    try:
        from core.utils import data_collector
        return data_collector.record_event(event_type, **kwargs)
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[安防] 事件采集失败（忽略）: {e}")
        return None


def _ensure_work_mode_column():
    """成员档案增加"办公方式"列（office=楼内办公 / field=常外出）。幂等，失败只记日志。"""
    try:
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(family_members)").fetchall()]
            if cols and "work_mode" not in cols:
                conn.execute("ALTER TABLE family_members ADD COLUMN work_mode TEXT DEFAULT ''")
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 成员办公方式列检查失败（忽略）: {e}")


def _member_extra(name: str) -> dict:
    """成员的 年龄/职业/办公方式；查不到返回空值。"""
    try:
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(family_members)").fetchall()]
            sel = "age, occupation" + (", work_mode" if "work_mode" in cols else "")
            row = conn.execute(f"SELECT {sel} FROM family_members WHERE name = ?", (name,)).fetchone()
        finally:
            conn.close()
        if row:
            return {"age": row[0], "occupation": row[1] or "",
                    "work_mode": (row[2] if len(row) > 2 else "") or ""}
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[安防] 查成员扩展信息失败（忽略）: {e}")
    return {"age": None, "occupation": "", "work_mode": ""}


def _is_school_kid(extra: dict) -> bool:
    age = extra.get("age")
    try:
        young = age is not None and int(age) <= 18
    except Exception:
        young = False
    return young and "学" in str(extra.get("occupation") or "")


def _load_arrival_history(policy) -> int:
    """从 event_log 的 passage 记录恢复进出门状态（重启后仍能算离开时长）。返回恢复条数。"""
    try:
        from core.utils.greeting_policy import _parse_utc_ts
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            rows = conn.execute(
                "SELECT label, extra, created_at FROM event_log "
                "WHERE event_type='passage' ORDER BY id DESC LIMIT 1500").fetchall()
        finally:
            conn.close()
        items = []
        for label, extra, created in reversed(rows):
            try:
                ex = json.loads(extra or "{}")
            except Exception:
                ex = {}
            ts = ex.get("ts") or _parse_utc_ts(created)
            if ts and ex.get("direction") in ("in", "out"):
                items.append((float(ts), ex["direction"], (label or "").lower() or None))
        policy.load_history(items)
        return len(items)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 恢复进出门记录失败（从空状态开始）: {e}")
        return 0


def _extract_name(after) -> str:
    """取事件的人名（Frigate 0.17 原生人脸识别结果 sub_label）。
    兼容 string 与 [name, score] 列表两种格式。"""
    raw = after.get("sub_label")
    if isinstance(raw, list) and raw:
        return str(raw[0]).strip()
    if isinstance(raw, str):
        return raw.strip()
    return ""


# devices.db 家人名单缓存（60 秒刷新，避免每次事件都读 db）
_family_cache = {"ts": 0.0, "names": set(), "extra": set()}
_FAMILY_CACHE_TTL = 60


def _load_family_names() -> set:
    """从 devices.db.family_members 加载家人真名集合（小写），60 秒缓存。
    config.security.family_names 作为兼容白名单并入（不立即失效老配置）。"""
    now = time.time()
    if now - _family_cache["ts"] < _FAMILY_CACHE_TTL:
        return _family_cache["names"]
    try:
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            rows = conn.execute(
                "SELECT name FROM family_members WHERE name IS NOT NULL AND name != ''"
            ).fetchall()
            names = {str(r[0]).strip().lower() for r in rows if r[0]}
        finally:
            conn.close()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 读取家人名单失败（沿用缓存）: {e}")
        names = set(_family_cache.get("names") or set())
    # 合并 config 白名单兜底（extra 由 start_frigate_security_listener 注入；
    # 独立调用/单测时若未注入，尝试现场读 config_loader）
    extra = set(_family_cache.get("extra") or set())
    if not extra:
        try:
            from config.config_loader import load_config
            sec = (load_config() or {}).get("security") or {}
            extra = {str(n).strip().lower() for n in (sec.get("family_names") or [])}
        except Exception:
            pass
    names |= extra
    _family_cache.update(ts=now, names=names)
    return names


def _is_family(name: str) -> bool:
    """是否家人（真名匹配，大小写不敏感）。空名不算。"""
    n = (name or "").strip().lower()
    return bool(n) and n in _load_family_names()


# ── 安防播报的 TTS 引擎解析（2026-10-07）──────────────────────────
# 背景：安防播报原来只会打 8086(MossNano)。主人把主 TTS 换成 Qwen3-TTS 后，
# 安防播报还是 MossNano 的声音 —— 一句一个音色。
# ⚠️ 不能靠 tts_config.provider 判断：switch-provider 只写配置文件、
#    不更新该字段（实测切到 Qwen3 后它仍是 MossNano）。必须读运行时配置。
_ENGINE_CACHE = {"ts": 0.0, "type": "", "cfg": {}}
_ENGINE_CACHE_TTL = 10


def _current_tts_engine_impl():
    """返回 (type, provider配置dict)，读运行时合并配置。失败返回 ("", {})。"""
    now = time.time()
    if now - _ENGINE_CACHE["ts"] < _ENGINE_CACHE_TTL:
        return _ENGINE_CACHE["type"], _ENGINE_CACHE["cfg"]
    etype, pcfg = "", {}
    try:
        from config.config_loader import load_config

        cfg = load_config() or {}
        sel = (cfg.get("selected_module") or {}).get("TTS", "")
        p = (cfg.get("TTS") or {}).get(sel) or {}
        if isinstance(p, dict):
            etype = str(p.get("type") or "")
            pcfg = dict(p)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 读取运行时 TTS 引擎失败: {e}")
    _ENGINE_CACHE.update(ts=now, type=etype, cfg=pcfg)
    return etype, pcfg


# MossNano 音色名 → Qwen3-TTS 预置音色。
# 后台历史数据里 alert_voice=zh_3（MossNano 男声·志明），换 Qwen3 后若原样
# 发给 8087 会当成无效音色回落成默认女声 —— 威慑音就没了。按性别映射。
_MOSS_TO_QWEN3_TABLE = {
    "zh_1": "Uncle_Fu", "zh_3": "Uncle_Fu", "zh_10": "Uncle_Fu",
    "zh_4": "Serena", "zh_6": "Vivian", "zh_11": "Serena",
}


def build_security_tts_request(text, voice, default_moss_url, engine=None):
    """安防播报该打哪个 TTS 服务、带什么参数。

    返回 (url, payload)。纯函数（engine 可注入便于测试），
    不传 engine 时读运行时配置。
    """
    etype, pcfg = engine if engine is not None else _current_tts_engine_impl()
    if etype == "qwen3_tts":
        url = str(pcfg.get("api_url") or "http://127.0.0.1:8087/api/tts")
        speaker = _MOSS_TO_QWEN3_TABLE.get(str(voice or ""), voice) if voice else None
        speaker = speaker or str(pcfg.get("speaker") or "Serena")
        return url, {
            "text": text, "model": pcfg.get("model", "0.6B"), "mode": "custom",
            "speaker": speaker, "language": pcfg.get("language", "Chinese"),
            "instruct": pcfg.get("instruct", ""), "speed": 1.0,
            "temperature": pcfg.get("temperature", 0.9),
            "top_k": pcfg.get("top_k", 50), "top_p": pcfg.get("top_p", 1.0),
            "seed": pcfg.get("seed", 1234),
        }
    payload = {"text": text, "speed": 1.0, "volume": 5.0}
    if voice:
        payload["voice"] = voice
    return default_moss_url, payload


def start_frigate_security_listener(server, loop, config):
    """启动 Frigate MQTT 事件监听（paho 后台线程，回调线程安全投递到主 loop）"""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.bind(tag=TAG).warning("paho-mqtt 未安装，安防事件链路未启动")
        return None

    sec_cfg = config.get("security", {}) or {}
    host = sec_cfg.get("mqtt_host", "127.0.0.1")
    port = int(sec_cfg.get("mqtt_port", 1883))
    # 家人名单：默认走 devices.db.family_members（动态同步，60s 缓存），
    # config.security.family_names 作为额外白名单兜底（老配置不失效）。
    family_names = {str(n).strip().lower()
                    for n in sec_cfg.get("family_names", [])}
    _family_cache["extra"] = family_names
    _load_family_names()  # 预热缓存
    night_start = int(sec_cfg.get("night_start", 22))
    night_end = int(sec_cfg.get("night_end", 7))
    # 摄像头中文名映射（cam_1 → 三楼），播报更友好
    camera_names = {str(k): str(v) for k, v
                    in sec_cfg.get("camera_names", {}).items()}
    # Frigate API（判案时主动查事件最新 sub_label，不依赖 update 消息必达）
    frigate_api = str(sec_cfg.get("frigate_api", "http://localhost:15000")).rstrip("/")
    # 语音播报：走 MOSS-Nano 服务合成 wav（ChatTTS 8082 已删），base64 随 security 消息下发前端播放
    # 【2026-09-11】ChatTTS 8082 服务与 provider 均删；唯一本地 TTS 为 MOSS-Nano 8086
    tts_url = str(sec_cfg.get("tts_url", "http://127.0.0.1:8086/api/tts"))
    # 欢迎语情境化（主人 2026-09-08 要求）：星期/天气/时段/归家异常作输入，
    # LLM 生成有温度的欢迎语（每次不同），失败自动降级内置模板
    def _read_welcome_llm():
        """每次调用时从 YAML 重新读（支持后台切换后热生效，无需重启）。
        读取失败则回退到启动时的 sec_cfg 快照。"""
        try:
            import yaml as _y
            from pathlib import Path as _P
            from config.config_loader import load_yaml_expanded
            p = _P(__file__).parent.parent.parent / "data" / ".config.yaml"
            # 必须展开 ${VAR}：api_key 在配置里是引用，直接 safe_load 会拿到字面量 → 401
            c = load_yaml_expanded(p) or {}
            wl = (c.get("security") or {}).get("welcome_llm") or {}
            if wl.get("base_url"):
                return wl
        except Exception:
            pass
        return sec_cfg.get("welcome_llm", {}) or {}
    # 归家基线：正常到家小时数，用于识别"早归/晚归"并写进欢迎语
    expected_home_hour = int(sec_cfg.get("expected_home_hour", 19))
    # 门口机摄像头：白天的外人播报改用门口机专属话术（"门外有人逗留"等）
    doorbell_cameras = {str(c).strip() for c in sec_cfg.get("doorbell_cameras", [])}
    # 门口机/逗留判定（主人 2026-09-09 需求 + 修正）：后台摄像头管理里每台摄像头
    # 有设备类型（normal/doorbell）+ 两个独立功能开关（徘徊逗留检测/访客提醒，
    # 开启后各设时长）。白天外人只对"功能开启"的摄像头做逗留判定。
    # DB 读取异步后台执行，失败走 config 兜底（兜底视为全部开启、60 秒）
    doorbell_ids = set(doorbell_cameras)
    cam_features = {}   # cid → {role, dwell_on, dwell_s, visitor_on, visitor_s}
    ip2cam_cache = {}   # IP → Frigate camera_id（启动时拉一次，实时判定时复用）

    def _refresh_cam_features():
        """实时重读后台摄像头配置（设备类型/功能开关/时长），后台改完即时生效。
        sqlite 轻量查询 ~1ms；失败沿用上次缓存不中断判定。"""
        import sqlite3
        db_path = Path(__file__).parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT url, role, dwell_enabled, dwell_seconds, "
                "visitor_enabled, visitor_seconds FROM cameras").fetchall()
        finally:
            conn.close()
        for u, role, dw_on, dw_s, vi_on, vi_s in rows:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", str(u))
            cid = ip2cam_cache.get(m.group(1)) if m else None
            if not cid:
                continue
            cam_features[cid] = {
                "role": role or "normal",
                "dwell_on": bool(dw_on), "dwell_s": int(dw_s or 60),
                "visitor_on": bool(vi_on), "visitor_s": int(vi_s or 60),
            }
            if (role or "normal") == "doorbell" and bool(vi_on):
                doorbell_ids.add(cid)

    def _load_doorbell_from_db():
        """从后台 cameras 表加载设备类型与功能开关：摄像头 url 按 IP 与
        Frigate camera_id 匹配。依赖 Frigate /api/config 拿映射。"""
        async def _job():
            import sqlite3
            db_path = Path(__file__).parent.parent.parent / "data" / "devices.db"
            # ① Frigate camera_id → RTSP url 的 IP 映射
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"{frigate_api}/api/config",
                                     timeout=aiohttp.ClientTimeout(total=6)) as r:
                        if r.status == 200:
                            cfgj = await r.json()
                            for cid, cv in (cfgj.get("cameras") or {}).items():
                                inputs = (cv.get("ffmpeg", {}) or {}).get("inputs") or []
                                m = re.search(r"(\d+\.\d+\.\d+\.\d+)",
                                              str((inputs[0] or {}).get("path", "")) if inputs else "")
                                if m:
                                    ip2cam_cache[m.group(1)] = cid
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"[安防] 获取 Frigate 配置失败（门口机走 config 兜底）: {e}")
            # ② 读 DB 设备类型与功能开关，按 IP 匹配
            conn = None
            try:
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT url, role, dwell_enabled, dwell_seconds, "
                    "visitor_enabled, visitor_seconds FROM cameras"
                ).fetchall()
                for u, role, dw_on, dw_s, vi_on, vi_s in rows:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", str(u))
                    cid = ip2cam_cache.get(m.group(1)) if m else None
                    if not cid:
                        continue
                    cam_features[cid] = {
                        "role": role or "normal",
                        "dwell_on": bool(dw_on), "dwell_s": int(dw_s or 60),
                        "visitor_on": bool(vi_on), "visitor_s": int(vi_s or 60),
                    }
                    # 访客提醒开启的门口机 → 门口机话术池
                    if (role or "normal") == "doorbell" and bool(vi_on):
                        doorbell_ids.add(cid)
                if cam_features:
                    logger.bind(tag=TAG).info(
                        f"[安防] 摄像头功能配置已从后台加载: "
                        f"{ {c: (v['role'], '逗留' if v['dwell_on'] else '-', '访客' if v['visitor_on'] else '-') for c, v in sorted(cam_features.items())} }")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[安防] 后台摄像头表读取失败（走 config 兜底）: {e}")
            finally:
                if conn:
                    conn.close()
        asyncio.create_task(_job())

    _load_doorbell_from_db()
    # ── 听门功能已删除（2026-10-06，主人裁决）──────────────────────
    # 原设计：判案定为外人后，收音 6 秒 → FunASR 识别对方喊什么 → 命中
    # 「救命/报警」等词就播报。删除原因（主人原话）：
    #   「如果门口可视对讲机发现有人，人家要么按门铃、要么跟家里人呼叫，
    #接听通话以后在这儿监听 6 秒能听到什么呢？这个逻辑完全不对」
    #   「听到外面有人喊这个跟你有什么关系？你只要不开门就好了」
    # 识别室内呼救属于**室内语音助手**的职责（麦克风/ESP32 唤醒词那一层），
    # 与摄像头安防是两件事。收音客户端本身保留在
    # integrations/doorphone/panasonic_client.py，将来室内场景可复用。
    # 判案轮询参数（参考 frigate-notify recheck_delay：延迟后反复重查直到有结论）
    # 先等 judge_wait 秒，再每 poll_interval 秒查一次，最多 poll_max 次；
    # 最长 judge_wait + (poll_max-1)*poll_interval 秒后必须给结论（约 17 秒）
    judge_wait = int(sec_cfg.get("judge_wait", 5))
    poll_interval = int(sec_cfg.get("judge_poll_interval", 3))
    poll_max = int(sec_cfg.get("judge_poll_max", 5))

    def _debug_camera_on():
        """调试模式（主人 2026-09-09 需求）：data/debug_mode.json
        {"enabled": true, "modules": {"camera": true}} 时摄像头安防进入测试态——
        强制视为深夜、绕过家人冷却、缩短判案等待、放宽告警冷却，随时可测不必等到深夜。
        文件不存在/损坏一律视为关闭（正常运行）。"""
        try:
            from pathlib import Path as _Path
            p = _Path(__file__).parent.parent.parent / "data" / "debug_mode.json"
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return bool(d.get("enabled")) and bool((d.get("modules") or {}).get("camera"))
        except Exception:
            return False

    last_ts = {}
    family_last_ts = {}
    # 已识别出家人的事件 id（延迟判案用）：new 时人脸未识别完，等几秒看识别结果
    family_hits = set()
    # 各摄像头最近一次识别出家人的时间戳：Frigate 会把连续走动拆成多个事件
    # （正脸识别出家人 → 转身/低头跟踪丢失开新事件无脸），家人刚出现过，
    # 宽限期内的新无脸事件大概率是同一人，不得翻脸报陌生人
    family_seen = {}
    family_grace = int((config.get("security", {}) or {}).get("family_grace", 120))
    # 家人头像气泡（LED 鱼缸）：同人冷却秒数，防 Frigate update 高频重复飘气泡
    _bubble_cooldown = int((config.get("security", {}) or {}).get("family_bubble_cooldown", 60))
    bubble_last_ts = {}

    # 话术池：同一场景每次随机换说法，让播报有温度不呆板（主人要求：照此办理到所有播报类功能）
    _WELCOME_LINES = [
        "{name}回来啦～",
        "欢迎回家，{name}～",
        "{name}，你回来啦，今天辛苦了～",
        "呀，{name}回来啦，辛苦啦～",
        "{name}到家啦，好好休息～",
        "叮咚～{name}回家了！",
    ]
    # 同事（关系属员工）：不说"回家/想你"这类家人口吻
    _COLLEAGUE_LINES = [
        "{name}来啦～",
        "{name}，早～",
        "{name}到啦，辛苦了～",
        "{name}来了～",
    ]
    _VISITOR_LINES = [
        "发现一位访客",
        "门口来了一位客人，请留意",
        "有访客到访，去看一眼吧",
        "门外有客人到了",
    ]
    # 门口机专属话术（doorbell_cameras 里摄像头的白天外人事件）
    _DOORBELL_LINES = [
        "门外有人逗留，请留意",
        "发现访客，去看看吧",
        "门口有人，可能是访客",
        "门外来了客人，请留意一下",
    ]
    _STRANGER_LINES = [
        "深夜发现陌生人，请注意！{camera}",
        "警报：{camera}深夜出现陌生人，请留意！",
        "注意！{camera}有陌生人活动，请提高警惕！",
        "{camera}发现可疑人员，深夜请务必小心！",
    ]
    _OBJECT_LINES = {
        "car": ["检测到车辆经过", "门外有车辆经过", "{camera}有车辆出现"],
        "cat": ["有只小猫路过～", "{camera}来了一只小猫", "小猫在{camera}出没～"],
        "dog": ["有只小狗路过～", "{camera}来了一只小狗", "小狗在{camera}出没～"],
    }

    def _pick(pool, **fmt):
        """从话术池随机选一条并填充变量"""
        import random
        return random.choice(pool).format(**fmt)

    # 天气缓存（30 分钟），欢迎语情境用；失败时沿用旧值，不阻塞播报。
    # 主人 2026-09-08 要求：正常天气不提，只有恶劣天气（雨雪雾雷大风等）才值得关怀
    _weather_cache = {"ts": 0.0, "bad": ""}

    async def _weather_desc():
        """恶劣天气描述（正常天气返回空串，不进欢迎语）；本地 /api/weather/current，30 分钟缓存"""
        if _weather_cache["bad"] and time.time() - _weather_cache["ts"] < 1800:
            return _weather_cache["bad"]
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                        "http://localhost:8003/api/weather/current",
                        timeout=aiohttp.ClientTimeout(total=5)) as r:
                    cur = ((await r.json()).get("data") or {}).get("current") or {}
                    cond = str(cur.get("text", ""))
                    temp = str(cur.get("temp", ""))
                    # 恶劣天气关键词：雨雪雾雷风雹沙尘霾；极端温度也算（太冷太热值得关怀）
                    bad = any(k in cond for k in ("雨", "雪", "雾", "雷", "风", "雹", "沙", "尘", "霾"))
                    try:
                        t = int(float(temp))
                        if t <= 0 or t >= 35:
                            bad = True
                    except ValueError:
                        pass
                    text = f"{cond}，{temp}度".strip("，") if bad else ""
                    _weather_cache.update(ts=time.time(), bad=text)
        except Exception:
            pass  # 天气拿不到不阻塞，沿用旧值/空值
        return _weather_cache["bad"]

    def _arrival_note(now=None):
        """归家时间异常描述（对照 expected_home_hour）：早归/晚归超出 2 小时才提示。
        跨零点修正（主人 2026-09-08 指出半夜 12 点被说成"提前回来"）：
        凌晨 0~5 点属于"昨晚熬到深夜才回家"，按 +24h 归一为晚归，
        绝不能算成早归。"""
        import datetime
        now = now or datetime.datetime.now()
        h = now.hour + now.minute / 60.0
        # 凌晨时段归一为前一日的深夜（0 点 → 24 点）
        effective = h + 24 if h < 6 else h
        delta = effective - expected_home_hour
        if delta >= 2:
            return f"今天比平时晚了约{round(delta)}小时才回来"
        if delta <= -2:
            return f"今天比平时早了约{round(-delta)}小时就到家了"
        return "正常时间回家"

    async def _welcome_context(with_arrival_note=True):
        """欢迎语情境变量汇总：星期/工作日周末/时段/归家异常/天气。
        with_arrival_note=False：到访策略已提供真实离开时长，不再按 19 点基线说"早归/晚归"。"""
        import datetime
        now = datetime.datetime.now()
        wd = now.weekday()
        day_desc = ("周末" if wd >= 5 else "工作日") + "，星期" + "一二三四五六日"[wd]
        hour = now.hour
        phase = ("午夜" if hour == 0 else "凌晨" if hour < 6 else "清晨" if hour < 9
                 else "上午" if hour < 12 else "中午" if hour < 14 else "下午" if hour < 18
                 else "傍晚" if hour < 21 else "夜里")
        parts = [day_desc, f"{phase}{hour}点"]
        if with_arrival_note:
            parts.append(_arrival_note(now))
        weather = await _weather_desc()  # 正常天气返回空，只有恶劣天气才进情境
        if weather:
            parts.append(f"外面{weather}")
        return "；".join(parts)

    def _member_profile(name):
        """查家庭成员身份与昵称（devices.db family_members，主人要求：称呼对齐成员数据）。
        返回 (nickname, relation)，查不到返回 ('', '')。"""
        conn = None
        try:
            import sqlite3 as _sq
            from pathlib import Path as _Path
            # 与 family_api.py 同构：server 根/data/devices.db
            db_path = _Path(__file__).parent.parent.parent / "data" / "devices.db"
            conn = _sq.connect(str(db_path))
            row = conn.execute(
                "SELECT nickname, relation FROM family_members WHERE name = ?",
                (name,)).fetchone()
            if row:
                return (row[0] or "", row[1] or "")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] 查成员身份失败（用名字直呼兜底）: {e}")
        finally:
            if conn:
                conn.close()
        return "", ""

    def _is_employee_relation(relation):
        if greeting_policy is not None:
            return greeting_policy.is_employee(relation)
        # 问候策略不可用时仍要区分同事，免得对同事说"回家了/想你"
        from core.utils.greeting_policy import _DEFAULT_EMPLOYEE_RELATIONS
        return str(relation or "").strip() in _DEFAULT_EMPLOYEE_RELATIONS

    async def _generate_welcome(call_name, relation="", extra="", arrival_note=True):
        """欢迎语 LLM 生成（主人要求：像大模型一样每次不同、有温度；
        称呼用成员昵称、身份按家庭成员关系对齐，不得叫错身份）。
        失败返回 None，由调用方走模板兜底。

        extra：T9/T10 追加的事实性情境（今天第几次见、距上次多久、已说过哪些话、
        当前是"家人带朋友"还是"员工带客人"）。只给事实与语气要求，话术仍由 LLM 生成。"""
        try:
            ctx = await _welcome_context(arrival_note)
            employee = _is_employee_relation(relation)
            identity = (f"身份：{relation}。" if employee else f"家庭身份：{relation}。") if relation else ""
            _wl = _read_welcome_llm()
            wl_base = str(_wl.get("base_url", "http://127.0.0.1:1234/v1")).rstrip("/")
            wl_model = str(_wl.get("model_name", "qwen3-4b-instruct-2507"))
            wl_key = str(_wl.get("api_key", "lm-studio"))
            wl_timeout = int(_wl.get("timeout", 8))
            async with aiohttp.ClientSession() as s:
                async with s.post(
                        f"{wl_base}/chat/completions",
                        headers={"Authorization": f"Bearer {wl_key}"},
                        json={
                            "model": wl_model,
                            "messages": [
                                {"role": "system", "content": (
                                    ("你是 AI 管家（机器，不是员工），生成一句问候这位同事来公司的播报语，"
                                     "语气职业得体，不说\"回家\"\"想你\"等家人口吻。"
                                     if employee else
                                     "你是家庭 AI 管家（机器，不是家庭成员），生成一句欢迎这位家人回家的播报语。") +
                                    "必须用给出的称呼（昵称）直呼对方；家庭身份仅供理解语境，"
                                    "绝不能使用\"爸/妈/儿/闺女\"等亲属称呼——亲属称呼是家人之间叫的，机器不适用。"
                                    "根据身份调整语气即可（对长辈尊重、对小孩亲昵）。"
                                    "语气分寸：礼貌得体的管家式关怀，不打情骂俏、不过分亲昵——"
                                    "可以说\"辛苦了\"\"早点休息\"这类得体关怀，"
                                    "绝不说\"心疼\"\"想你\"这类亲密用语（主人 2026-09-09 指出）。"
                                    "关怀要符合逻辑，把情境融成一句自然的话，绝不能机械罗列："
                                    "1.晚归/深夜回家要表达关心（如\"这么晚才回来，辛苦了\"）；"
                                    "2.天气只有在下雨/下雪/刮大风/太冷太热时才提，且要和关怀连起来说"
                                    "（如\"外面还下雨呢，有没有淋到呀\"）；正常天气一个字都不要提；"
                                    "3.情境里没写到的事不要编造。"
                                    "要求：20字以内，口语化、有温度、有新鲜感，每次都不同。"
                                    "直接输出播报语本身，不要引号不要解释。")},
                                {"role": "user", "content": (
                                    f"称呼：{call_name}；{identity}{ctx}"
                                    + (f"；{extra}" if extra else ""))},
                            ],
                            "max_tokens": 80,
                            "temperature": 0.95,
                        },
                        timeout=aiohttp.ClientTimeout(total=wl_timeout)) as r:
                    if r.status != 200:
                        logger.bind(tag=TAG).warning(
                            f"[安防] 欢迎语 LLM HTTP {r.status}，走模板兜底")
                        return None
                    # 2026-10-06 加固：原实现直接取 ["content"]，模型开思考时
                    # 返回里只有 reasoning_content、没有 content → KeyError → 每次都退回模板
                    # （实测 oMLX/Qwen3-8B 开思考时 100% 失败，问候语因此一直机械）。
                    _msg = ((await r.json()).get("choices") or [{}])[0].get("message") or {}
                    text = str(_msg.get("content") or "").strip()
                    if not text:
                        _rc = str(_msg.get("reasoning_content") or "")
                        logger.bind(tag=TAG).warning(
                            "[安防] 欢迎语 LLM 无 content"
                            + ("（只返回了 reasoning_content，疑似思考未关闭）" if _rc else "")
                            + "，走模板兜底")
                        return None
            text = text.strip().strip('"「」').split("\n")[0].strip()
            if not text:
                return None
            logger.bind(tag=TAG).info(
                f"[安防] 欢迎语 LLM 生成成功: {text}（称呼: {call_name}；身份: {relation or '未知'}；情境: {ctx}）")
            return text
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] 欢迎语 LLM 生成失败（走模板兜底）: {e}")
            return None

    async def _welcome_family(name_key, name, camera):
        """家人欢迎（含防骚扰冷却）；返回 True 表示本次有播报。
        优先 LLM 情境化生成，失败降级内置话术池；称呼/身份对齐家庭成员数据。
        调试模式（摄像头模块勾选时）绕过冷却，便于反复测试。"""
        if not _debug_camera_on() and time.time() - family_last_ts.get(name_key, 0) < _FAMILY_COOLDOWN:
            # 冷却内静默；顺带丢弃待确认车牌，避免稍后再补一句多余的"您的车回来了"
            if plate_coordinator is not None:
                plate_coordinator.cancel_pending()
            return False
        family_last_ts[name_key] = time.time()
        nickname, relation = _member_profile(name)
        call_name = nickname or name

        # ── T9/T10：问候策略（频次感知 + 场景细分）──────────────────────
        # 主人要求：一天进出 20 次不能每次都同一句问候；且要区分
        # "家人带朋友（私人社交）"与"员工带客人（参观/商务）"两种语气。
        decision = {"action": "speak", "reason": "no_policy"}
        scene = "family_only"
        extra_ctx = ""
        if greeting_policy is not None:
            try:
                greeting_policy.note_person(camera, name_key)
                scene = greeting_policy.classify_scene(name_key, relation, camera)
                decision = greeting_policy.decide(call_name)
                if decision["action"] == "silent":
                    # 今天招呼够多次了，再说就是打扰；连待确认车牌一起丢弃，
                    # 免得稍后又补一句"您的车回来了"
                    if plate_coordinator is not None:
                        plate_coordinator.cancel_pending()
                    _collect_event("face", camera_id=camera, label=call_name,
                                   greeting_triggered=False, greeting_text="",
                                   extra={"policy": "silent",
                                          "reason": decision["reason"],
                                          "scene": scene})
                    logger.bind(tag=TAG).info(
                        f"[安防] {call_name} 问候静默（{decision['reason']}）")
                    return False
                extra_ctx = "；".join(
                    x for x in (greeting_policy.build_llm_context(call_name),
                                greeting_policy.scene_hint(scene)) if x)
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"[安防] 问候策略异常（按完整问候处理）: {e}")
                decision = {"action": "speak", "reason": "policy_error"}

        # 车牌三段式②③：若有待确认的车牌事件，交由协调器做"车牌 + 人脸"联合确认
        # 并完成个性化播报，本函数随即返回，避免与协调器重复播同一句欢迎语。
        # 协调器返回 None 表示没有待融合的车牌 → 走下面原有欢迎流程，行为不变。
        if plate_coordinator is not None:
            try:
                fused = await plate_coordinator.on_face(name_key, name,
                                                        camera_name=camera)
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"[安防] 车牌身份融合异常（回退原欢迎流程）: {e}")
                fused = None
            if fused:
                return True

        employee = _is_employee_relation(relation)
        if decision.get("action") == "minimal" and greeting_policy is not None:
            # 极简档：不调 LLM，一句轻应答即可（频繁进出时任何"有内容"的话都是打扰）
            # 同事不说"又出门呀/回来啦"这类家人口吻，改用同事话术
            text = (_pick(_COLLEAGUE_LINES, name=call_name) if employee
                    else greeting_policy.minimal_line(call_name))
        else:
            text = await _generate_welcome(call_name, relation, extra_ctx)
            if not text:
                text = _pick(_COLLEAGUE_LINES if employee else _WELCOME_LINES, name=call_name)
        # 家人欢迎：用后台主音色（voice 不传 = 跟随后台设置）
        await _broadcast({"level": "info", "source": f"摄像头·{camera}",
                          "text": text}, speak=True)
        _collect_event("face", camera_id=camera,
                       label=call_name, greeting_triggered=True,
                       greeting_text=text,
                       extra={"scene": scene,
                              "action": decision.get("action"),
                              "reason": decision.get("reason")})
        return True

    async def _query_camera_family_recent(camera_id, window_sec):
        """查该摄像头最近 window_sec 秒内的事件，是否有任何事件识别出家人。
        按 frigate-notify 思路以"事件窗"而非"单事件 id"为判据，一次兜住三种情况：
        MQTT update 丢失 / Frigate 拆事件（正脸转身拆成两个事件）/ 人脸识别慢。
        返回 (家人名, 命中事件id)。"""
        after = int(time.time() - window_sec)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                        f"{frigate_api}/api/events",
                        params={"camera": camera_id, "after": str(after), "limit": "50"},
                        timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status != 200:
                        return "", ""
                    for ev in await r.json():
                        # [2026-10-19] Frigate REST /api/events 返回的是扁平事件对象
                        # （sub_label 在顶层），不是 MQTT 那种 after 嵌套结构。
                        # 原写法 ev.get("after") 恒为空 dict → 事件窗兜底永远查不到家人，
                        # 夜间家人被反复误报成陌生人。两种结构都兼容。
                        nm = _extract_name(ev.get("after") or ev)
                        if _is_family(nm):
                            return nm, str(ev.get("id", ""))
        except Exception:
            pass
        return "", ""

    async def _delayed_person_judge(eid, camera_id, camera, night):
        """轮询判案（frigate-notify recheck_delay 范式）：
        人脸识别可能晚于 new 十余秒才完成，单次延迟判案必误报；
        故先等 judge_wait 秒，再反复查"摄像头最近事件窗"直到有结论：
        任何一轮发现家人（MQTT 实时记录或事件窗 API）→ 转欢迎；查完仍无 → 告警。
        调试模式：等待缩为 1 秒、只查 1 轮（8 秒小窗等识别），告警不设冷却。"""
        debug_on = _debug_camera_on()
        wait_s, polls, grace = (1, 1, 8) if debug_on else (judge_wait, poll_max, family_grace)
        await asyncio.sleep(wait_s)
        for i in range(polls):
            # MQTT 实时记录的家人出现（宽限期内无脸新事件=同一人转身拆分）
            if time.time() - family_seen.get(camera_id, 0) < grace:
                logger.bind(tag=TAG).info(
                    f"[安防] {camera} 第{i + 1}轮判案：家人宽限期内，不告警")
                return
            if eid and eid in family_hits:
                return
            # 事件窗 API 查询：窗内任何事件认出家人即判家人（识别慢也能等到）
            nm, hit_eid = await _query_camera_family_recent(camera_id, grace)
            if nm:
                if hit_eid:
                    family_hits.add(hit_eid)
                family_seen[camera_id] = time.time()
                logger.bind(tag=TAG).info(
                    f"[安防] {camera} 第{i + 1}轮判案：事件窗命中家人，转欢迎")
                await _welcome_family(nm.lower(), nm, camera)  # 冷却内静默
                return
            if i < poll_max - 1:
                await asyncio.sleep(poll_interval)
        # 全部轮询完仍无家人结论 → 按时段告警（调试模式不设告警冷却，便于反复触发）
        now = time.time()
        if night:
            key = f"{camera_id}:night_person"
            if not debug_on and now - last_ts.get(key, 0) < _EVENT_COOLDOWN:
                return
            last_ts[key] = now
            # 深夜陌生人报警：唯一使用「安防告警音色」（威慑音）的场景，
            # 显式传入；其余播报一律用后台主音色。
            await _broadcast({"level": "alert", "source": f"摄像头·{camera}",
                              "camera_id": camera_id,
                              "text": _pick(_STRANGER_LINES, camera=camera)},
                             speak=True, voice=_alert_voice())
        else:
            # 白天外人：不马上提醒，逗留超过设定时长才播报（主人 2026-09-09 需求）
            asyncio.create_task(_dwell_then_alert(eid, camera_id, camera))
            return

    async def _dwell_then_alert(eid, camera_id, camera):
        """逗留判定（主人 2026-09-09 + 功能开关修正）：白天外人持续出现超过
        设定时长才提醒。门口机走"访客提醒"开关/时长，普通摄像头走"徘徊逗留
        检测"开关/时长；功能关闭则不判定。事件提前结束（人走了）→ 记日志
        不播报，避免路过者频繁触发。调试模式压缩为 5 秒。"""
        # 每次实时查 DB（后台改配置即时生效，~1ms 轻量；失败沿用启动缓存）
        try:
            _refresh_cam_features()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] 逗留判定前读取摄像头配置失败（用缓存）: {e}")
        feat = cam_features.get(camera_id)
        if feat:
            is_doorbell = feat["role"] == "doorbell"
            enabled = feat["visitor_on"] if is_doorbell else feat["dwell_on"]
            dwell = feat["visitor_s"] if is_doorbell else feat["dwell_s"]
            if not enabled:
                logger.bind(tag=TAG).info(
                    f"[安防] {camera} "
                    f"{'访客提醒' if is_doorbell else '徘徊逗留检测'}已关闭，不判定")
                return
        else:
            dwell = 60  # 后台无该摄像头配置时走 config 兜底（默认开启 60 秒）
        if _debug_camera_on():
            dwell = 5
        start = time.time()
        # 轮询事件 end 字段：end 为空 = 人还在画面里
        while True:
            await asyncio.sleep(5)
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                            f"{frigate_api}/api/events/{eid}",
                            timeout=aiohttp.ClientTimeout(total=4)) as r:
                        if r.status == 404:
                            # 事件在 Frigate 查不到（模拟测试/重启丢失）：
                            # 调试模式视为持续逗留便于测试，正常运行视为已离开不播报
                            if not _debug_camera_on():
                                logger.bind(tag=TAG).info(
                                    f"[安防] {camera} 事件已不可查（可能已结束），不提醒")
                                return
                            end = None
                        elif r.status != 200:
                            return  # 事件查不到，宁可不报
                        else:
                            end = (await r.json()).get("end_time")
            except Exception:
                return
            if end:
                logger.bind(tag=TAG).info(
                    f"[安防] {camera} 外人逗留 {time.time() - start:.0f} 秒后离开"
                    f"（未到 {dwell}s 判定线），不提醒")
                return
            if time.time() - start >= dwell:
                break
        # 逗留达标 → 播报（门口机专属话术 / 普通摄像头访客话术）
        now = time.time()
        if not _debug_camera_on() and now - last_ts.get(f"{camera_id}:person", 0) < _EVENT_COOLDOWN:
            return
        last_ts[f"{camera_id}:person"] = now
        pool = _DOORBELL_LINES if camera_id in doorbell_ids else _VISITOR_LINES
        logger.bind(tag=TAG).info(
            f"[安防] {camera} 外人逗留超过 {dwell} 秒，触发提醒（{'门口机' if camera_id in doorbell_ids else '普通摄像头'}）")
        # 白天访客提醒用主音色（主人 2026-10-06：只有深夜报警才用威慑音）
        await _broadcast({"level": "warn", "source": f"摄像头·{camera}",
                          "camera_id": camera_id,
                          "text": _pick(pool)}, speak=True)

    # ── 音色解析：跟随后台设置 ────────────────────────────────────
    # 主人 2026-10-06 定案：音色不是代码里写死的值，一律读后台配置。
    #   · 主音色 voice      → 所有日常播报（家人欢迎/访客提醒/车牌播报…）
    #   · 安防音色 alert_voice → 仅深夜陌生人报警（威慑感）
    # 后台改音色即时生效（与 TTS provider 同一张tts_config 表，见
    # core/providers/tts/moss_nano.py:_get_current_voice），切换引擎不失效。
    _voice_cache = {"ts": 0.0, "voice": None, "alert_voice": None}
    _VOICE_CACHE_TTL = 5  # 秒，与 provider 的 5 秒缓存同频
    _current_tts_engine = _current_tts_engine_impl
    _MOSS_TO_QWEN3 = _MOSS_TO_QWEN3_TABLE

    def _read_backend_voice():
        """读后台 tts_config 表里的音色设置。

        返回 (main_voice, alert_voice)：
          main_voice   主音色，家人欢迎/访客/车牌等日常播报
          alert_voice  安防音色，深夜陌生人报警专用
        读不到返回 (None, None)，调用方按「不传 voice」交给 8086 用自身默认值。
        """
        now = time.time()
        if now - _voice_cache["ts"] < _VOICE_CACHE_TTL:
            return _voice_cache["voice"], _voice_cache["alert_voice"]
        main_voice = None
        alert_voice = None
        try:
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
            conn = sqlite3.connect(str(db_path), timeout=2)
            try:
                rows = conn.execute(
                    "SELECT config_key, config_value FROM tts_config "
                    "WHERE config_key IN ('voice', 'alert_voice', 'provider')"
                ).fetchall()
            finally:
                conn.close()
            cfg = {str(k): ("" if v is None else str(v)) for k, v in rows}
            etype, pcfg = _current_tts_engine()
            db_voice = cfg.get("voice", "").strip() or None
            if etype == "qwen3_tts":
                # Qwen3 的运行时音色 = DB tts_config.voice（与 provider 同源），
                # 读不到用配置里的 speaker
                main_voice = db_voice or (str(pcfg.get("speaker") or "").strip() or None)
            elif etype == "huoshan_double_stream":
                main_voice = str(pcfg.get("speaker") or "").strip() or None
            elif etype == "moss_nano" or cfg.get("provider", "").strip() == "MossNano":
                # 只认 MossNano 自己存的音色：selected_module 可能指向别的引擎，
                # 其音色（如火山双流 speaker）在 8086 不存在，混用会导致播报音色错乱。
                main_voice = db_voice
            else:
                main_voice = None
            alert_voice = cfg.get("alert_voice", "").strip() or None
            # 后台没配安防音色时，回落到 config.yaml 的 security.alert_voice
            if not alert_voice:
                try:
                    import yaml as _y
                    sec = (_y.safe_load(
                        (Path(__file__).resolve().parent.parent.parent / "data" / ".config.yaml")
                        .read_text(encoding="utf-8")) or {}).get("security") or {}
                    alert_voice = str(sec.get("alert_voice") or "").strip() or None
                except Exception:
                    pass
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] 读取后台音色失败（用服务默认）: {e}")
        if main_voice is None and _voice_cache["voice"] is not None:
            main_voice = _voice_cache["voice"]  # 读失败沿用上次值，不让播报掉回默认音色
        if alert_voice is None and _voice_cache["alert_voice"] is not None:
            alert_voice = _voice_cache["alert_voice"]
        _voice_cache.update(ts=now, voice=main_voice, alert_voice=alert_voice)
        logger.bind(tag=TAG).info(
            f"[安防] 音色已同步后台：主音色={main_voice or '服务默认'}，"
            f"安防音色={alert_voice or '服务默认'}")
        return main_voice, alert_voice

    def _alert_voice():
        """深夜陌生人报警专用音色（后台「安防告警音色」）。"""
        return _read_backend_voice()[1]

    async def _tts_base64(text, voice=None):
        """调 MOSS-Nano(8086) 服务合成语音，返回 base64(wav)；失败 None（降级纯文本）
        8086 请求体为 {text, voice, speed, volume}；voice 参考音频音色。

        voice=None → 不传 voice，8086 用它自己的默认音色（DEFAULT_VOICE）。
        ⚠️ 不能传代码里自造的音色名：8086 的 VOICE_PROMPTS 只有 zh_1/zh_3/zh_10/
        zh_4/zh_6/zh_11 六个，查不到就静默换成默认音色 zh_1（男声）且不报错
        （服务端 moss_nano_server.py:`VOICE_PROMPTS.get(voice) or ...[DEFAULT_VOICE]`），
        这就是历史上家人欢迎用男声的原因。
        """
        payload = {"text": text, "speed": 1.0, "volume": 5.0}
        if voice:
            payload["voice"] = voice
        # 【引擎感知 2026-10-07】主 TTS 是 Qwen3-TTS 时改打 8087，
        # 并把 MossNano 音色名映射成 Qwen3 预置音色（否则 8087 静默回落默认音色）。
        url, payload = build_security_tts_request(text, voice, tts_url)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                        url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    import base64 as _b64
                    return _b64.b64encode(await r.read()).decode()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] TTS 合成失败（降级纯文本）: {e}")
            return None

    def _remember_in_dialogue(handlers, ev, spoken):
        """把主动播报写进各连接的对话历史（assistant 角色）。

        2026-10-13 事故：播报只推给前端、不进对话历史，主人追问"你怎么知道李博回来了"，
        助手根本不知道自己刚说过这句话，回答"我没有获取到相关动态，是不是你记错啦"。
        这里写入的是"助手自己说过的话 + 依据来源"，任何异常都不得影响播报。"""
        text = str(ev.get("text") or "").strip()
        if not text:
            return
        source = str(ev.get("source") or "安防").strip()
        note = f"{text}（这是我刚才{'主动语音' if spoken else '在屏幕上'}播报的，依据来自{source}的识别结果）"
        try:
            from core.utils.dialogue import Message
            for h in handlers:
                d = getattr(h, "dialogue", None)
                if d is not None:
                    d.put(Message(role="assistant", content=note))
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[安防] 播报写入对话历史失败（不影响播报）: {e}")

    async def _broadcast(ev, speak=False, voice=None):
        """安防播报广播。voice=None 表示「用后台主音色」。

        主人 2026-10-06 定案：默认跟随后台主音色，只有深夜陌生人报警由调用方
        显式传 alert_voice（威慑音）。这里不再有写死的音色名。
        """
        payload = {"type": "security", **ev}
        if speak:
            if voice is None:
                voice = _read_backend_voice()[0]  # 主音色（跟随后台）
            # 语音播报：欢迎语/告警/访客等需要出声的场景，合成失败不影响文本广播
            audio_b64 = await _tts_base64(ev.get("text", ""), voice=voice)
            if audio_b64:
                payload["audio_b64"] = audio_b64
        body = json.dumps(payload, ensure_ascii=False)
        async with server.connections_lock:
            conns = list(server.active_connections)
        sent = 0
        handlers = {}
        for ws in conns:
            try:
                await ws.send(body)
                sent += 1
                h = getattr(ws, "_homebrain_handler", None)
                if h is not None:
                    handlers[id(h)] = h
            except Exception:
                pass
        _remember_in_dialogue(handlers.values(), ev, speak)
        logger.bind(tag=TAG).info(
            f"[安防] 事件广播: {ev.get('text')}"
            f"（送达 {sent} 个连接，{'含语音' if speak else '仅文本'}）")

    async def _broadcast_family_bubble(name, camera):
        """家人识别 → 通知 LED 鱼缸飘起该成员头像气泡（纯 UI 事件）。

        与 _broadcast 分开：不参与 TTS 合成、不写对话历史（气泡不是「播报」）。
        只传人名，头像由前端从已缓存的成员表（/api/family/members）取，保持消息轻量。
        由同步的 MQTT 回调经 run_coroutine_threadsafe 调度，故独立成协程。"""
        payload = {"type": "family_arrival", "name": name, "camera": camera}
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
        logger.bind(tag=TAG).info(f"[安防] 家人气泡广播: {name}（送达 {sent} 个连接）")

    # ── 车牌三段式协调器（T4-T7，2026-09-26）──────────────────────────
    # 依赖注入而非内部实现：广播复用上面的 _broadcast（自带 TTS），
    # 欢迎语复用 _generate_welcome（称呼/分寸/防机械罗列的提示词已调教好），
    # 避免重复实现导致两套话术口径。
    # 初始化失败绝不影响安防主链路：降级为 plate_coordinator=None，行为回到改动前。
    plate_coordinator = None
    try:
        from core.utils.plate_welcome import PlateWelcomeCoordinator
        plate_coordinator = PlateWelcomeCoordinator(
            sec_cfg.get("plate_welcome") or {}, _broadcast, _generate_welcome)
        if plate_coordinator.enabled:
            logger.bind(tag=TAG).info(
                f"[安防] 车牌三段式已启用（融合窗 "
                f"{plate_coordinator.window_seconds:.0f}s，预欢迎冷却 "
                f"{plate_coordinator.cooldown_seconds:.0f}s）")
        else:
            logger.bind(tag=TAG).info("[安防] 车牌三段式已配置为关闭")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 车牌三段式初始化失败（不影响安防）: {e}")
        plate_coordinator = None

    # ── 问候策略（T9/T10，2026-09-26）─────────────────────────────────
    # 频次感知（一天进出多次不过度打扰）+ 防重复（历史话术作 LLM 负例）
    # + 场景细分（家人带朋友 / 员工带客人）。同样：初始化失败则降级为 None，
    # 行为回到"每次识别到家人都完整问候"的改动前状态。
    greeting_policy = None
    try:
        from core.utils.greeting_policy import GreetingPolicy
        greeting_policy = GreetingPolicy(sec_cfg.get("greeting_policy") or {})
        if greeting_policy.enabled:
            logger.bind(tag=TAG).info(
                f"[安防] 问候策略已启用（每日完整问候上限 "
                f"{greeting_policy.today_max_full} 次，静默阈值 "
                f"{greeting_policy.silent_after_count} 次，共现窗 "
                f"{greeting_policy.cooccur_window:.0f}s）")
        else:
            logger.bind(tag=TAG).info("[安防] 问候策略已配置为关闭")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 问候策略初始化失败（不影响安防）: {e}")
        greeting_policy = None

    # ── 到访播报：先决定说不说，再决定说什么（2026-10-13 欢迎语分层方案）──────────
    # 方案：docs/plans/2026-10-13_欢迎语分层设计方案.md。arrival_policy=None 时退回旧流程。
    _ap_cfg = sec_cfg.get("arrival_policy") or {}
    dir_params = _ap_cfg.get("direction") or {}
    field_members = {str(n).strip().lower() for n in (_ap_cfg.get("field_members") or [])}
    arrival_policy = None
    try:
        from core.utils.arrival_policy import ArrivalPolicy
        arrival_policy = ArrivalPolicy(_ap_cfg)
        if arrival_policy.enabled:
            _ensure_work_mode_column()
            n_hist = _load_arrival_history(arrival_policy)
            logger.bind(tag=TAG).info(
                f"[安防] 到访播报策略已启用（恢复进出门记录 {n_hist} 条）")
        else:
            arrival_policy = None
            logger.bind(tag=TAG).info("[安防] 到访播报策略已配置为关闭，沿用旧问候流程")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] 到访播报策略初始化失败（沿用旧流程）: {e}")
        arrival_policy = None

    arrival_decided = {}   # 事件 id → 决策时间，每个事件只决策一次
    arrival_group = {}     # kind → [条目]，多人同时进门的合并窗口

    _MIN_FAMILY = ["回来啦", "回来啦～", "嗯，回来了"]
    _MIN_COLLEAGUE = ["来啦", "回来啦", "来了～"]
    _MORNING_LINES = ["早，{name}", "{name}，早上好", "早啊，{name}"]
    _NIGHT_LINES = ["回来啦，早点休息", "{name}回来啦，早点休息", "辛苦了，早点休息"]
    _TRIP_LINES = ["{name}，路上小心", "路上小心，{name}", "{name}，注意安全"]

    def _pick_avoid(pool, recent, **fmt):
        """从话术池选一条，尽量避开最近说过的。"""
        import random
        cands = [t.format(**fmt) for t in pool]
        fresh = [t for t in cands if t not in (recent or [])]
        return random.choice(fresh or cands)

    def _period_cn(hour):
        for lo, hi, name in ((5, 9, "早上"), (9, 12, "上午"), (12, 14, "中午"),
                             (14, 18, "下午"), (18, 20, "傍晚"), (20, 23, "晚上")):
            if lo <= hour < hi:
                return name
        return "深夜"

    def _arrival_facts(dec, call_name):
        """给大模型的事实（只给事实，不给"第几次"）。离开时长只有下界，措辞用"至少"。"""
        parts = [f"现在是{_period_cn(dec['hour'])}，对方刚进门"]
        lb = dec.get("away_lb")
        if lb:
            parts.append(f"这次出门至少 {lb / 3600:.1f} 小时（实际可能更久）" if lb >= 3600
                         else f"这次出门至少约 {int(lb // 60)} 分钟")
        if dec.get("first_today"):
            parts.append("今天第一次见到对方")
        parts.append("你不知道对方去了哪里、做了什么，只表达欢迎和对辛苦的关怀，不要编造去向")
        if greeting_policy is not None:
            recent = greeting_policy.recent_greetings(call_name)
            if recent:
                quoted = "、".join(f"「{t}」" for t in recent[:5])
                parts.append(f"最近已经说过这些话，不要重复也不要换个说法表达同一个意思：{quoted}")
        return "；".join(parts)

    def _assistant_busy():
        """助手正在说话时不插话（播报改为只显示文字）。"""
        try:
            for ws in list(server.active_connections):
                h = getattr(ws, "_homebrain_handler", None)
                if h is not None and getattr(h, "client_is_speaking", False):
                    return True
        except Exception:
            pass
        return False

    async def _speak_items(items, camera, kind):
        """items: [(name_key, name, call_name, relation, tier, dec, extra_ctx)]；一人一句或多人合并一句。"""
        from core.utils.arrival_policy import (MINIMAL, FULL, MORNING, NIGHT_LIGHT,
                                               SAFE_TRIP, KIND_COLLEAGUE)
        colleague = kind == KIND_COLLEAGUE
        recent = []
        if greeting_policy is not None:
            recent = greeting_policy.recent_greetings(items[0][2])
        if len(items) > 1:
            names = "、".join(i[2] for i in items)
            text = f"{names}，来啦～" if colleague else f"欢迎回家，{names}～"
            if all(i[4] == NIGHT_LIGHT for i in items):
                text = f"回来啦，{names}，早点休息"
        else:
            name_key, name, call_name, relation, tier, dec, extra_ctx = items[0]
            text = None
            if tier == FULL:
                text = await _generate_welcome(call_name, relation, extra_ctx, arrival_note=False)
                if not text:
                    text = _pick(_COLLEAGUE_LINES if colleague else _WELCOME_LINES, name=call_name)
            elif tier == MORNING:
                text = _pick_avoid(_MORNING_LINES, recent, name=call_name)
            elif tier == NIGHT_LIGHT:
                text = _pick_avoid(_NIGHT_LINES, recent, name=call_name)
            elif tier == SAFE_TRIP:
                text = _pick_avoid(_TRIP_LINES, recent, name=call_name)
            else:
                text = _pick_avoid(_MIN_COLLEAGUE if colleague else _MIN_FAMILY, recent)
        busy = _assistant_busy()
        await _broadcast({"level": "info", "source": f"摄像头·{camera}", "text": text},
                         speak=not busy)
        now = time.time()
        for name_key, name, call_name, relation, tier, dec, _ctx in items:
            arrival_policy.note_spoken(name_key, now, tier)
            _collect_event("face", camera_id=camera, label=call_name,
                           greeting_triggered=True, greeting_text=text,
                           extra={"tier": tier, "reason": dec.get("reason"),
                                  "away_lb": dec.get("away_lb"), "group": len(items),
                                  "text_only": busy})
        return True

    async def _arrival_flow(eid, name_key, name, camera, camera_id, after, final):
        """家人/同事的一次到访：判方向 → 决策 → 播报。每个事件只决策一次。
        方向未知且事件未结束时先等（后续 update 路径变长才判得出）；结束仍未知则不说。"""
        from core.utils.entry_direction import classify_direction
        from core.utils.arrival_policy import (SILENT, FULL, MINIMAL, KIND_FAMILY,
                                               KIND_COLLEAGUE, WORK_FIELD, WORK_OFFICE)
        if eid in arrival_decided:
            return False
        direction = classify_direction(after.get("path_data"), dir_params)
        if direction == "unknown" and not final:
            return False
        arrival_decided[eid] = time.time()
        if len(arrival_decided) > 2000:
            arrival_decided.clear()

        nickname, relation = _member_profile(name)
        call_name = nickname or name
        kind = KIND_COLLEAGUE if _is_employee_relation(relation) else KIND_FAMILY
        extra = _member_extra(name)
        work_mode = str(extra.get("work_mode") or "").strip().lower()
        if work_mode not in (WORK_FIELD, WORK_OFFICE):
            work_mode = WORK_FIELD if name_key in field_members else WORK_OFFICE
        t_in = float(after.get("start_time") or time.time())
        dec = arrival_policy.decide(name_key, kind, direction, t=t_in,
                                    work_mode=work_mode, school_kid=_is_school_kid(extra))
        tier = dec["tier"]
        # 登记这次进/出门（决策之后登记，决策要看"上一次"）
        arrival_policy.note_passage(t_in if direction == "in" else time.time(), direction, name_key)
        _collect_event("passage", camera_id=camera, label=name_key,
                       extra={"direction": direction, "ts": t_in if direction == "in" else time.time(),
                              "tier": tier, "reason": dec["reason"], "away_lb": dec["away_lb"],
                              "kind": kind, "work_mode": work_mode})
        logger.bind(tag=TAG).info(
            f"[到访] {call_name}（{relation or '未登记'}）方向={direction} 档位={tier} "
            f"原因={dec['reason']} 离开下界={dec['away_lb'] and round(dec['away_lb'] / 60)}分钟")
        if tier == SILENT:
            if plate_coordinator is not None:
                plate_coordinator.cancel_pending()
            return False

        # 每日保底闸门（沿用旧策略的次数上限）
        if greeting_policy is not None:
            cnt = greeting_policy.today_count(call_name)
            if cnt >= greeting_policy.silent_after_count:
                logger.bind(tag=TAG).info(f"[到访] {call_name} 今日已问候 {cnt} 次，静默")
                return False
            if tier == FULL and cnt >= greeting_policy.today_max_full:
                tier = MINIMAL
            try:
                greeting_policy.note_person(camera, name_key)
                scene = greeting_policy.classify_scene(name_key, relation, camera)
            except Exception:
                scene = "family_only"
        else:
            scene = "family_only"

        if plate_coordinator is not None and tier in (FULL, MINIMAL):
            try:
                if await plate_coordinator.on_face(name_key, name, camera_name=camera):
                    return True
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[安防] 车牌身份融合异常（继续普通播报）: {e}")

        extra_ctx = _arrival_facts(dec, call_name)
        if greeting_policy is not None:
            hint = greeting_policy.scene_hint(scene)
            if hint:
                extra_ctx += "；" + hint
        entry = (name_key, name, call_name, relation, tier, dec, extra_ctx)
        window = float(arrival_policy.cfg.get("group_window_seconds", 5))
        grp = arrival_group.get(kind)
        if grp is not None:
            grp.append(entry)       # 已有同类人在等合并窗口，交给先到的那个一起说
            return True
        arrival_group[kind] = [entry]
        if window > 0:
            await asyncio.sleep(window)
        items = arrival_group.pop(kind, [entry])
        return await _speak_items(items, camera, kind)

    def _on_person_end(after, camera_id, camera):
        """事件结束：认出的家人/同事补做决策（方向此刻才明朗）；没认出的出门登记为"有人出过门"。"""
        from core.utils.entry_direction import classify_direction
        eid = str(after.get("id", ""))
        name = _extract_name(after)
        name_key = name.lower()
        if _is_family(name_key):
            asyncio.run_coroutine_threadsafe(
                _arrival_flow(eid, name_key, name, camera, camera_id, after, True), loop)
            return
        if eid in arrival_decided:
            return
        arrival_decided[eid] = time.time()
        direction = classify_direction(after.get("path_data"), dir_params)
        if direction == "out":
            ts = float(after.get("end_time") or time.time())
            arrival_policy.note_passage(ts, "out", None)
            _collect_event("passage", camera_id=camera, label="",
                           extra={"direction": "out", "ts": ts, "anonymous": True})
            return
        # [2026-10-15 展厅迎宾] 没认出来的人**往里走** → 迎宾演示（原本静默）。
        # 只认 direction=="in"；unknown 不迎（宁可少说）。总开关默认关闭，
        # 由 data/.config.yaml 的 security.showroom_welcome.enabled 控制。
        if direction == "in":
            try:
                from core.utils import showroom_welcome
                asyncio.run_coroutine_threadsafe(
                    showroom_welcome.maybe_welcome(
                        camera_id, camera, _broadcast, _assistant_busy, logger.bind(tag=TAG).warning),
                    loop,
                )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[迎宾] 触发失败（不影响安防）: {e}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="homebrain-security")

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            c.subscribe("frigate/events")
            # Frigate 0.18 LPR 的车牌更新走独立主题，字段在顶层（plate/name/camera），
            # 与 frigate/events 的 after/type 结构不同，需分开解析
            c.subscribe("frigate/tracked_object_update")
            logger.bind(tag=TAG).info(
                f"[安防] 已连接 MQTT {host}:{port}，订阅 frigate/events + "
                f"frigate/tracked_object_update")
        else:
            logger.bind(tag=TAG).warning(f"[安防] MQTT 连接失败: {reason_code}")

    def on_message(c, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        # ── 车牌专用主题：结构不同，单独处理后直接返回，不进入下面的事件流程 ──
        if msg.topic == "frigate/tracked_object_update":
            plate_raw = str(data.get("plate") or "").strip()
            if plate_raw and plate_coordinator is not None:
                cam_id = str(data.get("camera") or "")
                asyncio.run_coroutine_threadsafe(
                    plate_coordinator.on_plate(plate_raw, cam_id,
                                               camera_names.get(cam_id, cam_id)),
                    loop,
                )
            return
        after = data.get("after") or {}
        # ⚠️ Frigate 真实消息的 type 在顶层（"new"/"update"/"end"），不在 after 里；
        # new=目标刚确认（人脸识别通常尚未完成），update=状态更新（含识别出 sub_label）
        ev_type = data.get("type")
        if ev_type == "end" and arrival_policy is not None \
                and after.get("label") == "person":
            _on_person_end(after, after.get("camera", ""),
                           camera_names.get(after.get("camera", ""), after.get("camera", "")))
            return
        if ev_type not in ("new", "update"):
            return
        label = after.get("label", "object")
        camera_id = after.get("camera", "")
        camera = camera_names.get(camera_id, camera_id)
        now = time.time()

        # ── 人脸分级（仅 person 事件）：家人欢迎 / 外人提示 / 深夜陌生人告警 ──
        # 注意：new 时 sub_label 多为空，「主人」通常在几秒后的 update 里才识别出来，
        # 因此家人欢迎必须同时处理两种 type（update 高频，靠 10 分钟冷却拦截）
        if label == "person":
            name = _extract_name(after)
            name_key = name.lower()
            eid = str(after.get("id", ""))
            if _is_family(name_key):
                # 家人：欢迎语，同人 10 分钟内不重复（防骚扰 update 高频）
                if eid:
                    family_hits.add(eid)
                    if len(family_hits) > 1000:
                        family_hits.clear()
                family_seen[camera_id] = time.time()  # 记录家人出现时间（拆事件宽限用）
                # 家人头像气泡：LED 鱼缸待机时飘起该成员头像。
                # 放在欢迎语分流之前——不管后面欢迎语是播报/静默/冷却，气泡都照飘；
                # 同人 _bubble_cooldown 秒内只广播一次（防 update 高频重复飘）。
                if time.time() - bubble_last_ts.get(name_key, 0) >= _bubble_cooldown:
                    bubble_last_ts[name_key] = time.time()
                    if len(bubble_last_ts) > 500:
                        bubble_last_ts.clear()
                    asyncio.run_coroutine_threadsafe(
                        _broadcast_family_bubble(name, camera), loop)
                if arrival_policy is not None:
                    asyncio.run_coroutine_threadsafe(
                        _arrival_flow(eid, name_key, name, camera, camera_id, after, False), loop)
                else:
                    asyncio.run_coroutine_threadsafe(
                        _welcome_family(name_key, name, camera), loop)
                return
            if ev_type != "new":
                return  # update 且未识别出家人 → 忽略（判案交给延迟任务，防 update 刷屏）
            # T10 场景细分：记录一次"陌生人/非家人"观测，供"已知人脸 + 未知人脸
            # 共现"判据使用（家人带朋友 / 员工带客人）。只在 new 事件记，避免 update 刷屏。
            if greeting_policy is not None:
                try:
                    greeting_policy.note_person(camera, None)
                except Exception:
                    pass
            # new 且未识别出人：延迟判案（给人脸识别留时间窗，认出家人就不报警）
            # 调试模式强制深夜以便随时测告警；但门口机例外——访客提醒本就走
            # 逗留判定（不分昼夜），强制深夜会让调试模式测不到逗留链路。
            # 先实时刷新后台配置（主人在后台改设备类型/开关后即时生效）
            try:
                _refresh_cam_features()
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[安防] 事件判定前读取摄像头配置失败（用缓存）: {e}")
            is_doorbell = camera_id in doorbell_ids
            night = ((_debug_camera_on() and not is_doorbell)
                     or _is_night(time.localtime().tm_hour,
                                  night_start, night_end))
            asyncio.run_coroutine_threadsafe(
                _delayed_person_judge(eid, camera_id, camera, night), loop)
            return

        # ── 非人员目标：原有冷却广播 ──
        # 车牌优先：Frigate 0.18 LPR 把识别结果写在车辆对象的
        # recognized_license_plate（未在 Frigate 侧登记的车牌）或 sub_label（已登记）。
        # 命中车牌就交给三段式协调器（它会做预欢迎播报），
        # 因此这里直接 return，不再播通用的"有车辆"，避免一次到场说两句话。
        plate_raw = str(after.get("recognized_license_plate")
                        or after.get("plate") or "").strip()
        if plate_raw and plate_coordinator is not None:
            asyncio.run_coroutine_threadsafe(
                plate_coordinator.on_plate(plate_raw, camera_id, camera), loop)
            return
        key = f"{camera}:{label}"
        if now - last_ts.get(key, 0) < _EVENT_COOLDOWN:
            return
        last_ts[key] = now
        pool = _OBJECT_LINES.get(label)
        text = _pick(pool, camera=camera) if pool else LABEL_CN.get(label, f"检测到{label}")
        level = "warn"
        _collect_event(label, camera_id=camera, label=label)
        asyncio.run_coroutine_threadsafe(
            _broadcast({"level": level, "source": f"摄像头·{camera}", "text": text}),
            loop,
        )

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect_async(host, port, keepalive=60)
        client.loop_start()
        logger.bind(tag=TAG).info(
            f"[安防] Frigate 事件监听已启动（{host}:{port}，Frigate 未运行时会自动重试）")
        return client
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[安防] MQTT 监听启动失败: {e}")
        return None
