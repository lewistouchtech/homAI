"""云端 LLM 工具预筛选（2026-10-12）。

背景：云端 LLM 原先旁路筛选，60 个工具定义（约 3.3 万字符 ≈ 1.2 万 token）每轮全带，
首 token 慢；而老筛选器 `filter_tools_by_query` 为本地小模型设计，召回率仅约 54%
（漏 web_search / 日报 / 备忘录等），直接套到云端会「没工具可用」。

做法（只把符合要求的工具推给大模型，同时保证不漏常用工具）：
  1. 常驻小工具集 ALWAYS_TOOLS：高频且 schema 很小，任何一句话都带；
  2. 场景命中：复用 `filter_tools_by_query`（TOOL_SCENES + 场景记忆 + 设备分流）；
  3. 补充场景 EXTRA_SCENES：老场景表没覆盖的白名单工具（备忘录/短信/系统控制/摄像头…），关键词取宽；
  4. 白名单外的未知工具原样放行（保守，不替未知工具做主）。

纯函数，无副作用。输出保持输入顺序，保证前缀稳定（利于云端 prompt 缓存）。
"""
from typing import Any, Dict, Iterable, List, Set, Tuple

from core.utils.tool_filter import CORE_TOOLS, TOOL_SCENES, filter_tools_by_query

ALWAYS_TOOLS: Tuple[str, ...] = (
    "web_search", "web_search_read", "get_daily_briefing", "search_from_ragflow",
    "get_weather", "close_stage", "handle_exit_intent", "ha_control",
)
# ⚠️ 常驻集只放「只读/无副作用、不会被误调成展示动作」的工具。
# 2026-10-12 实测：show_news_full（弹出全屏新闻）曾在常驻集里，主人说「打开摄像头」
# 大模型却把新闻打开了——会弹窗的展示类工具一律必须靠场景关键词才暴露。

EXTRA_SCENES: Dict[str, Dict[str, Any]] = {
    "notes": {
        "keywords": ["备忘录", "笔记", "记事", "便签", "记下", "记一下", "帮我记", "记录一下"],
        "tools": ["mac_notes", "mac_reminder"],
    },
    "reminder_more": {
        "keywords": ["提醒", "闹钟", "待办", "代办", "代班", "事项", "取消提醒", "删除提醒"],
        "tools": ["set_reminder", "list_reminders", "cancel_reminder", "mac_reminder"],
    },
    "message": {
        "keywords": ["短信", "微信", "发给", "告诉", "通知", "留言", "发消息", "邮件", "打电话", "电话",
                     "飞书", "飞叔", "飞数", "iMessage", "信息"],
        "tools": ["mac_message", "send_feishu_message", "read_feishu_messages", "send_wechat_message",
                  "send_email_message", "send_sms_message", "send_voice_message"],
    },
    "mail_more": {
        "keywords": ["邮件", "邮箱", "收件箱", "整理邮箱", "写邮件", "回邮件", "发邮件"],
        "tools": ["mail_action", "mail_compose", "mail_compose_close", "organize_mailbox"],
    },
    "mac_system": {
        "keywords": ["音量", "静音", "截图", "锁屏", "关机", "重启", "休眠", "电量", "wifi", "WiFi",
                     "蓝牙", "系统", "勿扰", "深色", "电脑"],
        "tools": ["mac_system", "mac_app"],
    },
    "mac_app_more": {
        # 不收「打开/关闭」这类裸动词：任何「打开X」都会暴露 mac_app，大模型会拿它去开错东西
        "keywords": ["应用", "软件", "app", "App", "访达", "终端", "计算器", "预览", "启动应用"],
        "tools": ["mac_app"],
    },
    "browser": {
        "keywords": ["浏览器", "网页", "网站", "网址", "链接", "Safari", "Chrome", "上网", "网上",
                     "搜索", "搜一下", "查一下", "百度", "谷歌"],
        "tools": ["mac_browser", "mac_web", "web_view", "web_read"],
    },
    "camera_more": {
        "keywords": ["摄像头", "监控", "门口", "有人", "来人", "陌生人", "进来", "出入", "快递", "画面", "车库", "院子"],
        "tools": ["query_camera_status", "show_camera_view", "describe_camera_event"],
    },
    "health_more": {
        "keywords": ["健康", "血压", "血糖", "体重", "心率", "血氧", "体温", "睡眠", "步数",
                     "身体", "指标", "报告", "手环", "体检"],
        "tools": ["get_health_status", "get_current_health_status", "get_health_history",
                  "query_health_history", "check_health_alerts", "show_health_report",
                  "health_report_page", "receive_health_data", "register_health_device"],
    },
    "scene_more": {
        "keywords": ["场景", "模式", "一键", "全部", "所有", "回家", "离家", "睡觉", "起床", "出门",
                     "关灯", "开灯", "电影", "观影", "会客",
                     "心情", "氛围", "气氛", "放松", "太累", "好累", "有点累", "不开心", "难过", "郁闷",
                     "庆祝", "聚会", "浪漫", "温馨", "看书", "读书"],
        "tools": ["run_ai_scene", "batch_device_control", "ha_control"],
    },
    "music_more": {
        "keywords": ["歌", "音乐", "播放", "放首", "来首", "听歌", "暂停", "继续", "下一首", "上一首",
                     "声音", "大声", "小声"],
        "tools": ["mac_music", "ha_music", "play_music"],
    },
    "map_more": {
        "keywords": ["地图", "附近", "周边", "导航", "路线", "怎么走", "怎么去", "定位", "距离", "多远",
                     "多久", "餐厅", "饭店", "景点", "酒店", "咖啡"],
        "tools": ["search_nearby", "show_map", "get_travel_info", "plan_commute"],
    },
    # 相册 / 新闻 / 做菜必须各自成组：曾合成一组，说「打开相册」连新闻、菜谱工具一起暴露，
    # 大模型就会误调（2026-10-12 主人实测「打开摄像头」弹出新闻）。
    "album_more": {
        "keywords": ["相册", "照片", "相片"],
        "tools": ["show_album"],
    },
    "news_more": {
        # [2026-10-12 主人实测] 补「上一条/下一条/第N条」这类**不含「新闻」二字**的说法。
        # 成因：主人说「上一条」「下一条」时句子里没有任何新闻关键词 → 本场景不命中 →
        #   get_news_from_newsnow 被预筛掉 → 模型手上没新闻工具，只能反问
        #   「你是说想回到第6条资讯吗？」，或退化用 web_search 硬搜（序号对不上）。
        # 修法：把「相对翻动」和「第N条」的说法也算作新闻场景信号。
        # 代价：主人在新闻窗口开着时说这些词会带上新闻工具 —— 这正是我们要的；
        #   窗口没开时模型会照常追问，不会乱播。
        "keywords": ["新闻", "资讯", "热搜", "最新消息", "最近有什么", "第一个", "第二个", "第三个",
                     "第四个", "第五个", "详细内容",
                     "上一条", "下一条", "上一个", "下一个", "上条", "下条",
                     "第1条", "第2条", "第3条", "第4条", "第5条", "第6条", "第7条", "第8条",
                     "第9条", "第10条", "第一条", "第二条", "第三条", "第四条", "第五条",
                     "第六条", "第七条", "第八条", "第九条", "第十条",
                     "播放第", "播第", "读第", "翻到第"],
        "tools": ["show_news_full", "get_news_from_newsnow", "get_news_from_chinanews"],
    },
    "cook_more": {
        "keywords": ["菜谱", "食谱", "做法", "怎么做", "做菜", "下一步", "上一步", "下一页", "上一页",
                     "翻页", "食材"],
        "tools": ["cook_recipe", "recipe_page", "show_recipe"],
    },
    # [2026-10-14 新增] 展厅迎宾演示。关键词刻意收窄：只认「演示/来个/迎宾」这类**明确演示**的说法。
    # 不能收「欢迎」「到家」「回来」这类词 —— 那属于日常回家问候（安防欢迎语分层管），
    # 误暴露本工具会让大模型拿"演示迎宾"顶替真实欢迎，反而制造打扰。
    "welcome_demo": {
        "keywords": ["演示迎宾", "迎宾演示", "演示一下欢迎", "演示迎客", "来个迎宾",
                     "迎宾一下", "欢迎演示", "迎宾流程"],
        "tools": ["welcome_demo"],
    },
}

