# -*- coding: utf-8 -*-
"""地点簿管理 API（2026-10-05）

给管理后台「房屋状态 → 地点簿」区块用。对应 core/utils/place_manager.py。

设计要点：
    1. 新增/更新地点时，若没传坐标就用地址自动 geocode，并把省/市/区县一起存下。
       高德对陌生地址会「尽力猜」匹配同名 POI（实测「火星基地A区」→ 新疆哈密，
       status 仍为 1），所以这里用 geocode_detail（内含行政区一致性校验）：
         - 解析成功且行政区对得上 → 静默保存
         - 解析成功但行政区对不上 → 返回 warning，前端弹黄色告警让用户确认
         - 解析失败 → 拒绝保存并说明原因，不静默存一个没有坐标的地点
    2. 「家」不允许通过本 API 新建（is_primary 全局唯一），也不允许删。
    3. 所有异常都返回 JSON，不抛栈给前端。

    [2026-10-05] 取消了 force_save（强行保存无坐标地点）：
        那个开关在管理台 UI 上并不存在，但早期版本的报错文案却让用户
        「勾选『仍然保存』再试一次」—— 指引用户去做一件根本做不到的事。
        而且存下没有坐标的地点，用户要等到下次对话算路程时才发现问题，
        白等一轮。所以现在一律拒绝保存，把地址要求说清楚。
"""
import json
import sqlite3

from aiohttp import web

from core.utils.geo import geocode_detail
from core.utils.place_manager import (
    COMMUTE_MODES,
    LIFE_STAGES,
    PLACE_TYPES,
    create_place,
    delete_place,
    ensure_schema,
    get_db_path,
    get_place,
    list_places,
    update_place,
)

TAG = __name__


def _json(payload, status=200):
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json",
        status=status,
    )


def _ok(**kwargs):
    payload = {"success": True}
    payload.update(kwargs)
    return _json(payload)


def _fail(message, status=400, **kwargs):
    payload = {"success": False, "message": message}
    payload.update(kwargs)
    return _json(payload, status)


def _type_options():
    """地点类型 / 出行方式 / 人生阶段的受控枚举，供前端下拉使用"""
    return {
        "place_types": [{"value": k, "label": v[0]} for k, v in PLACE_TYPES.items()],
        "commute_modes": [{"value": k, "label": v[0]} for k, v in COMMUTE_MODES.items()],
        "life_stages": list(LIFE_STAGES.keys()),
    }


def _members_for_select():
    """成员下拉选项。称呼用 nickname（= 伊娃对他的称呼），与注入 LLM 的口径一致。"""
    db = get_db_path()
    try:
        conn = sqlite3.connect(db, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, name, nickname, life_stage FROM family_members ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r["id"],
                "label": (r["nickname"] or "").strip() or (r["name"] or "").strip(),
                "name": r["name"],
                "life_stage": r["life_stage"] if "life_stage" in r.keys() else "",
            }
            for r in rows
        ]
    except Exception:
        return []


