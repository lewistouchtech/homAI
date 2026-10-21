# -*- coding: utf-8 -*-
"""地点簿（places 表）统一入口（2026-10-05）

为什么需要这个模块（见 docs/plans/2026-10-05_家庭成员档案与常用地点设计方案.md）：
    以前家里只有一个地址（houses 表），且要靠 LLM 自己拼明文地址去调高德，
    拼不对就变成「你学校在哪儿」的反问 —— 这正是要消除的割裂感。
    现在地点存进 places 表，工具只收语义标签（"公司"/"学校"/"奶奶家"），
    服务端查表拿地址，LLM 全程不接触地址字符串。

设计要点：
    1. 「家」唯一：is_primary=1 全局只有一条，由 houses 表首条迁移而来，不可删。
    2. aliases 是语义匹配抓手：主人自己填「公司,单位,上班,工作」，
       新增地点、改叫法都不用改代码 —— 商用时每家叫法都不同，必须靠数据解决。
    3. 匹配不到时返回候选列表，让 LLM 二选一追问（「你是要去爸爸的公司还是小花朵的学校？」），
       比开放追问自然得多。
    4. 所有函数异常一律返回 None / 空列表，绝不抛 —— 地点簿挂掉不能连带对话主链。

数据库路径与 family_api / member_profile 同构：server/homebrain-server/data/devices.db
"""
import os
import sqlite3
from typing import Optional

# 数据库路径：core/utils/ → core/ → homebrain-server/
_DB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)

