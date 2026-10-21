# -*- coding: utf-8 -*-
"""在家庭看板中央窗口里显示地图

用户说"我家附近最近的饭店""打开去xxx的路线""在地图上看看附近的理发店"时，
LLM 先调 search_nearby 拿到带坐标的 POI，再调本工具把地图嵌到看板中央窗口。

实现方式：向所有已连接的看板（Web 终端）广播 map_view 指令，
看板用 iframe 嵌入高德地图页面（uri.amap.com，免 key，实测可嵌套）。

[2026-10-05] 设计取舍（重要，别改回）：
    为什么不自己用 JS API 画图，而用 iframe 嵌高德官方页面？
      1. iframe 嵌 ditu.amap.com 实测可用（无 X-Frame-Options 限制），
         而 uri.amap.com 的 JS API Key 需要单独申请、还要配域名白名单；
      2. iframe 里的地图是高德官方页面，稳定、样式统一、无需自己维护渲染；
      3. 代价是「一张图同时画多条路线」做不到（高德 URI 的 from/to 各只支持 1 个点）。
    所以：多点标注用 marker URI（可标多个点），单条路线用 navigation URI。
    如果将来主人要求「3 条路线并排显示」，才需要申请 JS API Key 改成自绘。
"""
import json
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.api.config_api import get_websocket_server_instance

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 出行方式 → 高德 navigation URI 的 mode 参数
# [2026-10-05] 骑行（ride）已移除：高德 URI 文档明确写「骑行仅在移动端有效」，
# 实测 PC 上传 mode=ride 会被当成驾车（页面标题显示「开车去」）。
# 主人要骑行导航时改为回落到 walk，并明确告知。
MODE_MAP = {
    "car": "car",
    "drive": "car",
    "驾车": "car",
    "开车": "car",
    "walk": "walk",
    "walking": "walk",
    "步行": "walk",
    "走路": "walk",
    "ride": "walk",
    "骑行": "walk",
    "骑车": "walk",
    "自行车": "walk",
    "bus": "bus",
    "公交": "bus",
    "transit": "bus",
}

# 短途（米）用步行导航，超过则用驾车。
#
# [2026-10-05] 主人实测反馈：「点步行就出来导航了，但点驾车出不来，提示距离太近」。
# 根因：高德 PC 页面在短距离时会判定「步行更快」，从而**不展示驾车路线方案**，
# 而步行则是 turn-by-turn 逐步指引（向西 417 米 → 右转 → 向北），一定有内容。
# 参考数据（高德 Web 服务 direction 接口实测）：
#     678 米：驾车 4.2 分钟 / 步行 9.0 分钟
#     25.9 公里：驾车 45.6 分钟 / 步行 330 分钟
# 所以 1 公里以内直接用步行最稳妥：既有导航可看，步行也确实合理。
WALK_THRESHOLD_M = 1000