def _resolve_coords(data: dict, old: dict = None):
    """决定保存时要写的坐标与行政区。

    返回 (lng, lat, province, city, district, adcode, warning)
    - 用户显式传了坐标 → 用用户的（手工修正优先于自动解析）
    - 地址变了 → **立即用新地址重新 geocode**（绝不能沿用旧坐标，
      早期版本只把坐标清空，导致用户改完地址还得等下一次对话才重新定位）
    - 地址没变且有旧坐标 → 沿用旧的，不重复调高德（高德 QPS 很敏感）

    ⚠️ 「地址变了」只在 data 里真的带了 address 字段时才判断。
    早期版本写成「拿 data.get('address') 的空串去和旧地址比」，结果用户只改个
    地点名称也会被判成「地址变了」，坐标被清空，下次算路程又得重新调高德。
    """
    has_coord = data.get("lng") not in (None, "") and data.get("lat") not in (None, "")
    if has_coord:
        try:
            return (float(data["lng"]), float(data["lat"]),
                    (data.get("province") or "").strip(), (data.get("city") or "").strip(),
                    (data.get("district") or "").strip(), (data.get("adcode") or "").strip(),
                    "")
        except (TypeError, ValueError):
            return None

    address_changed = (
        old is not None
        and "address" in data
        and (data.get("address") or "").strip() != (old.get("address") or "").strip()
    )
    if address_changed:
        # 地址变了 → 旧坐标立刻作废，绝不能沿用（否则会拿旧地址的坐标去算路程）。
        # 这里不直接返回，而是落到下面的 geocode 流程，用新地址当场重新定位，
        # 这样主人改完地址就能立刻看到新坐标，不用等到下次对话。
        pass
    elif old is not None and old.get("lng") is not None and old.get("lat") is not None:
        # 地址没变且已有坐标：沿用，不重复调高德（省 QPS，高德限流很敏感）
        return (old.get("lng"), old.get("lat"),
                (old.get("province") or ""), (old.get("city") or ""),
                (old.get("district") or ""), (old.get("adcode") or ""), "")

    address = (data.get("address") or "").strip()
    if not address:
        return None

    geo = geocode_detail(address)
    if not geo:
        return ("__GEO_FAILED__",)  # 哨兵：解析失败

    # 行政区一致性由 geocode_detail 内部校验，这里再确认一次命中了什么，
    # 拿返回的省/市跟用户输入对一下，对不上就给前端一个 warning（不阻塞保存）
    warning = ""
    want = address
    got = "".join(filter(None, [geo.get("province"), geo.get("city"), geo.get("district")]))
    # 只在「返回的行政区一个字都没出现在用户输入里」时告警
    # （用户写简称如「朝阳区」而高德返回「北京市朝阳区」属正常，不该告警）
    if got and not any(part and part in want for part in
                       (geo.get("district"), geo.get("city"), geo.get("province"))):
        warning = (
            f"高德把这个地址匹配到了「{got}」，和你填的地址对不上。"
            f"确认要保存吗？如果不对，请把地址写得更完整一些。"
        )

    return (geo["lng"], geo["lat"], geo.get("province") or "", geo.get("city") or "",
            geo.get("district") or "", geo.get("adcode") or "", warning)


async def handle_get_places(request):
    """GET /api/places —— 地点簿列表 + 下拉选项 + 成员选项"""
    try:
        ensure_schema()
        return _ok(places=list_places(), **_type_options(),
                   members=_members_for_select())
    except Exception as e:
        return _fail(f"读取地点簿失败：{e}", 500)


async def handle_create_place(request):
    """POST /api/places

    {
      "name": "爸爸的公司", "type": "work", "member_id": 15,
      "address": "北京市朝阳区建国路88号",
      "aliases": "公司,单位,上班,工作",
      "commute_mode": "drive", "depart_time": "08:00"
    }
    """
    try:
        data = await request.json()
        ensure_schema()

        name = (data.get("name") or "").strip()
        if not name:
            return _fail("地点名称不能为空")

        ptype = (data.get("type") or "other").strip()
        if ptype == "home":
            return _fail("「家」请用上面的「➕ 设置家庭地址」录入，"
                         "它全局唯一，不能在这里重复添加")

        coords = _resolve_coords(data)
        if coords is None:
            return _fail("请填写地点地址")
        if coords and coords[0] == "__GEO_FAILED__":
            # 解析失败一律拒绝保存。存一个没有坐标的地点看起来"成功了"，
            # 但用户要等到下次对话算路程时才发现，白等一轮 ——
            # 所以宁可现在报错，也不让坏数据进库。
            # 早期版本的提示写「可以勾选『仍然保存』」，但 UI 上根本没有这个复选框，
            # 用户被指引去做一件做不到的事。文案必须和实际能做的事一致。
            addr = (data.get("address") or "").strip()
            return _fail(
                f"没能从地址「{addr}」解析出坐标，所以没法保存。"
                f"高德对不认识的地址会乱猜（实测「火星基地A区」会匹配到新疆哈密），"
                f"这里已经挡掉了。请把地址写得更完整一些，"
                f"带上省/市/区县，例如「北京市朝阳区建国路88号」，再保存一次。"
            )

        lng, lat, province, city, district, adcode, warning = coords
        payload = dict(data)
        payload.update({
            "name": name, "type": ptype, "lng": lng, "lat": lat,
            "province": province, "city": city, "district": district, "adcode": adcode,
        })
        result = create_place(payload)
        if not result.get("success"):
            return _fail(result.get("message") or "添加失败")
        return _ok(place=result.get("place"), message=result.get("message"),
                   warning=warning)
    except Exception as e:
        return _fail(f"添加地点失败：{e}", 500)


