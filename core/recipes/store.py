# -*- coding: utf-8 -*-
"""菜谱本地库：sqlite 存菜谱元数据，图片存本地目录

存储布局：
  data/recipes.db                        —— 菜谱表
  data/recipe_images/<id>/cover.jpg      —— 成品图
  data/recipe_images/<id>/step_1.jpg     —— 第 1 步配图（以此类推）

图片对外访问路径：/recipe-images/<id>/cover.jpg（见 http_server.py 路由）
"""
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

_SERVER_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _SERVER_ROOT / "data" / "recipes.db"
IMAGE_DIR = _SERVER_ROOT / "data" / "recipe_images"

# 菜名归一化时要剥掉的营销装饰词（豆果菜名常见「#话题#」「【xx】」前缀）
_TOPIC_RE = re.compile(r"#[^#]{0,40}#")
_BRACKET_RE = re.compile(r"[【\[][^】\]]{0,20}[】\]]")
_NON_WORD_RE = re.compile(r"[\s\-_·・,，。.、:：!！?？]+")


def normalize_name(name: str) -> str:
    """把菜名归一化成用于匹配的 key：去营销词、去标点空白、转小写。

    例："#年饭要飘香，就用外婆乡#年夜饭红烧肉肥而不腻有技巧"
        → 归一化后仍保留「红烧肉」，靠模糊匹配命中。
    """
    if not name:
        return ""
    s = _TOPIC_RE.sub("", name)
    s = _BRACKET_RE.sub("", s)
    s = _NON_WORD_RE.sub("", s)
    return s.strip().lower()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等）。首次使用或每次写入前调用都安全。"""
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                name_key     TEXT NOT NULL DEFAULT '',
                ingredients  TEXT NOT NULL DEFAULT '[]',
                steps        TEXT NOT NULL DEFAULT '[]',
                cover_img    TEXT NOT NULL DEFAULT '',
                source       TEXT NOT NULL DEFAULT '',
                source_url   TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT '',
                updated_at   TEXT NOT NULL DEFAULT ''
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_recipes_name_key ON recipes(name_key)")


def _row_to_dict(row: sqlite3.Row) -> Dict:
    """把数据库行转成对外的菜谱 dict（解析 JSON 字段）。"""
    def _load(s, default):
        try:
            return json.loads(s) if s else default
        except Exception:
            return default

    return {
        "id": row["id"],
        "name": row["name"],
        "name_key": row["name_key"],
        "ingredients": _load(row["ingredients"], []),
        "steps": _load(row["steps"], []),
        "cover_img": row["cover_img"] or "",
        "source": row["source"] or "",
        "source_url": row["source_url"] or "",
        "created_at": row["created_at"] or "",
    }


def _score_match(query_key: str, row_name_key: str) -> int:
    """给「查询词」与「库中菜名」的匹配打分，越大越像。

    策略（从严到宽）：
      100 完全相等
       80 库名以查询词开头 / 结尾
       60 查询词是库名的子串（如「红烧肉」⊂「年夜饭红烧肉」）
       40 库名是查询词的子串
        0 不匹配
    """
    if not query_key or not row_name_key:
        return 0
    if query_key == row_name_key:
        return 100
    if row_name_key.startswith(query_key) or row_name_key.endswith(query_key):
        return 80
    if query_key in row_name_key:
        return 60
    if row_name_key in query_key:
        return 40
    return 0


def find_recipe(name: str) -> Optional[Dict]:
    """按菜名查本地库。先精确（归一化相等），再模糊取最像的一条。"""
    if not name:
        return None
    init_db()
    key = normalize_name(name)
    with _conn() as c:
        rows = c.execute("SELECT * FROM recipes").fetchall()

    best, best_score = None, 0
    for row in rows:
        s = _score_match(key, row["name_key"])
        if s > best_score:
            best, best_score = row, s
    if best is None or best_score <= 0:
        return None
    return _row_to_dict(best)


def get_recipe_by_id(rid: int) -> Optional[Dict]:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
    return _row_to_dict(row) if row else None


def list_recipes() -> List[Dict]:
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT * FROM recipes ORDER BY id DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def save_recipe(recipe: Dict) -> int:
    """保存/更新一条菜谱，返回记录 id。

    - 若库中已有同名（归一化相等）记录，则更新它（覆盖食材/步骤/图片）；
    - 否则插入新记录。
    图片由调用方先落盘到 data/recipe_images/<id>/，这里只存相对路径。
    """
    init_db()
    name = (recipe.get("name") or "").strip()
    if not name:
        raise ValueError("菜谱缺少 name")
    key = normalize_name(name)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ingredients = json.dumps(recipe.get("ingredients", []), ensure_ascii=False)
    steps = json.dumps(recipe.get("steps", []), ensure_ascii=False)

    with _conn() as c:
        row = c.execute(
            "SELECT id FROM recipes WHERE name_key = ?", (key,)
        ).fetchone()
        if row:
            rid = row["id"]
            c.execute(
                """UPDATE recipes SET name=?, ingredients=?, steps=?, cover_img=?,
                   source=?, source_url=?, updated_at=? WHERE id=?""",
                (
                    name,
                    ingredients,
                    steps,
                    recipe.get("cover_img", ""),
                    recipe.get("source", ""),
                    recipe.get("source_url", ""),
                    now,
                    rid,
                ),
            )
        else:
            cur = c.execute(
                """INSERT INTO recipes
                   (name, name_key, ingredients, steps, cover_img, source,
                    source_url, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    key,
                    ingredients,
                    steps,
                    recipe.get("cover_img", ""),
                    recipe.get("source", ""),
                    recipe.get("source_url", ""),
                    now,
                    now,
                ),
            )
            rid = cur.lastrowid
    return rid


def recipe_image_dir(rid: int) -> Path:
    """返回某条菜谱的图片目录（不自动创建）。"""
    return IMAGE_DIR / str(rid)


def save_image(rid: int, filename: str, data: bytes) -> str:
    """把图片字节落盘，返回可对外访问的相对路径（如 "<id>/cover.jpg"）。"""
    d = recipe_image_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(data)
    return f"{rid}/{filename}"