SHOW_MAP_DESC = {
    "type": "function",
    "function": {
        "name": "show_map",
        "description": (
            "在家庭看板屏幕中央的窗口里显示地图。\n"
            "【什么时候用】用户说「打开地图」「把地图展示出来」「在地图上看看」"
            "「我家附近有什么」「最近的饭店/理发店/加油站/药店在哪」这类话时。\n"
            "【两种用法】\n"
            "1) 用户只说要看地图、没说找什么 → **直接把 places 留空调用本工具**"
            "（不要反问用户，也不要因为拿不到坐标就不调用），系统会自动以我家为中心打开地图。\n"
            "2) 用户问附近某类地点 → 先调 search_nearby，再把结果传进 places："
            "第一个元素放「我家」，其余放商家（带名称、经纬度、距离）。\n"
            "【重要·不要漏掉】用户提到「地图」「附近」「最近的」等词时，你必须调用本工具，"
            "不能只用语音回答却不动画面 —— 主人是看着屏幕用的。\n"
            "【说话方式】屏幕上已经显示了名称和距离，你只用一句话说结论就行"
            "（例如「最近的是老王拉面，大约 400 米」），**不要把每家的名称距离都念一遍**。\n"
            "用户说「关掉地图」「关闭这个窗口」时用 close_stage，不用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "places": {
                    "type": "array",
                    "description": (
                        "要标注的点。第一个元素请放我家（type=home），"
                        "其余放商家（type=poi）。最多 9 个商家。\n"
                        "⚠️ **只在你刚从 search_nearby 拿到这些商家的坐标时，才可以填 places**。"
                        "除此之外**绝对不要自己编经纬度** —— 你并不真的知道任何地点的经纬度，"
                        "编出来的数字会让地图指错位置（实测把 15 公里外的学校标到了 0.6 公里外）。"
                        "凡是凭地名指路（去学校、去公司、去某个家庭常去地点），"
                        "请改用 place_names 参数或 plan_commute 工具。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "地点名称，如「我家」「老王拉面」"},
                            "lng": {"type": "number", "description": "经度，**只能来自 search_nearby 的返回结果**，如 116.568562"},
                            "lat": {"type": "number", "description": "纬度，**只能来自 search_nearby 的返回结果**，如 40.054108"},
                            "type": {
                                "type": "string",
                                "enum": ["home", "poi"],
                                "description": "home=我家，poi=商家。第一个必须是 home。",
                            },
                            "distance": {"type": "string", "description": "距离描述，如「417 米」，可选"},
                        },
                        "required": ["name", "lng", "lat"],
                    },
                },
                "title": {
                    "type": "string",
                    "description": "地图窗口标题，如「我家附近的餐厅」。不需要时留空。",
                },
                "place_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要显示的**地点名称**（如「小花朵学校」「爸爸的公司」），"
                        "不用你给坐标——系统会从家庭地点簿里查。"
                        "⚠️ 用户问「到某个家庭成员常去地点的路线」时，"
                        "**优先用本参数或直接改用 plan_commute**，"
                        "**绝对不要自己编造经纬度**。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["marker", "car", "walk", "bus"],
                    "description": (
                        "出行方式。marker=只标注所有点不画路线；"
                        "car/walk/bus=画「家→第2个点」的路线，分别是驾车/步行/公交。"
                        "**用户明确说了出行方式就照做**（开车去→car，走过去→walk，坐公交→bus）。"
                        "用户只说「导航到XX」没提方式时，传 car 或 walk 均可，系统会按距离兜底"
                        "（1 公里内自动步行、更远自动驾车）。"
                        "**不要传 ride**：高德 URI 在电脑端不支持骑行，会被当成驾车。"
                    ),
                },
            },
            "required": [],
        },
    },
}