PLACES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS places (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,                       -- '爸爸的公司' / '小花朵的学校' / '奶奶家'
    type          TEXT NOT NULL DEFAULT 'other',        -- home/work/school/kindergarten/class/
                                                        -- gym/grocery/hospital/relative/other
    member_id     INTEGER,                              -- 归属成员；NULL = 全家共用
    address       TEXT DEFAULT '',
    lng           REAL,
    lat           REAL,
    province      TEXT DEFAULT '',
    city          TEXT DEFAULT '',
    district      TEXT DEFAULT '',
    adcode        TEXT DEFAULT '',
    commute_mode  TEXT DEFAULT '',                      -- drive/transit/walk/ride
    depart_time   TEXT DEFAULT '',                      -- 'HH:MM'，第二阶段主动提醒用
    aliases       TEXT DEFAULT '',                      -- '公司,单位,上班,工作'（逗号分隔）
    is_primary    INTEGER DEFAULT 0,                    -- 1 = 这是「家」，全局唯一，不可删
    notes         TEXT DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_places_member ON places(member_id);
CREATE INDEX IF NOT EXISTS idx_places_type ON places(type);
"""

# family_members 的两个新列（2026-10-05）
NEW_MEMBER_COLUMNS = [
    ("life_stage", "TEXT DEFAULT ''"),
    ("relation_detail", "TEXT DEFAULT ''"),
]

# 地点类型受控枚举：值 → (中文标签, 匹配用的同义词)
PLACE_TYPES = {
    "home": ("家", ("家", "我家", "家里", "回家", "到家", "住的地方", "home")),
    "work": ("公司/单位", ("公司", "单位", "上班", "工作", "办公室", "公司地址", "workplace", "work", "job", "上班的地方")),
    "school": ("学校", ("学校", "上学", "小学", "初中", "高中", "课堂", "school")),
    "kindergarten": ("幼儿园", ("幼儿园", "幼稚园", "托儿所", "kindergarten")),
    "class": ("兴趣班", ("兴趣班", "培训班", "课外班", "辅导班", "上课", "class")),
    "gym": ("健身房", ("健身房", "运动馆", "游泳", "瑜伽", "gym")),
    "grocery": ("超市/商场", ("超市", "商场", "购物中心", "菜场", "菜市场", "grocery")),
    "hospital": ("医院", ("医院", "卫生院", "诊所", "看病", "hospital")),
    "relative": ("亲友家", ("亲友家", "奶奶家", "姥姥家", "外婆家", "爷爷家", "姥爷家", "姑姑家", "舅舅家", "亲戚家", "老家")),
    "other": ("其他", ("其他", "别的", "other")),
}

# 中文标签 → 类型值，供管理台下拉与 LLM 传中文时用
TYPE_LABEL_TO_VALUE = {label: value for value, (label, _) in PLACE_TYPES.items()}

# 出行方式受控枚举：值 → (中文标签, 同义词)
COMMUTE_MODES = {
    "drive": ("驾车", ("驾车", "开车", "开车去", "自驾", "drive", "car")),
    "transit": ("公交/地铁", ("公交", "地铁", "坐车", "公共交通", "transit", "bus")),
    "walk": ("步行", ("步行", "走路", "走着", "走过去", "walk")),
    "ride": ("骑行", ("骑行", "骑车", "自行车", "骑过去", "ride")),
}
MODE_LABEL_TO_VALUE = {label: value for value, (label, _) in COMMUTE_MODES.items()}

# 人生阶段受控枚举（family_members.life_stage）
LIFE_STAGES = {
    "婴幼儿": "0-3 岁",
    "学龄前": "3-6 岁",
    "小学": "6-12 岁",
    "中学": "12-18 岁",
    "大学": "18-22 岁",
    "工作": "22-60 岁",
    "退休": "60 岁以上",
    "未成年": "其他未成年",
}


def get_db_path() -> str:
    """devices.db 路径"""
    return os.path.join(_DB_DIR, "devices.db")


def _connect(db_path: str = None):
    conn = sqlite3.connect(db_path or get_db_path(), timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def ensure_schema(db_path: str = None) -> bool:
    """确保 places 表和 family_members 新列存在（幂等）

    服务启动时（device_manager._init_database）和管理台 API 都会调一次，
    这样即使没跑迁移脚本也不会因为表不存在而报错。
    """
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return False
    conn = None
    try:
        conn = _connect(path)
        conn.executescript(PLACES_TABLE_DDL)
        if _table_exists(conn, "family_members"):
            cols = {r[1] for r in conn.execute("PRAGMA table_info(family_members)").fetchall()}
            for col, ddl in NEW_MEMBER_COLUMNS:
                if col not in cols:
                    conn.execute(f"ALTER TABLE family_members ADD COLUMN {col} {ddl}")
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 基础读写

def split_aliases(aliases) -> list:
    """把 '公司,单位、 上班 ' 拆成 ['公司','单位','上班']（兼容中英文逗号与顿号）"""
    if not aliases:
        return []
    if isinstance(aliases, (list, tuple)):
        items = list(aliases)
    else:
        text = str(aliases)
        for sep in ("，", "、", ";", "；", "|"):
            text = text.replace(sep, ",")
        items = text.split(",")
    return [i.strip() for i in items if i and i.strip()]


def _row_to_place(row) -> dict:
    """sqlite3.Row → dict，并把别名拆成列表"""
    if row is None:
        return None
    d = dict(row)
    d["alias_list"] = split_aliases(d.get("aliases"))
    d["type_label"] = PLACE_TYPES.get(d.get("type"), (d.get("type") or "其他", ()))[0]
    d["mode_label"] = COMMUTE_MODES.get(d.get("commute_mode"), ("", ()))[0]
    d["is_primary"] = bool(d.get("is_primary"))
    d.setdefault("member_name", "")
    return d


def ensure_region(place: dict, db_path: str = None) -> dict:
    """补齐地点的省/市/区县，已有坐标不动。

    为什么需要：「家」是从 houses 表迁移过来的，坐标早就存好了，
    所以永远不会走「坐标缺失 → 现算并回写」那条路，city 会一直是空的。
    而高德 direction/transit/integrate 强制要求 city/cityd，缺了就查不了公交，
    表现成「坐公交怎么走」永远降级。这里按地址重新解析一次行政区补上，
    **只写 province/city/district/adcode，不覆盖已有坐标**（避免手标的坐标被冲掉）。

    返回该 place（原地也更新了字段）。
    """
    if not place:
        return place
    if (place.get("city") or "").strip() and (place.get("province") or "").strip():
        return place
    address = (place.get("address") or "").strip()
    if not address:
        return place

    try:
        from core.utils.geo import geocode_detail
        geo = geocode_detail(address)
    except Exception:
        return place
    if not geo:
        return place

    province = geo.get("province") or ""
    city = geo.get("city") or ""
    district = geo.get("district") or ""
    adcode = geo.get("adcode") or ""
    place["province"] = place.get("province") or province
    place["city"] = place.get("city") or city
    place["district"] = place.get("district") or district
    place["adcode"] = place.get("adcode") or adcode

    try:
        conn = _connect(db_path or get_db_path())
        try:
            conn.execute(
                "UPDATE places SET province=?, city=?, district=?, adcode=? WHERE id=?",
                (place["province"], place["city"], place["district"], place["adcode"],
                 place.get("id")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return place


def _member_names(conn) -> dict:
    """member_id → 伊娃对这位成员的称呼（nickname），没有则用姓名。

    这里刻意用 nickname 而不是 name：注入给 LLM 的称呼要和 member_profile
    里注入的保持一致，否则会出现「档案里叫主人、总览里叫李威」的割裂。
    """
    try:
        if not _table_exists(conn, "family_members"):
            return {}
        rows = conn.execute(
            "SELECT id, name, nickname FROM family_members"
        ).fetchall()
        return {
            r["id"]: (r["nickname"] or "").strip() or (r["name"] or "").strip()
            for r in rows
        }
    except Exception:
        return {}


def list_places(db_path: str = None) -> list:
    """所有地点，「家」排最前，其余按 id。每条带上归属成员的称呼（member_name）。"""
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return []
    conn = None
    try:
        conn = _connect(path)
        if not _table_exists(conn, "places"):
            return []
        rows = conn.execute(
            "SELECT * FROM places ORDER BY is_primary DESC, id"
        ).fetchall()
        places = [_row_to_place(r) for r in rows]
        names = _member_names(conn)
        for p in places:
            p["member_name"] = names.get(p.get("member_id"), "")
        return places
    except Exception:
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_place(place_id: int, db_path: str = None) -> Optional[dict]:
    """按 id 取地点"""
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return None
    conn = None
    try:
        conn = _connect(path)
        if not _table_exists(conn, "places"):
            return None
        return _row_to_place(
            conn.execute("SELECT * FROM places WHERE id=?", (place_id,)).fetchone()
        )
    except Exception:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_home_place(db_path: str = None) -> Optional[dict]:
    """取「家」（is_primary=1）"""
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return None
    conn = None
    try:
        conn = _connect(path)
        if not _table_exists(conn, "places"):
            return None
        return _row_to_place(
            conn.execute("SELECT * FROM places WHERE is_primary=1 LIMIT 1").fetchone()
        )
    except Exception:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _has_home(db_path: str = None) -> bool:
    return get_home_place(db_path) is not None


def create_place(data: dict, db_path: str = None) -> dict:
    """新增地点。

    返回 {"success": bool, "place": dict, "message": str}
    「家」只能有一条：已有 is_primary=1 时不允许再建 type='home'。
    """
    path = db_path or get_db_path()
    name = (data.get("name") or "").strip()
    if not name:
        return {"success": False, "message": "地点名称不能为空"}

    ptype = (data.get("type") or "other").strip()
    if ptype not in PLACE_TYPES:
        ptype = TYPE_LABEL_TO_VALUE.get(ptype, "other")

    if ptype == "home" and _has_home(path):
        return {"success": False, "message": "「家」已经设置过了，一台设备只有一个家"}

    mode = (data.get("commute_mode") or "").strip()
    if mode and mode not in COMMUTE_MODES:
        mode = MODE_LABEL_TO_VALUE.get(mode, "")

    member_id = data.get("member_id")
    try:
        member_id = int(member_id) if member_id not in (None, "", "0") else None
    except (TypeError, ValueError):
        member_id = None

    is_primary = 1 if ptype == "home" else 0
    aliases = data.get("aliases")
    aliases = ",".join(split_aliases(aliases))

    conn = None
    try:
        conn = _connect(path)
        conn.executescript(PLACES_TABLE_DDL)
        names = _member_names(conn)
        cur = conn.execute(
            "INSERT INTO places (name, type, member_id, address, lng, lat, province, "
            "city, district, adcode, commute_mode, depart_time, aliases, is_primary, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name, ptype, member_id,
                (data.get("address") or "").strip(),
                data.get("lng"), data.get("lat"),
                (data.get("province") or "").strip(),
                (data.get("city") or "").strip(),
                (data.get("district") or "").strip(),
                (data.get("adcode") or "").strip(),
                mode,
                (data.get("depart_time") or "").strip(),
                aliases, is_primary,
                (data.get("notes") or "").strip(),
            ),
        )
        conn.commit()
        place_id = cur.lastrowid
        created = get_place(place_id, path)
        if created:
            created["member_name"] = names.get(created.get("member_id"), "")
        return {"success": True, "place": created, "message": f"地点「{name}」已添加"}
    except Exception as e:
        return {"success": False, "message": f"添加失败：{e}"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def update_place(place_id: int, data: dict, db_path: str = None) -> dict:
    """更新地点：**只更新 data 里显式传入的字段**，没传的保持原样。

    为什么必须做成部分更新：
        踩过的坑 —— 早期版本把所有字段都重写一遍（没传的用空串/NULL 兜底），
        结果调用方只传 {"address": "..."} 就把 member_id 冲成了 NULL、
        别名也清空了，地点变成「全家共用」还匹配不上「我上班」。
        地点簿是主人手工维护的数据，静默清空是不可接受的。

    另外：地址变了就必须清掉旧坐标，否则会拿着「上一个地址」的坐标去算路程
    （实测：把学校从「温溪山庄15号」改成「建国路88号」，坐标还留在家附近，
     结果算出 0 米）。清掉后由调用方或 _ensure_coords 现算。

    「家」不可通过本接口改地址，也不可改类型 —— 它由「设置家庭地址」入口维护。
    """
    path = db_path or get_db_path()
    old = get_place(place_id, path)
    if not old:
        return {"success": False, "message": "地点不存在"}

    sets = []
    args = []

    def put(col, value):
        sets.append(f"{col}=?")
        args.append(value)

    if "name" in data and str(data["name"]).strip():
        put("name", str(data["name"]).strip())

    if "type" in data and data["type"]:
        ptype = str(data["type"]).strip()
        if ptype not in PLACE_TYPES:
            ptype = TYPE_LABEL_TO_VALUE.get(ptype, "")
        if ptype and ptype != old.get("type"):
            if old["is_primary"] or ptype == "home":
                return {"success": False, "message": "「家」的类型是锁定的，不能改成别的类型"}
            put("type", ptype)

    if "member_id" in data:
        try:
            mid = data["member_id"]
            mid = int(mid) if mid not in (None, "", "0") else None
        except (TypeError, ValueError):
            return {"success": False, "message": "归属成员填的不对"}
        put("member_id", mid)

    if "address" in data:
        new_addr = str(data["address"] or "").strip()
        if old["is_primary"]:
            # 「家」的地址只能从「设置家庭地址」改，避免出现两个数据源
            if new_addr and new_addr != (old.get("address") or "").strip():
                return {
                    "success": False,
                    "message": "「家」的地址请到「房屋状态 → 设置家庭地址」里修改",
                }
        elif new_addr != (old.get("address") or "").strip():
            put("address", new_addr)
            # 地址变了 → 旧坐标作废，必须一起清掉，否则会拿旧坐标算错路程
            put("lng", None)
            put("lat", None)
            for col in ("province", "city", "district", "adcode"):
                put(col, "")

    # 坐标：只在显式传了非空值时才写（传 None 表示「地址变了，清掉」已在上面处理）
    for col in ("lng", "lat", "province", "city", "district", "adcode"):
        if col in data and col not in ("province", "city", "district", "adcode"):
            val = data[col]
            try:
                put(col, float(val) if val not in (None, "") else None)
            except (TypeError, ValueError):
                return {"success": False, "message": f"{col} 填的不是数字"}
        elif col in data and col in ("province", "city", "district", "adcode"):
            put(col, str(data[col] or "").strip())

    if "commute_mode" in data:
        mode = str(data["commute_mode"] or "").strip()
        if mode and mode not in COMMUTE_MODES:
            mode = MODE_LABEL_TO_VALUE.get(mode, "")
        put("commute_mode", mode)

    for col in ("depart_time", "aliases", "notes"):
        if col in data:
            val = data[col]
            if col == "aliases":
                val = ",".join(split_aliases(val))
            else:
                val = str(val or "").strip()
            put(col, val)

    if not sets:
        return {"success": True, "place": old, "message": "没有需要更新的内容"}

    conn = None
    try:
        conn = _connect(path)
        names = _member_names(conn)
        args.append(place_id)
        conn.execute(
            f"UPDATE places SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            args,
        )
        conn.commit()
        updated = get_place(place_id, path)
        if updated:
            updated["member_name"] = names.get(updated.get("member_id"), "")
        return {"success": True, "place": updated, "message": "地点已更新"}
    except Exception as e:
        return {"success": False, "message": f"更新失败：{e}"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def delete_place(place_id: int, db_path: str = None) -> dict:
    """删除地点。「家」不可删（否则地图和附近搜索会失去中心点）。"""
    path = db_path or get_db_path()
    old = get_place(place_id, path)
    if not old:
        return {"success": False, "message": "地点不存在"}
    if old["is_primary"]:
        return {"success": False, "message": "「家」不能删除。如需改地址请到「设置家庭地址」里改"}
    conn = None
    try:
        conn = _connect(path)
        conn.execute("DELETE FROM places WHERE id=?", (place_id,))
        conn.commit()
        return {"success": True, "message": f"地点「{old['name']}」已删除"}
    except Exception as e:
        return {"success": False, "message": f"删除失败：{e}"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 语义匹配

def _match_tier(place: dict, keyword: str) -> int:
    """判断地点与语义标签的匹配档位，0 表示不匹配。**档位比分数重要**。

    分三档从严到宽，任何地方只要有一档命中就够了，不跨档比较：
        3  精确命中  别名完全相等 / 名称完全相等      「上班」→ aliases 里的「上班」
        2  类型命中  类型的同义词命中                「公司」→ type=work
        1  宽松包含  别名或名称互相包含，且别名 ≥2 字

    [2026-10-05] 必须分档，不能把所有条件混在一起算一个分数。
    踩过的坑：宽松包含用的是「别名 in 关键词」，于是「家」是「奶奶家」的子串，
    导致用户说「去奶奶家」时匹配上了「我家」。只要有更精确的档位命中，
    宽松档就完全不参与，跨档的坑自然消失。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return 0

    name = (place.get("name") or "").lower()
    aliases = [a.lower() for a in place.get("alias_list", [])]

    if kw in aliases or kw == name:
        return 3

    _, syns = PLACE_TYPES.get(place.get("type"), ("", ()))
    if kw in [s.lower() for s in syns]:
        return 2

    # 宽松档：别名至少 2 个字才允许参与子串匹配，避免「家」吃掉「奶奶家」
    for a in aliases:
        if len(a) >= 2 and (kw in a or a in kw):
            return 1
    if len(name) >= 2 and (kw in name or name in kw):
        return 1
    return 0


def _member_bonus(place: dict, member_id) -> int:
    """归属成员就是当前说话人时加分（同档内优先用「我的公司」而不是别人的）"""
    return 10 if (member_id and place.get("member_id") == member_id) else 0


def resolve_place(keyword: str, member_id=None, db_path: str = None) -> dict:
    """按语义标签找地点。

    返回：
        {"place": dict|None, "candidates": [dict,...]}
    candidates 非空表示没匹配上但有相近的，LLM 应该二选一追问而不是开放追问。
    """
    places = list_places(db_path)
    kw = (keyword or "").strip().lower()
    if not kw:
        return {"place": None, "candidates": []}

    # 从严到宽逐档找，命中即停 —— 保证「奶奶家」不会被「家」的子串规则截胡
    for tier in (3, 2, 1):
        scored = [
            (_member_bonus(p, member_id), p) for p in places if _match_tier(p, kw) == tier
        ]
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        best = scored[0][0]
        # 同分说明有歧义（比如夫妻两人各有一个都叫「公司」的地点），交给调用方追问
        return {"place": scored[0][1], "candidates": [p for s, p in scored if s == best]}

    # 三档都没命中：按类型给候选，好让 LLM 追问而不是开放式反问
    type_hint = None
    for value, (_, syns) in PLACE_TYPES.items():
        if kw in [s.lower() for s in syns]:
            type_hint = value
            break
    if type_hint:
        # 有明确的类型意图（如「奶奶家」→relative）但一个都没配：
        # 候选留空，让调用方明确说「这个地点还没设置」，而不是拿别的地点凑数
        return {"place": None, "candidates": [], "type_hint": type_hint,
                "type_label": PLACE_TYPES[type_hint][0]}
    return {"place": None, "candidates": [p for p in places if not p["is_primary"]][:4],
            "type_hint": None, "type_label": ""}


def get_member_places(member_id: int, db_path: str = None) -> list:
    """某成员的常去地点（「家」永远包含在内）。

    供 <member_profile> 注入：让 LLM 知道「我上班了」对应哪个地点。

    ⚠️ member_id 为空的地点**不进任何人的个人档案**，只在 <family_places> 总览里。
    原因：管理台里「归属成员」是可以留空的，留空的语义应该是「还没指定归属」，
    而不是「全家共用」。把「妈妈的工作单位」这种他人私人的地点塞进丈夫的档案里，
    既不正确也不合适 —— 个人档案只放「家」+「他自己的地点」。
    """
    places = list_places(db_path)
    home = [p for p in places if p["is_primary"]]
    if not member_id:
        return home
    own = [p for p in places if p.get("member_id") == member_id and not p["is_primary"]]
    return home + own


# ---------------------------------------------------------------- 给 LLM 注入

def get_family_places_overview(db_path: str = None, limit: int = 15) -> str:
    """家庭地点总览（紧凑单行格式），注入 <family_places>。

    为什么必须「全员可见、且不依赖声纹是否识别成功」：
        1. 小花朵问「妈妈上班要多久」时妈妈没说话，妈妈的个人档案根本没被注入；
        2. 声纹万一没识别出说话人，个人档案是空的，但家庭地点总览应该照样在，
           LLM 至少能回答「小花朵的学校在 xxx，你知道怎么去吗」。

    控制 token：紧凑格式 + 最多 limit 条（超出按 updated_at 取最近的）。
    """
    places = list_places(db_path)
    if not places:
        return ""

    lines = []
    for p in places[:limit]:
        owner = p.get("member_name") or ""
        name = p.get("name") or ""
        head = f"{owner}·{name}" if owner else name
        if p["is_primary"]:
            head = f"{head}（全员）"
        lines.append(f"{head}：{p.get('address') or '（地址未填）'}")

    if len(places) > limit:
        lines.append(f"……另有 {len(places) - limit} 个地点未列出")

    return "\n".join(lines)


def get_places_for_prompt(member_id: int = None, db_path: str = None) -> str:
    """当前说话人的常去地点段，注入 <member_profile>。

    只列名称 + 别名 + 出行方式 + 有没有坐标，不列完整地址（完整地址在
    <family_places> 里，避免同一份地址在 prompt 里出现两遍）。
    """
    places = get_member_places(member_id, db_path)
    if not places:
        return ""

    lines = []
    for p in places:
        bits = [f"名称「{p['name']}」"]
        if p["is_primary"]:
            bits.append("类型：家")
        else:
            label = p.get("type_label") or p.get("type")
            bits.append(f"类型：{label}")
        if p.get("alias_list"):
            bits.append("别名：" + "/".join(p["alias_list"][:6]))
        if p.get("mode_label"):
            bits.append(f"常用方式：{p['mode_label']}")
        if p.get("depart_time"):
            bits.append(f"出门时间：{p['depart_time']}")
        bits.append("坐标已存" if p.get("lng") is not None else "⚠️坐标缺失")
        lines.append("- " + "；".join(bits))
    return "\n".join(lines)
