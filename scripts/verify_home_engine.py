#!/usr/bin/env python3
"""家居引擎真实端到端验证（对着本机 docker 测试 HA，不碰主人的真设备）

用法：
    cd server/homebrain-server
    docker 容器 hb-ha-test（端口 8124，demo 集成；configuration.yaml 需带 customize 锁/音箱中文名 + scene(id) + script，见测试记录文档）
    venv/bin/python scripts/verify_home_engine.py

做的事：
  1. 把 demo 实体改成中文名、建楼层/房间并分配、暴露给 conversation（幂等，可重复跑）；
  2. 走真实引擎链路（寻址 → 安全分级 → 动作 → 真发 HA 服务 → 回读校验 → 播报）逐场景验证，
     每条都断言 **HA 里的真实状态**，并打印播报原文；
  3. 退出码 = 失败条数。

令牌取 /tmp/hb_ha_token.txt（HA 长效令牌）。⚠️ registry 的 WS 订阅任务会让 asyncio 不退出，必须 os._exit。
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.environ.get("HB_TEST_HA", "http://localhost:8124")
TOKEN = open("/tmp/hb_ha_token.txt").read().strip()

import core.home.config as C  # noqa: E402
import core.home.executor as X  # noqa: E402
import core.home.registry as R  # noqa: E402

for _m in (C, X, R):
    _m.get_ha_config = lambda force=False: (BASE, TOKEN)

from core.home import engine, guard  # noqa: E402
from core.home.registry import get_registry  # noqa: E402
from core.home.session import get_session_store  # noqa: E402

LAYOUT = {
    "一层": ["客厅", "厨房", "玄关", "车库", "走廊"],
    "二层": ["卧室", "办公室"],
}
DEVICES = {   # entity_id: (中文名, 房间)
    "light.bed_light": ("床头灯", "卧室"),
    "light.ceiling_lights": ("客厅吊灯", "客厅"),
    "light.kitchen_lights": ("厨房灯", "厨房"),
    "light.office_rgbw_lights": ("射灯一", "办公室"),
    "light.living_room_rgbww_lights": ("客厅射灯", "客厅"),
    "light.entrance_color_white_lights": ("玄关灯", "玄关"),
    "cover.living_room_window": ("客厅窗帘", "客厅"),
    "cover.kitchen_window": ("厨房窗帘", "厨房"),
    "cover.hall_window": ("走廊窗帘", "走廊"),
    "cover.garage_door": ("车库门", "车库"),
    "lock.front_door": ("前门锁", "玄关"),
    "lock.kitchen_door": ("厨房门锁", "厨房"),
    "climate.hvac": ("客厅空调", "客厅"),
    "climate.heatpump": ("卧室空调", "卧室"),
    "fan.living_room_fan": ("客厅风扇", "客厅"),
    "media_player.living_room": ("客厅音箱", "客厅"),
    "media_player.bedroom": ("卧室音箱", "卧室"),
    "switch.decorative_lights": ("装饰灯", "客厅"),
    "scene.guan_ying_mo_shi": ("观影模式", "客厅"),
    "script.good_night": ("晚安", "卧室"),
}
# demo 的锁/音箱没有 unique_id，进不了实体注册表：名字靠 configuration.yaml 的 customize，也分配不了房间
NO_REGISTRY = {"lock.front_door", "lock.kitchen_door", "media_player.living_room", "media_player.bedroom"}
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
RESULTS = []


# ---------------- HA 辅助 ----------------
async def svc(domain, service, **data):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/services/{domain}/{service}", headers=H, json=data) as r:
            return r.status


async def state(eid):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/api/states/{eid}", headers=H) as r:
            return await r.json()


async def set_raw_state(eid, st, attrs=None):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/states/{eid}", headers=H, json={"state": st, "attributes": attrs or {}}) as r:
            return r.status


async def settle(eid, want, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if (await state(eid))["state"] == want:
            return True
        await asyncio.sleep(0.3)
    return False


async def ws_setup():
    """改名 / 建楼层房间 / 分配 / 暴露（幂等）"""
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(BASE.replace("http", "ws") + "/api/websocket")
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        await ws.receive_json()
        n = [0]

        async def call(t, **k):
            n[0] += 1
            await ws.send_json({"id": n[0], "type": t, **k})
            while True:
                m = await ws.receive_json()
                if m.get("id") == n[0]:
                    if m.get("success") is False:
                        print(f"[setup] {t} {k.get('entity_id', '')} 失败: {m.get('error')}", flush=True)
                    return m

        floors = {f["name"]: f["floor_id"] for f in (await call("config/floor_registry/list"))["result"]}
        areas = {a["name"]: a["area_id"] for a in (await call("config/area_registry/list"))["result"]}
        for level, (fname, rooms) in enumerate(LAYOUT.items(), start=1):
            if fname not in floors:
                floors[fname] = (await call("config/floor_registry/create", name=fname, level=level))["result"]["floor_id"]
            for room in rooms:
                if room not in areas:
                    areas[room] = (await call("config/area_registry/create", name=room,
                                              floor_id=floors[fname]))["result"]["area_id"]
                else:
                    await call("config/area_registry/update", area_id=areas[room], floor_id=floors[fname])
        for eid, (name, room) in DEVICES.items():
            if eid not in NO_REGISTRY:
                await call("config/entity_registry/update", entity_id=eid, name=name, area_id=areas[room])
        await call("homeassistant/expose_entity", assistants=["conversation"],
                   entity_ids=list(DEVICES), should_expose=True)
        await ws.close()


# ---------------- 断言框架 ----------------
def check(name, cond, spoken="", note=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}" + (f"\n     播报：{spoken}" if spoken else "") + (f"\n     {note}" if note else ""),
          flush=True)


def make_conn(did="verify-dev", speaker=None, vp=False):
    return SimpleNamespace(device_id=did, current_speaker=speaker,
                           voiceprint_provider=object() if vp else None, config={},
                           logger=SimpleNamespace(bind=lambda **k: SimpleNamespace(
                               info=lambda *a, **k: None, warning=lambda *a, **k: None)))


async def say(conn, *cmds):
    return await engine.run(conn, commands=list(cmds))


async def reset():
    get_session_store().clear()
    for eid in ("lock.front_door", "lock.kitchen_door"):
        await svc("lock", "lock", entity_id=eid)
    await svc("cover", "close_cover", entity_id="cover.garage_door")
    await svc("light", "turn_off", entity_id="light.bed_light")
    await svc("light", "turn_on", entity_id="light.ceiling_lights")
    await svc("climate", "set_temperature", entity_id="climate.hvac", temperature=21)
    await asyncio.sleep(1.2)


# ---------------- 场景 ----------------
async def main():
    await ws_setup()
    await svc("lock", "lock", entity_id="lock.front_door")
    reg = get_registry()
    await reg.ensure_ready(force=True)
    await asyncio.sleep(2)
    check("注册表载入中文名/房间/楼层",
          reg.get("light.bed_light") and reg.get("light.bed_light").name == "床头灯"
          and reg.get("light.bed_light").area == "卧室" and reg.get("light.bed_light").floor == "二层",
          note=f"实体 {len(reg.all())} 个 / 区域 {len(reg.areas)} 个 / 订阅 live={reg.live}")
    conn = make_conn()

    # 1 开灯
    await reset()
    r = await say(conn, {"action": "turn_on", "name": "床头灯"})
    check("1 开灯：真亮 + 校验ok + 播报", (await state("light.bed_light"))["state"] == "on"
          and r.statuses == ["ok"] and "已打开床头灯" in r.spoken, r.spoken)

    # 2 离线
    await reset()
    await set_raw_state("light.office_rgbw_lights", "unavailable")
    await asyncio.sleep(0.8)
    r = await say(conn, {"action": "turn_on", "name": "射灯一"})
    check("2 离线设备：如实说离线，绝不说已打开", r.statuses == ["offline"] and "离线" in r.spoken
          and "已打开" not in r.spoken, r.spoken)
    await svc("light", "turn_off", entity_id="light.office_rgbw_lights")
    await asyncio.sleep(0.8)

    # 3 门锁确认流程
    await reset()
    r = await say(conn, {"action": "unlock", "name": "前门锁"})
    check("3a 开锁先问不做", r.ask and "确认" in r.spoken and (await state("lock.front_door"))["state"] == "locked", r.spoken)
    x = await engine.confirm_or_cancel(conn, "把客厅的灯打开")
    check("3b 无关话不触发开锁、待确认还在", x is None and (await state("lock.front_door"))["state"] == "locked"
          and engine.has_pending(conn))
    x = await engine.confirm_or_cancel(conn, "确认")
    check("3c 说「确认」才真开", (await settle("lock.front_door", "unlocked")) and "已打开" in x.spoken, x.spoken)
    r = await say(conn, {"action": "lock", "name": "前门锁"})
    check("3d 上锁不需确认", (await settle("lock.front_door", "locked")) and "已锁上" in r.spoken, r.spoken)
    await say(conn, {"action": "unlock", "name": "前门锁"})
    x = await engine.confirm_or_cancel(conn, "算了")
    check("3e 取消", "不动" in x.spoken and (await state("lock.front_door"))["state"] == "locked", x.spoken)
    r = await say(conn, {"action": "turn_on", "name": "前门锁"})
    check("3f 对锁说「打开(on)」不会被当成开锁", (await state("lock.front_door"))["state"] == "locked"
          and "开锁" in r.spoken, r.spoken)

    # 4 撤销
    await reset()
    await svc("light", "turn_on", entity_id="light.bed_light", brightness_pct=40)
    await asyncio.sleep(1)
    await say(conn, {"action": "set_brightness", "name": "床头灯", "value": 90})
    b = (await state("light.bed_light"))["attributes"].get("brightness")
    r = await say(conn, {"action": "undo"})
    await asyncio.sleep(1)
    b2 = (await state("light.bed_light"))["attributes"].get("brightness")
    check("4a 撤销：亮度还原", abs(round(b2 / 255 * 100) - 40) <= 2 and b > b2, r.spoken, f"调后 {b} → 撤销后 {b2}")
    await say(conn, {"action": "close", "name": "客厅窗帘"})
    await settle("cover.living_room_window", "closed")
    await svc("cover", "open_cover", entity_id="cover.living_room_window")
    await settle("cover.living_room_window", "open")
    await say(conn, {"action": "close", "name": "客厅窗帘"})
    await settle("cover.living_room_window", "closed")
    r = await say(conn, {"action": "undo"})
    check("4b 撤销：窗帘还原", await settle("cover.living_room_window", "open"), r.spoken)
    await svc("cover", "open_cover", entity_id="cover.garage_door")
    await settle("cover.garage_door", "open", 20)
    await say(conn, {"action": "close", "name": "车库门"})
    await settle("cover.garage_door", "closed", 20)
    r = await say(conn, {"action": "undo"})
    await asyncio.sleep(1)
    check("4c 撤销不自动重开车库门", (await state("cover.garage_door"))["state"] == "closed" and "确认" in r.spoken, r.spoken)

    # 5 窗帘 stop（客厅窗帘是 demo 里会渐变移动的那个）
    await reset()
    await svc("cover", "open_cover", entity_id="cover.living_room_window")
    await settle("cover.living_room_window", "open", 20)
    await svc("cover", "close_cover", entity_id="cover.living_room_window")
    await asyncio.sleep(1.5)
    mid = await state("cover.living_room_window")
    r = await say(conn, {"action": "stop", "name": "客厅窗帘"})
    await asyncio.sleep(0.5)
    p1 = (await state("cover.living_room_window"))["attributes"].get("current_position")
    await asyncio.sleep(2.5)
    after = await state("cover.living_room_window")
    check("5 窗帘 stop = 停在半路（不是关）", "已停下" in r.spoken and after["state"] != "closed"
          and after["attributes"].get("current_position") == p1 and (p1 or 0) > 0, r.spoken,
          f"关的途中 {mid['state']}/{mid['attributes'].get('current_position')} → 停后 {after['state']}/{p1}→{after['attributes'].get('current_position')}")

    # 6 空调
    await reset()
    r = await say(conn, {"action": "temperature_down", "name": "客厅空调"})
    t = (await state("climate.hvac"))["attributes"].get("temperature")
    check("6a 调低一度基于当前温度（21→20）", t == 20 and "已设为20度" in r.spoken, r.spoken, f"HA 温度={t}")
    t0 = time.time()
    r = await say(conn, {"action": "set_temperature", "name": "客厅空调", "value": 20})
    check("6b 设成当前值：秒回「本来就是」，不白等", time.time() - t0 < 1.5 and r.statuses == ["already"], r.spoken,
          f"耗时 {time.time() - t0:.2f}s")
    r = await say(conn, {"action": "set_temperature", "name": "客厅空调", "value": 99})
    check("6c 越界温度如实拒绝", "之间" in r.spoken, r.spoken)

    # 7 歧义
    r = await say(conn, {"action": "turn_on", "domain": "light"})
    check("7a 多个房间有灯 → 反问房间", r.ask and "要哪个房间" in r.spoken, r.spoken)
    r = await say(make_conn("dev-ke"), {"action": "turn_off", "domain": "light", "area": "客厅"})
    check("7b 指定房间 → 合并播报", "客厅" in r.spoken, r.spoken)

    # 8 找不到
    r = await say(conn, {"action": "turn_on", "domain": "light", "area": "车库"})
    check("8a 房间没有这类设备", "车库没有灯" in r.spoken and "有灯的是" in r.spoken, r.spoken)
    r = await say(conn, {"action": "turn_on", "domain": "input_boolean"})
    check("8b 家里没有这类设备", "家里没有开关" in r.spoken, r.spoken)
    r = await say(conn, {"action": "turn_on", "name": "不存在的灯", "domain": "light"})
    check("8c 名字找不到", "没找到" in r.spoken, r.spoken)

    # 9 场景/脚本
    await reset()
    await svc("light", "turn_on", entity_id="light.ceiling_lights")
    await asyncio.sleep(0.8)
    r = await say(conn, {"action": "activate", "name": "观影模式"})
    check("9a 场景：真执行（吊灯被关）", await settle("light.ceiling_lights", "off"), r.spoken)
    await svc("light", "turn_on", entity_id="light.bed_light")
    await asyncio.sleep(0.8)
    r = await say(conn, {"action": "activate", "name": "晚安"})
    check("9b 脚本：真执行（床头灯被关）", await settle("light.bed_light", "off"), r.spoken)

    # 10 声纹 / 家人关系
    await reset()
    vconn = make_conn("dev-vp", speaker=None, vp=True)
    r = await say(vconn, {"action": "unlock", "name": "前门锁"})
    check("10a 声纹开启但没认出人 → 拒绝开锁", (await state("lock.front_door"))["state"] == "locked" and "认出" in r.spoken, r.spoken)
    guard._member = lambda s: {"name": s, "relation": "同事", "age": 38, "life_stage": ""}
    r = await say(make_conn("dev-vp2", speaker="某同事", vp=True), {"action": "unlock", "name": "前门锁"})
    check("10b 同事声纹认出也不能开锁", (await state("lock.front_door"))["state"] == "locked" and "家里人" in r.spoken, r.spoken)
    guard._member = lambda s: {"name": s, "relation": "父亲", "age": 52, "life_stage": ""}
    fconn = make_conn("dev-vp3", speaker="李威", vp=True)
    r = await say(fconn, {"action": "unlock", "name": "前门锁"})
    x = await engine.confirm_or_cancel(fconn, "确认")
    check("10c 家人：问 → 确认 → 真开", await settle("lock.front_door", "unlocked") and x is not None, x.spoken if x else r.spoken)
    await svc("lock", "lock", entity_id="lock.front_door")
    guard._member = lambda s: None

    # 11 entities / 车库门
    await reset()
    from plugins_func.functions import ha_control as hc
    res = await hc.apply_entities({"lock.front_door": "unlock", "light.bed_light": "on"})
    check("11a 老入口 entities 开锁被拦且整批不执行", "error" in res and (await state("lock.front_door"))["state"] == "locked"
          and (await state("light.bed_light"))["state"] == "off", res.get("error", ""))
    res = await hc.apply_entities({"light.bed_light": "on", "lock.kitchen_door": "turn_on"}, trusted=True)
    check("11b 混合批不抛异常", res.get("ok") == 1 and "lock.kitchen_door" in res.get("failed", {}), res.get("spoken", ""))
    r = await say(conn, {"action": "open", "name": "车库门"})
    check("11c 开车库门先确认", r.ask and (await state("cover.garage_door"))["state"] == "closed", r.spoken)

    # 12 语音直通的确认钩子
    from core.utils import home_intent
    home_intent._log_record = lambda rec: None
    home_intent._pipeline = AsyncMock(return_value="__走了判定管线__")
    await reset()
    hconn = make_conn("dev-hook")
    await say(hconn, {"action": "unlock", "name": "前门锁"})
    out = await home_intent._confirm_first_then_pipeline(hconn, "今天天气好")
    check("12a 待确认时说无关话 → 放行给原管线，锁不动", out == "__走了判定管线__" and (await state("lock.front_door"))["state"] == "locked")
    out = await home_intent._confirm_first_then_pipeline(hconn, "确认")
    check("12b 直通钩子吃掉「确认」→ 真开锁", await settle("lock.front_door", "unlocked") and out and "已打开" in out, out or "")
    await svc("lock", "lock", entity_id="lock.front_door")

    # 13 订阅稳定性（force 刷新不再踢掉常驻订阅）
    reg = get_registry()
    for _ in range(3):
        await reg.ensure_ready(force=True)
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(BASE.replace("http", "ws") + "/api/websocket")
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        await ws.receive_json()
        await ws.send_json({"id": 1, "type": "config/area_registry/update", "area_id": "ke_ting", "name": "客厅", "aliases": ["大厅"]})
        await ws.receive_json()
        await ws.send_json({"id": 2, "type": "config/area_registry/update", "area_id": "ke_ting", "name": "客厅", "aliases": []})
        await ws.receive_json()
        await ws.close()
    await asyncio.sleep(4)
    await svc("light", "turn_on", entity_id="light.bed_light")
    await asyncio.sleep(1)
    check("13 force 刷新 + 注册表变更后订阅仍在线并收到实时状态", reg.live and reg.get("light.bed_light").state == "on")

    # 14 总览/查询/目录
    r = await engine.run(conn, summary=True)
    check("14a 全屋总览", "家里现在" in r.spoken, r.spoken)
    r = await engine.run(conn, query=["前门锁", "客厅空调"])
    check("14b 查询", "前门锁" in r.spoken and "客厅空调" in r.spoken, r.spoken)
    text = engine.catalog_summary()
    check("14c 提示词摘要紧凑且无 entity_id", len(text) < 400 and "." not in text.replace("。", ""), text, f"{len(text)} 字")

    # 15 心情场景：用户说心情 → 直接调 + 一句人话 → 微调 → 看语境保存
    import json
    import tempfile
    from core.home import scene_plan, scenes
    from plugins_func.functions import run_ai_scene as ras
    from plugins_func.functions import batch_device_control as bd
    _tmp = tempfile.mkdtemp()
    scenes.CUSTOM_PATH = os.path.join(_tmp, "custom_scenes.json")
    ras._HISTORY_PATH = os.path.join(_tmp, "scene_history.json")        # 不污染真实使用历史
    await reset()
    await svc("light", "turn_on", entity_id="light.ceiling_lights", brightness_pct=100)
    await svc("cover", "open_cover", entity_id="cover.living_room_window")
    await settle("cover.living_room_window", "open", 20)
    await svc("climate", "set_temperature", entity_id="climate.hvac", temperature=21)
    await asyncio.sleep(1.5)
    mconn = make_conn("dev-mood")
    plan_cmds = [
        {"action": "set_brightness", "domain": "light", "area": "客厅", "value": 30},
        {"action": "set_color_temp", "domain": "light", "area": "客厅", "value": 2700},
        {"action": "close", "name": "客厅窗帘"},
        {"action": "set_temperature", "name": "客厅空调", "value": 24},
        {"action": "unlock", "name": "前门锁"},
    ]
    SAY = "好，给你弄得柔和一点，歇会儿吧"
    r = await ras.run_ai_scene(mconn, scene_name="放松", action="mood", commands=plan_cmds, say=SAY,
                               music="轻音乐", context="我有点累")
    await asyncio.sleep(1.5)
    st_l = await state("light.ceiling_lights")
    pct = round(st_l["attributes"].get("brightness", 0) / 255 * 100)
    check("15a 用户累了说放松：直接调，不问确认，只回一句人话（不提设备/数字/状况，不提开锁和离线）",
          r.response == SAY and abs(pct - 30) <= 2 and (await settle("cover.living_room_window", "closed", 25))
          and (await state("climate.hvac"))["attributes"].get("temperature") == 24
          and (await state("lock.front_door"))["state"] == "locked" and not engine.has_pending(mconn),
          r.response, f"灯亮度 {pct}%  色温 {st_l['attributes'].get('color_temp_kelvin')}K")
    r = await engine.run(mconn, commands=[{"action": "brightness_up", "ref": "last"}])
    await asyncio.sleep(1.5)
    pct2 = round((await state("light.ceiling_lights"))["attributes"].get("brightness", 0) / 255 * 100)
    check("15b 「再亮一点」继续调：真亮了，且不念「窗帘/空调不支持」", pct2 > pct and "不支持" not in r.spoken
          and "窗帘" not in r.spoken, r.spoken, f"{pct}% → {pct2}%")
    r = await ras.run_ai_scene(mconn, scene_name="放松", action="save_current")
    saved = json.load(open(scenes.CUSTOM_PATH, encoding="utf-8")) if os.path.exists(scenes.CUSTOM_PATH) else {}
    lamp = [c for c in saved.get("放松", {}).get("commands", []) if c.get("action") == "set_brightness"]
    check("15c 用户主动说「存成场景」：存下来的是当前状态（含刚才调亮的），不含开锁", bool(lamp)
          and abs(lamp[0]["value"] - pct2) <= 2 and "unlock" not in json.dumps(saved), r.response, f"存的亮度 {lamp[0]['value'] if lamp else None}%")
    await svc("light", "turn_on", entity_id="light.ceiling_lights", brightness_pct=100)
    await svc("cover", "open_cover", entity_id="cover.living_room_window")
    await settle("cover.living_room_window", "open", 25)
    await asyncio.sleep(1.5)
    r = await ras.run_ai_scene(mconn, scene_name="放松", action="run")
    await asyncio.sleep(1.5)
    pct3 = round((await state("light.ceiling_lights"))["attributes"].get("brightness", 0) / 255 * 100)
    check("15d 以后一句话复用：已保存场景真执行；带音乐提示时交给大模型自然地说并另放音乐", abs(pct3 - pct2) <= 2
          and (await settle("cover.living_room_window", "closed", 25)) and r.response.startswith("好了，放松给你调好了")
          and "音乐工具" in r.response, r.response)
    # 用户开心：大模型觉得合适才问
    await ras.run_ai_scene(mconn, scene_name="派对", action="mood", say="来，气氛给你拉起来",
                           save_ask="喜欢的话，我把这个存下来？", commands=[{"action": "set_brightness", "domain": "light", "area": "客厅", "value": 90}])
    r = await ras.run_ai_scene(mconn, scene_name="派对", action="mood", say="来，气氛给你拉起来",
                               save_ask="喜欢的话，我把这个存下来？", commands=[{"action": "set_brightness", "domain": "light", "area": "客厅", "value": 90}])
    out = await home_intent._confirm_first_then_pipeline(mconn, "好的")
    saved = json.load(open(scenes.CUSTOM_PATH, encoding="utf-8"))
    check("15e 用户开心：问了才存；回答「好的」→ 存好", "来，气氛给你拉起来" in r.response and "喜欢的话" in r.response
          and "派对" in saved and "存好了" in (out or ""), (r.response + " → 用户：好的 → " + (out or "")))
    out = await home_intent._confirm_first_then_pipeline(mconn, "好的")
    check("15f 存完再说「好的」不会重复触发", out == "__走了判定管线__")

    # 16 预置场景按真实结果汇报（原来离线也报「成功操作 N/N」）
    await set_raw_state("light.office_rgbw_lights", "unavailable")
    await asyncio.sleep(0.8)
    r = await ras.run_ai_scene(mconn, scene_name="dining", action="run") if False else None
    ras._SCENES_CACHE["_e2e"] = {"name": "测试场景", "devices": [
        {"action": "turn_on", "brightness": 255, "color_temp": 3000, "entity_id": "light.office_rgbw_lights"},
        {"action": "turn_on", "entity_id": "light.bed_light"}]}
    r = await ras.run_ai_scene(mconn, scene_name="_e2e", action="run")
    check("16a 预置场景一个设备离线：不念设备状况，一句人话（没人关心哪盏灯）",
          r.response == "好了，测试场景给你调好了" and "成功操作" not in r.response, r.response)
    del ras._SCENES_CACHE["_e2e"]
    await svc("light", "turn_off", entity_id="light.office_rgbw_lights")
    await svc("light", "turn_off", entity_id="light.bed_light")
    await asyncio.sleep(1)
    r = await bd.batch_device_control(mconn, [{"action": "turn_on", "name": "床头灯"}, {"action": "turn_off", "name": "客厅吊灯"}])
    check("16b 批量控制：说人话、按真实结果", "已打开床头灯" in r.response and "已关闭客厅吊灯" in r.response, r.response)

    await reset()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n==== 共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)} ====", flush=True)
    for n in failed:
        print("  ❌", n, flush=True)
    sys.stdout.flush()
    os._exit(len(failed))


asyncio.run(main())
