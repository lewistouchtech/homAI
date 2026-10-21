# -*- coding: utf-8 -*-
"""菜品成品图搜索 —— 给"大模型兜底菜谱"补一张成品图

主人要求（2026-10-08）：大模型兜底生成的菜谱不能没图；每步的图可以不要，
但**这道菜的成品图应该有**。

实测（2026-10-08）：
  - ✅ 必应图片（cn.bing.com/images）可用：能拿到真实菜品照片，且能下载
  - ❌ 百度图片 / 搜狗图片 都被挡（forbid / 空结果）

做法：搜必应图片 → 取前若干张候选 → 逐张尝试下载 → 第一张合格（是图片且够大）就用。
"""
import asyncio
import html as _html
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_TIMEOUT = httpx.Timeout(15.0, connect=6.0)
_MIN_INTERVAL = 1.0
_last_ts = 0.0
_MAX_TRY = 6          # 最多试下载几张候选
_MIN_BYTES = 8000     # 太小的多半是占位图/图标


async def _throttle():
    global _last_ts
    wait = _MIN_INTERVAL - (time.monotonic() - _last_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_ts = time.monotonic()


async def fetch_dish_image(dish_name: str) -> Optional[bytes]:
    """按菜名搜一张成品图，返回图片字节；拿不到返回 None。"""
    if not dish_name or not dish_name.strip():
        return None
    kw = dish_name.strip()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as c:
            await _throttle()
            r = await c.get(
                f"https://cn.bing.com/images/search?q={quote(kw)}", headers=_HEADERS
            )
            if r.status_code != 200:
                logger.bind(tag=TAG).warning(f"必应图片搜索失败: HTTP {r.status_code}")
                return None
            urls = [_html.unescape(u) for u in re.findall(r"&quot;murl&quot;:&quot;(.*?)&quot;", r.text)]
            if not urls:
                logger.bind(tag=TAG).info(f"必应图片没搜到结果: {kw}")
                return None
            for u in urls[:_MAX_TRY]:
                try:
                    await _throttle()
                    ir = await c.get(u, headers=_HEADERS)
                    ctype = ir.headers.get("content-type", "")
                    if ir.status_code == 200 and ctype.startswith("image") and len(ir.content) >= _MIN_BYTES:
                        logger.bind(tag=TAG).info(f"成品图获取成功: {kw} {len(ir.content)//1024}KB")
                        return ir.content
                except Exception:
                    continue
    except Exception as e:
        logger.bind(tag=TAG).warning(f"成品图搜索异常: {type(e).__name__}: {e}")
    logger.bind(tag=TAG).info(f"成品图没拿到: {kw}")
    return None