@register_function("show_map", SHOW_MAP_DESC, ToolType.SYSTEM_CTL)
async def show_map(conn: "ConnectionHandler", places: list = None, title: str = "",
                   mode: str = "marker", place_names: list = None):
    server = get_websocket_server_instance()
    if server is None:
        logger.bind(tag=TAG).warning("[地图] WebSocket 服务实例未就绪，无法广播")
        return ActionResponse(
            Action.REQLLM,
            f"{MAP_NOT_SHOWN}看板连接服务还没有启动，地图没地方显示。请确认看板页面已打开。",
            None,
        )

    points = _normalize_places(places)

    # [2026-10-05] LLM 会在 places 里编造经纬度（实测把「汤圆学校」标到离家 0.6 公里处），
    # 这里用家庭地点簿做一道硬纠正。只改地点簿里查得到的点，
    # search_nearby 拿到的真实 POI 结果不受影响。
    if points:
        points = await _correct_fabricated_coords(points)

    # [2026-10-05] 新增 place_names：让 LLM 只说地名，坐标由服务端查家庭地点簿。
    #
    # 为什么必须有这个参数：show_map 的 places 要求 LLM 自己给经纬度，
    # 但 LLM 根本不知道这些数（也不该知道）。于是「到小花朵学校的路线」这类请求，
    # LLM 只能传空 places → 兜底显示「我家」一个点 → 没有学校、没有路线，
    # 而它还回答「已经帮你打开了」（主人实测连问 3 次都是这样）。
    # 有了 place_names，即使 LLM 挑了本工具而不是 plan_commute，也能画出真路线。
    if not points and place_names:
        home_loc = _get_home_location()
        resolved = _places_by_name(place_names, home_loc)
        if resolved:
            # [2026-10-05] 从地名解析出来的点只有坐标、没有路程，
            # 这里补一次高德真实驾车路程，否则地图标签和伊娃播报会和
            # 高德画的路程对不上（主人实测 14.3 公里 vs 21 公里）。
            resolved = await _fill_real_route_distance(resolved)
            points = resolved
            if not title:
                title = f"路线 · 去{points[-1]['name']}"

    if not points:
        # [2026-10-05] places 为空 = 用户只想「看看地图」而没指定地点。
        # 这种情况下后端自动填入我家坐标，把地图打开 —— 否则 LLM 因为拿不到
        # 我家坐标（它并不知道这个数）会直接放弃调用，表现为「没搭理用户」。
        # 主人实测反馈：「我说让他把地图展示出来，结果他没搭理我」。
        home_loc = _get_home_location()
        if not home_loc:
            return ActionResponse(
                Action.REQLLM,
                "还没有设置家庭地址，所以没法在地图上定位。请先在管理后台的"
                "「房屋状态」里填一次家庭地址。",
                None,
            )
        points = [{"name": "我家", "lng": home_loc[0], "lat": home_loc[1],
                   "type": "home", "distance": ""}]

        # [2026-10-05] 重要：这里只画出了「我家」一个点，是**没有路线**的。
        # 主人实测：「到小花朵学校的路线」这句话没命中任何场景，LLM 手里没有
        # plan_commute，于是调了本工具却传不出坐标（它并不知道经纬度），
        # 结果屏幕上只有「我家」一个定位点、没有学校、也没有路线，
        # 而它还回答「已经帮你打开了」。
        # 所以这里必须明确告诉 LLM：这是一个空的兜底地图，
        # 你没有指定任何地点，**不要**说「已打开某某的路线」。
        return ActionResponse(
            Action.REQLLM,
            f"已打开以「我家」为中心的地形图（只是兜底显示，**没有任何指定地点、"
            f"也没有路线**）。\n"
            f"⚠️ 你这次没有传 places，说明你并不知道用户要去哪的坐标。"
            f"如果用户要的是家庭成员常去地点（公司/学校/亲友家）的路线，"
            f"**必须改用 plan_commute 工具**，它 destination 传「学校」「公司」这类词即可，"
            f"坐标由服务端查家庭地点簿，你不需要知道经纬度。\n"
            f"现在请告诉用户「我打开的是咱家位置，你要去看哪里？」，"
            f"**绝对不要说**「已经帮你打开到某某的路线了」—— 屏幕上根本没有那条路线。",
            None,
        )

    # [2026-10-05] 先过 MODE_MAP（骑行→步行），再按距离智能选实际方式
    requested = MODE_MAP.get((mode or "marker").lower(), "marker")
    resolved_mode = _auto_mode(points, requested)
    url = _build_url(points, resolved_mode)

    async with server.connections_lock:
        conns = list(server.connections) if hasattr(server, "connections") else list(server.active_connections)
    payload = json.dumps(
        {
            "type": "map_view",
            "action": "open",
            "title": title or _default_title(points),
            "url": url,
            "mode": resolved_mode,
            "places": [
                {"name": p["name"], "lng": p["lng"], "lat": p["lat"],
                 "type": p["type"], "distance": p.get("distance", ""),
                 "duration": p.get("duration", ""),
                 # [2026-10-05] 把地址一起下发：前端 focusMapPoi 切换路线时
                 # 也要自己拼高德 URL，必须用真实地址而不是我们的口语称呼
                 "addr": p.get("addr", "")}
                for p in points
            ],
        },
        ensure_ascii=False,
    )
    sent = 0
    fail = []
    for ws in conns:
        try:
            await ws.send(payload)
            sent += 1
        except Exception as e:
            # [2026-10-05] 原来这里 except: pass 把失败原因吞掉了，导致排查时
            # 只看到「sent=0」却不知道是连接不存在还是发送失败。现在记下来。
            fail.append(f"{type(e).__name__}: {e}")
            logger.bind(tag=TAG).warning(f"[地图] 广播失败：{type(e).__name__}: {e}")

    # [2026-10-05] 「送达」的判定必须看**有屏终端**，不能只看连接数。
    #
    # 踩过的坑（主人实测）：「把路线打开给我看」，伊娃语音回「已经打开了」，
    # 但屏幕上什么都没有。原因就是这个 sent 计数把**管理台页面**也算进去了 ——
    # 管理台也连着同一条 WebSocket（device-id=admin-console），收到 map_view 后
    # 根本没有处理它的代码，但它让 sent>0，于是服务端宣称「已显示」。
    # 同理，ESP32 / ReSpeaker 这些无屏设备收到也没用，它们没有地图窗口。
    # 只有 web-terminal- 开头的看板页面才会渲染中央窗口。
    screen_conns = [ws for ws in conns if _is_screen_ws(ws)]
    sent_screen = 0
    for ws in screen_conns:
        try:
            await ws.send(payload)
            sent_screen += 1
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[地图] 看板广播失败：{type(e).__name__}: {e}")

    logger.bind(tag=TAG).info(
        f"[地图] 地图指令已广播（连接 {len(conns)} 个，其中看板 {len(screen_conns)} 个；"
        f"送达 {sent} 个，看板送达 {sent_screen} 个；{len(points)} 个点，mode={resolved_mode}）"
        + (f"，失败原因：{fail[0]}" if fail else ""))
    if sent_screen == 0:
        if sent > 0:
            # 有连接、但一个能显示地图的看板都没有 —— 必须说清楚，
            # 否则 LLM 会照着「已经打开了」播报，用户白等一场。
            return ActionResponse(
                Action.REQLLM,
                f"{MAP_NOT_SHOWN}当前没有打开着的家庭看板屏幕，地图不会出现在任何屏幕上。"
                f"请明确告诉用户「看板页面现在没开，地图没法显示，请先打开看板」，"
                f"**不要**说「已经打开地图了」。",
                None,
            )
        return ActionResponse(
            Action.REQLLM,
            f"{MAP_NOT_SHOWN}家庭看板目前不在线，地图没地方显示。请确认看板页面已打开。",
            None,
        )

    return ActionResponse(Action.REQLLM, _reply_text(points, screen_conns), None)


