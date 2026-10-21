# -*- coding: utf-8 -*-
"""下厨房（移动版）菜谱抓取器 —— 豆果之外的**第二来源**

⚠️ 关键实测（2026-10-08）：
  - 桌面版 www.xiachufang.com 被反爬挡住（连首页都 404）
  - 但**移动版 m.xiachufang.com 可用**（搜索 + 详情都能抓）
  - 其余站点（美食杰/好豆/香哈/心食谱/天天美食/懒饭/贝太厨房）实测全部不可用

链路：
  1. 搜索 GET https://m.xiachufang.com/search/?keyword=<菜名>
     <a href="/recipe/<id>/" class="recipe-96-horizon"> …
        <img data-src="…" alt="菜名" title="菜名">
  2. 详情 GET https://m.xiachufang.com/recipe/<id>/
     - 菜名：<h1>…</h1>
     - 食材：class="ing-name" / class="ing-amount"
     - 成品图：class="recipe-cover" 里的 <img src="…">
     - 步骤：<div class="recipe-steps"> 下的 step 块（文字 + 步骤图，图在 i2.chuimg.com）
"""
import asyncio
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

from config.logger import setup_logging
from core.recipes import store

TAG = __name__
logger = setup_logging()

_BASE = "https://m.xiachufang.com"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# 用移动端 UA，桌面 UA 会被挡
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_RATE_LIMIT_CODES = {403, 429, 503}
_MAX_TRIES = 3
_BACKOFF_BASE = 3.0
_MIN_INTERVAL = 1.2
_last_ts = 0.0


class XiachufangBusy(Exception):
    """下厨房临时限流。"""
    pass


async def _throttle():
    global _last_ts
    wait = _MIN_INTERVAL - (time.monotonic() - _last_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_ts = time.monotonic()


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    last_err = ""
    rate_limited = False
    for i in range(_MAX_TRIES):
        await _throttle()
        try:
            r = await client.get(url, headers=_HEADERS)
            if r.status_code == 200:
                return r.text
            if r.status_code in _RATE_LIMIT_CODES:
                rate_limited = True
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if i < _MAX_TRIES - 1:
            await asyncio.sleep(_BACKOFF_BASE * (i + 1))
    logger.bind(tag=TAG).warning(f"下厨房抓取失败 {url} | {last_err}")
    if rate_limited:
        raise XiachufangBusy(last_err)
    return ""


def _parse_candidates(html: str) -> List[Dict]:
    """搜索页 → [{"id": "100124682", "title": "不焯水不放油的家常红烧肉"}]"""
    out, seen = [], set()
    for m in re.finditer(
        r'href="/recipe/(\d+)/"[^>]*class="recipe-96-horizon".*?<img[^>]*?(?:alt|title)="([^"]*)"',
        html, re.S,
    ):
        rid, title = m.group(1), m.group(2)
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "title": title})
    return out


def _parse_detail(html: str) -> Dict:
    """详情页 → {name, ingredients, cover_url, steps:[{text, img_url}]}"""
    name = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        name = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()

    ingredients = []
    for a, b in re.findall(
        r'class="ing-name[^"]*"[^>]*>(.*?)<.*?class="ing-amount[^"]*"[^>]*>(.*?)<',
        html, re.S,
    ):
        n = re.sub(r"<[^>]+>|\s+", " ", a).strip()
        amt = re.sub(r"<[^>]+>|\s+", " ", b).strip()
        if n:
            ingredients.append(f"{n}: {amt}" if amt else n)

    cover_url = ""
    cm = re.search(r'class="recipe-cover[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"', html, re.S)
    if cm:
        cover_url = cm.group(1)

    steps: List[Dict] = []
    for seg in html.split('class="step step"')[1:]:
        seg = seg.split(">", 1)[-1]          # 去掉被切开的标签属性残渣
        im = re.search(r'(https://i2\.chuimg\.com/[^"\s)]+)', seg)
        txt = re.sub(r"<[^>]*>", " ", seg)
        txt = txt.split("<")[0]              # 去掉被截断的半截标签
        txt = re.sub(r"\s+", " ", txt).strip()
        txt = re.sub(r"^步骤\s*\d+\s*", "", txt).strip()
        txt = txt[:200].strip()              # 步骤文字后面常跟页面尾巴，合理截断
        if not txt and not im:
            continue
        steps.append({"text": txt, "img_url": im.group(1) if im else ""})

    return {"name": name, "ingredients": ingredients, "cover_url": cover_url, "steps": steps}


async def _download(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        await _throttle()
        r = await client.get(url, headers=_HEADERS)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:
        logger.bind(tag=TAG).warning(f"下厨房图片下载失败: {e}")
    return None


async def fetch_recipe(dish_name: str) -> Optional[Dict]:
    """抓一份下厨房菜谱（含图片字节）。结构同 douguo.fetch_recipe。"""
    if not dish_name or not dish_name.strip():
        return None
    kw = dish_name.strip()

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
        search_html = await _fetch_text(client, f"{_BASE}/search/?keyword={quote(kw)}")
        if not search_html:
            return None
        cands = _parse_candidates(search_html)
        if not cands:
            return None

        # 挑最像的一条
        key = store.normalize_name(kw)
        best, best_score = None, 0
        for c in cands:
            s = store._score_match(key, store.normalize_name(c["title"]))
            if s > best_score:
                best, best_score = c, s
        if best is None or best_score <= 0:
            logger.bind(tag=TAG).info(f"下厨房搜到 {len(cands)} 条但都对不上: {kw}")
            return None

        detail_html = await _fetch_text(client, f"{_BASE}/recipe/{best['id']}/")
        if not detail_html:
            return None
        detail = _parse_detail(detail_html)
        if not detail["name"]:
            detail["name"] = best["title"] or kw
        if not detail["steps"]:
            logger.bind(tag=TAG).info(f"下厨房菜谱 {kw} 未解析到步骤，放弃")
            return None

        cover_bytes = await _download(client, detail["cover_url"])
        for i, s in enumerate(detail["steps"], 1):
            if s.get("img_url"):
                b = await _download(client, s["img_url"])
                if b:
                    s["img_bytes"] = b
                    s["img_name"] = f"step_{i}.jpg"

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
        "source": "xiachufang",
        "source_url": f"{_BASE}/recipe/{best['id']}/",
    }
