import random
import requests
import json
import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from markitdown import MarkItDown
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler


TAG = __name__
logger = setup_logging()

CHANNEL_MAP = {
    "V2EX": "v2ex-share",
    "知乎": "zhihu",
    "微博": "weibo",
    "联合早报": "zaobao",
    "酷安": "coolapk",
    "MKTNews": "mktnews-flash",
    "华尔街见闻": "wallstreetcn-quick",
    "36氪": "36kr-quick",
    "抖音": "douyin",
    "虎扑": "hupu",
    "百度贴吧": "tieba",
    "今日头条": "toutiao",
    "IT之家": "ithome",
    "澎湃新闻": "thepaper",
    "卫星通讯社": "sputniknewscn",
    "参考消息": "cankaoxiaoxi",
    "远景论坛": "pcbeta-windows11",
    "财联社": "cls-depth",
    "雪球": "xueqiu-hotstock",
    "格隆汇": "gelonghui",
    "法布财经": "fastbull-express",
    "Solidot": "solidot",
    "Hacker News": "hackernews",
    "Product Hunt": "producthunt",
    "Github": "github-trending-today",
    "哔哩哔哩": "bilibili-hot-search",
    "快手": "kuaishou",
    "靠谱新闻": "kaopu",
    "金十数据": "jin10",
    "百度热搜": "baidu",
    "牛客": "nowcoder",
    "少数派": "sspai",
    "稀土掘金": "juejin",
    "凤凰网": "ifeng",
    "虫部落": "chongbuluo-latest",
}


# 默认新闻来源字典，当配置中没有指定时使用
DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"


def get_news_sources_from_config(conn):
    """从配置中获取新闻源字符串"""
    try:
        # 尝试从插件配置中获取新闻源
        if (
            conn.config.get("plugins")
            and conn.config["plugins"].get("get_news_from_newsnow")
            and conn.config["plugins"]["get_news_from_newsnow"].get("news_sources")
        ):
            # 获取配置的新闻源字符串
            news_sources_config = conn.config["plugins"]["get_news_from_newsnow"][
                "news_sources"
            ]

            if isinstance(news_sources_config, str) and news_sources_config.strip():
                logger.bind(tag=TAG).debug(f"使用配置的新闻源: {news_sources_config}")
                return news_sources_config
            else:
                logger.bind(tag=TAG).warning("新闻源配置为空或格式错误，使用默认配置")
        else:
            logger.bind(tag=TAG).debug("未找到新闻源配置，使用默认配置")

        return DEFAULT_NEWS_SOURCES

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻源配置失败: {e}，使用默认配置")
        return DEFAULT_NEWS_SOURCES


# 从CHANNEL_MAP获取所有可用的新闻源名称
available_sources = list(CHANNEL_MAP.keys())
example_sources_str = "、".join(available_sources)

GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_newsnow",
        "description": (
            "获取最新新闻。默认与主界面「今日资讯」看板同源（新浪/腾讯/澎湃聚合），"
            "保证语音播报内容与看板一致；用户指定其他新闻源时从该源获取，"
            f"标准的名称是：{example_sources_str}（例如用户要求百度新闻，其实就是百度热搜）。"
            "【重要】当用户关心的是某个**主题/领域**的新闻（如「AI 方面的新闻」「特斯拉的消息」"
            "「最近房地产的政策」），请把该主题词传给 keyword 参数，本工具会联网搜索该主题的相关报道，"
            "并把结果推到网页大屏上；此时不要传 source。参数只有 source/index/detail/keyword/lang 五个，"
            "不要自造 category、topic、type 等其它名字。"
            "不指定序号则随机选一条；用户说「播放第 N 条新闻」时请传 index=N。"
            "用户可以要求获取详细内容，此时会获取新闻的详细内容。"
            # [2026-10-12 主人实测修正] 参照菜谱窗口的正确做法（recipe_page 的描述）。
            "【窗口规则·重要】如果新闻卡片列表窗口**已经打开**，用户说「下一条」「上一条」"
            "「播放第 N 条」「播放详细内容」，都只是在这个已打开的窗口里翻动/播报："
            "直接用本工具传 index，**绝对不要**调用 close_stage 去关窗口 —— "
            "关掉再打开会让用户以为窗口失灵。只有用户明确说「关闭新闻窗口」「关掉窗口」"
            "「关闭窗口」时才用 close_stage。"
            "【什么时候才该弹窗口】"
            "① 用户明确要「看」——说「打开新闻」「把新闻放到屏幕上」「显示新闻」→ 调 show_news_full；"
            "② 用户要的是**联网搜来的新内容**（如「AI 方面的新闻」「特斯拉的消息」）→ "
            "   本工具自己会把它推到大屏，无需你额外调 show_news_full；"
            "③ 播报看板上**已经有的**某一条（说「第N条」「下一条」）→ "
            "   **无论窗口开没开都只用本工具，不要弹窗**。用户只是要听，不是要看，"
            "   弹窗会突然盖掉他正在看的其它内容（如相册、摄像头）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "主题关键词，例如「AI」「人工智能」「特斯拉」。"
                    "用户想看某个方面的新闻时必须传这个，否则只能给出通用的一条新闻。可选参数",
                },
                "source": {
                    "type": "string",
                    "description": f"新闻源的标准中文名称，例如{example_sources_str}等。可选参数，不提供则使用今日资讯看板聚合源",
                },
                "index": {
                    "type": "integer",
                    "description": "要播报的新闻序号，从 1 开始（对应今日资讯看板窗口里的第 N 条）。"
                                   "窗口已打开时说「下一条」，传当前条数+1；说「上一条」传当前条数-1。"
                                   "可选，不提供则随机选择一条",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否获取详细内容，默认为false。如果为true，则获取上一条新闻的详细内容",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}


def fetch_news_from_api(conn: "ConnectionHandler", source="thepaper"):
    """从API获取新闻列表"""
    try:
        api_url = f"https://newsnow.busiyi.world/api/s?id={source}"

        news_config = conn.config.get("plugins", {}).get("get_news_from_newsnow", {})
        if news_config.get("url"):
            api_url = news_config["url"] + source

        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(api_url, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()

        if "items" in data:
            return data["items"]
        else:
            logger.bind(tag=TAG).error(f"获取新闻API响应格式错误: {data}")
            return []

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻API失败: {e}")
        return []


def fetch_news_detail(url):
    """获取新闻详情页内容并使用MarkItDown清理HTML"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        # 使用MarkItDown清理HTML内容
        md = MarkItDown(enable_plugins=False)
        result = md.convert(response)

        # 获取清理后的文本内容
        clean_text = result.text_content

        # 如果清理后的内容为空，返回提示信息
        if not clean_text or len(clean_text.strip()) == 0:
            logger.bind(tag=TAG).warning(f"清理后的新闻内容为空: {url}")
            return "无法解析新闻详情内容，可能是网站结构特殊或内容受限。"

        return clean_text
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {e}")
        return "无法获取详细内容"


async def _push_news_to_stage(news_list: list, title: str) -> int:
    """把新闻列表推送到网页大屏的中央舞台卡片，返回送达的连接数。

    复用 show_news_full 用的同一条 news_full_view 通道：
    前端 openNewsFullView 已支持 title/category/source/time/url 五个字段，
    且卡片本身可点击打开原文，所以搜索结果能零前端改动直接上屏。
    """
    try:
        import json

        from core.api.config_api import get_websocket_server_instance

        server = get_websocket_server_instance()
        if server is None:
            logger.bind(tag=TAG).warning("[主题新闻] WebSocket 服务实例未就绪，跳过推送")
            return -1

        payload = json.dumps(
            {"type": "news_full_view", "action": "open", "title": title, "news": news_list},
            ensure_ascii=False,
        )
        async with server.connections_lock:
            conns = list(server.active_connections)
        sent = 0
        for ws in conns:
            try:
                await ws.send(payload)
                sent += 1
            except Exception:
                pass
        logger.bind(tag=TAG).info(f"[主题新闻] 已推送大屏：{len(news_list)} 条，送达 {sent} 个连接")
        return sent
    except Exception as e:
        # 推送失败不影响语音播报
        logger.bind(tag=TAG).warning(f"[主题新闻] 推送大屏失败（不影响播报）: {e}")
        return -1


# 国内权威媒体域名：用于把中文新闻排到前面。
# 实测不能靠「标题中文比例」判断——西门子、格罗方德等企业站标题也是中文，
# 但不是新闻；显式列域名更可靠且可维护。
_CN_NEWS_HOSTS = (
    'xinhuanet.com', 'news.cn', 'people.com.cn', 'cctv.com', 'chinanews.com',
    'thepaper.cn', 'sina.com.cn', 'sohu.com', '163.com', 'qq.com',
    'ifeng.com', 'yicai.com', 'stcn.com', 'cls.cn', 'jiemian.com',
    '36kr.com', 'huxiu.com', 'caixin.com', 'bbc.com', 'cn.nytimes.com',
    'rfi.fr', 'dw.com', 'voachinese.com', 'zaobao.com', 'zaobao.com.sg',
    'cna.com.tw', 'udn.com', 'zaobao.com/realtime/china',
)


def _is_ascii_word(text: str) -> bool:
    # 关键词是否纯英文/缩写（如 AI、Tesla）。这类词直接搜中文源命中率极低。
    t = (text or '').strip()
    if not t:
        return False
    return all(ord(ch) < 128 for ch in t)


def _has_cn_news(items) -> bool:
    return any(_is_cn_news(it.get('url') or '') for it in (items or []))


def _is_cn_news(url: str) -> bool:
    host = _host_of(url).lower()
    if not host:
        return False
    return any(h in host for h in _CN_NEWS_HOSTS)


# newsnow 高质量中文频道（只挑财经/科技类权威源，避免微博知乎等噪声）
_NEWSNOW_CN_CHANNELS = (
    'cls-depth', 'ithome', 'thepaper', 'jin10', 'solidot',
    'wallstreetcn-quick', '36kr-quick', 'cankaoxiaoxi', 'sputniknewscn',
)

# 主题词 -> 补充关键词。仅靠用户给的词过滤命中率太低
# （实测搜「AI」时，标题里带字面 AI 的只有一小部分，
#  但「英伟达」「大模型」「算力」等词其实都是 AI 相关）。
_TOPIC_ALIASES = {
    'AI': ['AI', 'AIGC', '人工智能', '大模型', '英伟达', 'OpenAI', '算力',
           '芯片', 'GPT', '机器学习', 'DeepSeek', '智能体', 'LLM'],
    '人工智能': ['AI', 'AIGC', '人工智能', '大模型', '英伟达', 'OpenAI', '算力',
                 '芯片', 'GPT', '机器学习', 'DeepSeek', '智能体', 'LLM'],
    '房地产': ['房', '楼市', '地产', '物业', '限购', '房价', '住房', '不动产'],
    '股票': ['股', 'A股', '港股', '美股', '涨', '跌', '指数', '券商'],
    '天气': ['天气', '气温', '降雨', '台风', '寒潮'],
    '体育': ['球', '赛', '冠军', '联赛', '运动员'],
    '汽车': ['车', '汽车', '新能源', '特斯拉', '比亚迪', '驾驶'],
}


def _match_terms(keyword: str) -> list:
    """用户关键词 + 主题同义词表，过滤新闻标题用。"""
    terms = [keyword]
    k = (keyword or '').strip()
    for key, alias in _TOPIC_ALIASES.items():
        if key in k or k in key:
            terms.extend(alias)
            break
    # 去重且保持顺序
    seen = set()
    out = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def _newsnow_cn_items(keyword: str, max_results: int) -> list:
    """用关键词过滤 newsnow 的中文频道，返回纯中文新闻条目。

    newsnow 不支持关键词搜索（/api/search 返回 HTML），
    所以做法是：并发拉几个高质量中文频道，用关键词过滤标题。
    实测搜「AI」在财联社/IT之家/澎湃/金十 里能命中 12 条真新闻，
    全部是中文标题，无需翻译。
    """
    import asyncio

    terms = _match_terms(keyword)

    async def _one(sid):
        try:
            url = 'https://newsnow.busiyi.world/api/s?id=' + sid
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as c:
                resp = await c.get(url, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                return []
            return resp.json().get('items') or []
        except Exception as e:
            logger.bind(tag=TAG).debug(f"[主题新闻] newsnow 源 {sid} 失败: {type(e).__name__}")
            return []

    results = await asyncio.gather(*[_one(s) for s in _NEWSNOW_CN_CHANNELS])

    src_names = {
        'cls-depth': '财联社', 'ithome': 'IT之家', 'thepaper': '澎湃新闻',
        'jin10': '金十数据', 'solidot': 'Solidot', 'wallstreetcn-quick': '华尔街见闻',
        '36kr-quick': '36氪', 'cankaoxiaoxi': '参考消息', 'sputniknewscn': '卫星通讯社',
    }
    # 轮流从各频道取，避免财联社一家填满（实测原先按频道顺序取，
    # 结果 5 条全是财联社，源太单一）。
    per_channel = []
    seen = set()
    for sid, lst in zip(_NEWSNOW_CN_CHANNELS, results):
        picked = []
        for it in lst or []:
            title = (it.get('title') or '').strip()
            if not title or title in seen:
                continue
            if not any(t in title for t in terms):
                continue
            seen.add(title)
            picked.append({
                'title': title,
                'snippet': '',
                'url': it.get('url', '') or '',
                'time': '',
                'source': src_names.get(sid, sid),
            })
            if len(picked) >= 3:
                break
        per_channel.append(picked)

    items = []
    idx = 0
    while len(items) < max_results * 2:
        added = False
        for picked in per_channel:
            if idx < len(picked):
                items.append(picked[idx])
                added = True
                if len(items) >= max_results * 2:
                    break
        if not added:
            break
        idx += 1

    logger.bind(tag=TAG).info(
        f"[主题新闻] newsnow 中文源过滤「{keyword}」命中 {len(items)} 条（词: {terms[:4]}）"
    )
    return items


async def _search_news_topic(web_search_config: dict, keyword: str, max_results: int) -> dict:
    """Tavily news 专用搜索：topic='news' + days 时间窗，返回正规新闻报道。

    返回 {'items': [{title,snippet,url,time}], 'error': str}，
    与 web_search.raw_search 同结构，便于统一处理；失败返回空 items 由调用方回落。
    """
    provider = str(web_search_config.get('provider', '')).lower().strip()
    api_key = web_search_config.get('api_key', '') or ''
    if not api_key.isascii():
        api_key = ''
    # 非 ASCII 的 key 必是占位符（如"你的Tavily API密钥"），按未配置处理
    if provider != 'tavily' or not api_key:
        return {'items': [], 'error': ''}

    days = int(web_search_config.get('news_days', 3) or 3)
    days = max(1, min(7, days))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            resp = await client.post(
                'https://api.tavily.com/search',
                json={
                    'query': keyword,
                    'topic': 'news',
                    'days': days,
                    'max_results': max_results,
                    'search_depth': 'advanced',
                    'include_answer': False,
                },
                headers={'Authorization': 'Bearer ' + api_key},
            )
        if resp.status_code != 200:
            logger.bind(tag=TAG).warning(f"[主题新闻] news 搜索 HTTP {resp.status_code}")
            return {'items': [], 'error': ''}
        data = resp.json()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[主题新闻] news 搜索异常: {type(e).__name__}: {e}")
        return {'items': [], 'error': ''}

    items = []
    for it in data.get('results') or []:
        title = (it.get('title') or '').strip()
        if not title:
            continue
        items.append(
            {
                'title': title,
                'snippet': it.get('content', '') or '',
                'url': it.get('url', '') or '',
                'time': it.get('published_date', '') or '',
            }
        )
    logger.bind(tag=TAG).info(f"[主题新闻] news 专用搜索返回 {len(items)} 条（{days} 天内）")
    return {'items': items, 'error': ''}


async def _search_news_by_keyword(conn, keyword: str, lang: str) -> ActionResponse:
    """按主题关键词联网搜索新闻，并推送到网页大屏。

    搜索走 web_search 的 raw_search（同一套 provider/api_key 配置），
    不另造引擎，避免两套搜索逻辑漂移。
    """
    try:
        from urllib.parse import urlparse

        from plugins_func.functions.web_search import raw_search
    except Exception as e:
        logger.bind(tag=TAG).error(f"导入搜索能力失败: {e}")
        return ActionResponse(Action.REQLLM, "抱歉，联网搜索能力暂时不可用。", None)

    web_search_config = conn.config.get('plugins', {}).get('web_search', {})
    max_results = int(web_search_config.get('max_results', 5) or 5)
    max_results = max(3, min(8, max_results))

    query = keyword + ' 最新新闻'

    # 2026-10-11 主人指正：中文源还是英文源无所谓，
    # 能拿到信息就行。日志实证 LLM 本就自动把英文标题
    # 翻成中文播报（实测：Tavily 返回英文源，播报出来是「生数
    # 科技发布了支持4K输出的视频生成模型」）。
    # 但主人同时指出「网上的中文源很多」——
    # 确实如此：newsnow（本项目本来就在用的开源聚合器，
    # CHANNEL_MAP 里 40+ 中文源）**不支持关键词搜索**，
    # 只能按频道取。这里用关键词过滤它的高质量
    # 中文频道标题，拿到就是纯中文真新闻，无需翻译。
    # Tavily 降为补充（中文源不够时补齐条数 / 兜底冷门主题）。
    cn_items = await _newsnow_cn_items(keyword, max_results)


    # 2026-10-11 实测：全网杂搜（raw_search）返回的是一堆导航站首页、
    # 社交媒体标签页（instagram.com/popular/...）、外文聚合页，抓正文多半失败/超时。
    # Tavily 的 topic='news' + days 参数返回的是**正规新闻报道**，
    # 每条带 published_date、是真文章链接（实测 21世纪经济报道/搜狐/信息化观察网等）。
    # 所以新闻优先走 news 专用搜索，失败再回落全网杂搜。
    # 中文源够数就用它（纯中文、无需翻译）；不够才走 Tavily 补齐。
    if len(cn_items) >= max_results:
        res = {'items': cn_items, 'error': ''}
    else:
        res = await _search_news_topic(web_search_config, keyword, max_results * 2)
    # 2026-10-11 实测：直接搜 'AI' 返回的 10 条**一条中文源都没有**
    # （全是 ABC/NYTimes/Axios/AP 等外媒），而搜 '人工智能' 才能召回新华网、
    # 纽约时报中文网等。英文/缩写类关键词需要再补一轮中文检索，
    # 否则中文家庭场景下搜不到国内报道。这里把两轮结果合并去重。
    if cn_items and not _has_cn_news(res.get('items') or []):
        _extra_q = keyword + ' 中国' if _is_ascii_word(keyword) else None
        if _extra_q:
            _extra = await _search_news_topic(web_search_config, _extra_q, max_results)
            _merged = list(cn_items) + list(res.get('items') or [])
            _seen = {(it.get('url') or '') for it in _merged}
            for it in _extra.get('items') or []:
                u = it.get('url') or ''
                if u and u not in _seen:
                    _seen.add(u)
                    _merged.append(it)
            res = dict(res)
            res['items'] = _merged

    if not (res.get('items') or []) and cn_items:
        res = {'items': cn_items, 'error': ''}
    if not (res.get('items') or []):
        logger.bind(tag=TAG).info(f"[主题新闻] news 专用搜索无结果，回落全网搜索: {keyword}")
        try:
            res = await raw_search(web_search_config, query, max_results)
        except httpx.TimeoutException:
            logger.bind(tag=TAG).error(f"[主题新闻] 搜索超时: {keyword}")
            return ActionResponse(Action.REQLLM, '联网搜索超时了，稍后再试试吧。', None)
        except Exception as e:
            logger.bind(tag=TAG).error(f"[主题新闻] 搜索异常: {type(e).__name__}: {e}")
            return ActionResponse(Action.REQLLM, '联网搜索出了点问题，稍后再试试。', None)

    if res.get("error"):
        logger.bind(tag=TAG).warning(f"[主题新闻] 搜索未执行: {res['error']}")
        return ActionResponse(Action.REQLLM, str(res["error"]), None)

    items = [it for it in (res.get('items') or []) if it.get('title')]
    if not items:
        return ActionResponse(
            Action.REQLLM, f'没搜到「{keyword}」相关的新闻，换个说法再试试吧。', None
        )

    # 排序优先级：① 国内权威媒体的中文报道 → ② 其它正规报道 → ③ 栏目首页/聚合页。
    # 实测不加这条时，搜「AI」返回的全是 ABC/NYTimes/Axios 等英文外媒，
    # 对中文家庭场景不合适；而首页类结果抓详情只会得到一堆导航链接。
    def _rank(it):
        t = (it.get('title') or '')
        u = (it.get('url') or '')
        is_cn = 0 if _is_cn_news(u) else 1
        is_index = 0
        if any(k in t for k in ('最新资讯', '新闻中心', '新闻首页', '资讯首页',
                                '频道', '专栏', '头条资讯', '每日', '工具集',
                                '开放平台', '汇总', '大全', '专题')):
            is_index = 1
        return (is_cn, is_index)

    items.sort(key=_rank)
    # 中文源优先占位：只按稳定排序截断时，中文源可能被外文挤到只剩一两条
    # （实测搜 'AI' 时中文只召回到 1 条，排在第 1 后其余全是外媒）。
    # 既然专门做了中文补查，就把够数的中文源排在前面，不够再用外文补齐。
    _cn_items = [it for it in items if _is_cn_news(it.get('url') or '')]
    _other = [it for it in items if not _is_cn_news(it.get('url') or '')]
    items = (_cn_items + _other)[:max_results]

    # 转成看板卡片需要的字段。前端卡片渲染 title/category/source/time/url，
    # 其中 time 会显示在卡片右下角 —— 搜索结果带 published_date，显示出来更可信。
    display = []
    for it in items:
        url = it.get('url', '') or ''
        display.append(
            {
                'title': it.get('title', ''),
                # newsnow 条目自带中文源名（财联社/澎湃新闻），
                # Tavily 条目没有才回退用域名。
                'source': it.get('source') or _host_of(url),
                'category': '',
                'time': _fmt_date(it.get('time', '')),
                'url': url,
            }
        )

    sent = await _push_news_to_stage(display, keyword + ' 相关新闻')

    # 2026-10-11 把整份搜索结果挂到 conn 上，供后续讲第 N 条详情时使用。
    # 之前只有看板分支写 last_newsnow_link，搜索结果没写，详情分支拿不到东西，
    # 回落成把标题重念一遍（主人实测听到重复内容）。这里存全量。
    conn.last_keyword_news = {'keyword': keyword, 'items': items}
    _first = items[0]
    _first_url = _first.get('url', '') or ''
    conn.last_newsnow_link = {
        'url': _first_url or '#',
        'title': _first.get('title', ''),
        'source_id': 'search',
        'source_name': _host_of(_first_url),
    }

    lines = [f"以下是关于「{keyword}」的网络检索结果（按相关度排序）："]
    for i, it in enumerate(items, 1):
        snippet = (it.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        lines.append(f"{i}. {it.get('title', '')}")
        if snippet:
            lines.append(f"   摘要：{snippet}")

    screen_note = (
        "\n（以上结果已推送到家庭看板屏幕显示。）"
        if sent > 0
        else "\n（家庭看板当前不在线，以上内容仅语音播报。）"
    )

    return ActionResponse(
        Action.REQLLM,
        "\n".join(lines)
        + screen_note
        + f"\n\n请用{lang}自然地向用户播报以上「{keyword}」相关新闻，不要逐条念编号，"
        f"挑最重要的 2~3 条讲清楚即可，并说明屏幕上也有这些新闻。",
        None,
    )


def _fmt_date(raw) -> str:
    """把 Tavily 的 RFC1123 日期（如 'Fri, 09 Oct 2026 16:00:00 GMT'）
    格式化成 '10-09' 这种短日期；解析不了就原样返回短片段。"""
    if not raw:
        return ''
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(str(raw))
        if dt is not None:
            return f'{dt.month:02d}-{dt.day:02d}'
    except Exception:
        pass
    s = str(raw)[:16]
    return '' if s.lower().startswith(('fri', 'sat', 'sun', 'mon', 'tue', 'wed', 'thu')) else s


def _host_of(url: str) -> str:
    """取网址的域名部分，用于卡片上的来源显示。"""
    try:
        if not url:
            return ""
        from urllib.parse import urlparse

        return urlparse(url).netloc or ""
    except Exception:
        return ""


@register_function(
    "get_news_from_newsnow",
    GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_newsnow(
    conn: "ConnectionHandler",
    source: str = None,
    index: int = 0,
    detail: bool = False,
    lang: str = "zh_CN",
    keyword: str = None,
    **kwargs,
):
    """获取新闻并随机选择一条进行播报，或获取上一条新闻的详细内容。

    2026-09-21 修复：默认与主界面「今日资讯」看板同源（/api/news 聚合），
    保证语音播报内容与看板一致；index 指定播报看板第 N 条。

    2026-10-11 新增 keyword：用户要「某方面的新闻」（如 AI 新闻）时，
    走联网搜索按主题拿结果，并推送到网页大屏卡片；不再随机抽一条不对味的新闻。
    **kwargs：LLM 常会自造 category/topic 等参数名（实测豆包传过 category='科技'），
    旧实现直接 func(conn, **arguments) 解包 → TypeError 整个工具崩掉、英文报错被念出来。
    这里显式吞掉多余参数，保证未知参数只会退化成「忽略」，不会让工具失败。
    """
    try:
        if kwargs:
            logger.bind(tag=TAG).info(
                f"忽略 LLM 多传的未知参数: {sorted(kwargs.keys())}"
            )

        # 获取当前配置的新闻源
        news_sources = get_news_sources_from_config(conn)

        # 如果detail为True，获取上一条新闻的详细内容
        detail = str(detail).lower() == 'true'
        if detail:
            # 2026-10-11：上一次若是「主题搜索」，优先从搜索结果里按序号取，
            # 这样说「第 2 条讲讲详情」能拿到对应那条，而不是永远拿第一条/重念标题。
            kw_news = getattr(conn, 'last_keyword_news', None)
            if kw_news and kw_news.get('items'):
                _idx = int(index or 0)
                _items = kw_news['items']
                if _idx > len(_items):
                    return ActionResponse(
                        Action.REQLLM,
                        f'刚才关于「{kw_news.get("keyword", "")}」只搜到 {len(_items)} 条，'
                        f'没有第 {_idx} 条。',
                        None,
                    )
                _target = _items[_idx - 1] if _idx > 0 else _items[0]
                _t_url = _target.get('url', '') or ''
                _t_title = _target.get('title', '') or ''
                if not _t_url:
                    return ActionResponse(
                        Action.REQLLM, '这条搜索结果没有可用的链接获取详细内容。', None
                    )
                _content = fetch_news_detail(_t_url)
                if not _content or _content == '无法获取详细内容':
                    return ActionResponse(
                        Action.REQLLM,
                        f'抱歉，无法获取《{_t_title}》的详细内容，可能是链接已失效或网站结构发生变化。',
                        None,
                    )
                # 2026-10-11 实测：抓到导航站首页时正文几乎全是 [首页](..) 这类链接
                # （实测 8317 字符里 markdown 链接占绝大多数），念出来是一堆
                # 「首页 AI资讯 产品库」。按链接密度识别并如实告知，不喂垃圾给 LLM。
                _links = _content.count('](')
                if len(_content) and _links > 12 and _links * 60 > len(_content):
                    return ActionResponse(
                        Action.REQLLM,
                        f'这条《{_t_title}》是新闻网站的栏目首页而非具体报道，'
                        f'抓不到有效正文。如需了解详情，请换一条具体的新闻。',
                        None,
                    )
                return ActionResponse(
                    Action.REQLLM,
                    f'根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n'
                    f'新闻标题: {_t_title}\n'
                    f'详细内容: {_content}\n\n'
                    f'(请对上述新闻内容进行总结，提取关键信息，以自然、流畅的方式向用户播报，'
                    f'不要提及这是总结，就像是在讲述一个完整的新闻故事)',
                    None,
                )

            if (
                not hasattr(conn, "last_newsnow_link")
                or not conn.last_newsnow_link
                or "url" not in conn.last_newsnow_link
            ):
                return ActionResponse(
                    Action.REQLLM,
                    "抱歉，没有找到最近查询的新闻，请先获取一条新闻。",
                    None,
                )

            url = conn.last_newsnow_link.get("url")
            title = conn.last_newsnow_link.get("title", "未知标题")
            source_id = conn.last_newsnow_link.get("source_id", "thepaper")
            source_name = CHANNEL_MAP.get(source_id, "未知来源")

            if not url or url == "#":
                return ActionResponse(
                    Action.REQLLM, "抱歉，该新闻没有可用的链接获取详细内容。", None
                )

            logger.bind(tag=TAG).debug(
                f"获取新闻详情: {title}, 来源: {source_name}, URL={url}"
            )

            # 获取新闻详情
            detail_content = fetch_news_detail(url)

            if not detail_content or detail_content == "无法获取详细内容":
                return ActionResponse(
                    Action.REQLLM,
                    f"抱歉，无法获取《{title}》的详细内容，可能是链接已失效或网站结构发生变化。",
                    None,
                )

            # 构建详情报告
            detail_report = (
                f"根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n"
                f"新闻标题: {title}\n"
                # f"新闻来源: {source_name}\n"
                f"详细内容: {detail_content}\n\n"
                f"(请对上述新闻内容进行总结，提取关键信息，以自然、流畅的方式向用户播报，"
                f"不要提及这是总结，就像是在讲述一个完整的新闻故事)"
            )

            return ActionResponse(Action.REQLLM, detail_report, None)

        # 2026-10-11 新增：按主题搜新闻（用户要「AI 方面的新闻」这类带主题的问法）
        # 复用 web_search 的 raw_search，与 web_search_read / web_read 同一套引擎与配置。
        if keyword and str(keyword).strip():
            return await _search_news_by_keyword(conn, str(keyword).strip(), lang)

        # 2026-09-21 修复：优先与「今日资讯」看板同源（保证语音与看板内容一致）；
        # 用户明确指定其他源（非澎湃）时仍走 newsnow；看板不可用时降级 newsnow。
        index = int(index or 0)
        if (not source) or source == "澎湃新闻" or index > 0:
            try:
                from core.api.news_api import get_realtime_news

                board_news = await get_realtime_news()
            except Exception as e:
                logger.bind(tag=TAG).warning(f"今日资讯同源获取失败，降级 newsnow: {e}")
                board_news = []
            if board_news:
                if index > 0:
                    if index > len(board_news):
                        # 2026-09-21 优化：直接播报，免走一轮 LLM
                        return ActionResponse(
                            Action.RESPONSE,
                            f"今日资讯看板目前只有 {len(board_news)} 条新闻，没有第 {index} 条。",
                            None,
                        )
                    selected_news = board_news[index - 1]
                else:
                    selected_news = random.choice(board_news)
                conn.last_newsnow_link = {
                    "url": selected_news.get("url", "#") or "#",
                    "title": selected_news.get("title", "未知标题"),
                    "source_id": "board",
                }
                # 2026-09-21 优化：看板同源路径改为直接播报（Action.RESPONSE），
                # 不再走第二轮 LLM。实测第二轮 LLM 首 token 约 2.2 秒、且会携带
                # 全量工具 schema，是「播放新闻」十几秒延时的主要来源；直接播报
                # 的内容与看板完全一致，也无需模型复述（更准更快）。
                _src = selected_news.get("source", "") or ""
                _prefix = f"今日资讯第 {index} 条：" if index > 0 else "今日资讯："
                speak_text = f"{_prefix}{selected_news['title']}。"
                if _src:
                    speak_text += f"（{_src}）"
                speak_text += "需要新闻详情可以告诉我。"
                return ActionResponse(Action.RESPONSE, speak_text, None)

        # 否则，获取新闻列表并随机选择一条
        # 将中文名称转换为英文ID
        english_source_id = None
        source = source or "澎湃新闻"

        # 检查输入的中文名称是否在配置的新闻源中
        news_sources_list = [
            name.strip() for name in news_sources.split(";") if name.strip()
        ]
        if source in news_sources_list:
            # 如果输入的中文名称在配置的新闻源中，在 CHANNEL_MAP 中查找对应的英文ID
            english_source_id = CHANNEL_MAP.get(source)

        # 如果找不到对应的英文ID，使用默认源
        if not english_source_id:
            logger.bind(tag=TAG).warning(f"无效的新闻源: {source}，使用默认源澎湃新闻")
            english_source_id = "thepaper"
            source = "澎湃新闻"

        logger.bind(tag=TAG).info(f"获取新闻: 新闻源={source}({english_source_id})")

        # 获取新闻列表
        news_items = fetch_news_from_api(conn, english_source_id)

        if not news_items:
            return ActionResponse(
                Action.REQLLM,
                f"抱歉，未能从{source}获取到新闻信息，请稍后再试或尝试其他新闻源。",
                None,
            )

        # 随机选择一条新闻
        selected_news = random.choice(news_items)

        # 保存当前新闻链接到连接对象，以便后续查询详情
        if not hasattr(conn, "last_newsnow_link"):
            conn.last_newsnow_link = {}
        conn.last_newsnow_link = {
            "url": selected_news.get("url", "#"),
            "title": selected_news.get("title", "未知标题"),
            "source_id": english_source_id,
        }

        # 构建新闻报告
        news_report = (
            f"根据下列数据，用{lang}回应用户的新闻查询请求：\n\n"
            f"新闻标题: {selected_news['title']}\n"
            # f"新闻来源: {source}\n"
            f"(请以自然、流畅的方式向用户播报这条新闻标题，"
            f"提示用户可以要求获取详细内容，此时会获取新闻的详细内容。)"
        )

        return ActionResponse(Action.REQLLM, news_report, None)

    except Exception as e:
        logger.bind(tag=TAG).error(f"获取新闻出错: {e}")
        return ActionResponse(
            Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None
        )
