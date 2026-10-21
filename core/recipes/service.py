# -*- coding: utf-8 -*-
"""菜谱服务层：对上层（工具插件）提供「给菜名 → 拿到完整菜谱」的单一入口

流程（对应主人的设计）：
  1. 先查本地库 → 命中直接用（快、稳、离线可用）
  2. 未命中 → 联网抓取（豆果）→ 抓到即存本地库
  3. 返回统一结构的菜谱 dict，并附带前端/语音两种呈现格式
"""
from typing import Dict, List, Optional

from config.logger import setup_logging
from core.recipes import douguo, store, xiachufang

TAG = __name__
logger = setup_logging()

# 图片对外访问前缀（http_server.py 里注册的静态路由）
_IMG_PREFIX = "/recipe-images/"

# 联网菜谱来源，按顺序尝试（2026-10-08：豆果为主，下厨房移动版为第二来源）
# ⚠️ 存「模块 + 函数名」，调用时才取函数——若在这里直接绑定函数对象，
#    unittest.mock 打桩不生效（会绕过桩、真联网）。
_SOURCES = (
    (douguo, "豆果", "fetch_recipe"),
    (xiachufang, "下厨房", "fetch_recipe"),
)


async def get_recipe(dish_name: str, allow_fetch: bool = True) -> Optional[Dict]:
    """按菜名取菜谱。先本地库，未命中再联网抓取并沉淀。

    返回 {"recipe": {...}, "from": "local"|"fetched"}；彻底拿不到返回 None。
    """
    if not dish_name or not dish_name.strip():
        return None
    name = dish_name.strip()

    # 1) 本地库
    local = store.find_recipe(name)
    if local:
        logger.bind(tag=TAG).info(f"菜谱命中本地库: {name} -> {local['name']}")
        return {"recipe": local, "from": "local"}

    if not allow_fetch:
        return None

    # 2) 联网抓取：多来源依次尝试（豆果 → 下厨房移动版）
    fetched = None
    for mod, label, fname in _SOURCES:
        fetcher = getattr(mod, fname)
        try:
            logger.bind(tag=TAG).info(f"本地库无「{name}」，尝试来源：{label}…")
            fetched = await fetcher(name)
        except (douguo.RecipeSourceBusy, xiachufang.XiachufangBusy) as e:
            logger.bind(tag=TAG).warning(f"来源 {label} 限流，换下一个：{e}")
            continue
        except Exception as e:
            logger.bind(tag=TAG).warning(f"来源 {label} 异常，换下一个：{type(e).__name__}: {e}")
            continue
        if fetched:
            logger.bind(tag=TAG).info(f"来源 {label} 抓到菜谱：{fetched.get('name')}")
            break
    if not fetched:
        return None

    # 3) 先入库拿到 id（图片路径依赖 id），再落盘图片，最后回填图片路径
    rid = store.save_recipe(
        {
            "name": fetched["name"],
            "ingredients": fetched["ingredients"],
            "steps": [{"text": s.get("text", ""), "img": ""} for s in fetched["steps"]],
            "source": fetched.get("source", ""),
            "source_url": fetched.get("source_url", ""),
        }
    )

    cover_rel = ""
    if fetched.get("cover_bytes"):
        cover_rel = store.save_image(rid, "cover.jpg", fetched["cover_bytes"])

    steps_saved: List[Dict] = []
    for s in fetched["steps"]:
        item = {"text": s.get("text", ""), "img": ""}
        if s.get("img_bytes") and s.get("img_name"):
            item["img"] = store.save_image(rid, s["img_name"], s["img_bytes"])
        steps_saved.append(item)

    store.save_recipe(
        {
            "name": fetched["name"],
            "ingredients": fetched["ingredients"],
            "steps": steps_saved,
            "cover_img": cover_rel,
            "source": fetched.get("source", ""),
            "source_url": fetched.get("source_url", ""),
        }
    )

    recipe = store.get_recipe_by_id(rid)
    logger.bind(tag=TAG).info(
        f"菜谱已抓取入库: id={rid} {fetched['name']} 食材{len(fetched['ingredients'])} 步骤{len(steps_saved)}"
    )
    return {"recipe": recipe, "from": "fetched"}