# 有屏终端的 device_id 前缀（网页端 MacBook / iPad 浏览器）。
# 与 tool_filter.py / mac_control 里的约定保持一致。
_SCREEN_DEVICE_PREFIX = "web-terminal-"

# 「地图没有真的显示出去」时，返回文本前缀带这个标记。
#
# [2026-10-05] 存在的意义：让 plan_commute 能可靠地判断 show_map 到底有没有成功，
# 而不用去猜返回文本里有没有「不在线」这几个字。plan_commute 以前直接丢弃
# show_map 的返回值、无条件对 LLM 说「路线已经显示在看板上」，
# 于是没有看板时它照样说已显示 —— 主人实测「回复已经打开了，实际没有」。
# 判断失败请用这个常量，不要匹配中文字符串。
MAP_NOT_SHOWN = "【地图未显示】"


def _is_screen_ws(ws) -> bool:
    """这个 WebSocket 连接是不是「有屏终端」（网页浏览器看板）

    [2026-10-05] 新增。判断「地图有没有真的显示出去」必须用这个，
    不能只数连接数 —— 管理台页面（device-id=admin-console）和无屏硬件设备
    （ReSpeaker / ESP32）也连着同一条 WebSocket，它们收到 map_view 也不会渲染任何东西。
    """
    handler = getattr(ws, "_homebrain_handler", None)
    device_id = (getattr(handler, "device_id", None) or "") if handler else ""
    return str(device_id).startswith(_SCREEN_DEVICE_PREFIX)


def _has_screen_device(conns) -> bool:
    """在线连接里是否有「有屏」终端（网页浏览器看板）

    [2026-10-05] 主人定案：有屏终端以屏幕显示为主，不该把列表念一遍；
    无屏终端（ReSpeaker、ESP32 等）才需要语音播报。
    判定沿用项目既有约定：device_id 以 web-terminal- 开头 = 网页端 = 有屏。
    """
    return any(_is_screen_ws(ws) for ws in (conns or []))


