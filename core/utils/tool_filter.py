"""
工具按需加载过滤器
根据用户输入动态选择需要的工具
"""

from typing import List, Dict, Any


# 工具场景分类
TOOL_SCENES = {
    'weather': {
        'keywords': ['天气', '温度', '下雨', '下雪', '晴天', '阴天', '天气预报'],
        'tools': ['get_weather'],
    },
    'music': {
        # 2026-09-21 修复：主人实测"暂停/上一首/下一首/停止"等裸控制词不命中任何场景，
        # 候选被收敛到核心 3 个工具 → mac_music 不可见 → "暂停"指令从未被调用。
        'keywords': ['播放', '听歌', '放首歌', '歌曲', '切歌', '暂停播放', '来点音乐', '放音乐', '音乐',
                     '暂停', '上一首', '下一首', '前一首', '后一首', '换一首', '换首歌', '换个歌',
                     '停止播放', '停止回放', '停止音乐', '别放了', '不要放了',
                     '大点声', '小点声', '大声点', '小声点', '声音大一点', '声音小一点',
                     '音量', '关闭音乐', '关掉音乐',
                     # 2026-10-13：点播口语（来点/想听/放一首…）与常点歌手，否则只说歌手名时 mac_music 不可见
                     '来点歌', '来点好听的', '来点轻音乐', '来一首', '来首', '放一首', '放首', '放个歌',
                     '想听', '我要听', '给我放', '点歌', '点一首', '随机放', '随便放',
                     '周杰伦', '林俊杰', '陈奕迅', '邓紫棋', '王菲', '张学友', '刘德华', '五月天',
                     '蔡依林', '薛之谦', '李荣浩', '毛不易', '邓丽君', '孙燕姿', '张惠妹', '田馥甄'],
        # 2026-09-16：play_music（本地 MP3 推给对讲设备）已移除
        # 2026-09-17：HA 的 MCP 接入点已关闭（收敛为自研直连），改用自研 ha_music；
        # 音乐分两个播放端，两者都保留、互不冲突：
        #   - mac_music → 服务器所在 Mac 的 Apple Music（osascript）
        #   - ha_music  → 房间里的音箱（HA + Music Assistant）
        'tools': ['mac_music', 'ha_music'],
    },
    'news': {
        # 2026-09-17 修：'报告'类词会误命中（"打开家庭报告"被当成新闻），
        # 且原含裸'资讯'。"报告"归 health 场景，这里移除易混词。
        'keywords': ['新闻', '资讯', '今天发生', '热搜', '今日新闻', '今日资讯'],
        # 2026-10-11：原先只给 get_news_from_*，实测「今天的有没有关于AI方面的新闻」
        # 因为没有「查/搜」这类动词，命中不了 search 场景 → LLM 拿不到任何搜索工具，
        # 只能调 get_news_from_newsnow 并自造 category='科技' → TypeError 崩溃，
        # 英文报错原文还被 TTS 念了出来。现补上搜索工具，
        # 并把 web_search/web_search_read 纳入本场景，让主题类新闻问法走搜索。
        # （schema 实测 news 场景 1877 → 3298 字符，远低于 MAX_TOOL_SCHEMA_CHARS=12000）
        'tools': ['show_news_full', 'get_news_from_newsnow', 'get_news_from_chinanews',
                  'web_search', 'web_search_read'],
    },
    'home': {
        # 2026-09-17：原关键词漏了「亮度」类表达，导致「调亮一点」「亮度增加50%」
        # 「亮度设置为100」匹配不到任何场景 → 工具被过滤光 → LLM 只能回「好的」糊弄。
        # 这里补齐所有常见亮度/明暗表达。
        'keywords': ['灯', '空调', '电视', '开关', '窗帘', 'HomeAssistant', '设备', '电器', '家里开', '状态', '房间', '室内', '客厅', '卧室',
                     '亮度', '调亮', '调暗', '亮一点', '亮一些', '亮些', '亮点', '暗一点', '暗一些', '暗些', '暗点',
                     '最亮', '最暗', '变亮', '变暗', '灯光', '光线', '暖光', '冷光', '色温'],
        # 2026-09-15：hass_get_state/hass_set_state 已下线，统一走 ha_control
        # 2026-09-17：ha_control 与 ha_apply_scene 已合并为单一 ha_control
        # （单个设备/批量都用 entities 参数，避免大模型在重叠工具间选错）
        'tools': ['ha_control'],
    },
    # 场景/情绪组合：LLM 看到设备清单后自主搭配（调暗灯光+关窗帘+放音乐），
    # 必须同时给 HA 控制 + 音乐播放工具
    'scene_mood': {
        'keywords': ['心情', '氛围', '场景', '浪漫', '温馨', '派对', '观影', '会客', '舒服', '放松一下',
                     '太累', '好累', '有点累', '不开心', '难过', '郁闷', '庆祝', '聚会', '气氛', '来点'],
        'tools': ['ha_control', 'run_ai_scene', 'mac_music', 'ha_music'],
    },
    # 退出/关闭桌面应用
    # 2026-09-17 修：原先关键词只有"退出备忘录""关闭应用"这类完整短语，
    # 而用户实际说的是"关闭备忘录""关掉相册"——子串匹配不到 app_quit，
    # 反而因"备忘录"命中 reminder 场景，LLM 拿到的是提醒类工具，
    # 于是"关闭备忘录"关不掉。这里补齐"关闭/关掉/退出 + 各应用名"的组合。
    'app_quit': {
        'keywords': ['退出应用', '退出程序', '关闭应用', '关掉应用',
                     '退出微信', '退出浏览器', '退出备忘录',
                     '关闭备忘录', '关掉备忘录', '退出备忘录',
                     '关闭微信', '关掉微信', '关闭浏览器', '关掉浏览器',
                     '关闭访达', '关掉访达', '关闭日历', '关掉日历',
                     # 2026-09-21：'关闭音乐/关掉音乐'改归 music 场景（用户语义是停止播放，
                     # 不是退出 Music 应用）；"音乐"子串本身已能命中 music 场景
                     '关闭邮件', '关掉邮件',
                     '关闭终端', '关掉终端', '关闭计算器', '关掉计算器'],
        'tools': ['mac_app'],
    },
    # 2026-09-15 新增：直接打开桌面应用（需求变更：原生窗口置顶于浏览器前，
    # 不再截图/嵌套/覆盖——embed_mac_app/overlay_mac_app 已整体下线）
    # 2026-09-17 修：原关键词含裸"打开/启动"泛词，导致"打开新闻""打开相册"
    # 也命中本场景把 mac_app 塞进去（噪音，且与 show_news_full/show_album 抢）。
    # 改为"打开/启动 + 应用名"组合。
    'app_open': {
        'keywords': ['打开应用', '启动应用',
                     '打开浏览器', '打开Safari', '打开邮箱', '打开邮件', '打开Mail',
                     '打开备忘录', '打开访达', '打开Finder', '打开日历', '打开地图',
                     '打开照片', '打开终端', '打开微信', '打开Spotify', '打开计算器',
                     '打开便签', '打开预览', '打开FaceTime',
                     '启动浏览器', '启动微信', '启动备忘录'],
        'tools': ['mac_app'],
    },
    'reminder': {
        # 2026-09-23 修复：裸"提醒"会把邮件/平台通知误拽进本场景
        # （"千问余额不足提醒"、"苹果发过来的提醒和广告"→ 邮件工具被排除，
        # 且后续短句继承 reminder 连续跑偏）。改为明确的设提醒短语。
        'keywords': ['提醒我', '个提醒', '设提醒', '定提醒', '加提醒', '闹钟', '定时', '备忘录'],
        'tools': ['set_reminder', 'list_reminders', 'cancel_reminder'],
    },
    'travel': {
        # 2026-09-24 新增：出行/路程/POI 搜索场景
        # 2026-10-05 补 show_map：查附近时要能把地图打到看板中央窗口，
        #   否则 LLM 只能念文字，主人看不到位置图。
        # 2026-10-05 补 plan_commute：主人说「怎么走」但没说地点名时（如「我上班怎么走」），
        #   靠的是家庭成员档案里的常去地点，get_travel_info 拿不到这个语义。
        'keywords': ['路程', '距离', '多久到', '多长时间', '开车', '出门', '路况', '高德',
                     '附近', '周边', '餐厅', '饭店', '咖啡馆', '咖啡店', '酒店', '景点',
                     '怎么走', '导航', 'ETA', '预计时间', '几点出发'],
        'tools': ['get_travel_info', 'search_nearby', 'show_map', 'plan_commute'],
    },
    # 2026-10-05 新增：通勤场景。主人说「我上班了」「我上学了」「接孩子放学」
    # 时给 plan_commute —— 它只收语义标签（公司/学校），服务端查地点簿拿地址，
    # LLM 不需要也不能自己拼明文地址，这正是消除「你学校在哪儿」那句反问的关键。
    # 关键词不用「上班/上学」单独触发太宽，会把「上班吗」这类闲聊拽进来，
    # 靠「上班了」「上学了」这种带完成态的短语更准。
    'commute': {
        # 2026-10-05 补关键词。主人实测说「到小花朵学校的路线」「把小花朵学校的
        # 路线打开给我看」，原来命中不了任何场景 → 工具被收敛到 3 个核心工具
        # → LLM 看不见地图工具却回答「已打开」，纯幻觉。
        # 教训：家庭成员常去地点的路线是**高频句式**，不能只靠「上班了/上学了」
        # 这类带完成态的短词兜着，凡是「去某个已知地点」的表达都要能命中。
        'keywords': ['上班了', '上学了', '下班了', '放学了', '要出门了', '出门了',
                     '接我', '接孩子', '送孩子', '送我', '去上班', '去上学',
                     '通勤', '路程多久', '走多久', '到公司', '到学校', '到家了',
                     # ↓ 新增：各种「去某个已知地点 + 路线」的句式
                     '路线', '导航', '怎么去', '怎么走', '走哪条', '哪条路',
                     '出发', '去趟', '去一趟'],
        'tools': ['plan_commute', 'show_map', 'get_travel_info'],
    },
    # 2026-10-05 新增：地图场景。主人说「打开地图」「在地图上看看」「地图上找」
    # 时给 search_nearby（先查点）+ show_map（把图打到看板）。
    'map': {
        'keywords': ['地图', '在哪', '位置', '方位', '怎么过去', '带我去', '路线图',
                     '找找看', '定位一下', '显示位置'],
        'tools': ['search_nearby', 'show_map', 'get_travel_info', 'plan_commute'],
    },
    # 2026-10-05 新增：出行方式切换。主人说「步行去」「开车过去」「骑车去」
    # 是在地图面板打开后追加的指令，LLM 靠 show_map 的 mode 参数重画路线。
    'map_nav': {
        'keywords': ['步行去', '走着去', '走路去', '开车去', '开车过去', '驾车去',
                     '骑过去', '骑车去', '骑行去', '坐公交', '坐地铁'],
        'tools': ['show_map'],
    },
    'calendar': {
        # 2026-09-24 新增：日程场景（与 travel 联动）
        # 注意：关键词必须收紧，避免把"开灯/去客厅"等家居指令截胡
        'keywords': ['日程', '行程', '安排', '会议', '约会', '日历', '记一下', '帮我记',
                     '添加日程', '添加行程', '新建日程', '明天下午', '今天上午'],
        'tools': ['mac_calendar', 'get_travel_info', 'search_nearby', 'set_reminder',
                  'plan_commute'],
    },
    'lunar': {
        'keywords': ['农历', '黄历', '宜忌', '节气'],
        'tools': ['get_lunar'],
    },
    'health': {
        'keywords': ['健康', '血压', '血糖', '体重', '心率', '血氧', '体温', '睡眠', '步数', '身高', 'bmi', '指标',
                     '家庭报告', '健康报告'],
        # 精简到 3 个：本地 qwen3-4b 模型在 8 个工具下函数调用成功率极低，
        # 只暴露最常用的（全指标查询 / 健康建议 / 看板报告），
        # 其余健康工具仍在白名单里，只是不进场景、不塞给模型
        # 2026-09-17 修：补 close_stage，使"关闭健康报告"可关闭看板窗口
        # 2026-10-11：补 health_report_page，「详细解读」翻页讲解用
        'tools': ['get_health_status', 'check_health_alerts', 'show_health_report', 'close_stage', 'health_report_page'],
    },
    'notify': {
        # 2026-09-21 修复：ASR 常把"飞书"识别成"飞叔/飞数"，且"发一条消息"不匹配
        # "发个消息/发消息"；不命中场景时候选被收敛到核心工具 → LLM 回"没有这个功能"。
        'keywords': ['飞书', '飞叔', '飞数', '飞輸',
                     '发条消息', '发个消息', '发一条消息', '发条飞书', '发消息', '给飞书',
                     '发微信', '通知我', '告诉我一声', '留言'],
        'tools': ['send_feishu_message'],
    },
    # 2026-09-21 新增：查飞书消息（读主人私聊最近消息）
    'feishu_read': {
        'keywords': ['查飞书', '看飞书', '飞书消息', '飞书的消息', '飞书有什么', '有没有飞书', '飞书里',
                     '查一下飞书', '看一下飞书', '看下飞书', '飞书最近', '飞书有没有',
                     '查飞叔', '查飞数', '飞叔有什么', '飞数有什么'],
        'tools': ['read_feishu_messages'],
    },
    'camera': {
        'keywords': ['摄像头', '监控', '画面', '看看门口', '视频', '三楼', '3楼', '办公室', '二楼', '2楼'],
        'tools': ['show_camera_view', 'close_stage'],
    },
    # 2026-09-17 修：原先关键词只有"关闭窗口/关闭当前/关掉窗口/关闭页面"这类
    # 泛化说法，用户实际说"关闭相册""关闭健康报告""关闭新闻"时匹配不到 →
    # LLM 无 close_stage 可用，看板窗口关不掉。这里补齐"关闭+各窗口名"组合。
    'stage_close': {
        'keywords': ['关闭窗口', '关闭当前', '关掉窗口', '关闭页面', '关掉监控', '关闭监控',
                     '关闭相册', '关掉相册', '关闭照片', '关掉照片',
                     '关闭健康', '关闭报告', '关掉报告', '关闭健康报告',
                     '关闭新闻', '关掉新闻', '关闭资讯', '关掉资讯',
                     '关闭摄像头', '关掉摄像头', '关闭画面',
                     # 2026-10-05 补地图：主人会说「关掉地图」「关闭这个窗口」
                     '关闭地图', '关掉地图', '地图关掉', '收起地图',
                     # 2026-10-07 补菜谱
                     '关闭菜谱', '关掉菜谱', '关闭食谱', '关掉食谱',
                     '关掉吧', '关掉它', '关掉这个', '把它关掉',
                     # 2026-10-05 补：主人原话是「关闭这个窗口」，原关键词表里没有这几种说法，
                     # 导致命中不到 stage_close 场景，LLM 看不到 close_stage，窗口关不掉。
                     '关闭这个窗口', '关掉这个窗口', '关闭这个', '把这个关掉',
                     '关上', '隐藏窗口', '退出窗口'],
        'tools': ['close_stage'],
    },
    'album': {
        'keywords': ['相册', '照片', '相片', '家庭相册', '看看照片'],
        # 2026-09-17 修：补 close_stage，使"关闭相册"时关闭工具与展示工具同候选
        'tools': ['show_album', 'close_stage'],
    },
    'search': {
        # 2026-10-07：「上网」类口令同时给「搜索」和「读取正文」两个工具，
        # 由 LLM 按语义选：给了网址 → web_read；只是要搜信息 → web_search。
        'keywords': ['上网', '网上', '搜索', '搜一下', '帮我查', '查一下',
                     '这个网页', '这篇文章', '这个链接', '网页内容', '读网页',
                     '打开这个链接', '看看这篇'],
        'tools': ['web_search', 'web_read', 'web_search_read', 'web_view'],
    },
    # 2026-09-22 新增：邮件读写（apple-mail MCP，驱动本机邮件 App；多账户自动覆盖）
    'mail': {
        'keywords': ['邮件', '邮箱', '收件箱', '查邮件', '看邮件', '读邮件', '邮件里', '新邮件',
                     '未读邮件', '发邮件', '回邮件', '回复邮件', '收邮件'],
        # 2026-09-28 主人裁决收敛：51个邮件MCP工具收敛为 mail_action 单入口
        # （底层工具保留执行能力但不对LLM可见，场景里只给统一入口即可）
        'tools': ['mail_action'],
    },
    # 2026-10-07 新增：做菜 / 菜谱场景。用户说「红烧肉怎么做」「给我看看番茄炒蛋的做法」
    # 「我想做可乐鸡翅」「XX需要什么食材」时给 cook_recipe。
    # 关键词既收「菜谱/食谱/做法」这类名词，也收「怎么做/怎么烧/怎么炒」这类问法，
    # 以及「炒个/炖个/煮个」这类点菜句式。
    'cooking': {
        'keywords': ['菜谱', '食谱', '做法', '怎么做', '怎么烧', '怎么炒', '怎么炖', '怎么煮',
                     '怎么蒸', '怎么焖', '怎么做菜', '教我做', '教我烧', '教我炒',
                     '食材', '配料', '下厨', '做菜', '做饭', '炒个', '炖个', '煮个',
                     '蒸个', '焖个', '凉拌', '红烧', '清蒸', '爆炒', '煲汤', '做道',
                     '想做', '我要做', '晚上吃什么', '今天吃什么',
                     # 2026-10-07 陪做翻页：菜谱打开后说"下一页/上一页"要能命中本场景
                     '下一页', '上一页', '下一步', '上一步', '翻页', '翻一页', '翻一步',
                     '往下翻', '往前翻', '翻回去', '返回上一步', '回到上一步', '继续',
                     # 2026-10-07 跳步：主人会说"翻到第四步""跳到第3步"
                     '翻到', '跳到', '看第', '到第'],
        'tools': ['cook_recipe', 'recipe_page', 'show_recipe'],
    },
}

