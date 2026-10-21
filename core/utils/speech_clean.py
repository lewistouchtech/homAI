"""播报文本清洗（2026-10-05）

主人反馈：「它经常播报一些网址、编码之类的，应该用自然语言来播报」。

根因：工具返回的文本是给**屏幕和 LLM 看**的，里面常含
URL、百分号编码、JSON 片段、坐标等技术内容；而 `Action.RESPONSE`
这类工具结果会**原样送进 TTS**，于是 TTS 把
「https://uri.amap.com/marker?markers=116.5%2C40.0」当自然语言念了出来。

对策：在播报的唯一入口（`base.py: tts_one_sentence`）统一清洗，
而不是在每个工具里各自过滤 —— 那样既漏得慢又难维护。
"""
import re

# [2026-10-20] 表情符号剥离：人设 v3.1 要求每句回复末尾带一个情绪 emoji
# （驱动 ESP32 表情动画、网页端字幕显示），但 emoji 不能被 TTS 念出来。
# 这里只作用 N admissions「送 TTS 的文本」，屏幕显示走的是原文本，不受影响（主人裁定：网页端可以显示、不要念出来）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"    # 表情/符号/物件主段（含 21 个表意 emoji）
    "\U00002600-\U000027BF"    # 杂项符号（☀✂✅ 等）
    "\U0001F1E6-\U0001F1FF"    # 旗帜
    "\U00002B00-\U00002BFF"    # 星形/箭头装饰
    "\U0000FE00-\U0000FE0F"    # 变体选择符
    "\U0000200D"               # ZWJ 连接符
    "]+"
)

# 需要整段丢弃的行（纯技术内容，念出来毫无意义）
_DROP_LINE_PATTERNS = [
    re.compile(r"^\s*https?://", re.I),                      # 整行就是一个链接
    re.compile(r"^\s*www\.", re.I),
    re.compile(r"^\s*[\[{<]"),                                # 整行是 JSON / 数组
    re.compile(r"^\s*\{[\s\S]*\}?\s*$"),
]

# 行内要抹掉的片段（顺序敏感：markdown 链接必须在裸 URL 之前处理，
# 否则先抹掉 URL 会留下 "[文字](" 这种残骸）
_STRIP_PATTERNS = [
    # markdown 链接 [文字](url) → 只留文字
    re.compile(r"\[([^\]]{0,60})\]\([^)]*\)"),
    # 带 percent-encoding 的长 URL（高德 URI 的 markers/navigation 就是这种）
    re.compile(r"https?://\S*[%][0-9A-Fa-f]{2}\S*"),
    re.compile(r"https?://\S+", re.I),
    re.compile(r"\bwww\.\S+", re.I),
    # 裸坐标，例如 116.573944,40.01034
    re.compile(r"\b\d{2,3}\.\d{3,}\s*,\s*\d{2,3}\.\d{3,}\b"),
    # 剩余的 percent-encoding 片段
    re.compile(r"%[0-9A-Fa-f]{2}"),
    # [2026-10-05] 无 scheme 的 URL 残骸：分段把 https:// 切掉后半段的情况。
    # 形如 marker?markers=116.5&src=homebrain&callnative=0 或 uri.amap.com/xxx
    re.compile(r"\b[\w.-]+\.(?:com|cn|net|org)/[^\s，。！？]*", re.I),
    re.compile(r"\b\w+=[0-9][^\s，。！？]{4,}(?:&\w+=[^\s，。！？]*)*"),
    # markdown 强调/代码标记
    re.compile(r"[*`#>]+"),
]

# 出现这些说明是给模型看的结构化内容，不该念
_TECH_MARKERS = (
    "```", "function_call", "tool_calls", "```json", "traceback",
)

# 判断「这一行原本带链接」的探针（用于识别链接被抹掉后的残骸）
_URL_HINT = re.compile(r"https?://|www\.|%[0-9A-Fa-f]{2}", re.I)

# 这些是「给屏幕看的引导语」，念出来没有信息量（主人在屏幕上都看得到）
_DROP_LINE_TEXT = (
    "【地图】", "【导航到各店】", "【音乐】", "【相册】", "【字幕】",
    "地图（标出", "标出我家", "点开是", "链接：", "网址：",
)