def _reply_text(points: list, conns) -> str:
    """给 LLM 的话术：有屏只说结论，无屏才给完整播报内容

    [2026-10-05] 主人反馈：「有屏版本应该直接在屏幕上显示，不要语音播报一圈」。
    地图面板上已经画了编号 + 名称 + 距离，语音再念一遍是重复信息。
    """
    pois = [p for p in points if p["type"] == "poi"]
    if not pois:
        return "已把地图显示到家庭看板上了。"
    nearest = pois[0]

    if _has_screen_device(conns):
        # 有屏：只给结论，不复述列表（屏幕上已经全都写着）
        dist = nearest.get("distance")
        dur = nearest.get("duration")
        if dist and dur:
            how = f"（{dist}，{dur}）"
        elif dist:
            how = f"（{dist}）"
        else:
            # [2026-10-05] 拿不到路程就**不要编**。
            # 这里必须让 LLM 改用 plan_commute 拿真实路程，
            # 而不是让它拿直线距离或「很近」糊弄过去 ——
            # 主人实测过「伊娃说 14.3 公里、地图上写 21 公里」这种对不上的情况。
            how = ""
        tail = how or ""
        extra = (
            ""
            if how
            else "⚠️ 本次**没能拿到路程数据**（距离/时间都查不到），"
                 "你**不要**报任何距离数字，也**不要**说「很近」。"
                 "如果要告诉用户距离和时间，请改用 plan_commute 工具。"
        )
        return (
            f"已把地图显示在家庭看板上，{len(pois)} 个地点都标了编号和距离。{extra}"
            f"请只用一句话告诉用户最近的是{nearest['name']}{tail}，"
            f"**不要把每家的名称和距离念出来**，屏幕上已经有了。"
        )

    # 无屏：给完整播报内容，让 LLM 念给用户听
    return f"已把地图显示出来了。{_describe(points)}"


def _normalize_places(places) -> list:
    """校验并整理点位

    [2026-10-05] LLM 传的数据不一定干净，这里做几层防御：
      - 经纬度必须是合法数字且在中国境内合理范围（过滤 0,0 和明显错值）
      - 名字要非空（LLM 可能只给了坐标）
      - 第一个点没有标记 type 时按 home 处理（LLM 常常忘记写）
      - 高德 marker URI 最多 10 个点，超了截断
    """
    if not isinstance(places, list):
        return []

    out = []
    for i, p in enumerate(places):
        if not isinstance(p, dict):
            continue
        try:
            lng = float(p.get("lng"))
            lat = float(p.get("lat"))
        except (TypeError, ValueError):
            continue
        # 经纬度合法范围（中国境内），顺便挡掉 0,0 这种默认值
        if not (72.0 <= lng <= 136.0 and 3.0 <= lat <= 54.5):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            name = "我家" if i == 0 else f"地点{i}"
        ptype = p.get("type") if p.get("type") in ("home", "poi") else ("home" if i == 0 else "poi")
        out.append({
            "name": name,
            "lng": round(lng, 6),
            "lat": round(lat, 6),
            "type": ptype,
            "distance": str(p.get("distance") or "").strip(),
            # [2026-10-05] 保留地址字段：给高德 URI 用。
            # 高德不认我们自己起的口语名（「汤圆学校」），只认真实地址，
            # 传错名字会让它重新搜 POI 并覆盖掉我们给的正确坐标（主人实测踩到）。
            "addr": str(p.get("addr") or p.get("address") or "").strip(),
        })
        if len(out) >= 10:  # 高德 marker URI 硬上限
            break
    return out