# 多轮对话场景记忆：语音交互里用户常以短句跟进（"三楼的""对""就是他"），
# 这类短句匹配不到任何关键词，但意图仍属于上一轮的场景。
# 规则：本轮没匹配到场景 + 上一条指令在 120 秒内 + 本轮不超过 12 字
# → 继承上一轮的场景工具。
_SCENE_MEMORY = {'scene': None, 'time': 0.0}
_SCENE_MEMORY_TTL = 120.0
_SCENE_FOLLOWUP_MAX_LEN = 20

# 网页端设备 id 前缀（浏览器 terminal 页，含 MacBook / iPad 浏览器）
BROWSER_DEVICE_PREFIX = 'web-terminal-'


def _device_music_tool(device_id: str = None) -> str:
    """按设备类型决定音乐工具（2026-09-17 主人定案）。

    网页端（MacBook / iPad 浏览器）→ mac_music（本机 Apple Music）
    硬件端（ESP32 等）→ ha_music（房间音箱）

    依据：网页端用户就坐在电脑/iPad 前，播放音乐理应落在本机；
    硬件端设备本身就是房间里的语音终端，理应驱动房间音箱。
    在过滤层做物理隔离，LLM 根本看不到另一个工具，杜绝误选。
    """
    if device_id and str(device_id).startswith(BROWSER_DEVICE_PREFIX):
        return 'mac_music'
    return 'ha_music'

