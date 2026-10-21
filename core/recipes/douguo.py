# -*- coding: utf-8 -*-
"""豆果美食菜谱抓取器

实测（2026-10-07）确认可行的链路：
  1. 先 GET 首页拿 cookie（不拿的话搜索页会返回空）
  2. 搜索：GET https://www.douguo.com/caipu/<菜名> → 页面里
     <a class="cook-img" href="/cookbook/<id>.html" title="菜名"> 给出候选
  3. 详情：GET https://www.douguo.com/cookbook/<id>.html
     - 页面内嵌一段 JSON（含 name / image / recipeIngredient）
     - 步骤在多个 <div class="stepcont clearfix"> 块里，
       每块含 data-origin 大图 + <div class="stepinfo">文字</div>
  4. 图片：成品图在 https://cp1.douguo.com + 相对路径；
     步骤图本身就是 cp1.douguo.com 的完整 URL。

⚠️ 两个坑（实测踩过）：
  - 频率限制：短时间内连查多个菜会返回「服务器压力大，请稍后重试」，
    必须退避重试（_fetch_text 里做了指数退避）。
  - 菜名带营销词（如「#年饭要飘香，就用外婆乡#年夜饭红烧肉…」），
    需清洗后再入库/展示（store.normalize_name / _clean_name）。
"""
import asyncio
import json
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

from config.logger import setup_logging
from core.recipes import store

TAG = __name__
logger = setup_logging()


class RecipeSourceBusy(Exception):
    """菜谱源临时限流（不是"没这道菜"，而是"暂时访问受限"）。"""
    pass


_HOME = "https://www.douguo.com/"
_CDN = "https://cp1.douguo.com"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_RATE_LIMIT_MARK = "服务器压力大"
# 实测（2026-10-07）：豆果限流会从「服务器压力大」升级成 403，必须一并当限流处理
_RATE_LIMIT_CODES = {403, 429, 503}

# 最多重试次数（含首次），退避 3s / 6s / 9s
_MAX_TRIES = 4
_BACKOFF_BASE = 3.0
# 豆果请求最小间隔：连续猛请求会触发限流，节流一下更稳
_MIN_INTERVAL = 1.2
_last_request_ts = 0.0


async def _throttle():
    """保证两次豆果请求之间至少间隔 _MIN_INTERVAL 秒。"""
    global _last_request_ts
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_ts = time.monotonic()


def _clean_name(name: str) -> str:
    """清洗豆果菜名里的营销装饰词，只留真正的菜名部分。"""
    if not name:
        return ""
    s = re.sub(r"#[^#]{0,40}#", "", name)      # 去掉 #话题# 前缀
    s = re.sub(r"[【\[][^】\]]{0,20}[】\]]", "", s)  # 去掉【xx】
    return s.strip(" -_·・,，。.")


async def _fetch_text(client: httpx.AsyncClient, url: str, referer: str = _HOME) -> str:
    """GET 一个页面，遇到豆果限流（"服务器压力大" 或 403/429/503）时退避重试。

    返回 HTML 文本；其它失败返回 ""；限流重试耗尽抛 RecipeSourceBusy。
    """
    last_err = ""
    rate_limited = False
    for i in range(_MAX_TRIES):
        await _throttle()
        try:
            r = await client.get(url, headers={**_HEADERS, "Referer": referer})
            text = r.text
            if r.status_code == 200 and _RATE_LIMIT_MARK not in text:
                return text
            if r.status_code in _RATE_LIMIT_CODES or _RATE_LIMIT_MARK in text:
                rate_limited = True
                last_err = f"被限流(HTTP {r.status_code})"
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:  # 网络异常也重试
            last_err = f"{type(e).__name__}: {e}"
        if i < _MAX_TRIES - 1:
            await asyncio.sleep(_BACKOFF_BASE * (i + 1))
    logger.bind(tag=TAG).warning(f"抓取失败 {url} | {last_err}")
    if rate_limited:
        raise RecipeSourceBusy(last_err)
    return ""


def _parse_candidates(html: str) -> List[Dict]:
    """从搜索页解析候选菜谱：返回 [{"id": "3323930", "title": "..."}]"""
    out, seen = [], set()
    for m in re.finditer(
        r'href="/cookbook/(\d+)\.html"[^>]*?title="([^"]*)"', html
    ):
        rid, title = m.group(1), m.group(2)
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "title": title})
    return out


def _pick_best(dish_name: str, candidates: List[Dict]) -> Optional[Dict]:
    """在候选里挑与用户所点菜名最像的一条（按归一化名匹配度）。

    2026-10-08：**完全对不上时返回 None**——宁可不抓，也别把不相干的菜端上来。
    （以前会退回第一条，导致生僻/不存在的菜名返回一道毫不相关的菜；
    现在这种情况交给大模型兜底 show_recipe 更稳。）
    """
    if not candidates:
        return None
    key = store.normalize_name(dish_name)
    best, best_score = None, 0
    for c in candidates:
        s = store._score_match(key, store.normalize_name(c["title"]))
        if s > best_score:
            best, best_score = c, s
    return best if best_score > 0 else None