# 行内出现的引导语残骸（含被切掉前半截的「】」形式）
_INLINE_DROP_TEXT = (
    "【地图】", "】标出我家", "地图】标出", "】标出",
    "【导航到各店】", "】点击查看", "点开是高德完整路线页",
)


def clean_for_speech(text: str) -> str:
    """把工具/LLM 输出清洗成适合朗读的自然语言

    设计原则：**宁可少念，也不要念错**。
    - 纯技术行（URL / JSON）整行丢掉
    - 行内技术片段（链接、坐标、编码）抹掉
    - 给屏幕看的引导语（「【地图】…」这类）丢掉
    - 剩下的中文叙述原样保留

    [2026-10-05] 真实案例：search_nearby 会返回 4 条高德 URI（带 %E6%88%91 这类
    百分号编码），过滤前 TTS 会把整串编码念出来。
    """
    if not text:
        return ""

    s = str(text)

    # [2026-10-20] 先剥 emoji，再走原有清洗（emoji 不能被念出来）
    s = _EMOJI_RE.sub("", s)

    # 明显的代码/结构化内容整体不念
    if any(m in s for m in _TECH_MARKERS):
        return ""

    lines = []
    for raw_line in s.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # 整行就是技术内容 → 丢弃
        if any(p.match(line) for p in _DROP_LINE_PATTERNS):
            continue
        # 给屏幕看的引导语 → 丢弃（主人在屏幕上已经看到）
        if any(line.strip().startswith(k) for k in _DROP_LINE_TEXT):
            continue
        # [2026-10-05] 记住这行原本带链接，抹掉后会变成「店名：」这种残骸
        had_link = bool(_URL_HINT.search(line))
        lines.append((line, had_link))

    s = "\n".join(l for l, _ in lines)

    # 收拾残局：多余空白、空行、孤立的标点
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)

    # 丢掉「原本带链接、抹掉后只剩名字」的裸条目。
    # 典型：search_nearby 的「【导航到各店】」块每行是「店名：URL」，
    # 抹掉 URL 后剩「店名：」，念出来是一串没头没尾的名字。
    # ⚠️ 必须靠 had_link 标记判断，不能靠「行很短且无数字」去猜 ——
    #    早先那么做过，结果「好的」「明白」「地图已经打开了」这类正常短句全被误删，
    #    AI 会一个字都不说。只删「确定原本有链接」的行。
    kept = []
    for (orig, had_link) in lines:
        if had_link:
            # 用完整的剥离规则模拟「这行抹掉链接后还剩什么」，
            # 只用 _URL_HINT 不够 —— 它只吃掉 https:// 头，
            # uri.amap.com/xxx 这类域名+路径会残留下来，导致误判为「有实质内容」。
            stripped = orig
            for pat in _STRIP_PATTERNS:
                stripped = pat.sub(r"\1" if pat.groups else "", stripped)
            stripped = re.sub(r"^[·\-•\s：:、,，。]+", "", stripped).strip()
            # 抹掉链接后没剩下什么实质内容 → 丢
            if len(stripped) <= 24 and not re.search(r"[\d米公里分钟，。！？]", stripped):
                continue
        kept.append(orig)

    s = "\n".join(kept)

    # 再做一次行内抹除（上面 kept 里存的是原始行）
    for pat in _STRIP_PATTERNS:
        s = pat.sub(r"\1" if pat.groups else "", s)
    s = re.sub(r"[ \t]*[：:]\s*$", "", s, flags=re.M)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = re.sub(r"^[·\-•\s]+", "", s, flags=re.M)
    s = re.sub(r"\n[·\-•\s]+", "\n", s)

    # 行内引导语残骸（「411 米地图】标出我家和这 3 家」→ 只留「411 米」）
    for k in _INLINE_DROP_TEXT:
        if k in s:
            s = s.replace(k, " ")
    s = re.sub(r"[ \t]{2,}", " ", s)

    s = s.strip()

    # 清洗后什么都不剩（例如工具只返回了链接）→ 不念，交给 LLM 补话术
    if not s or not re.search(r"[\w\u4e00-\u9fff]", s):
        return ""

    return s


def has_spoken_content(text: str) -> bool:
    """清洗后是否还有可播报内容"""
    return bool(clean_for_speech(text))