# 核心工具（始终加载）
CORE_TOOLS = [
    'handle_exit_intent',
    'change_role',
    'close_stage',        # 随时可关闭中央舞台窗口
    # 2026-10-05 新增：plan_commute 常驻。
    #   原因：主人实测「到小花朵学校的路线」这句话**命中不了任何场景**
    #   （实测：'路线' 不在 travel/map/commute 任何一个场景的关键词里），
    #   于是工具被收敛到只剩上面 3 个 —— plan_commute 和 show_map 都看不见。
    #   结果 LLM 手里没有地图工具，却仍然回答「已经帮你打开了」，
    #   **这是纯幻觉**：既没有工具、也没有地图，屏幕上只有「我家」一个点。
    #   主人连问 3 次都被这样糊弄过去。
    #   把 plan_commute 放进常驻核心工具后，这类高频句式无论措辞怎么变
    #   都不会再让关键能力消失。它的 schema 也很小（约 1.2K 字符），
    #   常驻的 token 代价可以接受。
    'plan_commute',
]

# 工具 schema 的 token 预算：中文环境下 1 token ≈ 2 字节，
# 本地小模型（16K 上下文）装不下全量工具（23 个 ≈ 27K tokens），
# 超过预算时强制回落到核心工具，避免请求直接超限失败。
MAX_TOOL_SCHEMA_CHARS = 12000  # ≈ 6K tokens，给对话历史留余量