async def _correct_fabricated_coords(points: list) -> list:
    """纠正 LLM 编造的经纬度，改用家庭地点簿里的真实坐标

    [2026-10-05] 新增。起因是主人实测踩到的严重问题：
        问「打开地图看看汤圆学校」，屏幕上确实弹出了地图，
        但地图上标的「汤圆学校」位置离我家只有 0.6 公里 —— 完全错了。
        查后端日志才发现：LLM 自己传了坐标
        `{'name':'汤圆学校','lng':116.573025,'lat':40.005975}`，
        而汤圆学校真实坐标是 116.737696,40.03968。**那串数是它编的。**

    为什么它会编：show_map 的 places 参数 schema 里写着 `required: ["name","lng","lat"]`，
    等于在逼 LLM 给出它根本不可能知道的数。它没有真实坐标知识，只能瞎猜一个看起来合理的。
    光改提示词没用 —— LLM 的行为不可控，必须在代码层面兜住。

    修法（只改「地点簿里查得到」的点，不误伤真实数据）：
        1) 名字能在家庭地点簿里查到（resolve_place / 名称包含匹配），
           但和它传的坐标差超过 200 米 → 判定为编造，用地点簿真实坐标覆盖；
        2) 家（type=home）的坐标一律用 get_house_location()，不给 LLM 自由发挥的余地；
        3) 名字不在地点簿里的点**原样保留** —— 那些多半来自 search_nearby
           的高德真实查询结果，不能动。

    200 米阈值：地点簿坐标是人工确认过的，差 200 米以上就是两个不同的地方；
    差几米则是同一地点的不同表述方式，不必改。
    """
    if not points:
        return points

    home_loc = None
    try:
        home_loc = _get_home_location()
    except Exception:
        home_loc = None

    # 家庭地点簿：名字 → 真实坐标（一次性建索引）
    book = {}
    try:
        from core.utils.place_manager import list_places
        for p in list_places():
            nm = (p.get("name") or "").strip()
            if nm and p.get("lng") is not None and p.get("lat") is not None:
                book[nm] = p
    except Exception:
        book = {}

    def _lookup(name: str):
        """精确 → 包含 双向匹配地点簿"""
        if not name:
            return None
        if name in book:
            return book[name]
        for nm, p in book.items():
            if name in nm or nm in name:
                return p
        return None

    fixed = 0
    for p in points:
        # 家：强制用服务端坐标
        if p.get("type") == "home":
            if home_loc:
                p["lng"], p["lat"] = home_loc[0], home_loc[1]
            continue

        real = _lookup(p.get("name"))
        if not real:
            continue  # 不在地点簿 → 假设是 search_nearby 的真实结果，放行

        try:
            gap = _distance_m(p, {"lng": float(real["lng"]), "lat": float(real["lat"])})
        except Exception:
            continue
        if gap <= 200:
            continue  # 基本一致，不必动

        logger.bind(tag=TAG).warning(
            f"[地图] LLM 编造了「{p['name']}」的坐标"
            f"（{p['lng']},{p['lat']}，与真实地点相差 {gap/1000:.1f} 公里），已用地点簿真实坐标纠正"
        )
        p["lng"] = round(float(real["lng"]), 6)
        p["lat"] = round(float(real["lat"]), 6)
        # 地址换成真实的：高德不认口语名，地址错了会把它拽到同名 POI 上
        addr = (real.get("address") or "").strip()
        if addr:
            p["addr"] = addr
        # 编造坐标时 LLM 附带的距离也是编的，必须清空等真实数据填
        p["distance"] = ""
        p["duration"] = ""
        fixed += 1

    if fixed:
        logger.bind(tag=TAG).info(f"[地图] 已纠正 {fixed} 个编造的坐标")
        # [2026-10-05] 纠正后那些点原本的距离是被编造的（已在上面清空），
        # 现在用高德真实驾车路程补回来 —— 否则标签上会光秃秃没有距离。
        await _fill_real_route_distance(points)
    return points


