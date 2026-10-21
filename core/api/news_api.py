# -*- coding: utf-8 -*-
"""
今日新闻 API
获取当天实时新闻，支持多家新闻源
商用级实现：多源聚合、智能降级、日期感知缓存
"""

import os
import json
import time
import re
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta

# ==================== 缓存管理 ====================

_news_cache = {
    "data": [],
    "cached_at": 0,
    "cached_date": "",
    "source": ""
}

# 缓存有效期：6小时（确保每日至少更新4次）
CACHE_TTL = 6 * 3600

# ==================== 新闻源配置 ====================

# 新浪新闻 Roll API - 最稳定的中文新闻源
SINA_NEWS_API = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=30&page=1"

# 腾讯新闻 API
TENCENT_NEWS_API = "https://i.news.qq.com/trpc.qqnews_web.kv_srv.kv_srv_http_proxy/list?sub_srv_id=24hours&srv_id=pc&offset=0&limit=30&strategy=1&ext={%22pool%22:[%22top%22],%22geoid%22:%22100000%22}"

# 澎湃新闻 API
THEPAPER_API = "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar"

# 内置新闻模板（当所有 API 都不可用时使用）
FALLBACK_NEWS = [
    {"title": "智能家居行业标准发布，互联互通成为发展重点", "source": "科技日报", "time": "2小时前", "category": "科技", "url": ""},
    {"title": "人工智能助手成为家庭健康管理新宠，专家提醒需理性使用", "source": "经济参考报", "time": "3小时前", "category": "科技", "url": ""},
    {"title": "全国多地推进智慧城市建设，数字化治理成效显著", "source": "人民日报", "time": "4小时前", "category": "社会", "url": ""},
    {"title": "新能源汽车销量持续增长，智能化成竞争新焦点", "source": "澎湃新闻", "time": "5小时前", "category": "财经", "url": ""},
    {"title": "5G网络覆盖持续扩大，物联网应用加速落地", "source": "通信世界", "time": "6小时前", "category": "科技", "url": ""},
    {"title": "社区养老服务升级，智慧养老成新趋势", "source": "社会新闻", "time": "8小时前", "category": "社会", "url": ""},
    {"title": "中国芯技术突破，国产芯片产能持续提升", "source": "经济日报", "time": "10小时前", "category": "科技", "url": ""},
    {"title": "长三角一体化发展加速，科技创新成新引擎", "source": "光明日报", "time": "12小时前", "category": "社会", "url": ""},
    {"title": "夏季用电高峰来临，多地供电充足保障民生", "source": "电力新闻网", "time": "14小时前", "category": "民生", "url": ""},
    {"title": "教育部发布中小学课外培训新规，减轻学生负担", "source": "中国教育报", "time": "16小时前", "category": "教育", "url": ""},
]

# HTTP 请求头
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ==================== 新闻源获取函数 ====================