def filter_tools_by_query(
    all_tools: List[Dict[str, Any]],
    user_query: str,
    device_id: str = None,
    max_chars: int = None,
) -> List[Dict[str, Any]]:
    """
    根据用户输入过滤工具

    Args:
        all_tools: 所有工具的定义列表
        user_query: 用户输入文本
        device_id: 发令设备 id。以 web-terminal- 开头 = 网页端（MacBook/iPad 浏览器），
                   其余 = ESP32 等硬件端。用于按设备分流音乐工具（见 _device_music_tool）。
        max_chars: 工具 schema 大小预算（字符）。默认 MAX_TOOL_SCHEMA_CHARS（本地小模型场景）；
                   云端模型链路（如数字人用豆包）可传更大值，避免邮件等多工具场景被裁。

    Returns:
        过滤后的工具列表
    """
    if not user_query:
        return all_tools

    _budget = max_chars if max_chars else MAX_TOOL_SCHEMA_CHARS

    # 1. 提取工具名称到定义的映射
    tool_map = {}
    for tool in all_tools:
        if 'function' in tool and 'name' in tool['function']:
            tool_name = tool['function']['name']
            tool_map[tool_name] = tool

    # 2. 始终包含核心工具
    filtered_tools = []
    for tool_name in CORE_TOOLS:
        if tool_name in tool_map:
            filtered_tools.append(tool_map[tool_name])

    # 3. 根据关键词匹配场景
    matched_tools = set()
    matched_scenes = []
    dropped_scenes = []  # 2026-09-15：匹配了场景但工具全部缺失（如已下线），用于告警
    query_lower = user_query.lower()

    for scene, config in TOOL_SCENES.items():
        for keyword in config['keywords']:
            if keyword in user_query:
                # 匹配到场景，添加工具
                kept = 0
                for tool_name in config['tools']:
                    if tool_name in tool_map:
                        matched_tools.add(tool_name)
                        kept += 1
                if kept == 0:
                    dropped_scenes.append(scene)
                matched_scenes.append(scene)
                break  # 一个场景只添加一次

    if dropped_scenes:
        import logging
        logging.getLogger(__name__).warning(
            f"[工具过滤] 场景 {dropped_scenes} 匹配但工具已全部下线（silently dropped），"
            f"该意图将无工具可用！请同步更新 TOOL_SCENES 表"
        )

    # 3.5 设备分流音乐工具（2026-09-17 主人定案）
    # 规则：网页端（MacBook/iPad 浏览器，device_id 以 web-terminal- 开头）说播放音乐/点歌
    #      → 控制本机 Apple Music（mac_music）；
    #      硬件端（ESP32）说播放音乐 → 控制房间音箱（ha_music）。
    # 为什么在这里做而不是靠 LLM 选：两个工具描述语义天然重叠，小模型会按关键词
    #   瞎选（实测 4/4 全选错），且音箱未配置时必然失败。物理隔离 = 从根上杜绝误选。
    _MUSIC_TOOLS = ('mac_music', 'ha_music')
    if matched_tools & set(_MUSIC_TOOLS):
        prefer = _device_music_tool(device_id)
        matched_tools -= set(_MUSIC_TOOLS)
        if prefer in tool_map:
            matched_tools.add(prefer)

    # 3.6 多轮场景记忆：短句跟进继承上一轮场景
    import time as _time
    if matched_scenes:
        _SCENE_MEMORY['scene'] = matched_scenes[0]
        _SCENE_MEMORY['time'] = _time.time()
    elif (_SCENE_MEMORY['scene']
          and _time.time() - _SCENE_MEMORY['time'] < _SCENE_MEMORY_TTL
          and len(user_query.strip()) <= _SCENE_FOLLOWUP_MAX_LEN):
        # 2026-09-22 修：继承成功即续期。否则多轮短句跟进（"Yeah。"）在首次命中
        # 120 秒后突然丢失场景 → 只剩核心 3 工具 → 误选 handle_exit_intent 空承诺
        _SCENE_MEMORY['time'] = _time.time()
        for tool_name in TOOL_SCENES[_SCENE_MEMORY['scene']]['tools']:
            if tool_name in _MUSIC_TOOLS:
                # 继承场景时同样按设备分流
                tool_name = _device_music_tool(device_id)
            if tool_name in tool_map:
                matched_tools.add(tool_name)

    # 3.7 做菜场景屏蔽网页工具（2026-10-07 主人实测）
    # 用户说"查一下生日蛋糕的做法"时，既命中 search（"查一下"）也命中 cooking（"做法"），
    # 大模型会跑去 web_search + web_view 把菜谱网站的网页塞进屏幕，而不是走 cook_recipe。
    # 物理隔离：只要在做菜场景，就不给网页类工具——逼它走 cook_recipe
    # （本地库没有会自动联网抓，抓完按统一模板展示）。
    _cooking_active = ('cooking' in matched_scenes) or (
        _SCENE_MEMORY['scene'] == 'cooking'
        and _time.time() - _SCENE_MEMORY['time'] < _SCENE_MEMORY_TTL
    )
    if _cooking_active:
        for _t in ('web_search', 'web_search_read', 'web_view', 'web_read'):
            matched_tools.discard(_t)

    # 4. 添加匹配的工具（去重，避免与 CORE_TOOLS 重复——如 close_stage 既是
    #    核心工具又出现在 album/stage 等场景里）
    _existing_names = {t.get('function', {}).get('name') for t in filtered_tools}
    for tool_name in matched_tools:
        if tool_name not in _existing_names and tool_name in tool_map:
            filtered_tools.append(tool_map[tool_name])
            _existing_names.add(tool_name)

    # 5. 如果没有匹配任何场景，返回核心工具（而不是全量——
    #    全量 schema 可达 27K tokens，超出本地小模型 16K 上下文，请求会直接失败）
    #    ⚠️ 2026-10-10 修：原判断是 len(filtered_tools) <= len(CORE_TOOLS)，
    #    当 CORE_TOOLS 部分工具不在册（如 change_role/plan_commute 已下线，在册仅 2 个）
    #    且场景恰好只匹配 2 个工具时（如 album = show_album + close_stage），
    #    2+2=4 <= 4 会误触发兜底，把刚匹配到的场景工具丢掉，
    #    导致「打开家庭相册」时 LLM 看不见 show_album（实测踩坑）。
    #    正确语义：只看本轮有没有匹配到场景工具。
    if not matched_tools:
        filtered_tools = [tool_map[name] for name in CORE_TOOLS if name in tool_map]

    # 5.5 家居指令兜底：如果用户输入含家居关键词但 ha_control 不在列表里，强制加上
    # （2026-09-24 修复：数字人链路"开灯"被预算裁剪掉 ha_control，导致 LLM 说"不能控制"）
    home_keywords = ['灯', '空调', '电视', '开关', '窗帘', '亮度', '色温', '客厅', '卧室', '厨房', '卫生间']
    if any(kw in user_query for kw in home_keywords):
        if 'ha_control' not in [t.get('function', {}).get('name') for t in filtered_tools]:
            if 'ha_control' in tool_map:
                filtered_tools.append(tool_map['ha_control'])
                import logging
                logging.getLogger(__name__).info(f"[工具过滤] 家居指令兜底：强制加入 ha_control")

    # 6. 大小预算兜底：场景匹配后仍超过预算（如 HA 设备描述很长），
    #    2026-09-17 改：按「核心工具 → 优先工具 → 其余匹配工具」顺序贪心保留，
    #    逐个塞到塞不下为止。此前是超限就整体回落到核心工具，导致
    #    加了 HA MCP 音乐工具后，音乐/场景类意图会因超限而全部失效。
    import json as _json
    if len(_json.dumps(filtered_tools, ensure_ascii=False)) > _budget:
        # 优先级：设备控制与音乐播放最关键，不能被挤掉
        # （音乐工具已按设备分流，两个都列上，实际只会有其一进入 matched_tools）
        priority = ['ha_control', 'mac_music', 'ha_music',
                    'run_ai_scene', 'batch_device_control',
                    # 2026-10-05：家庭成员常去地点的路线是核心能力，
                    # 预算不够时也必须保住它，否则又会出现「LLM 看不见地图工具就瞎编已打开」
                    'plan_commute',
                    # 2026-09-22：邮件链路优先。整理邮箱依赖"搜索→批量删"，
                    # 字母序下 search-messages 排在最后，不优先会在预算裁剪时被裁掉
                    'list-accounts', 'search-messages', 'list-messages',
                    'batch-delete-messages', 'delete-message', 'move-message',
                    'get-message', 'get-unread-count']
        ordered = []
        for name in priority:
            if name in tool_map and name in matched_tools:
                ordered.append(name)
        for name in sorted(matched_tools):          # 其余匹配工具，稳定顺序
            if name not in ordered:
                ordered.append(name)

        trimmed = []
        for name in CORE_TOOLS:
            if name in tool_map:
                trimmed.append(tool_map[name])
        for name in ordered:
            if name in [t['function']['name'] for t in trimmed]:
                continue
            candidate = trimmed + [tool_map[name]]
            if len(_json.dumps(candidate, ensure_ascii=False)) > _budget:
                continue
            trimmed.append(tool_map[name])
        filtered_tools = trimmed
        import logging
        logging.getLogger(__name__).info(
            f"[工具过滤] 超预算（>{_budget} 字符）裁剪后保留 {len(trimmed)} 个工具: "
            f"{[t['function']['name'] for t in trimmed]}"
        )

    return filtered_tools


def get_tool_size_info(tools: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    获取工具列表的大小信息（用于调试）

    Args:
        tools: 工具列表

    Returns:
        大小信息字典
    """
    import json
    total_size = len(json.dumps(tools, ensure_ascii=False))
    tool_count = len(tools)

    return {
        'total_size': total_size,
        'tool_count': tool_count,
        'avg_size': total_size // tool_count if tool_count > 0 else 0,
    }