def _amap_label(p: dict) -> str:
    """给高德 URI 用的地点标识：**优先用真实地址，其次才用名称**。

    [2026-10-05] 主人实测发现的关键问题：
        地点簿里存的是「汤圆学校」，传给高德的终点名也是「汤圆学校」。
        但**高德不认这个名字** —— 它会拿这个名字去重新搜 POI，搜不到就乱匹配，
        反而把我传进去的**正确坐标覆盖掉**。
        主人实测汤圆学校那一条：坐标本来是准的（就落在「北京启智教培中心」），
        可屏幕上显示的却不是那个位置 —— 正是因为被名字带偏了。

    主人原话：「把实际的地址写进去，高德是准确的，它也会给出一个准确的路线」。

    所以规则是：传地址。地址是客观的、高德认得；名称是我们自己起的口语称呼，
    高德可能压根不认。
    """
    addr = (p.get("addr") or p.get("address") or "").strip()
    if addr:
        return addr
    return (p.get("name") or "").strip()


def _build_url(points: list, mode: str) -> str:
    """拼高德 URI API 地址（免 key，实测可直接用）"""
    if mode == "marker":
        markers = "|".join(f"{p['lng']},{p['lat']},{_amap_label(p)}" for p in points)
        return (f"https://uri.amap.com/marker?markers={quote(markers, safe='')}"
                f"&src=homebrain&callnative=0")

    # 路线模式：从第 1 个点（通常是家）到第 2 个点（通常是最近那家）
    if len(points) < 2:
        markers = "|".join(f"{p['lng']},{p['lat']},{_amap_label(p)}" for p in points)
        return f"https://uri.amap.com/marker?markers={quote(markers, safe='')}&src=homebrain&callnative=0"

    a, b = points[0], points[1]
    frm = f"{a['lng']},{a['lat']},{_amap_label(a)}"
    to = f"{b['lng']},{b['lat']},{_amap_label(b)}"
    return (
        f"https://uri.amap.com/navigation"
        f"?from={quote(frm, safe='')}"
        f"&to={quote(to, safe='')}"
        f"&mode={mode}&policy=1&src=homebrain&callnative=0"
    )


def _default_title(points: list) -> str:
    """按点位内容生成默认标题"""
    pois = [p for p in points if p["type"] == "poi"]
    if not pois:
        return "地图"
    if len(pois) == 1:
        return f"地图 · {pois[0]['name']}"
    return f"我家附近的 {len(pois)} 个地点"


def _places_by_name(names: list, home_loc) -> list:
    """按地点名称从家庭地点簿查坐标，组装成 show_map 的 points

    [2026-10-05] 新增。目的：让 LLM 不需要知道经纬度也能画出真路线。
    「家」固定作为起点（第 1 个点，type=home），因为路线是从家出发的。
    查不到的名称会被跳过；一个都查不到就返回空列表，由调用方走兜底逻辑。
    """
    out = []
    if home_loc:
        out.append({"name": "我家", "lng": home_loc[0], "lat": home_loc[1],
                    "type": "home", "distance": ""})
    try:
        from core.utils.place_manager import list_places, resolve_place
    except Exception:
        return out

    for raw in (names or []):
        n = str(raw or "").strip()
        if not n:
            continue
        place = None
        try:
            place = resolve_place(n, None).get("place")
        except Exception:
            place = None
        # 名称直接对不上时，退一步按「地点名包含」找，避免「睿志分校」这类简称匹配不到
        if not place:
            try:
                for p in list_places():
                    if p["is_primary"]:
                        continue
                    if n in (p.get("name") or "") or (p.get("name") or "") in n:
                        place = p
                        break
            except Exception:
                place = None
        if not place or place.get("lng") is None or place.get("lat") is None:
            continue
        out.append({
            "name": place.get("name") or n,
            "lng": float(place["lng"]), "lat": float(place["lat"]),
            "type": "poi",
            # [2026-10-05] 必须带上真实地址。少了这个字段，拼给高德的 URL
            # 会退回用我们的口语名（「汤圆学校」），而高德不认这个名字，
            # 会拿名字重新搜 POI 把正确坐标覆盖掉 —— 主人实测踩过。
            "addr": (place.get("address") or "").strip(),
            # [2026-10-05] 这里的 distance 必须是**高德的驾车路程**，不能是直线距离。
            # 踩过的坑：原来用 _distance_m()（Haversine）算直线距离填进来，
            # 结果汤圆学校「家→学校」直线 14.3 公里、驾车 21.8 公里，
            # 屏幕上和伊娃嘴里都报 14.3 公里，和 iframe 里高德画的路程对不上
            # （主人实测：「导航写 21 公里，伊娃却说 14.3 公里，框里也写 14.3」）。
            # 直线距离塞进「路程」字段是误导，必须留空等 _fill_real_route_distance 填。
            "distance": "",
        })
    return out