def _parse_detail(html: str) -> Dict:
    """解析菜谱详情页，返回 {name, ingredients, cover_url, steps:[{text,img_url}]}"""
    meta = {}
    m = re.search(
        r'(\{"@context".*?"recipeIngredient"\s*:\s*\[.*?\]\})', html, re.S
    )
    if m:
        try:
            meta = json.loads(m.group(1))
        except Exception as e:
            logger.bind(tag=TAG).warning(f"详情页 JSON 解析失败: {e}")

    name = _clean_name(meta.get("name", ""))
    ingredients = [str(x).strip() for x in meta.get("recipeIngredient", []) if str(x).strip()]

    cover_url = ""
    imgs = meta.get("image") or []
    if imgs:
        p = imgs[0]
        cover_url = p if p.startswith("http") else _CDN + p

    steps: List[Dict] = []
    for seg in html.split('<div class="stepcont')[1:]:
        im = re.search(r'data-origin="([^"]+)"', seg)
        tx = re.search(r'class="stepinfo">(.*?)</div>', seg, re.S)
        body = re.sub(r"<[^>]+>", " ", tx.group(1)) if tx else ""
        body = re.sub(r"\s+", " ", body).strip()
        body = re.sub(r"^步骤\s*\d+\s*", "", body).strip()
        if not body and not im:
            continue
        steps.append({"text": body, "img_url": im.group(1) if im else ""})

    return {
        "name": name,
        "ingredients": ingredients,
        "cover_url": cover_url,
        "steps": steps,
    }


async def _download(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """下载一张图片，失败返回 None。"""
    if not url:
        return None
    try:
        r = await client.get(url, headers={**_HEADERS, "Referer": _HOME})
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:
        logger.bind(tag=TAG).warning(f"图片下载失败 {url}: {e}")
    return None


async def fetch_recipe(dish_name: str) -> Optional[Dict]:
    """联网抓一份菜谱（含图片字节），失败返回 None。

    返回结构：
      {
        "name": "红烧肉",
        "ingredients": ["五花肉: 700克", ...],
        "steps": [{"text": "...", "img_bytes": b"...", "img_name": "step_1.jpg"}, ...],
        "cover_bytes": b"...",
        "source": "douguo",
        "source_url": "https://www.douguo.com/cookbook/3323930.html",
      }
    """
    if not dish_name or not dish_name.strip():
        return None
    kw = dish_name.strip()

    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS
    ) as client:
        # 1) 拿 cookie
        try:
            await _throttle()
            await client.get(_HOME, headers=_HEADERS)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"访问豆果首页失败: {e}")

        # 2) 搜索候选
        search_html = await _fetch_text(
            client, f"https://www.douguo.com/caipu/{quote(kw)}"
        )
        if not search_html:
            return None
        candidates = _parse_candidates(search_html)
        best = _pick_best(kw, candidates)
        if not best:
            logger.bind(tag=TAG).info(f"豆果搜不到菜谱: {kw}")
            return None
        logger.bind(tag=TAG).info(
            f"豆果搜索「{kw}」候选 {len(candidates)} 个，选中 {best['id']} {best['title']}"
        )

        # 3) 详情
        detail_url = f"https://www.douguo.com/cookbook/{best['id']}.html"
        detail_html = await _fetch_text(client, detail_url)
        if not detail_html:
            return None
        detail = _parse_detail(detail_html)
        if not detail["name"]:
            detail["name"] = _clean_name(best["title"]) or kw
        if not detail["steps"]:
            logger.bind(tag=TAG).info(f"菜谱 {kw} 未解析到步骤，放弃")
            return None

        # 4) 下载图片
        cover_bytes = await _download(client, detail["cover_url"])
        for i, s in enumerate(detail["steps"], 1):
            if s.get("img_url"):
                b = await _download(client, s["img_url"])
                if b:
                    s["img_bytes"] = b
                    s["img_name"] = f"step_{i}.jpg"

    # 整理步骤（只保留文字 + 图片字节/文件名，去掉原始 URL）
    steps_out = []
    for s in detail["steps"]:
        item = {"text": s.get("text", "")}
        if s.get("img_bytes"):
            item["img_bytes"] = s["img_bytes"]
            item["img_name"] = s["img_name"]
        steps_out.append(item)

    return {
        "name": detail["name"],
        "ingredients": detail["ingredients"],
        "steps": steps_out,
        "cover_bytes": cover_bytes,
        "source": "douguo",
        "source_url": detail_url,
    }