async def handle_update_place(request):
    """PUT /api/places/{place_id}"""
    place_id = int(request.match_info.get("place_id"))
    try:
        data = await request.json()
        ensure_schema()
        old = get_place(place_id)
        if not old:
            return _fail("地点不存在", 404)

        if old["is_primary"]:
            # 「家」的地址只能从「设置家庭地址」改，否则会出现两个数据源
            patch = {k: v for k, v in data.items()
                     if k in ("name", "notes", "aliases")}
            if patch:
                result = update_place(place_id, patch)
                if result.get("success"):
                    return _ok(place=result.get("place"), message=result.get("message"),
                               warning="")
            return _fail("「家」的地址请到「➕ 设置家庭地址」里修改，"
                         "这里只能改名称和备注")

        coords = _resolve_coords(data, old)
        if coords is not None and coords and coords[0] == "__GEO_FAILED__":
            addr = (data.get("address") or "").strip()
            return _fail(
                f"没能从地址「{addr}」解析出坐标，所以没法保存。"
                f"高德对不认识的地址会乱猜（实测「火星基地A区」会匹配到新疆哈密），"
                f"这里已经挡掉了。请把地址写得更完整一些，"
                f"带上省/市/区县，例如「北京市朝阳区建国路88号」，再保存一次。"
            )
        warning = ""
        if coords is not None:
            lng, lat, province, city, district, adcode, warning = coords
            # 「家」的字段不参与这里的写入
            data = dict(data)
            if lng is not None:
                data["lng"], data["lat"] = lng, lat
            data["province"], data["city"] = province, city
            data["district"], data["adcode"] = district, adcode

        result = update_place(place_id, data)
        if not result.get("success"):
            return _fail(result.get("message") or "更新失败")
        return _ok(place=result.get("place"), message=result.get("message"),
                   warning=warning)
    except Exception as e:
        return _fail(f"更新地点失败：{e}", 500)


async def handle_delete_place(request):
    """DELETE /api/places/{place_id}"""
    place_id = int(request.match_info.get("place_id"))
    try:
        ensure_schema()
        result = delete_place(place_id)
        if not result.get("success"):
            return _fail(result.get("message") or "删除失败")
        return _ok(message=result.get("message"))
    except Exception as e:
        return _fail(f"删除地点失败：{e}", 500)


async def handle_sync_home_place(request):
    """POST /api/places/sync-home —— 把「设置家庭地址」里改的地址同步到地点簿

    「家」的地址在两处可改（房屋地址弹窗、这里），所以提供一个显式同步接口。
    保存房屋地址后前端调一次，保证两个地方不会不一致。
    """
    try:
        ensure_schema()
        db = get_db_path()
        conn = sqlite3.connect(db, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            # ⚠️ 不同版本的 houses 表列不一样（早期没有省/市/区县），
            # 必须逐列探测，直接 SELECT province 会在旧库上抛 no such column。
            cols = {r[1] for r in conn.execute("PRAGMA table_info(houses)").fetchall()}
            wanted = ["id", "name", "address", "lng", "lat",
                      "province", "city", "district", "adcode"]
            picked = [c for c in wanted if c in cols]
            row = conn.execute(
                f"SELECT {', '.join(picked)} FROM houses ORDER BY id LIMIT 1"
            ).fetchone()
            home = dict(zip(picked, row)) if row else None
            existing = conn.execute(
                "SELECT id FROM places WHERE is_primary=1 LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        if not home:
            return _fail("houses 表里还没有家庭地址，请先在「➕ 设置家庭地址」里填一次", 404)
        if not existing:
            return _fail("地点簿里还没有「家」，请重启一次服务让它自动迁移", 404)

        result = update_place(existing["id"], {
            "name": home.get("name") or "家",
            "address": home.get("address") or "",
            "lng": home.get("lng"), "lat": home.get("lat"),
            "province": home.get("province") or "", "city": home.get("city") or "",
            "district": home.get("district") or "", "adcode": home.get("adcode") or "",
        }, db)
        if not result.get("success"):
            return _fail(result.get("message") or "同步失败")
        return _ok(place=result.get("place"), message="家庭地址已同步到地点簿")
    except Exception as e:
        return _fail(f"同步失败：{e}", 500)