async def _fill_real_route_distance(points: list) -> list:
    """给 points 里的 POI 补上**高德真实驾车路程**，就地修改并返回

    [2026-10-05] `_places_by_name` 只能从地点簿查到坐标，查不到路程。
    这里补一次高德 direction/driving，把「距离 + 预计时间」写进 distance 字段，
    这样地图框上的标签和伊娃的播报才和高德画的路程一致。

    拿不到就留空（宁可空着，也不要用直线距离冒充路程）。
    """
    pois = [p for p in points if p.get("type") == "poi" and p.get("distance") == ""]
    if not pois or len(points) < 2:
        return points
    try:
        from plugins_func.functions.travel_reminder.amap_client import get_amap_client
        client = get_amap_client()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[地图] 高德客户端不可用，地点标签不显示距离：{e}")
        return points
    if not client:
        return points

    for p in pois:
        try:
            o = f"{points[0]['lng']},{points[0]['lat']}"
            d = f"{p['lng']},{p['lat']}"
            route = await client.driving_direction(o, d)
            if not route:
                continue
            dist_km = route.get("distance_km")
            dur_min = route.get("duration_min")
            if dist_km is not None:
                p["distance"] = f"{round(float(dist_km), 1)} 公里"
            if dur_min:
                p["duration"] = f"{int(round(float(dur_min)))} 分钟"
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[地图] 补路程失败：{e}")
    return points


def _get_home_location():
    """取我家坐标（houses 表第一条 / places 的 is_primary 行），取不到返回 None

    [2026-10-05] show_map 需要它来支持「places 为空」的场景（用户只说
    「把地图打开」而没指定地点）。同时作为 LLM 传错坐标时的兜底。
    """
    try:
        import os
        db_path = os.path.join(os.getcwd(), "data", "devices.db")
        from core.utils.geo import get_house_location
        return get_house_location(db_path)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[地图] 读取家庭地址失败：{e}")
        return None


def _distance_m(a: dict, b: dict) -> float:
    """两点直线距离（米），Haversine 公式

    用来判断「这次导航是短途还是长途」，决定默认用步行还是驾车。
    """
    import math

    lon1, lat1 = math.radians(a["lng"]), math.radians(a["lat"])
    lon2, lat2 = math.radians(b["lng"]), math.radians(b["lat"])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


def _auto_mode(points: list, requested: str) -> str:
    """决定实际用的出行方式

    [2026-10-05] 规则（主人反馈 + 高德实测）：
      - 主人明确说了方式（步行/开车/公交）→ 就用他说的；
        说了「骑行/骑车」PC 上无效，回落到步行并告知。
      - 没指定方式（mode=marker 或空）→ 按距离智能选：
          1 公里以内用步行（高德短距离不展示驾车方案，步行必有 turn-by-turn 导航）
          1 公里以上用驾车（步行 25 公里要 5 个半小时，驾车 45 分钟）
    """
    mode = (requested or "marker").lower()
    if mode in ("walk", "car", "bus"):
        return mode
    # marker / 空 / 骑行 / 无法识别 → 智能选
    if len(points) < 2:
        return "marker"

    home = next((p for p in points if p["type"] == "home"), points[0])
    target = next((p for p in points if p["type"] != "home"), points[1])
    d = _distance_m(home, target)
    return "walk" if d <= WALK_THRESHOLD_M else "car"


def _describe(points: list) -> str:
    """给 LLM 一句话描述地图上有什么，方便它组织语音回答"""
    pois = [p for p in points if p["type"] == "poi"]
    if not pois:
        return ""
    nearest = pois[0]
    if nearest.get("distance"):
        return f"地图上标了我家和 {len(pois)} 个地点，最近的是{nearest['name']}（{nearest['distance']}）。"
    return f"地图上标了我家和 {len(pois)} 个地点。"
