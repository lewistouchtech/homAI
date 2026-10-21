"""
星空记忆星系 API（v2 只读）
聚合家庭记忆数据源，供 Web 终端星空渲染：
- member: 家庭成员（星系锚点，家人相关星围绕其连线）
- note: 家庭记事（memory_notes 表）
- schedule: 日程（member_schedules 表，今日+未来7天）
- health: 健康记录（health_data 表，每成员最近 5 条）
- memory: 珍贵时刻（conversation_summaries 表，valuable=1，主人确认只挂珍贵记忆）
- security: 安防摄像头（cameras 表，每颗星一个摄像头）
- device: 智能家居设备（Home Assistant REST API 实时拉取）
"""
import json
import sqlite3
import time
from pathlib import Path
from datetime import date, timedelta

import aiohttp
from aiohttp import web

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# 各类星的上限（防止星系过密）
MAX_NOTES = 60
MAX_HEALTH_PER_MEMBER = 5
MAX_SCHEDULES = 40
MAX_MEMORIES = 20
MAX_CAMERAS = 12
MAX_DEVICES = 60

# 工作中/激活状态（前端据此画"亮"，其余画"暗"）
DEVICE_ACTIVE_STATES = {"on", "open", "playing", "heating", "cooling",
                        "cleaning", "returning", "opening", "closing",
                        "unlocking", "locking"}

# HA 只挂可控设备（sensor/binary_sensor/automation 等只读实体不上星）
HA_DOMAINS = {"light", "switch", "cover", "lock", "fan",
              "climate", "media_player", "vacuum"}

# 设备状态中文映射（星标副标题）
DEVICE_STATE_CN = {
    "on": "开", "off": "关", "unavailable": "离线", "unknown": "未知",
    "locked": "已锁", "unlocked": "未锁",
    "open": "开", "closed": "关", "opening": "正在打开", "closing": "正在关闭",
    "idle": "空闲", "playing": "播放中", "paused": "已暂停",
    "standby": "待机", "heating": "制热", "cooling": "制冷",
    "cleaning": "清扫中", "docked": "回充座", "returning": "回充中",
}

# HA 配置读取缓存（避免每次请求都读 yaml）
_ha_cfg_cache = {"ts": 0.0, "base": "", "key": ""}


def _ha_config():
    """读 config.yaml 的 plugins.home_assistant（${HASS_TOKEN} 自动展开），300s 缓存"""
    now = time.time()
    if now - _ha_cfg_cache["ts"] < 300:
        return _ha_cfg_cache["base"], _ha_cfg_cache["key"]
    base, key = "", ""
    try:
        from config.config_loader import read_config, get_project_dir
        cfg = read_config(get_project_dir() + "config.yaml")
        ha = (cfg.get("plugins") or {}).get("home_assistant") or {}
        base = (ha.get("base_url") or "").rstrip("/")
        key = ha.get("api_key") or ""
    except Exception:
        pass
    _ha_cfg_cache.update(ts=now, base=base, key=key)
    return base, key


async def fetch_ha_devices():
    """异步调 HA REST API /api/states，过滤可控实体返回 device 星数据"""
    base, key = _ha_config()
    if not base or not key:
        return []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/api/states", headers=headers) as resp:
                if resp.status != 200:
                    return []
                states = await resp.json()
    except Exception:
        return []  # HA 不可达时星系其余部分正常
    devices = []
    for st in states:
        eid = st.get("entity_id") or ""
        dom = eid.split(".", 1)[0]
        if dom not in HA_DOMAINS:
            continue
        raw = st.get("state") or ""
        # 只剔除未接入占位实体（unknown）；unavailable 保留（前端标红显示掉线）
        if raw in ("unknown", ""):
            continue
        attrs = st.get("attributes") or {}
        name = attrs.get("friendly_name") or eid
        state_cn = DEVICE_STATE_CN.get(raw, raw)
        devices.append({
            "id": f"device-{eid}", "type": "device",
            "entity_id": eid, "domain": dom,
            "name": name,
            "title": f"{name} · {state_cn}",
            "content": f"智能家居 {dom} · 状态：{state_cn}",
            "state": raw,
            "active": raw in DEVICE_ACTIVE_STATES,
            "ts": "",
        })
    return devices[:MAX_DEVICES]

DATA_TYPE_LABELS = {
    "heart_rate": "心率",
    "weight": "体重",
    "blood_pressure": "血压",
    "blood_oxygen": "血氧",
    "blood_glucose": "血糖",
    "temperature": "体温",
    "sleep_hours": "睡眠",
    "steps": "步数",
}


def _format_health_title(data_type, value_main, value_extra):
    """健康星标题：数据类型 + 主要数值"""
    label = DATA_TYPE_LABELS.get(data_type, data_type)
    if data_type == "blood_pressure" and value_extra:
        try:
            extra = json.loads(value_extra)
            sys_ = extra.get("systolic", value_main)
            dia_ = extra.get("diastolic", "")
            return f"{label} {sys_}/{dia_}" if dia_ else f"{label} {sys_}"
        except (json.JSONDecodeError, TypeError):
            pass
    unit = {"weight": "kg", "temperature": "°C"}.get(data_type, "")
    return f"{label} {value_main}{unit}".strip()