def _img_url(rel: str) -> str:
    """把库里的相对图片路径转成前端可访问的 URL。"""
    return _IMG_PREFIX + rel if rel else ""


def to_display(recipe: Dict) -> Dict:
    """转成前端展示结构（图片转成 URL，供中央舞台渲染）。"""
    return {
        "id": recipe.get("id"),
        "name": recipe.get("name", ""),
        "ingredients": recipe.get("ingredients", []),
        "cover": _img_url(recipe.get("cover_img", "")),
        "source": recipe.get("source", ""),
        "source_url": recipe.get("source_url", ""),
        "steps": [
            {"text": s.get("text", ""), "img": _img_url(s.get("img", ""))}
            for s in recipe.get("steps", [])
        ],
    }


def to_full_text(recipe: Dict) -> str:
    """整份菜谱的纯文本（供无屏设备语音播报，或 LLM 参考）。"""
    lines = [f"【{recipe.get('name', '')}】"]
    ings = recipe.get("ingredients", [])
    if ings:
        lines.append("食材：" + "、".join(ings))
    steps = recipe.get("steps", [])
    if steps:
        lines.append(f"做法（共{len(steps)}步）：")
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. {s.get('text', '')}")
    return "\n".join(lines)


def to_summary(recipe: Dict) -> str:
    """一句话摘要（有屏时用：屏幕展示完整菜谱，语音只说结论）。"""
    n_steps = len(recipe.get("steps", []))
    n_ings = len(recipe.get("ingredients", []))
    return f"已把「{recipe.get('name', '')}」的菜谱显示在屏幕上了，{n_ings} 种食材、共 {n_steps} 步。"


# ------------------------------------------------------------------
# 做菜会话：进程内轻量状态，按设备记「当前这道菜 + 做到第几步」。
# 只存内存、不落库、不碰任何稳定模块；服务重启即清空。
# 定位由后端说了算（屏幕照着显示），这样翻页一定停在整步上，不会翻到半截。
# ------------------------------------------------------------------
_SESSIONS: Dict[str, Dict] = {}


def set_session(device_id: str, recipe: Dict) -> Dict:
    """记录某设备当前正在做的菜谱，步骤定位到第一步，返回展示结构。"""
    display = to_display(recipe)
    _SESSIONS[device_id] = {"display": display, "idx": 0}
    return display


def get_session(device_id: str) -> Optional[Dict]:
    return _SESSIONS.get(device_id)


def clear_session(device_id: str) -> None:
    _SESSIONS.pop(device_id, None)


def page_session(device_id: str, direction: str, step: Optional[int] = None) -> Optional[Dict]:
    """翻页 / 跳步。

    - step 给定（1 起）→ 直接跳到第 step 步（越界夹紧）
    - 否则按 direction：'prev' 上一步，其它=下一步
    返回 {display, idx, total, step_text, at_end, at_start, jumped}；
    该设备没有进行中的菜谱时返回 None。翻到头/翻到头就停住（不循环）。
    """
    s = _SESSIONS.get(device_id)
    if not s:
        return None
    steps = s["display"].get("steps", [])
    total = len(steps)
    idx = s["idx"]
    jumped = step is not None
    if jumped:
        try:
            idx = max(0, min(int(step) - 1, total - 1)) if total else 0
        except (TypeError, ValueError):
            idx = s["idx"]
            jumped = False
    elif direction == "prev":
        idx = max(idx - 1, 0)
    else:  # 默认下一页
        idx = min(idx + 1, total - 1) if total else 0
    s["idx"] = idx
    return {
        "display": s["display"],
        "idx": idx,
        "total": total,
        "step_text": steps[idx]["text"] if 0 <= idx < total else "",
        "at_end": idx >= total - 1,
        "at_start": idx <= 0,
        "jumped": jumped,
    }