async def fetch_sina_news(session: aiohttp.ClientSession) -> list:
    """从新浪新闻 Roll API 获取实时新闻"""
    try:
        async with session.get(
            SINA_NEWS_API,
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return parse_sina_news(data)
    except Exception as e:
        print(f"[News API] 新浪新闻获取失败: {e}")
    return []


def parse_sina_news(data: dict) -> list:
    """解析新浪新闻数据"""
    news_list = []
    try:
        items = data.get("result", {}).get("data", [])
        if not items:
            return news_list

        for item in items:
            title = item.get("title", "")
            if not title:
                continue

            # 清理 HTML 标签
            title = re.sub(r'<[^>]+>', '', title).strip()

            source = item.get("media_name", "") or item.get("source", "新浪新闻")
            ctime = item.get("ctime", "")
            category = "综合"
            cat = item.get("category", {})
            if isinstance(cat, dict):
                category = cat.get("title", "综合")
            elif isinstance(cat, str):
                category = cat

            # 解析时间 - 支持 Unix 时间戳和格式化字符串
            time_display = "刚刚"
            if ctime:
                # 尝试作为 Unix 时间戳解析
                try:
                    timestamp = int(ctime)
                    if timestamp > 1000000000:  # 合理的时间戳范围
                        dt = datetime.fromtimestamp(timestamp)
                        time_display = format_relative_time(dt)
                    else:
                        time_display = format_relative_time_from_str(str(ctime))
                except (ValueError, TypeError):
                    time_display = format_relative_time_from_str(str(ctime))

            url = item.get("url", "") or item.get("link", "")

            news_list.append({
                "title": title,
                "source": source,
                "time": time_display,
                "category": category,
                "url": url
            })
    except Exception as e:
        print(f"[News API] 解析新浪新闻失败: {e}")

    return news_list


async def fetch_tencent_news(session: aiohttp.ClientSession) -> list:
    """从腾讯新闻 API 获取实时新闻"""
    try:
        async with session.get(
            TENCENT_NEWS_API,
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return parse_tencent_news(data)
    except Exception as e:
        print(f"[News API] 腾讯新闻获取失败: {e}")
    return []


def parse_tencent_news(data: dict) -> list:
    """解析腾讯新闻数据"""
    news_list = []
    try:
        # 腾讯新闻返回格式: {data: {list: [...]}}
        items = data.get("data", {}).get("list", [])
        if not items:
            items = data.get("data", [])
        if not isinstance(items, list):
            return news_list

        for item in items:
            title = item.get("title", "")
            if not title:
                continue

            source = item.get("source", "") or item.get("media_name", "腾讯新闻")
            timestamp = item.get("timestamp", 0) or item.get("mtime", 0)
            category = item.get("category", "综合")
            url = item.get("url", "") or item.get("link", "")

            # 格式化时间
            time_display = "刚刚"
            if timestamp and isinstance(timestamp, (int, float)) and timestamp > 0:
                try:
                    dt = datetime.fromtimestamp(timestamp)
                    time_display = format_relative_time(dt)
                except Exception:
                    pass

            news_list.append({
                "title": title,
                "source": source,
                "time": time_display,
                "category": category,
                "url": url
            })
    except Exception as e:
        print(f"[News API] 解析腾讯新闻失败: {e}")

    return news_list


async def fetch_thepaper_news(session: aiohttp.ClientSession) -> list:
    """从澎湃新闻获取实时新闻"""
    try:
        async with session.get(
            THEPAPER_API,
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return parse_thepaper_news(data)
    except Exception as e:
        print(f"[News API] 澎湃新闻获取失败: {e}")
    return []


def parse_thepaper_news(data: dict) -> list:
    """解析澎湃新闻数据"""
    news_list = []
    try:
        # 澎湃右侧热门新闻
        hot_news = data.get("data", {}).get("hotNews", [])
        if not hot_news:
            return news_list

        for item in hot_news:
            title = item.get("name", "") or item.get("title", "")
            if not title:
                continue

            source = "澎湃新闻"
            pub_time = item.get("pubTimeLong", 0) or item.get("publishTime", 0)
            url = item.get("link", "") or item.get("url", "")
            category = item.get("categoryId", "") or "综合"

            time_display = "刚刚"
            if pub_time:
                try:
                    # 澎湃使用毫秒时间戳
                    dt = datetime.fromtimestamp(pub_time / 1000)
                    time_display = format_relative_time(dt)
                except Exception:
                    pass

            news_list.append({
                "title": title,
                "source": source,
                "time": time_display,
                "category": category if isinstance(category, str) else "综合",
                "url": url
            })
    except Exception as e:
        print(f"[News API] 解析澎湃新闻失败: {e}")

    return news_list


# ==================== 时间格式化 ====================

def format_relative_time(dt: datetime) -> str:
    """格式化相对时间"""
    now = datetime.now()
    diff = now - dt

    if diff.days > 0:
        return f"{diff.days}天前"
    elif diff.seconds > 3600:
        hours = diff.seconds // 3600
        return f"{hours}小时前"
    elif diff.seconds > 60:
        minutes = diff.seconds // 60
        return f"{minutes}分钟前"
    else:
        return "刚刚"


def format_relative_time_from_str(time_str: str) -> str:
    """从时间字符串格式化相对时间"""
    if not time_str:
        return "刚刚"

    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"]:
            try:
                dt = datetime.strptime(time_str, fmt)
                return format_relative_time(dt)
            except ValueError:
                continue
    except Exception:
        pass

    return time_str


# ==================== 核心新闻获取 ====================

async def get_realtime_news() -> list:
    """获取实时新闻（多源聚合 + 智能降级）"""
    today = datetime.now().strftime("%Y-%m-%d")
    now = time.time()

    # 检查缓存是否有效（同一天且未过期）
    if (_news_cache["data"] and
        _news_cache["cached_date"] == today and
        now - _news_cache["cached_at"] < CACHE_TTL):
        return _news_cache["data"]

    all_news = []
    sources_used = []

    # 创建共享 session 提高性能
    async with aiohttp.ClientSession() as session:
        # 并行获取多个新闻源
        tasks = {
            "新浪新闻": fetch_sina_news(session),
            "腾讯新闻": fetch_tencent_news(session),
            "澎湃新闻": fetch_thepaper_news(session),
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for (source_name, _), result in zip(tasks.items(), results):
            if isinstance(result, list) and len(result) > 0:
                all_news.extend(result)
                sources_used.append(source_name)
                print(f"[News API] {source_name}: 获取 {len(result)} 条新闻")
            elif isinstance(result, Exception):
                print(f"[News API] {source_name} 异常: {result}")

    # 去重（按标题）
    seen_titles = set()
    unique_news = []
    for news in all_news:
        title = news.get("title", "").strip()
        if title and title not in seen_titles and len(title) > 4:
            seen_titles.add(title)
            unique_news.append(news)

    # 如果所有 API 都失败，使用内置新闻
    if not unique_news:
        print("[News API] 所有新闻源不可用，使用内置新闻")
        unique_news = generate_fallback_news(today)
        sources_used.append("内置新闻")

    # 最多返回 20 条
    unique_news = unique_news[:20]

    # 更新缓存
    _news_cache["data"] = unique_news
    _news_cache["cached_at"] = now
    _news_cache["cached_date"] = today
    _news_cache["source"] = ", ".join(sources_used)

    print(f"[News API] 缓存更新: {len(unique_news)} 条新闻, 来源: {_news_cache['source']}")

    return unique_news


def generate_fallback_news(today: str) -> list:
    """生成当日内置新闻（带日期感知）"""
    news = list(FALLBACK_NEWS)
    now_dt = datetime.now()

    for i, item in enumerate(news):
        hours_ago = (i + 1) * 2
        if hours_ago < 24:
            item["time"] = f"{hours_ago}小时前"
        else:
            item["time"] = "昨天"
        item["url"] = ""

    return news


# ==================== HTTP 处理函数 ====================

async def handle_get_news(request):
    """处理 GET /api/news 请求"""
    try:
        limit = int(request.query.get("limit", 10))
        category = request.query.get("category", "")

        news_list = await get_realtime_news()

        # 按分类过滤
        if category:
            news_list = [n for n in news_list if n.get("category", "") == category]

        # 限制数量
        news_list = news_list[:limit]

        return web.json_response({
            "success": True,
            "news": news_list,
            "total": len(news_list),
            "updated_at": datetime.fromtimestamp(_news_cache["cached_at"]).strftime("%Y-%m-%d %H:%M:%S") if _news_cache["cached_at"] else "",
            "cached_date": _news_cache["cached_date"],
            "source": _news_cache.get("source", "")
        })
    except Exception as e:
        print(f"[News API] 处理请求异常: {e}")
        return web.json_response({
            "success": False,
            "error": str(e),
            "news": FALLBACK_NEWS[:10]
        }, status=500)


async def handle_refresh_news(request):
    """强制刷新新闻（清除缓存）"""
    global _news_cache
    _news_cache = {"data": [], "cached_at": 0, "cached_date": "", "source": ""}

    news_list = await get_realtime_news()

    return web.json_response({
        "success": True,
        "news": news_list[:10],
        "total": len(news_list),
        "message": "新闻已刷新",
        "source": _news_cache.get("source", "")
    })


# ==================== 新闻正文抓取 ====================

async def fetch_article_content(url: str) -> dict:
    """从新闻 URL 抓取正文内容"""
    import re
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {"success": False, "error": f"HTTP {resp.status}"}
                
                html = await resp.text()
                
                # 移除 script 和 style
                html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                
                # 新浪新闻 - 提取 id="artibody" 的内容（处理嵌套 div）
                if 'sina' in url or 'sinaimg' in url:
                    artibody_start = html.find('id="artibody"')
                    if artibody_start != -1:
                        # 找到 artibody 标签的开始位置
                        tag_end = html.find('>', artibody_start)
                        if tag_end != -1:
                            # 从标签结束后开始计算嵌套深度
                            depth = 1
                            pos = tag_end + 1
                            while pos < len(html) and depth > 0:
                                if html[pos:pos+5] == '<div ' or html[pos:pos+4] == '<div>':
                                    depth += 1
                                elif html[pos:pos+6] == '</div>':
                                    depth -= 1
                                    if depth == 0:
                                        html = html[tag_end+1:pos]
                                        break
                                pos += 1
                    else:
                        # 尝试 article 标签
                        article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
                        if article_match:
                            html = article_match.group(1)
                
                # 澎湃新闻
                if 'thepaper' in url:
                    article_match = re.search(r'<div[^>]*class="[^"]*contentDetail[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                    if article_match:
                        html = article_match.group(1)
                
                # 提取纯文本
                text = re.sub(r'<[^>]+>', '\n', html)
                # 清理空白行
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                text = '\n'.join(lines)
                # 移除过短行（导航、广告等）
                lines = [line for line in text.split('\n') if len(line) > 10]
                text = '\n'.join(lines)
                # 取前 5000 字符
                if len(text) > 5000:
                    text = text[:5000] + '...'
                
                if len(text) > 100:
                    return {"success": True, "content": text, "url": url}
                
                return {"success": False, "error": "未能提取正文内容"}
    except Exception as e:
        print(f"[News API] 抓取正文失败: {e}")
        return {"success": False, "error": str(e)}


async def handle_get_news_content(request):
    """处理 GET /api/news/content?url=..."""
    url = request.query.get("url", "")
    if not url:
        return web.json_response({"success": False, "error": "缺少 url 参数"})
    
    result = await fetch_article_content(url)
    return web.json_response(result)