_WEB_TOOLS = ("web_search", "web_search_read", "web_view", "web_read")


def _name(f: Dict[str, Any]) -> str:
    inner = f.get("function") if isinstance(f.get("function"), dict) else f
    return str((inner or {}).get("name") or "")


def _managed_names() -> Set[str]:
    names: Set[str] = set(ALWAYS_TOOLS) | set(CORE_TOOLS)
    for cfg in TOOL_SCENES.values():
        names |= set(cfg["tools"])
    for cfg in EXTRA_SCENES.values():
        names |= set(cfg["tools"])
    return names


def prefilter_tools(
    all_tools: List[Dict[str, Any]],
    query: str,
    device_id: str = None,
    extra_text: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """云端 LLM 的工具预筛选。

    extra_text：补充语境（如上一句用户话）。仅在本句很短（≤20 字，典型跟进语如「再来一个」）时参与匹配。
    """
    q = str(query or "")
    pool = q
    if len(q.strip()) <= 20:
        pool = " ".join([q] + [str(t) for t in extra_text if t])
    keep: Set[str] = set(ALWAYS_TOOLS)
    try:
        scene_tools = filter_tools_by_query(all_tools, q, device_id, max_chars=10 ** 7)
        keep |= {_name(t) for t in scene_tools}
    except Exception:
        pass
    for cfg in EXTRA_SCENES.values():
        if any(k in pool for k in cfg["keywords"]):
            keep |= set(cfg["tools"])
    if pool != q:
        # 短跟进语（「再来一个」「那明天呢」）：上一句的场景继续有效
        for cfg in TOOL_SCENES.values():
            if any(k in pool for k in cfg["keywords"]):
                keep |= set(cfg["tools"])
    # 做菜场景屏蔽网页类，逼它走 cook_recipe（沿用 2026-10-07 规则）
    if any(k in q for k in TOOL_SCENES["cooking"]["keywords"]):
        keep -= set(_WEB_TOOLS)
    # 音乐按设备分流：去掉另一端（沿用 2026-09-17 规则）
    if device_id is not None:
        from core.utils.tool_filter import _device_music_tool
        prefer = _device_music_tool(device_id)
        keep.discard("ha_music" if prefer == "mac_music" else "mac_music")
    managed = _managed_names()
    return [t for t in all_tools if _name(t) in keep or _name(t) not in managed]
