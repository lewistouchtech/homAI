import json
import sqlite3
import os
from aiohttp import web
from datetime import datetime

from core.utils.geo import geocode_detail

TAG = __name__

class HouseAPI:
    """房屋状态管理 API

    功能:
    1. 房屋管理:创建、更新、删除房屋
    2. 楼层管理:为房屋添加楼层
    3. 房间管理:为楼层添加房间
    4. 设备绑定:将设备绑定到房间
    """

    def __init__(self, config: dict):
        self.config = config
        self.db_path = os.path.join(os.getcwd(), "data", "devices.db")

    def _get_db_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def handle_get_houses(self, request):
        """获取所有房屋

        GET /api/houses
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # 获取所有房屋
            cursor.execute("""
                SELECT h.*,
                       (SELECT COUNT(*) FROM floors f WHERE f.house_id = h.id) as floor_count,
                       (SELECT COUNT(*) FROM rooms r WHERE r.floor_id IN
                           (SELECT id FROM floors WHERE house_id = h.id)) as room_count
                FROM houses h
                ORDER BY h.created_at DESC
            """)
            rows = cursor.fetchall()

            houses = []
            for row in rows:
                houses.append({
                    "id": row["id"],
                    "address": row["address"],
                    "name": row["name"],
                    "lng": row["lng"],
                    "lat": row["lat"],
                    # [2026-10-10] 结构化行政区划：编辑弹窗直接回显，
                    # 不用前端再拿 address 字符串猜（猜不准会导致城市/区县丢失）
                    "province": row["province"],
                    "city": row["city"],
                    "district": row["district"],
                    "adcode": row["adcode"],
                    "floor_count": row["floor_count"],
                    "room_count": row["room_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                })

            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "houses": houses
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_create_house(self, request):
        """创建房屋

        POST /api/houses
        {
            "address": "上海市浦东新区xxx路xxx号",
            "name": "我家"
        }

        [2026-10-04] 新增：保存时自动用高德把地址解析成经纬度一起存下来。
        这样以后查「我家附近哪家最近」可以直接带坐标，不用每次重新查地理编码。
        前端可传 lng/lat 覆盖自动解析的结果（用于自动标点不准时手工修正）。

        [2026-10-10] 新增：同时把省/市/区县/adcode 分列存库。
        以前编辑弹窗只能拿 address 字符串反解，实际只能解出 province，
        city/district 恒为空 → 主人反馈「城市和区没存住」。
        分列存之后编辑时直接读值回显，不会再丢。
        """
        try:
            data = await request.json()
            address = data.get("address", "").strip()
            name = data.get("name", "").strip()

            if not address:
                return web.Response(
                    text=json.dumps({"success": False, "message": "地址不能为空"}),
                    content_type="application/json",
                    status=400
                )

            # [2026-10-04] 坐标：优先用前端传的，否则按地址自动解析
            lng, lat = data.get("lng"), data.get("lat")
            # [2026-10-10] 结构化行政区划同样优先用前端传的（手工修正优先于自动解析）
            province = data.get("province")
            city = data.get("city")
            district = data.get("district")
            adcode = data.get("adcode")

            need_geo = (lng is None or lat is None
                        or not province or not district)
            if need_geo:
                detail = geocode_detail(address)
                if detail:
                    if lng is None:
                        lng = detail["lng"]
                    if lat is None:
                        lat = detail["lat"]
                    province = province or detail["province"]
                    city = city or detail["city"]
                    district = district or detail["district"]
                    adcode = adcode or detail["adcode"]

            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO houses (address, name, lng, lat, province, city, district, adcode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (address, name, lng, lat, province, city, district, adcode))

            house_id = cursor.lastrowid
            conn.commit()
            conn.close()

            msg = "房屋创建成功"
            if lng is None:
                # 地址高德解析不出来时不阻断保存，但要明确告知，否则查距离会静默失败
                msg += "（注意：地址未能解析出坐标，附近查询将不可用，请检查地址是否完整）"

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": msg,
                    "house_id": house_id,
                    "lng": lng,
                    "lat": lat,
                    "province": province,
                    "city": city,
                    "district": district,
                    "adcode": adcode
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_update_house(self, request):
        """更新房屋

        PUT /api/houses/{house_id}
        {
            "address": "新地址",
            "name": "新名称"
        }
        """
        try:
            house_id = request.match_info.get("house_id")
            if not house_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            data = await request.json()
            address = data.get("address", "").strip()
            name = data.get("name", "").strip()

            conn = self._get_db_connection()
            cursor = conn.cursor()

            # 检查房屋是否存在
            cursor.execute("SELECT id FROM houses WHERE id = ?", (house_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋不存在"}),
                    content_type="application/json",
                    status=404
                )

            # [2026-10-04] 地址变了才重新解析坐标；没传 lng/lat 且地址未变则沿用旧坐标
            lng, lat = data.get("lng"), data.get("lat")
            # [2026-10-10] 省/市/区县同理：前端传的优先，否则地址变了重新解析，
            # 地址没变则沿用旧值（沿用很重要——否则每次编辑都会把区县清空）
            province = data.get("province")
            city = data.get("city")
            district = data.get("district")
            adcode = data.get("adcode")

            if lng is None or lat is None or not province or not district:
                prev = cursor.execute(
                    "SELECT address, lng, lat, province, city, district, adcode "
                    "FROM houses WHERE id = ?", (house_id,)
                ).fetchone()

                address_unchanged = prev and (prev["address"] or "").strip() == address

                if address_unchanged:
                    if lng is None:
                        lng = prev["lng"]
                    if lat is None:
                        lat = prev["lat"]
                    province = province or prev["province"]
                    city = city or prev["city"]
                    district = district or prev["district"]
                    adcode = adcode or prev["adcode"]
                else:
                    detail = geocode_detail(address)
                    if detail:
                        if lng is None:
                            lng = detail["lng"]
                        if lat is None:
                            lat = detail["lat"]
                        province = province or detail["province"]
                        city = city or detail["city"]
                        district = district or detail["district"]
                        adcode = adcode or detail["adcode"]

            # 更新房屋信息
            cursor.execute("""
                UPDATE houses
                SET address = ?, name = ?, lng = ?, lat = ?,
                    province = ?, city = ?, district = ?, adcode = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
            """, (address, name, lng, lat, province, city, district, adcode, house_id))

            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": "房屋更新成功",
                    "lng": lng,
                    "lat": lat,
                    "province": province,
                    "city": city,
                    "district": district,
                    "adcode": adcode
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_delete_house(self, request):
        """删除房屋

        DELETE /api/houses/{house_id}
        """
        try:
            house_id = request.match_info.get("house_id")
            if not house_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            conn = self._get_db_connection()
            cursor = conn.cursor()

            # 检查房屋是否存在
            cursor.execute("SELECT id FROM houses WHERE id = ?", (house_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋不存在"}),
                    content_type="application/json",
                    status=404
                )

            # 删除房屋，并**显式**级联删除它下面的房间和楼层。
            #
            # [2026-10-05] 原来这里只执行 DELETE FROM houses，注释却写着「级联删除」。
            # floors 表的建表语句里有 ON DELETE CASCADE，但 SQLite 的外键级联
            # **必须先 PRAGMA foreign_keys = ON 才生效**，而这个连接没开，
            # 于是删除房屋后楼层全部变成孤儿（house_id 指向已不存在的房屋）。
            # 后果很隐蔽：房屋列表里看不到那些楼层了，主人以为「楼层又丢了」，
            # 但数据其实还在库里。
            # 所以这里显式按 rooms → floors → houses 的顺序删，不依赖 PRAGMA。
            floor_rows = cursor.execute(
                "SELECT id FROM floors WHERE house_id = ?", (house_id,)
            ).fetchall()
            floor_ids = [r[0] for r in floor_rows]
            room_count = 0
            if floor_ids:
                marks = ",".join("?" * len(floor_ids))
                cur = cursor.execute(
                    f"SELECT COUNT(*) FROM rooms WHERE floor_id IN ({marks})", floor_ids
                )
                room_count = cur.fetchone()[0]
                cursor.execute(f"DELETE FROM rooms WHERE floor_id IN ({marks})", floor_ids)
            cursor.execute("DELETE FROM floors WHERE house_id = ?", (house_id,))
            cursor.execute("DELETE FROM houses WHERE id = ?", (house_id,))

            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": f"房屋删除成功（同时清理了 {len(floor_ids)} 个楼层、{room_count} 个房间）"
                }, indent=2, ensure_ascii=False),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_get_floors(self, request):
        """获取房屋的所有楼层

        GET /api/houses/{house_id}/floors
        """
        try:
            house_id = request.match_info.get("house_id")
            if not house_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT f.*,
                       (SELECT COUNT(*) FROM rooms r WHERE r.floor_id = f.id) as room_count
                FROM floors f
                WHERE f.house_id = ?
                ORDER BY f.floor_number
            """, (house_id,))

            rows = cursor.fetchall()
            floors = []
            for row in rows:
                floors.append({
                    "id": row["id"],
                    "house_id": row["house_id"],
                    "floor_number": row["floor_number"],
                    "floor_name": row["floor_name"],
                    "room_count": row["room_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                })

            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "floors": floors
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_create_floor(self, request):
        """为房屋添加楼层

        POST /api/houses/{house_id}/floors
        {
            "floor_number": 1,        // 排序用序号，允许负数（地下室=-1 排最前）
            "floor_name": "一楼"      // 显示名，必填
        }

        [2026-10-05] 允许 floor_number 为负数、且 floor_name 必填。
        原因：以前 floor_number 既是序号又当「第几楼」显示，主人建「地下室」时
        被迫给它一个正整数序号，界面上就渲染成「4楼 - 地下室」——
        地下室怎么可能在一楼？现在的约定是：
            floor_name  = 唯一显示名，界面只显示它
            floor_number = 纯排序用，地面以下填负数（地下室 -1、车库 -2）
        """
        try:
            house_id = request.match_info.get("house_id")
            if not house_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            data = await request.json()
            floor_number = data.get("floor_number")
            floor_name = data.get("floor_name", "").strip()

            if floor_number is None or str(floor_number).strip() == "":
                return web.Response(
                    text=json.dumps({"success": False, "message": "排序序号不能为空"}),
                    content_type="application/json",
                    status=400
                )
            try:
                floor_number = int(floor_number)
            except (TypeError, ValueError):
                return web.Response(
                    text=json.dumps({"success": False,
                                     "message": f"排序序号「{floor_number}」不是数字"}),
                    content_type="application/json",
                    status=400
                )

            if not floor_name:
                return web.Response(
                    text=json.dumps(
                        {"success": False,
                         "message": "楼层名称不能为空（它是界面上唯一显示的东西）"}),
                    content_type="application/json",
                    status=400
                )

            conn = self._get_db_connection()
            cursor = conn.cursor()

            # 检查房屋是否存在
            cursor.execute("SELECT id FROM houses WHERE id = ?", (house_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "房屋不存在"}),
                    content_type="application/json",
                    status=404
                )

            # 检查排序序号是否重复
            cursor.execute("""
                SELECT id, floor_name FROM floors
                WHERE house_id = ? AND floor_number = ?
            """, (house_id, floor_number))

            dup = cursor.fetchone()
            if dup:
                conn.close()
                return web.Response(
                    text=json.dumps({
                        "success": False,
                        "message": f"排序序号 {floor_number} 已被「{dup['floor_name']}」占用，"
                                   f"换一个序号"
                    }),
                    content_type="application/json",
                    status=400
                )

            # 添加楼层
            cursor.execute("""
                INSERT INTO floors (house_id, floor_number, floor_name)
                VALUES (?, ?, ?)
            """, (house_id, floor_number, floor_name))

            floor_id = cursor.lastrowid
            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": "楼层添加成功",
                    "floor_id": floor_id
                }, indent=2, ensure_ascii=False),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_delete_floor(self, request):
        """删除楼层
        
        DELETE /api/floors/{floor_id}
        """
        try:
            floor_id = request.match_info.get("floor_id")
            if not floor_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层ID不能为空"}),
                    content_type="application/json",
                    status=400
                )
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # 检查楼层是否存在
            cursor.execute("SELECT id FROM floors WHERE id = ?", (floor_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层不存在"}),
                    content_type="application/json",
                    status=404
                )
            
            # 删除楼层，并显式级联删除它下面的房间
            # （和房屋删除一样，不依赖 SQLite 的 ON DELETE CASCADE —— 那个需要
            #  PRAGMA foreign_keys=ON 才生效，这里没有开，所以必须显式删）
            cur = cursor.execute(
                "SELECT COUNT(*) FROM rooms WHERE floor_id = ?", (floor_id,)
            )
            room_count = cur.fetchone()[0]
            cursor.execute("DELETE FROM rooms WHERE floor_id = ?", (floor_id,))
            cursor.execute("DELETE FROM floors WHERE id = ?", (floor_id,))

            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": f"楼层删除成功（同时清理了 {room_count} 个房间）"
                }, indent=2, ensure_ascii=False),
                content_type="application/json"
            )
            
        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_get_rooms(self, request):
        """获取楼层的所有房间

        GET /api/floors/{floor_id}/rooms
        """
        try:
            floor_id = request.match_info.get("floor_id")
            if not floor_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT r.*
                FROM rooms r
                WHERE r.floor_id = ?
                ORDER BY r.room_name
            """, (floor_id,))

            rows = cursor.fetchall()
            rooms = []
            for row in rows:
                rooms.append({
                    "id": row["id"],
                    "floor_id": row["floor_id"],
                    "room_name": row["room_name"],
                    "room_type": row["room_type"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                })

            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "rooms": rooms
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_create_room(self, request):
        """为楼层添加房间

        POST /api/floors/{floor_id}/rooms
        {
            "room_name": "客厅",
            "room_type": "living_room"
        }
        """
        try:
            floor_id = request.match_info.get("floor_id")
            if not floor_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            data = await request.json()
            room_name = data.get("room_name", "").strip()
            room_type = data.get("room_type", "general")

            if not room_name:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间名称不能为空"}),
                    content_type="application/json",
                    status=400
                )

            conn = self._get_db_connection()
            cursor = conn.cursor()

            # 检查楼层是否存在
            cursor.execute("SELECT id FROM floors WHERE id = ?", (floor_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层不存在"}),
                    content_type="application/json",
                    status=404
                )

            # 检查房间名称是否重复(同一楼层内)
            cursor.execute("""
                SELECT id FROM rooms
                WHERE floor_id = ? AND room_name = ?
            """, (floor_id, room_name))

            if cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "该房间名称已存在"}),
                    content_type="application/json",
                    status=400
                )

            # 添加房间
            cursor.execute("""
                INSERT INTO rooms (floor_id, room_name, room_type)
                VALUES (?, ?, ?)
            """, (floor_id, room_name, room_type))

            room_id = cursor.lastrowid
            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": "房间添加成功",
                    "room_id": room_id
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_get_floor(self, request):
        """获取单个楼层信息
        
        GET /api/floors/{floor_id}
        """
        try:
            floor_id = request.match_info.get("floor_id")
            if not floor_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层ID不能为空"}),
                    content_type="application/json",
                    status=400
                )
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT id, house_id, floor_number, floor_name FROM floors WHERE id = ?", (floor_id,))
            floor = cursor.fetchone()
            conn.close()
            
            if not floor:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层不存在"}),
                    content_type="application/json",
                    status=404
                )
            
            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "floor": {
                        "id": floor[0],
                        "house_id": floor[1],
                        "floor_number": floor[2],
                        "floor_name": floor[3]
                    }
                }, indent=2),
                content_type="application/json"
            )
            
        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_update_floor(self, request):
        """更新楼层（名称 + 排序序号）

        PUT /api/floors/{floor_id}
        {
            "floor_name": "一楼",
            "floor_number": -1        // 可选；不传则保持原值
        }

        [2026-10-05] 以前这个接口只能改 floor_name，floor_number 改不了，
        所以「地下室」被卡在一个正整数序号上动不了。现在两个都能改，
        并加了序号重复校验（同一个房屋内序号必须唯一）。
        """
        try:
            floor_id = request.match_info.get("floor_id")
            if not floor_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层ID不能为空"}),
                    content_type="application/json",
                    status=400
                )

            data = await request.json()
            conn = self._get_db_connection()
            cursor = conn.cursor()
            conn.row_factory = sqlite3.Row

            cursor.execute("SELECT * FROM floors WHERE id = ?", (floor_id,))
            floor = cursor.fetchone()
            if not floor:
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "楼层不存在"}),
                    content_type="application/json",
                    status=404
                )

            # 只更新传了的字段，没传的保持原样
            sets = []
            args = []

            if "floor_name" in data:
                floor_name = (data.get("floor_name") or "").strip()
                if not floor_name:
                    conn.close()
                    return web.Response(
                        text=json.dumps(
                            {"success": False,
                             "message": "楼层名称不能为空（它是界面上唯一显示的东西）"}),
                        content_type="application/json",
                        status=400
                    )
                sets.append("floor_name = ?")
                args.append(floor_name)

            if "floor_number" in data and str(data.get("floor_number")).strip() != "":
                try:
                    floor_number = int(data.get("floor_number"))
                except (TypeError, ValueError):
                    conn.close()
                    return web.Response(
                        text=json.dumps(
                            {"success": False,
                             "message": f"排序序号「{data.get('floor_number')}」不是数字"}),
                        content_type="application/json",
                        status=400
                    )
                if floor_number != floor["floor_number"]:
                    cursor.execute(
                        "SELECT id, floor_name FROM floors "
                        "WHERE house_id = ? AND floor_number = ? AND id != ?",
                        (floor["house_id"], floor_number, floor_id)
                    )
                    dup = cursor.fetchone()
                    if dup:
                        conn.close()
                        return web.Response(
                            text=json.dumps({
                                "success": False,
                                "message": f"排序序号 {floor_number} 已被"
                                           f"「{dup['floor_name']}」占用，换一个序号"
                            }, ensure_ascii=False),
                            content_type="application/json",
                            status=400
                        )
                sets.append("floor_number = ?")
                args.append(floor_number)

            if not sets:
                conn.close()
                return web.Response(
                    text=json.dumps({"success": True, "message": "没有需要更新的内容"},
                                    ensure_ascii=False),
                    content_type="application/json"
                )

            args.append(floor_id)
            cursor.execute(
                f"UPDATE floors SET {', '.join(sets)} WHERE id = ?", args
            )
            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({"success": True, "message": "楼层更新成功"},
                                indent=2, ensure_ascii=False),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)},
                                ensure_ascii=False),
                content_type="application/json",
                status=500
            )

        return response

    async def handle_get_room(self, request):
        """获取单个房间信息
        
        GET /api/rooms/{room_id}
        """
        try:
            room_id = request.match_info.get("room_id")
            if not room_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间ID不能为空"}),
                    content_type="application/json",
                    status=400
                )
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT id, floor_id, room_name FROM rooms WHERE id = ?", (room_id,))
            room = cursor.fetchone()
            conn.close()
            
            if not room:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间不存在"}),
                    content_type="application/json",
                    status=404
                )
            
            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "room": {
                        "id": room[0],
                        "floor_id": room[1],
                        "room_name": room[2]
                    }
                }, indent=2),
                content_type="application/json"
            )
            
        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_update_room(self, request):
        """更新房间
        
        PUT /api/rooms/{room_id}
        """
        try:
            room_id = request.match_info.get("room_id")
            if not room_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间ID不能为空"}),
                    content_type="application/json",
                    status=400
                )
            
            data = await request.json()
            room_name = data.get("room_name", "").strip()
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT id FROM rooms WHERE id = ?", (room_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间不存在"}),
                    content_type="application/json",
                    status=404
                )
            
            cursor.execute("UPDATE rooms SET room_name = ? WHERE id = ?", (room_name, room_id))
            conn.commit()
            conn.close()
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "房间更新成功"}, indent=2),
                content_type="application/json"
            )
            
        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_delete_room(self, request):
        """删除房间
        
        DELETE /api/rooms/{room_id}
        """
        try:
            room_id = request.match_info.get("room_id")
            if not room_id:
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间ID不能为空"}),
                    content_type="application/json",
                    status=400
                )
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT id FROM rooms WHERE id = ?", (room_id,))
            if not cursor.fetchone():
                conn.close()
                return web.Response(
                    text=json.dumps({"success": False, "message": "房间不存在"}),
                    content_type="application/json",
                    status=404
                )
            
            cursor.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
            conn.commit()
            conn.close()
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "房间删除成功"}, indent=2),
                content_type="application/json"
            )
            
        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        
        return response

    async def handle_get_all_rooms(self, request):
        """获取所有房间(用于下拉选择)

        GET /api/rooms/all
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # [2026-10-05] 补 floor_id / house_id：
            #   管理台改成「按楼层分组一次列出全部房间」后，前端要靠 floor_id 分组、
            #   靠 house_id 过滤（一个主机可能 someday 有多套房子）。
            #   原来只返回 floor_name，前端只能靠名字猜，遇到重名就分组错。
            cursor.execute("""
                SELECT r.id, r.room_name, r.room_type, r.floor_id,
                       f.floor_number, f.floor_name,
                       h.id as house_id, h.address, h.name as house_name
                FROM rooms r
                JOIN floors f ON r.floor_id = f.id
                JOIN houses h ON f.house_id = h.id
                ORDER BY h.name, f.floor_number, r.room_name
            """)

            rows = cursor.fetchall()
            rooms = []
            for row in rows:
                rooms.append({
                    "id": row["id"],
                    "room_name": row["room_name"],
                    "room_type": row["room_type"],
                    "floor_id": row["floor_id"],
                    "house_id": row["house_id"],
                    "floor_info": {
                        "floor_number": row["floor_number"],
                        "floor_name": row["floor_name"]
                    },
                    "house_info": {
                        "address": row["address"],
                        "name": row["house_name"]
                    },
                    "display_name": f"{row['house_name']} - {row['floor_name']} - {row['room_name']}"
                })

            conn.close()

            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "rooms": rooms
                }, indent=2),
                content_type="application/json"
            )

        except Exception as e:
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )

        return response