async def handle_galaxy(request):
    """GET /api/memory/galaxy — 星系星点+连线数据"""
    conn = None
    try:
        if not DB_PATH.exists():
            return web.json_response({
                "success": True, "stars": [], "links": [],
                "message": "devices.db 不存在，星系为空",
            })

        stars = []
        links = []
        member_names = {}

        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. 家庭成员（锚点星）
        try:
            rows = cur.execute(
                "SELECT id, name, gender, age FROM family_members ORDER BY id").fetchall()
            for r in rows:
                member_names[r["id"]] = r["name"]
                stars.append({
                    "id": f"member-{r['id']}", "type": "member",
                    "name": r["name"] or f"成员{r['id']}",
                    "title": r["name"] or f"成员{r['id']}",
                    "content": f"家庭成员 · {r['age'] or '?'}岁",
                    "ts": "",
                })
        except sqlite3.OperationalError:
            pass  # 表未建，跳过

        # 2. 家庭记事（memory_notes）
        try:
            rows = cur.execute(
                "SELECT id, key, title, content, updated_at FROM memory_notes "
                "WHERE active=1 ORDER BY updated_at DESC LIMIT ?",
                (MAX_NOTES,)).fetchall()
            for r in rows:
                stars.append({
                    "id": f"note-{r['id']}", "type": "note",
                    "title": r["title"] or r["key"],
                    "content": (r["content"] or "")[:300],
                    "ts": r["updated_at"] or "",
                })
        except sqlite3.OperationalError:
            pass

        # 3. 日程（今日 + 未来7天，全员）
        today = str(date.today())
        horizon = str(date.today() + timedelta(days=7))
        try:
            rows = cur.execute(
                "SELECT s.id, s.member_id, s.title, s.date, s.time, s.notes "
                "FROM member_schedules s "
                "WHERE s.date >= ? AND s.date <= ? "
                "ORDER BY s.date, s.time LIMIT ?",
                (today, horizon, MAX_SCHEDULES)).fetchall()
            for r in rows:
                mid = r["member_id"]
                stars.append({
                    "id": f"sched-{r['id']}", "type": "schedule",
                    "member_id": mid,
                    "member_name": member_names.get(mid, ""),
                    "title": r["title"] or "日程",
                    "content": f"{r['date']} {r['time'] or ''}".strip()
                               + (f" · {r['notes']}" if r["notes"] else ""),
                    "ts": f"{r['date']} {r['time'] or ''}".strip(),
                })
                if mid in member_names:
                    links.append({"from": f"member-{mid}", "to": f"sched-{r['id']}"})
        except sqlite3.OperationalError:
            pass

        # 4. 健康记录（每成员最近 N 条非基线）
        try:
            member_rows = cur.execute("SELECT id FROM family_members").fetchall()
            for m in member_rows:
                mid = m["id"]
                rows = cur.execute(
                    "SELECT id, data_type, value_main, value_extra, timestamp "
                    "FROM health_data WHERE member_id=? AND is_baseline=0 "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (mid, MAX_HEALTH_PER_MEMBER)).fetchall()
                for r in rows:
                    stars.append({
                        "id": f"health-{r['id']}", "type": "health",
                        "member_id": mid,
                        "member_name": member_names.get(mid, ""),
                        "title": _format_health_title(
                            r["data_type"], r["value_main"], r["value_extra"]),
                        "content": r["timestamp"] or "",
                        "ts": r["timestamp"] or "",
                    })
                    links.append({"from": f"member-{mid}", "to": f"health-{r['id']}"})
        except sqlite3.OperationalError:
            pass

        # 5. 珍贵时刻（conversation_summaries，valuable=1 优先，主人确认只挂珍贵记忆）
        try:
            rows = cur.execute(
                "SELECT id, session_id, topic, content, keywords, valuable, created_at "
                "FROM conversation_summaries WHERE valuable=1 "
                "ORDER BY created_at DESC LIMIT ?",
                (MAX_MEMORIES,)).fetchall()
            for r in rows:
                title = (r["topic"] or "").strip() or (r["content"] or "")[:30] or "珍贵时刻"
                stars.append({
                    "id": f"memory-{r['id']}", "type": "memory",
                    "title": title,
                    "content": (r["content"] or "")[:200],
                    "keywords": r["keywords"] or "",
                    "ts": str(r["created_at"] or ""),
                })
        except sqlite3.OperationalError:
            pass

        # 6. 安防摄像头（cameras 表，每颗星一个摄像头，外圈自由星）
        try:
            rows = cur.execute(
                "SELECT id, name, role, room_id FROM cameras LIMIT ?",
                (MAX_CAMERAS,)).fetchall()
            for r in rows:
                stars.append({
                    "id": f"security-{r['id']}", "type": "security",
                    "title": r["name"] or f"摄像头{r['id']}",
                    "content": "安防摄像头 · 双击查看监控",
                    "camera_id": r["id"],
                    "role": r["role"] or "normal",
                    "ts": "",
                })
        except sqlite3.OperationalError:
            pass

        # 7. 智能家居设备（Home Assistant 实时拉取，可控实体）
        ha_devices = await fetch_ha_devices()
        stars.extend(ha_devices)

        return web.json_response({
            "success": True,
            "stars": stars,
            "links": links,
            "counts": {
                "member": sum(1 for s in stars if s["type"] == "member"),
                "note": sum(1 for s in stars if s["type"] == "note"),
                "schedule": sum(1 for s in stars if s["type"] == "schedule"),
                "health": sum(1 for s in stars if s["type"] == "health"),
                "memory": sum(1 for s in stars if s["type"] == "memory"),
                "security": sum(1 for s in stars if s["type"] == "security"),
                "device": sum(1 for s in stars if s["type"] == "device"),
            },
        })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)
    finally:
        if conn:
            conn.close()
