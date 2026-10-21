"""
对话引擎模块 — 从 connection.py 拆分

职责：chat() 核心循环、工具调用、对话历史管理
"""
import json
import re
import uuid
import time
import asyncio
from collections import OrderedDict
from typing import Optional

from config.logger import setup_logging
from core.utils.dialogue import Message
from core.utils.fast_response import fast_response
from core.utils.tool_filter import filter_tools_by_query, get_tool_size_info
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from core.chat.quick_tools import quick_tool_reply

TAG = __name__
logger = setup_logging()

# 需要「每轮对话后实时保存」的向量记忆后端白名单。
# 只有这两个值得逐轮调用：它们把事实喂给 LLM 抽取，越早提交越不容易丢。
# 其余 provider（mem_local_short 自管会话）逐轮调只会产生噪音，故不在名单内。
_REALTIME_MEMORY_TYPES = ("LTM", "ltm")

TOOL_CALLING_RULES = """
<tool_calling>
实时信息（新闻/天气/股票）或执行操作（音乐/设备/拍照）时调用工具。<context>中的信息直接用。每次独立判断，不复用历史结果。
【不要调用工具的情况·重要】以下情形直接说话，**禁止**调用任何工具：
· 聊天、陪玩、做游戏（如数字炸弹、成语接龙、猜谜）、讲故事、说笑话、角色扮演；
· 用户只是陈述、感慨、提问常识或让你解释/复述某件事；
· 用户没明确要求「提醒我 / 记一下 / 定个闹钟 / 加到日程」时，不要调 set_reminder 之类工具。
反例（实测踩过）：用户说「我们继续玩刚才那个游戏吧，数字炸弹」→ 模型调了
set_reminder(title='数字炸弹游戏', time='10分钟后')，这是错的，直接接着玩游戏即可。
判断标准：用户要的是「你现在就对我说/陪我玩」还是「让系统在别处做一件事」？只有后者才调工具。
</tool_calling>
"""

# 告别意图规则层（方案#6）：LLM 实测对"再见"类短句倾向自然回复而非调用
# handle_exit_intent 工具（qwen3.7-plus 两轮复现），故用规则兜底——短句含告别词
# 且本轮无工具调用时，TTS 播完自动关连接回 idle，告别语仍由 LLM 个性化生成
FAREWELL_KEYWORDS = ("再见", "拜拜", "告辞", "不聊了", "不想聊了", "先这样", "没事了")

# C1 话术池化：LLM 处理出错时的提示，{detail} 填错误信息
LLM_ERROR_LINES = [
    "哎呀，我这边出了点状况：{detail}",
    "出了点小问题：{detail}，一会儿再试试",
    "哎，没成功：{detail}",
    "唔，出错了：{detail}",
]


# 工具失败话术池（2026-10-06）
# 主人反馈：说「播放今天的简报」，模型错调了 send_voice_message（拨打语音电话），
# PushPlus 返回 "积分不足"，这句接口原文被原样念了出来。
# 技术性报错一律换成下面这些有温度的话，原文只进日志。
TOOL_ERROR_LINES = [
    "这个我暂时做不到，等会儿再试试哈",
    "哎呀，没成功，我一会儿再试一下",
    "这个现在有点不灵，我先记着",
    "不好意思啊，这下没成功",
    "这会儿没成功，回头我再试试",
]

# 命中即视为「技术性报错」（不该念给用户听）
_TECHNICAL_ERROR_HINTS = (
    "返回错误", "Error", "error", "Exception", "Traceback",
    "HTTP", "http", "超时", "timeout", "Timeout",
    "积分不足", "未绑定", "401", "403", "404", "429", "500", "502",
    "token", "Token", "api", "API",
    # [2026-10-12 主人实测] 漏了一批 Python 侧的报错形态。
    # 现象：工具报 `'dict' object has no attribute 'strip'`，
    #   原名单里没有这些关键词 → 原文被 TTS 直接念出来，
    #   中文音素硬拼英文报错，发音像方言（主人说「有点像闽南语」）。
    # 教训：关键词是**黑名单**，天然漏；这类东西应该是**白名单**。
    "object has no attribute", "has no attribute", "'dict' object",
    "list index out of range", "KeyError", "TypeError", "ValueError",
    "AttributeError", "IndexError", "NameError", "ZeroDivision",
    "not subscriptable", "unhashable", "takes", "positional argument",
    "unsupported operand", "NoneType", "object of type",
    "Traceback (most recent call last)",
    "File \\"  # 堆栈里的文件路径行
)


# 前置过渡句总开关。置 False 可一键关掉全部过渡句（不影响其余链路）。
_LEAD_IN_ENABLED = True


def _is_farewell_query(query):
    """判定用户输入是否为告别短句（去标点后 ≤15 字且含告别词，防止长句误伤）"""
    if not query:
        return False
    from core.utils.util import remove_punctuation_and_length
    _, text = remove_punctuation_and_length(query)
    if len(text) > 15:
        return False
    return any(k in text for k in FAREWELL_KEYWORDS)


def _send_quick_reply(conn, text):
    """L1/L1.5/L1.7 快捷回复统一发送：投递 TTS 队列合成音频。
    对齐正常对话路径（FIRST→TEXT→LAST），sentence_start/stop JSON 由音频线程在
    FIRST/LAST 音频帧时自动下发；此前手动发 JSON 不投队列，导致快捷回复有字无声。

    ⚠️ 必须先清打断标志（2026-10-07）：这三条快捷路径在 chat() 里**提前 return**，
    走不到下面 depth==0 的复位动作。而「上一轮还在处理中就取消它」的修复会真的把
    client_abort 置真（主人实测「现在几点了」被回成静音就是这个），
    于是本轮的 MIDDLE/LAST 会被 TTS 文本线程当打断跳过 —— 有字幕没声音。
    """
    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )


# 2026-10-07：纯应声识别（「好嘞」「好的」「嗯」这种只表态、无信息的话）。
# 判停时若已播过垫场句，模型在调工具前再蹦一句纯应声就是重复应答 —— 丢弃。
# 刻意只匹配**纯应声**（整句就是应声词），
# 像「好嘞，我这就去找一下周杰伦的歌」这种有信息量的照常播。
_ACK_ONLY_RE = re.compile(
    r"^[\s，。！？、,.!?~～]*(?:好的|好嘞|好哒|好嘛|好呀|好呢|行|行嘞|行呀|嗯+|哦+|诶|哎|收到|"
    r"没问题|明白|知道了?|了解|OK|ok|Okay|okay|okay啦|这就|马上)[\s，。！？、,.!?~～]*$"
)


def _looks_like_ack(text: str) -> bool:
    """整句是不是「纯应声」（没有信息量）。"""
    t = (text or "").strip()
    if not t or len(t) > 12:
        return False
    return bool(_ACK_ONLY_RE.match(t))


# 【垫场句只能有一遍 2026-10-08】主人原话：「垫场句子只能有一遍，你不能有好几遍
# 垫场句子，这就感觉很奇怪。」
# 实测重复场景：垫场句「我去网上找找看哈」+ 模型自己按 prompt 要求说的
# 「好嘞，我这就查」→ 连着播就是两遍垫场。
# 纯应声（_ACK_ONLY_RE）拦不住后者，因为「好嘞，我这就查」带了点信息量。
# 所以再认一类「短促预告句」：以应声/预告词开头、且整体很短。
# 刻意限长（≤18 字）：有信息量的长句（如「好嘞，我这就去找一下周杰伦的歌」）照常播。
_PAD_ACK_PREFIX_RE = re.compile(
    r"^[\s，。！？、,.!?~～]*(?:好的|好嘞|好哒|好嘛|好呀|好呢|行|行嘞|行呀|嗯+|哦+|诶|收到|"
    r"没问题|明白|知道了?|了解|马上|稍等|稍等一下|这就|我这就|我去|我来|我给你|"
    r"等下啊?|等一下|等我|来啊?[，,]|我瞅|我翻|我刷|我看|我查|我搜|我找|我拍|我念|让我)"
)
# 「要去做点什么」的标志词：预告句才有，纯内容句没有。
_PAD_TASK_MARKERS = (
    "这就", "马上", "稍等", "去", "查", "看看", "看下", "找", "弄", "办", "处理", "试", "帮你", "给你",
    "瞅", "翻", "刷", "看一眼", "搜", "拍", "念", "等我", "等下", "想想",
)


def _looks_like_pad_duplicate(text: str) -> bool:
    """整句是不是「垫场句的重复」：纯应声，或短促的预告句。

    预告句的判据刻意收紧（应声开头 + 余下很短 + 含「要去做点什么」的标志词），
    否则会把真正的内容句误判掉。反例（必须保留）：
      「好嘞，这就继续数字大战游戏～」——余下 12 字，是正文，不是预告。
    """
    t = (text or "").strip()
    if not t or len(t) > 18:
        return False
    if _ACK_ONLY_RE.match(t):
        return True
    # 与过渡句池里任意一句完全相同（模型照抄/同源话术）也算重复
    try:
        from core.utils.lead_in import ALL_LEAD_INS
        _norm = re.sub(r"[\s，。！？、,.!?~～—]+", "", t)
        if _norm and any(_norm == re.sub(r"[\s，。！？、,.!?~～—]+", "", p) for p in ALL_LEAD_INS):
            return True
    except Exception:
        pass
    m = _PAD_ACK_PREFIX_RE.match(t)
    if not m:
        return False
    rest = t[m.end():]
    if len(rest) > 8:
        return False
    return any(k in rest for k in _PAD_TASK_MARKERS)


def _should_buffer_ack(pending, content, lead_in_spoken: bool, decided: bool) -> bool:
    """模型这段内容要不要先暂存？

    只有在「本轮已经播过垫场句」且「还没决定过」且「暂存后整段仍是垫场句的重复」
    时才暂存。暂存的意义：等看清后面有没有正文，再决定是丢掉（垫场句已应过声）
    还是补发（整轮就这一句，丢了会一个字都不说）。
    """
    if not lead_in_spoken or decided:
        return False
    return _looks_like_pad_duplicate("".join(pending) + (content or ""))


def _flush_preamble(conn):
    """把暂存的「可能是应声」内容补发出去（确认不是应声时调用）。"""
    buf = getattr(conn, "_pending_preamble_buf", None)
    if not buf:
        return
    text = "".join(buf)
    conn._pending_preamble_buf = []
    if text.strip():
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=text,
            )
        )


async def _tts_speak(conn, text: str):
    """前置钩子用的 TTS 播报（过渡语 / 衔接语 / C 类预告）。

    2026-10-06（过渡语 2.0）：原实现只发 `sentence_start` + `stop`，**不出声**——
    等于只给屏幕看字幕；真正出声的是 4 秒后的预录音（还是另一个音色）。
    现在改为用**主音色真的播出来**，字幕与语音同源，也不再发 stop
    （stop 会把前端 `_ignorePcm` 置真、并停掉流控发送器，连累紧随其后的正文音频）。
    """
    try:
        from core.utils.transition_voice import speak_transition, looks_like_template

        if looks_like_template(text):
            # 例：B 类衔接语模板 `查下{location}天气` 目前没有变量替换逻辑，
            # 念出来就是噪音，宁可不说。
            conn.logger.bind(tag=TAG).warning(f"[过渡语] 跳过未替换占位符的文案: {text!r}")
            return
        await speak_transition(conn, text)
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[speech_registry] TTS 播报失败: {e}")


def _polish_result(conn, result_dict: dict, template: str, anchors: list) -> str | None:
    """v3.0 P2.5：结果话术 LLM 润色层（锚点 + 校验回退）

    1. 用模板先填出基础句
    2. 调轻量 LLM 润色（≤1.5s 超时）
    3. 校验锚点是否原样保留
    4. 通过则返回润色句，失败/超时返回 None（回退模板）
    """
    try:
        # 1. 模板填充基础句
        base_text = template.format(**result_dict)

        # 2. 构建润色 prompt
        # [2026-10-06] 原来这里写死「你是伊娃」，等于强行告诉 LLM 它叫伊娃 ——
        # 后台改名后润色仍带旧名。改为从后台身份模块取名字（取不到用中性兜底）。
        try:
            from core.utils.assistant_identity import safe_name

            _polish_name = safe_name()
        except Exception:
            _polish_name = "家庭助手"
        anchor_str = ", ".join(f"{k}={v}" for k, v in result_dict.items() if k in anchors)
        polish_prompt = f"""你是{_polish_name}，一个知性温婉的AI管家。请把下面这句话润色得更自然口语化，但必须严格遵守：
1. 锚点（{anchor_str}）必须原样保留，一字不改
2. 只负责语序、语气、概括取舍，≤15 字口语句
3. 禁止新增锚点之外的事实
4. 禁止书面腔（"已开启"→"开了"）
5. 禁止加前缀铺垫

原句：{base_text}
润色后："""

        # 3. 调轻量 LLM（用主 LLM，超时 1.5s）
        import asyncio
        future = asyncio.run_coroutine_threadsafe(
            _call_polish_llm(conn, polish_prompt), conn.loop
        )
        polished = future.result(timeout=1.5)

        if not polished:
            return None

        # 4. 校验锚点
        for anchor in anchors:
            if anchor in result_dict:
                val = str(result_dict[anchor])
                if val not in polished:
                    conn.logger.bind(tag=TAG).warning(f"[speech_registry] 润色锚点校验失败: {anchor}={val} 不在 '{polished}' 中")
                    return None

        return polished.strip()
    except asyncio.TimeoutError:
        conn.logger.bind(tag=TAG).info("[speech_registry] 润色超时(1.5s)，回退模板")
        return None
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[speech_registry] 润色异常: {e}")
        return None


async def _call_polish_llm(conn, prompt: str) -> str:
    """调用轻量 LLM 做润色（复用主 LLM，temperature 0.3）"""
    try:
        response = await conn.llm.response(
            conn.session_id,
            [{"role": "user", "content": prompt}],
        )
        # 流式响应拼接
        text = ""
        for chunk in response:
            if isinstance(chunk, str):
                text += chunk
            elif isinstance(chunk, dict):
                text += chunk.get("content", "")
        return text
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[speech_registry] 润色 LLM 调用失败: {e}")
        return None


def _should_query_memory(conn, query: str) -> bool:
    """判断当前是否需要查询长期记忆。
    
    不查询场景：
    - 无 memory provider
    - 空 query
    - 纯指令/问候（<4字，如"开灯"、"你好"、"谢谢"）
    
    需查询场景（含关键词触发，优先级高于长度判断）：
    - 跨会话延续（"上次"、"之前"、"以前"、"你说过"、"我记得"）
    - 个性化请求（"推荐"、"喜欢"、"讨厌"、"偏好"、"习惯"）
    - 情感/个人语境（"心情"、"感觉"、"情绪"、"我家"、"我爸"、"我妈"、"我老婆"、"我老公"、"我儿子"、"我女儿"）
    - 含"我"且非纯指令词 -> 可能涉及个性化/回忆
    
    其他情况：不主动查（由语义检索自然过滤）

    ⚠️ 2026-10-06 删除了 `if conn.intent_type == "function_call": return False`。

    这道门禁是**永久关闭记忆读取**的元凶（主人实测发现）：
    - `conn.intent_type` 在 `initializer.py:179` 从配置读一次后**再也不变**
    - 而两份配置的 `selected_module.Intent` 都是 `function_call`（出厂默认）
    - 于是所有对话都判「不需要查记忆」→ **记忆能写但永远读不出来**
      实测 10 个问题（含「我以前养的猫叫什么名字」「我讨厌什么」）全部判 False，
      发给 LLM 的消息里没有【长期记忆】块。

    为什么这道门禁本来就不该存在：
    ① 工具调用不等于不需要记忆上下文。LLM 一边能调工具一边回答「我以前养的猫叫什么」，
       恰恰是最需要记忆的场景。
    ② 下面的关键词 / 长度 / 指令动词逻辑**本来就拦得住**纯指令
       （「开灯」「播放音乐」「关灯」都命中 command_verbs 或长度 <4），
       这道门禁是多余且有害的短路。
    ③ 数字人那条路（`core/api/llm_proxy_api.py:_should_query_memory_dh`）本来就没有这道判断，
       其 docstring 写着「数字人链路无意图分析环节，跳过 function_call 判断」——
       等于作者知道有这个差异，但没意识到它让主产品的记忆读取全废了。
    """
    if conn.memory is None or not query:
        return False

    q = query.strip()
    
    # 1. 明确触发关键词（优先级最高，不受长度限制）
    trigger_keywords = (
        "上次", "之前", "以前", "你说过", "我记得",
        "推荐", "喜欢", "讨厌", "偏好", "习惯",
        "心情", "感觉", "情绪",
        "我家", "我爸", "我妈", "我老婆", "我老公", "我儿子", "我女儿",
    )
    if any(kw in q for kw in trigger_keywords):
        return True
    
    # 2. 疑问词（短问句也查，如"你是谁"、"为什么"）
    question_words = ("怎么", "什么", "哪里", "谁", "为什么", "多少", "吗", "呢", "吧", "么", "啥", "咋")
    if any(w in q for w in question_words):
        return True
    
    # 3. 太短直接不查（指令/问候）
    if len(q) < 4:
        return False
    
    # 4. 含"我"但非典型指令 -> 可能需个性化
    # 典型指令词：开/关/放/播/查/搜/设置/调/打开/关闭/启动/停止
    command_verbs = ("开", "关", "放", "播", "查", "搜", "设置", "调", "打开", "关闭", "启动", "停止", "连接", "断开")
    if "我" in q and not any(cmd in q for cmd in command_verbs):
        return True
    
    # 5. 其他情况默认不查（避免无意义查询消耗资源）
    return False


def _recent_user_text(conn, current_query: str, max_chars: int = 120) -> str:
    """检索用的 query 文本：当前这句 + 上一句用户话（2026-10-13 WP-4）。

    为什么要带上一句：用户追问时经常只说「他呢」「那个呢」「还有吗」，
    单拿这几个字做向量检索必然召回不到东西。把上一句用户话拼在前面，
    指代就有了着落。只取 user 角色、跳过 is_temporary 的工具提醒，
    总长截到 max_chars，避免把整段历史塞进 embedding。
    """
    try:
        prev = []
        for m in reversed(getattr(conn.dialogue, "dialogue", []) or []):
            if getattr(m, "role", "") != "user":
                continue
            if getattr(m, "is_temporary", False):
                continue
            text = str(getattr(m, "content", "") or "").strip()
            if not text or text == (current_query or "").strip():
                continue
            prev.append(text)
            if len(prev) >= 1:
                break
        if not prev:
            return current_query
        combined = f"{prev[-1]} {current_query}".strip()
        return combined[-max_chars:]
    except Exception:
        return current_query


# ── 2026-10-13 WP-4：记忆查询结果短缓存 ──
# 原来这里是「复用上轮未完成的 future」，但**不校验那个 future 对应哪个 query**：
# 第一轮检索慢、第二轮已经换了新问题，代码照样把第一轮的旧记忆注进第二轮上下文
# （chat_engine.py 原 751-753 行 `result(timeout=0)` 直接取）。
# 现在改成：future 绑定它自己的 query；超时未完成的结果由包装协程在完成后
# 写入这个按 query 的短缓存，只有**同一个问题再问一次**才会命中。
_MEMORY_CACHE_MAX = 16
_memory_result_cache: "OrderedDict[str, str]" = OrderedDict()


async def _query_memory_guarded(memory, query: str, cache_key: str,
                                scope: Optional[str], speaker: Optional[str]) -> str:
    """查记忆并把结果放进短缓存（即使本轮已经超时放弃，结果也不浪费）。"""
    try:
        text = await memory.query_memory(query, scope=scope, speaker=speaker)
    except TypeError:
        # 兼容还没支持 scope/speaker 关键字的 provider（LTM 等）
        text = await memory.query_memory(query)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"记忆查询失败: {e}")
        return ""
    if text:
        _memory_result_cache[cache_key] = text
        _memory_result_cache.move_to_end(cache_key)
        while len(_memory_result_cache) > _MEMORY_CACHE_MAX:
            _memory_result_cache.popitem(last=False)
    return text


# 本轮等待记忆的上限。纯向量检索实测 22ms，200ms 足够；
# 原来只有 100ms，且超时后靠「下一轮盲取上轮 future」兜底（会串轮）。
_MEMORY_WAIT_SECONDS = 0.2


def _recent_context_block(conn) -> str:
    """短期上下文（L1）注入文本。任何失败都返回空串，不影响对话。"""
    try:
        from core.utils.memory_short_term import build_context_block

        return build_context_block(conn)
    except Exception:
        return ""


def _update_wake_greeting_state(conn):
    """[2026-10-19] 维护「本次唤醒是否已经叫过称呼」的状态。

    主人要求：每次唤醒后的第一句话叫一次称呼，后续轮次不要再叫。
    用 conn 级标志，避免多设备互相影响；会话结束（is_session_active() 变 False）
    后复位，下一次唤醒重新允许叫一次。任何异常都按「允许叫」处理，不影响对话。
    """
    try:
        from core.utils.wakeword_provider import get_wakeword_detector
        _wp = get_wakeword_detector()
        if _wp and _wp.is_session_active():
            _wp.extend_session()
            conn._wake_greeting_allowed = not getattr(conn, "_wake_greeting_done", False)
            conn._wake_greeting_done = True
        else:
            # 非唤醒路径（文字输入/会话已结束）视为新会话
            conn._wake_greeting_done = False
            conn._wake_greeting_allowed = True
    except Exception:
        conn._wake_greeting_allowed = True


def chat(conn, query, depth=0):
    """对话核心循环 — 处理用户消息，调用 LLM，管理工具调用"""
    if query is not None:
        conn.logger.bind(tag=TAG).info(f"大模型收到用户消息: {query}")

    # 【2026-09-24 对齐数字人链路】多轮对话滚动续期：
    # 每收到一轮用户输入（depth=0 顶层），延长唤醒会话 session_timeout 秒，
    # 用户连续说话时不会被误踢回待机，停下来超过超时时间才待机。
    if depth == 0 and query is not None:
        _update_wake_greeting_state(conn)

    # L0.5 记忆整理（2026-10-13 主人定的方案）：「把这段对话记下来」+ 随后的确认。
    # 旁路钩子：只有命中请求/正在等确认才接管；其余一律原样放行，行为同接入前。
    # 仅 ltm 后端支持（有 store_episode）；任何异常都降级走原链路。
    if depth == 0 and query is not None and hasattr(getattr(conn, "memory", None), "store_episode"):
        try:
            from core.utils.memory_curator import handle_turn as _curator_turn
            _cur_reply = _curator_turn(conn, query, lambda t: _send_quick_reply(conn, t))
            if _cur_reply:
                conn.logger.bind(tag=TAG).info(f"[记忆整理] 接管本轮: {query!r} -> {_cur_reply!r}")
                _send_quick_reply(conn, _cur_reply)
                conn.dialogue.put(Message(role="user", content=query))
                conn.dialogue.put(Message(role="assistant", content=_cur_reply))
                _save_dialogue_history(conn, query, _cur_reply)
                return _cur_reply
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[记忆整理] 旁路异常，降级原链路: {e!r}")

    # L1 规则层：快速响应（仅在最顶层 depth=0 时检查）
    if depth == 0 and query is not None:
        quick_reply = fast_response(query)
        if quick_reply:
            start_time = time.time()
            conn.logger.bind(tag=TAG).info(f"[L1规则层] 命中: '{query}' -> '{quick_reply}'")
            _send_quick_reply(conn, quick_reply)
            conn.logger.bind(tag=TAG).info(
                f"[L1规则层] 完成，延迟: {(time.time() - start_time) * 1000:.1f}ms"
            )
            return quick_reply

    # L1.5 快捷工具直答（J1：时间/日期/星期/农历零 LLM 直答，仅 depth=0 顶层检查）
    if depth == 0 and query is not None:
        tool_reply = quick_tool_reply(query)
        if tool_reply:
            start_time = time.time()
            conn.logger.bind(tag=TAG).info(f"[L1.5工具直答] 命中: '{query}' -> '{tool_reply}'")
            _send_quick_reply(conn, tool_reply)
            conn.logger.bind(tag=TAG).info(
                f"[L1.5工具直答] 完成，延迟: {(time.time() - start_time) * 1000:.1f}ms"
            )
            return tool_reply

    # L1.7 智能家居直通（P1，2026-10-02 方案：JEV 推测性扇出，云端先行）
    # 代码预筛候选 → 云端 JEV 三问判定 → 白名单内直接执行并播报，不进大模型；
    # 闲聊/无具体设备/超时/断网/异常/门锁类一律返回 None，走原链路（行为同接入前）。
    if depth == 0 and query is not None:
        try:
            from core.utils.home_intent import run_home_direct
            start_time = time.time()
            home_reply = run_home_direct(conn, query)
            if home_reply:
                conn.logger.bind(tag=TAG).info(
                    f"[L1.7家居直通] 命中并执行，耗时 {(time.time() - start_time) * 1000:.0f}ms"
                )
                _send_quick_reply(conn, home_reply)
                return home_reply
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[L1.7家居直通] 旁路异常，降级原链路: {e!r}")

    # 为最顶层时新建会话ID和发送FIRST请求
    #
    # 【2026-10-12 顺序修正 · 修过渡句被截断的竞态】
    # 原来这段在下面（过渡句投递之后）。`conn.sentence_id` 是本轮的音频流控标识：
    # 过渡句用**旧** id 发出去后这里才换新 id，回复首帧到达时 `sendAudio` 发现
    # `audio_flow_control.sentence_id != conn.sentence_id` → `AudioRateController.reset()`
    # → **队列里还没播完的过渡句被清空**。表现就是「过渡句刚起了个头就没了」。
    # 所以：**新 sentence_id 必须在过渡句投递之前就位**，让过渡句与本轮回复
    # 共用同一个 id、同一个 FIFO 队列（reply 天然排在过渡句之后，首帧即接）。
    if depth == 0:
        # 2026-09-21 修复（打断竞态）：复位打断标志必须放在 FIRST 入队之前。
        # 否则 TTS 文本线程可能先取到 FIRST（此时 client_abort 仍为 True）而将其按打断跳过，
        # 导致 tts_audio_first_sentence 无法重置、client_is_speaking 整轮保持 False，
        # 该轮长播报（新闻/工具结果）将完全无法被打断（2026-09-21 实测复现）。
        conn.client_abort = False
        conn.sentence_id = str(uuid.uuid4().hex)
        # [2026-10-18 P5，主人批准] 运行观察：本轮起点（用于算轮次耗时）
        conn._obs_turn_started = time.time()

    # 前置过渡句（2026-10-05 阶段2 D1/D2/D3）
    # 判停确认后文本已就绪，这里立刻出声填满等待，不等 LLM 返回工具名。
    # 原实现（:614）在 LLM 已返回 tool_calls 之后才播，那时工具还没执行完，
    # 衔接语成了事后诸葛亮，一秒都没抢到。
    # fire-and-forget 投递：主链路照原样并行推进，总时长不变，只降低感知延时。
    # 2026-10-06（过渡语 2.0）：本块现在真的会出声（走主音色，见 `_tts_speak`）。
    # `_lead_in_spoken` 记录本轮是否已经说过过渡语 —— 说过之后，
    # 下面 speech_registry 的 B 类衔接语不再重复播（实测会同一句播两遍）。
    _lead_in_spoken = False
    # [2026-10-13 音乐不停对话·第三段，主人批准方案 A] 旧「系统决定要回应 →
    # _pause_music_for_dialog 暂停音乐（触发点②）」已删除：音乐全程持续播放，
    # 混音由双路径解决（AEC 后音频喂 VAD/判停，原始音频喂 ASR/声纹）。
    if depth == 0 and query is not None and _LEAD_IN_ENABLED:
        try:
            from core.utils.lead_in import pick_lead_in

            # 【2026-10-12 ②】过渡句与问题相关联、但不复述原话。
            #   关联：由 pick_lead_in 内部按意图路由到专属池（查天气说天气）。
            #   不复述：pick_non_repeating() 排除与用户原话 ≥5 字连续重合的候选。
            # 【关于「按等待时长选长短句」】原计划按实测等待秒数分档，主人
            #   2026-10-12 明确否掉：「并没有限定超短句的回复……反正说长了，
            #   后面是排队嘛，这样就没有影响，反而显得更顺畅。」原因是过渡句与
            #   回复走同一个 AudioRateController FIFO，说长了不占总时长。
            #   所以池内随机轮换即可，不需要预算。
            _lead, _intent = pick_lead_in(query, return_key=True)
            if _lead:
                # [2026-10-11 C3] 原来在投递**之前**就置 _lead_in_spoken=True。
                # 一旦 run_coroutine_threadsafe 抛异常（无事件循环/TTS 未就绪），
                # 过渡句没出声，但下面 797 行的「模型纯应声去重」和 951 行的
                # 「B 类衔接语跳过」都以为已经说过 → 整段无声。
                # 改为投递成功后才置位。
                try:
                    asyncio.run_coroutine_threadsafe(
                        _tts_speak(conn, _lead), conn.loop
                    )
                except Exception as _speak_err:
                    conn.logger.bind(tag=TAG).warning(
                        f"[过渡句] 投递失败，不置已说标记: {_speak_err!r}")
                else:
                    _lead_in_spoken = True
                    # 记起点 + 意图，供观测点回填真实等待秒数（见
                    # core/providers/tts/base.py._audio_play_priority_thread）。
                    # 目前只用于日志与后续分析，不再驱动选句。
                    conn._lead_in_started_at = time.time()
                    conn._lead_in_intent = _intent
                    conn.logger.bind(tag=TAG).info(
                        f"[过渡句] 前置播报: {_lead!r}（意图 {_intent}）")
        except Exception as _e:
            conn.logger.bind(tag=TAG).warning(f"前置过渡句异常，跳过: {_e!r}")

    # 动态更新 LLM 实例
    if conn.server is not None and hasattr(conn.server, '_llm') and conn.server._llm is not None:
        if conn.server._llm is not conn.llm:
            conn.llm = conn.server._llm
            conn.logger.bind(tag=TAG).info("LLM 实例已动态更新为 server 最新配置")

    if depth == 0:
        conn.dialogue.put(Message(role="user", content=query))
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )

    # 最大递归深度
    MAX_DEPTH = 5
    force_final_answer = False

    if depth >= MAX_DEPTH:
        conn.logger.bind(tag=TAG).debug(
            f"已达到最大工具调用深度 {MAX_DEPTH}，将强制基于现有信息回答"
        )
        force_final_answer = True
        conn.dialogue.put(
            Message(
                role="user",
                content="[系统提示] 已达到最大工具调用次数限制，请你基于目前已经获取的所有信息，直接给出最终答案。不要再尝试调用任何工具。",
            )
        )

    # 长对话工具调用提醒
    force_reminder = False

    if depth == 0 and query is not None:
        dialogue_length = len(conn.dialogue.dialogue)
        current_turn = dialogue_length // 2

        if conn.tool_call_stats['last_call_turn'] >= 0:
            turns_since_last = current_turn - conn.tool_call_stats['last_call_turn']
            if turns_since_last > 3:
                conn.logger.bind(tag=TAG).warning(
                    f"检测到{turns_since_last}轮未调用工具，可能进入偷懒模式，将强制注入提醒"
                )
                force_reminder = True

        max_dialogue_turns = 5
        if dialogue_length > max_dialogue_turns * 2:
            # ⚠️ 2026-10-13 WP-5：截断**之前**先留一份快照。
            # trim_history 是就地重建 self.dialogue，事后拿不到被丢掉的内容，
            # 于是 5 轮之外的话题彻底消失（用户说「刚才我说的那个地方」就断片）。
            try:
                from core.utils.memory_short_term import (
                    append_rolling, capture_trimmed, persist_trimmed_rounds,
                )

                _snapshot = list(conn.dialogue.dialogue)
            except Exception:
                _snapshot = None
            removed = conn.dialogue.trim_history(max_turns=max_dialogue_turns)
            if removed > 0:
                conn.logger.bind(tag=TAG).info(
                    f"对话历史过长({dialogue_length}条)，已智能截断保留最近{max_dialogue_turns}轮，移除{removed}条消息"
                )
                if _snapshot:
                    try:
                        # 被保留的轮次数 = 截断后非 system 消息里 user 的个数
                        _kept_turns = sum(
                            1 for m in conn.dialogue.dialogue
                            if getattr(m, "role", "") == "user"
                        )
                        _rounds = capture_trimmed(_snapshot, _kept_turns * 2)
                        if _rounds:
                            append_rolling(conn, _rounds)
                            persist_trimmed_rounds(
                                getattr(conn, "session_id", None), _rounds,
                                speaker=getattr(conn, "current_speaker", None),
                            )
                            conn.logger.bind(tag=TAG).info(
                                f"[短期续接] 已接住 {len(_rounds)} 条被截断的对话，"
                                f"转入 <recent_context>"
                            )
                    except Exception as _e:
                        conn.logger.bind(tag=TAG).warning(f"[短期续接] 失败，保持原样: {_e!r}")

    from core.utils.foreground_activity import touch as _fg_touch
    _fg_touch()  # 前台有活动：ltm 抽取等后台重负载错开
    if depth == 0 and query is not None:
        try:
            _n = _shrink_old_tool_results(conn.dialogue.dialogue)
            if _n:
                conn.logger.bind(tag=TAG).info(f"[上下文瘦身] 已压缩 {_n} 条历史工具返回（只保留开头摘要）")
        except Exception as _e:
            conn.logger.bind(tag=TAG).warning(f"[上下文瘦身] 失败，保持原样: {_e!r}")

    # 获取工具列表
    functions = None
    if (
            conn.intent_type == "function_call"
            and hasattr(conn, "func_handler")
            and not force_final_answer
    ):
        all_functions = conn.func_handler.get_functions()

        # 工具筛选旁路（2026-09-28 主人裁决）：云端 LLM 无上下文限制，
        # 旁路后全部工具可见，杜绝"LLM 只看到 2 个工具说没配新闻API"的问题；
        # 本地 LLM 维持筛选（全量 schema 约 27K tokens 超出小模型上下文）。
        from core.utils.model_stack import tool_filter_active, tool_prefilter_active
        _filter_on = tool_filter_active(conn.config)
        _prefilter_on = (not _filter_on) and tool_prefilter_active(conn.config)

        # 2026-10-11 观测：每次函数集合变化时记一次「工具定义体积」，定位首 token 慢的根源
        # （线上每个请求 ~3.6 万输入 token，其中工具定义占大头）。
        try:
            _sizes = sorted(
                ((len(json.dumps(f, ensure_ascii=False)), (f.get("function") or f).get("name"))
                 for f in all_functions), reverse=True)
            _sig = (len(_sizes), sum(x for x, _ in _sizes))
            if getattr(conn, "_tool_size_logged", None) != _sig:
                conn._tool_size_logged = _sig
                conn.logger.bind(tag=TAG).info(
                    f"[工具体积] {_sig[0]} 个工具，定义共 {_sig[1]} 字符；最大 12 个: "
                    + ", ".join(f"{n}={x}" for x, n in _sizes[:12]))
        except Exception:
            pass

        # 2026-10-11 低频大体积工具组（飞书日历/任务/群消息/通讯录）按需暴露：
        # 历史 1468 轮零调用，却占每个请求 7.9 万字符（~2.5 万 token），是首 token 慢的大头。
        # 用户原话/最近几句对话里没出现相关词就不带；本轮已打开的组在递归轮次保持打开。
        try:
            from core.utils.tool_lazy_groups import select_lazy_tools
            if depth == 0:
                conn._lazy_opened_groups = set()
            # 只看「用户说的话」（本句 + 上两句用户话，让「改成明天下午」这类跟进语也能命中）。
            # 不能带助手回复/工具输出：助手自我介绍、简报里常有「日程/消息」，会把门控误打开。
            _recent = []
            for _m in reversed(list(conn.dialogue.dialogue)):
                # is_temporary：TOOL_CALLING_RULES 提醒（含「日程/提醒」字样）是系统注入的临时 user 消息，
                # 递归轮读到它会误开飞书组（2026-10-12 实锤：工具 60→73、前缀缓存失效、多 1.8 万 token）
                if getattr(_m, "is_temporary", False):
                    continue
                if getattr(_m, "role", "") == "user" and isinstance(getattr(_m, "content", None), str):
                    _recent.append(_m.content)
                    if len(_recent) >= 3:
                        break
            _pool = " ".join([str(query or "")] + _recent)
            all_functions, _opened, _closed = select_lazy_tools(
                all_functions, _pool, getattr(conn, "_lazy_opened_groups", ()))
            conn._lazy_opened_groups = set(getattr(conn, "_lazy_opened_groups", ())) | set(_opened)
            if _closed and depth == 0:
                conn.logger.bind(tag=TAG).info(
                    f"[工具按需] 本轮未出现 {','.join(_closed)} 相关词，暂不携带这组工具，剩余 {len(all_functions)} 个")
            elif _opened and depth == 0:
                conn.logger.bind(tag=TAG).info(f"[工具按需] 命中 {','.join(_opened)} 相关词，携带这组工具")
        except Exception as _lz:
            conn.logger.bind(tag=TAG).warning(f"[工具按需] 处理失败，按全量工具: {_lz!r}")

        if depth > 0 and getattr(conn, "_turn_tool_names", None):
            # 工具调用后的递归轮：沿用本轮首轮的工具集（工具集不变 → 云端前缀缓存可命中，
            # 也不会因为工具结果/提醒里的词又把别的组打开）。
            _names = conn._turn_tool_names
            functions = [f for f in all_functions
                         if ((f.get("function") if isinstance(f.get("function"), dict) else f) or {}).get("name") in _names]
        elif depth == 0 and query is not None and len(all_functions) > 2 and _prefilter_on:
            from core.utils.tool_prefilter import prefilter_tools
            _prev_user = []
            for _m in reversed(list(conn.dialogue.dialogue)):
                if getattr(_m, "is_temporary", False) or getattr(_m, "role", "") != "user":
                    continue
                if isinstance(getattr(_m, "content", None), str):
                    _prev_user.append(_m.content)
                    if len(_prev_user) >= 2:
                        break
            functions = prefilter_tools(all_functions, query, getattr(conn, "device_id", None), _prev_user[1:])
            size_info = get_tool_size_info(functions)
            conn.logger.bind(tag=TAG).info(
                f"[工具预筛选] {len(all_functions)}个工具 -> {len(functions)}个工具, "
                f"大小: {size_info['total_size']}字节: "
                + ",".join(((f.get("function") if isinstance(f.get("function"), dict) else f) or {}).get("name", "?")
                           for f in functions))
        elif depth == 0 and query is not None and len(all_functions) > 2 and _filter_on:
            filtered_functions = filter_tools_by_query(
                all_functions, query, getattr(conn, "device_id", None)
            )
            size_info = get_tool_size_info(filtered_functions)
            conn.logger.bind(tag=TAG).info(
                f"[工具过滤] {len(all_functions)}个工具 -> {len(filtered_functions)}个工具, "
                f"大小: {size_info['total_size']}字节"
            )
            functions = filtered_functions
        else:
            if depth == 0 and query is not None and len(all_functions) > 2 and not _filter_on:
                conn.logger.bind(tag=TAG).info(
                    f"[工具过滤] 云端LLM已旁路筛选，{len(all_functions)}个工具全部可见"
                )
            functions = all_functions

    if depth == 0:
        conn._turn_tool_names = (
            {((f.get("function") if isinstance(f.get("function"), dict) else f) or {}).get("name") for f in functions}
            if functions else None)

    # 长对话工具调用规则强化
    tool_call_reminder = None
    if depth == 0 and query is not None and functions is not None:
        dialogue_length = len(conn.dialogue.dialogue)
        if dialogue_length > 4:
            tool_summary = _get_tool_summary(functions)
            if tool_summary:
                if force_reminder:
                    tool_call_reminder = (
                        TOOL_CALLING_RULES +
                        f"[重要提醒] 多轮未使用工具，检查回复是否遗漏了必要的工具调用！上一轮未使用工具，本轮必须重新判断是否需要工具。"
                        f"当前可用工具: {tool_summary}。"
                    )
                    reminder_level = "强"
                else:
                    tool_call_reminder = (
                        TOOL_CALLING_RULES +
                        f"当前可用工具: {tool_summary}。"
                        f"仅当用户请求涉及实时信息查询或执行操作时调用，日常对话无需调用。"
                    )
                    reminder_level = "中"
                conn.logger.bind(tag=TAG).debug(
                    f"对话历史较长({dialogue_length}条)，已注入{reminder_level}等级工具调用规则强化，当前可用工具：{tool_summary}"
                )

    response_message = []

    if tool_call_reminder:
        conn.dialogue.put(Message(role="user", content=tool_call_reminder, is_temporary=True))

    # —— 记忆查询：异步非阻塞 + 智能触发判断 ——
    # 1. 判断是否需要查记忆（智能触发）
    _should_query_mem = _should_query_memory(conn, query)

    # 2. 命中短缓存：只有「同一个问题」才复用（2026-10-13 WP-4）
    #    原来这里是盲取上轮 future 的结果，不校验它对应哪个 query，
    #    第一轮慢查询的旧记忆会被注进第二轮的新问题里（串轮）。
    memory_str = None
    _mem_speaker = getattr(conn, "current_speaker", None)
    _mem_query_text = _recent_user_text(conn, query or "")
    _mem_cache_key = f"{_mem_query_text}|{_mem_speaker or ''}"
    _cached = _memory_result_cache.get(_mem_cache_key)
    if _cached:
        memory_str = _cached
        conn.logger.bind(tag=TAG).debug("记忆命中同问题短缓存")

    # 3. 发起新的异步查询（future 绑定它自己的 query，不跨轮盲用）
    _memory_future = None
    if _should_query_mem and conn.memory is not None and query and memory_str is None:
        _memory_future = asyncio.run_coroutine_threadsafe(
            _query_memory_guarded(
                conn.memory, _mem_query_text, _mem_cache_key, None, _mem_speaker
            ),
            conn.loop,
        )
        conn._memory_query_future = _memory_future
        conn._memory_query_text = _mem_query_text

    try:
        # 4. 尝试快速获取本轮查询结果（200ms 超时），不阻塞主流程。
        #    超时后**不再留给下一轮盲取**：结果由 _query_memory_guarded 在完成后
        #    写入按 query 的短缓存，只有同一个问题再问一次才会用到。
        if _memory_future is not None:
            try:
                memory_str = _memory_future.result(timeout=_MEMORY_WAIT_SECONDS)
            except Exception:
                conn.logger.bind(tag=TAG).debug(
                    f"记忆查询未在 {int(_MEMORY_WAIT_SECONDS * 1000)}ms 内完成，"
                    f"本轮跳过注入（结果完成后进短缓存）"
                )
                memory_str = None

        # 声纹识别出成员 → 注入完整档案（基本信息/关系/健康数据）
        member_profile = None
        speaker = getattr(conn, "current_speaker", None)
        if speaker:
            from core.utils.member_profile import get_member_profile_text
            member_profile = get_member_profile_text(speaker)
            if member_profile:
                conn.logger.bind(tag=TAG).info(
                    f"成员档案已注入 LLM：{speaker}（{len(member_profile)} 字）")

        if conn.intent_type == "function_call" and functions is not None:
            llm_responses = conn.llm.response_with_functions(
                conn.session_id,
                conn.dialogue.get_llm_dialogue_with_memory(
                    memory_str, conn.config.get("voiceprint", {}),
                    member_profile=member_profile,
                    recent_context=_recent_context_block(conn),
                    allow_greeting_name=getattr(conn, "_wake_greeting_allowed", True),
                ),
                functions=functions,
            )
        else:
            llm_responses = conn.llm.response(
                conn.session_id,
                conn.dialogue.get_llm_dialogue_with_memory(
                    memory_str, conn.config.get("voiceprint", {}),
                    member_profile=member_profile,
                    recent_context=_recent_context_block(conn),
                    allow_greeting_name=getattr(conn, "_wake_greeting_allowed", True),
                ),
            )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
        # C1 话术池化：LLM 出错提示随机换说法
        from core.utils.phrase_pool import pick
        error_text = pick(LLM_ERROR_LINES, detail=str(e)[:50])
        # 2026-10-11：原先直接给前端发 sentence_start/stop，但开头已入队 FIRST、
        # 这里没有 LAST → TTS 队列悬空、也不出声。改走 TTS 队列：念错误话术并在顶层补 LAST。
        try:
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=error_text,
                )
            )
            if depth == 0:
                conn.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=conn.sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
        except Exception as notify_err:
            conn.logger.bind(tag=TAG).error(f"发送错误通知失败: {notify_err}")
        return None

    # 处理流式响应
    tool_call_flag = False
    tool_calls_list = []
    content_arguments = ""
    # 2026-10-07：垫场句已播时，模型在调工具前的纯应声要丢掉（避免「回复两条」）
    conn._pending_preamble_buf = []
    _pending_preamble = conn._pending_preamble_buf
    _preamble_decided = False
    # 2026-09-20 修复：只在最顶层新一轮对话(depth=0)重置打断标志。
    # 工具执行后的递归 chat(depth+1) 若也清标志，用户在工具执行期间的打断会被吞掉，
    # 旧回复继续流式推给 TTS。depth>0 时保留标志，流式循环首轮即 break。
    # 2026-09-21 调整：复位动作已上移至函数开头 FIRST 入队之前（见上方注释），此处不再重复。
    was_aborted = False
    emotion_flag = True
    try:
        for response in llm_responses:
            _fg_touch()
            if conn.client_abort:
                was_aborted = True
                break
            if conn.intent_type == "function_call" and functions is not None:
                if isinstance(response, dict):
                    content = response.get("content", "")
                    tools_call = response.get("tool_calls", None)
                elif isinstance(response, (list, tuple)) and len(response) >= 2:
                    content, tools_call = response[0], response[1]
                else:
                    content = response
                    tools_call = None
                if content is not None and len(content) > 0:
                    content_arguments += content

                if not tool_call_flag and content_arguments.startswith("<tool_call>"):
                    tool_call_flag = True

                if tools_call is not None and len(tools_call) > 0:
                    tool_call_flag = True
                    _merge_tool_calls(tool_calls_list, tools_call)

                # 【任务标记 2026-10-08】一旦模型决定调工具，这一轮就是「主人在等结果
                # 的任务」——从此刻起算，到整轮结束为止。期间用户插话不打断这一轮，
                # 改为排队等结果播报完再回答（见 receiveAudioHandle._is_task_running）。
                # 注意起点必须是「检测到工具调用」，不能是「工具开始执行」：
                # 实测 web_search 只要 2 秒就返回，而结果总结还要 7 秒，
                # 按工具执行窗口算的话，插话正好落在总结期 → 仍被取消。
                if tool_call_flag and not getattr(conn, "_task_running", False):
                    conn._task_running = True
                    conn._task_started_at = time.time()
                    import threading as _th
                    conn._task_turn_seq = getattr(conn, "_chat_turn_by_thread", {}).get(_th.get_ident())
            else:
                content = response

            if emotion_flag and content is not None and content.strip():
                from core.utils import textUtils
                asyncio.run_coroutine_threadsafe(
                    textUtils.get_emotion(conn, content),
                    conn.loop,
                )
                emotion_flag = False

            if content is not None and len(content) > 0:
                if not tool_call_flag:
                    response_message.append(content)
                    # 2026-10-07（主人实测「一条请求回复两条」+「应了两声」）：
                    # 本轮判停时已经播过垫场句（_lead_in_spoken），模型又常以纯应声开头
                    # （「好嘞，」「好的，」），两句叠在一起像重复应答。
                    # 处理：先把「可能是应声」的内容暂存，等看清后面有没有正文再决定 ——
                    #   后面还有正文 → 丢掉暂存的纯应声（垫场句已经应过声了）
                    #   整轮就只有这一句应声 → 补发（那就是全部回复，绝不能丢，否则不出声）
                    #   调了工具 → 也丢掉（工具结果会另起一轮说话）
                    # 只有「短且像应声」才暂存，长内容一律照常立刻发送，不影响首响延迟。
                    if _should_buffer_ack(_pending_preamble, content,
                                          _lead_in_spoken, _preamble_decided):
                        _pending_preamble.append(content)
                        continue
                    if _lead_in_spoken and not _preamble_decided:
                        if _pending_preamble:
                            conn.logger.bind(tag=TAG).info(
                                "[过渡语] 丢弃与垫场句重复的应声: "
                                f"{''.join(_pending_preamble)!r}")
                            _pending_preamble.clear()
                        _preamble_decided = True
                    conn.tts.tts_text_queue.put(
                        TTSMessageDTO(
                            sentence_id=conn.sentence_id,
                            sentence_type=SentenceType.MIDDLE,
                            content_type=ContentType.TEXT,
                            content_detail=content,
                        )
                    )
    except Exception as e:
        # [2026-10-12] 旧流被新一轮主动接管：安静退出，不当错误处理。
        # 否则旧轮会念一句「伊娃有点忙」扰乱主人，而那句话本就不该出现
        # （主人要听的是新一轮的回复）。放在 except Exception 之前单独捕获。
        if type(e).__name__ == "_LLMTurnSuperseded":
            conn.logger.bind(tag=TAG).info(
                "本轮已被更新的轮次接管，静默退出（不念错误提示）"
            )
            return
        conn.logger.bind(tag=TAG).error(f"LLM stream processing error: {e}")
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=_get_system_error_response(conn.config),
            )
        )
        if depth == 0:
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
        return

    # 2026-10-07：流结束。暂存区里还有内容只有两种情况：
    #   ① 整轮回复就是一句纯应声（上面从没等到正文）→ 必须补发，否则一个字都不说
    #   ② 模型调了工具（应声后面直接是工具调用）→ 丢弃，工具结果会另起一轮说话
    if _pending_preamble and tool_call_flag and _lead_in_spoken:
        conn.logger.bind(tag=TAG).info(
            f"[过渡语] 丢弃重复应声（垫场句已应过声）: {''.join(_pending_preamble)!r}")
        _pending_preamble.clear()
    else:
        _flush_preamble(conn)
    _preamble_decided = True

    # 处理 function call
    # 2026-10-11：流式循环里被用户打断（was_aborted）时，已累积的工具调用不再执行——
    # 否则用户已经喊停，开灯/发消息之类的动作还会照做。
    if tool_call_flag and was_aborted:
        conn.logger.bind(tag=TAG).info(
            f"[打断] 本轮已被打断，丢弃 {len(tool_calls_list)} 个未执行的工具调用"
        )
    if tool_call_flag and not was_aborted:
        bHasError = False
        if len(tool_calls_list) == 0 and content_arguments:
            from core.utils.util import extract_json_from_string
            a = extract_json_from_string(content_arguments)
            if a is not None:
                try:
                    content_arguments_json = json.loads(a)
                    tool_calls_list.append(
                        {
                            "id": str(uuid.uuid4().hex),
                            "name": content_arguments_json["name"],
                            "arguments": json.dumps(
                                content_arguments_json["arguments"],
                                ensure_ascii=False,
                            ),
                        }
                    )
                except Exception as e:
                    bHasError = True
                    response_message.append(a)
            else:
                bHasError = True
                response_message.append(content_arguments)
            if bHasError:
                conn.logger.bind(tag=TAG).error(
                    f"function call error: {content_arguments}"
                )

        if not bHasError and len(tool_calls_list) > 0:
            if depth == 0:
                current_turn = len(conn.dialogue.dialogue) // 2
                conn.tool_call_stats['last_call_turn'] = current_turn
                conn.tool_call_stats['consecutive_no_call'] = 0
                conn.logger.bind(tag=TAG).debug(
                    f"工具调用统计更新: 当前轮次={current_turn}"
                )

            if len(response_message) > 0:
                text_buff = "".join(response_message)
                conn.tts_MessageText = text_buff
                conn.dialogue.put(Message(role="assistant", content=text_buff))
            response_message.clear()

            conn.logger.bind(tag=TAG).debug(
                f"检测到 {len(tool_calls_list)} 个工具调用"
            )

            # 【过渡语 2.0 · 2026-10-06】原实现在这里（LLM 返回 tool_calls 之后，约 +4s）
            # 播一句 2026-09-17 录的 24kHz 固定预录音。三个问题：
            #   1) 太晚——等待的前 4 秒仍然静音，没抢到任何时间；
            #   2) 音色是另一个（老引擎 24kHz），与主音色 MossNano(48kHz) 不一致；
            #   3) 与屏幕字幕（lead_in 的「我看看天气」）不是同一句。
            # 过渡语已前移到判停那一刻（本文件顶部 `_tts_speak` → transition_voice），
            # 由主音色真正出声，故此处不再重复播报。

            # 【v3.0 P1】speech_registry 前置钩子：C 类 pre-announce / B 类衔接语
            if depth == 0 and hasattr(conn, 'speech_specs') and conn.speech_specs:
                # 2026-09-28 修复：原代码误写 futures_with_data（第627行才定义），
                # 模型一旦返回 tool_calls 且 speech_specs 已加载，此处必崩
                # ("cannot access local variable 'futures_with_data'")，
                # 导致工具调用全部静默失败。钩子需要的是工具列表 tool_calls_list。
                for tool_call_data in tool_calls_list:
                    tool_name = tool_call_data.get('name', '')
                    spec = conn.speech_specs.get(tool_name)
                    if not spec:
                        continue
                    tier = spec.get('tier', 'B')
                    try:
                        if tier == 'C' and spec.get('pre_announce'):
                            # C 类：fire-and-forget 先出声
                            announce_text = spec['pre_announce'].replace(
                                '{duration_phrase}', spec.get('duration_phrase', '要十几秒')
                            )
                            asyncio.run_coroutine_threadsafe(
                                _tts_speak(conn, announce_text), conn.loop
                            )
                            conn.logger.bind(tag=TAG).info(f"[speech_registry] C类 pre-announce: {tool_name} -> {announce_text}")
                            # 【v3.0 P3】启动进度插播器
                            if spec.get('progress_mode') == 'heartbeat':
                                from core.chat.progress_announcer import ProgressAnnouncer
                                announcer = ProgressAnnouncer(
                                    conn,
                                    interval=spec.get('heartbeat_interval', 25)
                                )
                                announcer.start()
                                conn._progress_announcer = announcer
                        elif tier == 'B' and spec.get('lead_in'):
                            # B 类：衔接语。2026-10-06：本轮已经在判停那一刻说过过渡语了，
                            # 这里就不再重复（否则同一句会播两遍，实测踩到）。
                            if _lead_in_spoken:
                                conn.logger.bind(tag=TAG).info(
                                    f"[speech_registry] B类衔接语跳过（本轮已有过渡语）: {tool_name}")
                            else:
                                asyncio.run_coroutine_threadsafe(
                                    _tts_speak(conn, spec['lead_in']), conn.loop
                                )
                                conn.logger.bind(tag=TAG).info(f"[speech_registry] B类衔接语: {tool_name} -> {spec['lead_in']}")
                    except Exception as e:
                        conn.logger.bind(tag=TAG).warning(f"[speech_registry] 前置钩子失败: {e}")

            futures_with_data = []
            tool_results = []
            for tool_call_data in tool_calls_list:
                if conn.client_abort:
                    conn.logger.bind(tag=TAG).info(
                        f"[打断] 提交工具前发现已被打断，跳过: {tool_call_data['name']}"
                    )
                    continue
                conn.logger.bind(tag=TAG).debug(
                    f"function_name={tool_call_data['name']}, function_id={tool_call_data['id']}, function_arguments={tool_call_data['arguments']}"
                )

                future = asyncio.run_coroutine_threadsafe(
                    conn.func_handler.handle_llm_function_call(
                        conn, tool_call_data
                    ),
                    conn.loop,
                )
                futures_with_data.append((future, tool_call_data))

            for future, tool_call_data in futures_with_data:
                tool_results.append(
                    (_wait_tool_result(conn, future, tool_call_data), tool_call_data)
                )

            if tool_results:
                try:
                    _handle_function_result(conn, tool_results, depth=depth)
                except Exception as e:
                    # 工具结果处理出错也必须走到下面的 LAST，否则 TTS 队列悬空、后续轮次卡死
                    conn.logger.bind(tag=TAG).error(f"[工具结果处理异常] {e!r}", exc_info=True)
                    if depth == 0:
                        from core.utils.phrase_pool import pick
                        conn.tts.tts_one_sentence(
                            conn, ContentType.TEXT, content_detail=pick(TOOL_ERROR_LINES)
                        )

            # [2026-10-11 C2] 工具全部返回 → 立刻停掉心跳插播器。
            # 原实现只 start 从不 stop，心跳会一直播下去（修好「从未生效」后暴露的新风险）。
            _pa = getattr(conn, "_progress_announcer", None)
            if _pa is not None:
                try:
                    _pa.stop()
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"停止进度插播器失败: {e!r}")
                conn._progress_announcer = None

    # [2026-10-11 C2] 心跳插播器的兜底停止点。上面工具正常返回处已停一次，
    # 这里再兜一次是因为异常/abort 路径可能绕不过去 —— 漏停的后果是心跳一直播下去。
    _pa_tail = getattr(conn, "_progress_announcer", None)
    if _pa_tail is not None:
        try:
            _pa_tail.stop()
        except Exception:
            pass
        conn._progress_announcer = None

    # 存储对话内容
    if len(response_message) > 0:
        text_buff = "".join(response_message)
        conn.tts_MessageText = text_buff
        if was_aborted:
            # 打断恢复（方案#5）：历史中标记"被打断"，让 LLM 知道此回复未说完，
            # 避免下一轮重复念剩余内容或误认为已完整作答；tts_MessageText 保持原文不污染存档
            marked_buff = text_buff + "（[此回复被用户打断，未说完的部分不必继续]）"
            conn.dialogue.put(Message(role="assistant", content=marked_buff))
        else:
            conn.dialogue.put(Message(role="assistant", content=text_buff))

        if depth == 0 and not tool_call_flag:
            conn.tool_call_stats['consecutive_no_call'] += 1

    if depth == 0:
        # [2026-10-18 P5，主人批准] 运行观察：每轮收口记一次（旁路，失败不影响主链路）。
        # 报告里用它回答「一轮多久、多少轮调了工具、多少轮被打断」。
        try:
            from core.utils import observation as _obs
            _obs.bump("turn")
            if tool_call_flag:
                _obs.bump("turn_with_tool")
            if was_aborted:
                _obs.bump("turn_aborted")
            _t0 = getattr(conn, "_obs_turn_started", None)
            _reply = "".join(response_message) if len(response_message) > 0 else ""
            _obs.note_turn(
                device_id=getattr(conn, "device_id", "") or "",
                session_id=getattr(conn, "session_id", "") or "",
                user_head=str(query)[:80],
                turn_ms=int((time.time() - _t0) * 1000) if _t0 else None,
                reply_chars=len(_reply),
                with_tool=bool(tool_call_flag),
                aborted=bool(was_aborted),
            )
        except Exception:
            pass

        # 告别意图规则层兜底（方案#6）：LLM 未调 handle_exit_intent 时，
        # 短句含告别词 → TTS 播完自动关连接回 idle；LLM 已调工具则不干预
        if not tool_call_flag and _is_farewell_query(query) and not conn.close_after_chat:
            conn.close_after_chat = True
            conn.logger.bind(tag=TAG).info(
                f"[告别意图] 规则层检测到告别语，TTS 播完后自动关闭连接回 idle: {query}"
            )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )
        conn.logger.bind(tag=TAG).debug(
            lambda: json.dumps(
                conn.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
            )
        )

        user_text = query
        ai_text = conn.tts_MessageText if hasattr(conn, 'tts_MessageText') else ''
        # ⚠️ 2026-10-13：对话历史表只要有 user_text 就存。原来和下面的摘要一起放在
        # `if user_text and ai_text:` 里，调用了工具的轮次（联网搜索/查路线/简报）没有
        # 助手文字 → 主人说的话整轮不进表 → 短期回灌、夜间梳理、「记下来」整理全部漏掉。
        if user_text:
            _save_dialogue_history(conn, user_text, ai_text or '')
        if user_text and ai_text:
            try:
                from core.utils.conversation_summary import save_conversation_entry
                session_id = getattr(conn, 'session_id', 'unknown')
                speaker = getattr(conn, 'current_speaker', None) or '未知'
                try:
                    # [2026-10-06] 原来 except 分支写死 '伊娃'，兜底念别人名字
                    from core.utils.assistant_identity import safe_name

                    _assistant_name = safe_name()
                except Exception:
                    _assistant_name = "家庭助手"
                save_conversation_entry(session_id, speaker, user_text)
                save_conversation_entry(session_id, _assistant_name, ai_text)
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"保存对话摘要失败: {e}")

        # ⚠️ 2026-10-13 WP-3：这一块从 `if user_text and ai_text:` 里搬出来了。
        # 原来 LLM 没产出文字（纯工具调用轮次，ai_text 为空）时整段不执行 →
        # 「打开客厅的灯」后面跟的那句用户陈述永远不会进记忆。
        # 现在只要有 user_text 就提交；ai_text 为空时 assistant_ctx 传 None。
        if user_text:
            # 向量记忆后端的实时保存（每轮对话后，避免只在关闭连接时才保存导致丢失）
            #
            # ⚠️ 2026-10-06 修复：原来这里写死 `if memory_type == "LTM"`。
            # 切到 ltm 后实时保存被静默关掉——只有 WebSocket 断开时
            # （connection.py 的 _save_and_close）才会写记忆，而页面一直开着
            # 就等于永远不写。实测表现：说了 6 句，ltm 一条日志都没有。
            # 两个 provider 都实现了同一套 async save_memory(msgs, session_id) 契约，
            # 这里按白名单放行；不写成「所有 provider 都试」——
            # 白名单外的 provider 逐轮调用只会刷无意义的日志。
            if conn.memory is not None:
                memory_type = conn.config.get("Memory", {}).get(
                    conn.config.get("selected_module", {}).get("Memory", ""), {}
                ).get("type", "")
                if memory_type in _REALTIME_MEMORY_TYPES:
                    try:
                        # ⚠️ 2026-10-13 WP-1/WP-3：不再用 `_non_temp[-2:]` 猜本轮消息。
                        # 工具调用轮次的末两条是 tool + assistant，`[-2:]` 里
                        # **没有 user 消息** → provider 的 user 白名单一过滤就什么都不剩，
                        # 这一轮用户说的话等于从没存在过（代码推演，已由单测覆盖）。
                        # 现在直接把本轮的 user_text 交出去，并带上：
                        #   speaker      声纹识别出的说话人 → 决定记忆归属 scope
                        #   assistant_ctx 助手这一句 → 只作消歧上下文
                        #                （挡「AI 叫汤圆」被记成主人名字这类错记）
                        #   turn         幂等键的一部分，重复提交不再产生重复记忆
                        _speaker = getattr(conn, 'current_speaker', None)
                        _turn = getattr(conn, 'turn_index', None)
                        if _turn is None:
                            # 没有显式轮次计数时用「本连接已保存的轮数」当轮次号
                            _turn = getattr(conn, '_memory_turn', 0)
                            conn._memory_turn = _turn + 1
                        asyncio.run_coroutine_threadsafe(
                            conn.memory.save_memory(
                                [{"role": "user", "content": user_text,
                                  "speaker": _speaker}],
                                conn.device_id,
                                speaker=_speaker,
                                assistant_ctx=ai_text or None,
                                device_id=conn.device_id,
                                turn=_turn,
                            ),
                            conn.loop,
                        )
                        conn.logger.bind(tag=TAG).info(
                            f"实时记忆已提交 → {memory_type}"
                            f"（说话人={_speaker or '未识别'}，{len(user_text)} 字）"
                        )
                    except Exception as e:
                        conn.logger.bind(tag=TAG).warning(f"实时保存记忆失败: {e}")

        if tool_call_reminder and len(conn.dialogue.dialogue) > 0:
            original_length = len(conn.dialogue.dialogue)
            conn.dialogue.dialogue = [
                msg for msg in conn.dialogue.dialogue
                if not getattr(msg, 'is_temporary', False)
            ]
            if len(conn.dialogue.dialogue) < original_length:
                conn.logger.bind(tag=TAG).debug("已清理临时的工具调用提醒消息")

    return True


# [2026-10-12] 判断一段工具返回文本「是不是给用户看的中文业务提示」。
# 只有这三种才允许直接念给用户，其余（含所有拿不准的技术性文本）一律换话术。
# 见 `_handle_function_result` 里 Action.ERROR 分支的注释。
_USER_MESSAGE_MARKERS = (
    "不在允许列表", "不在白名单", "请先", "请到", "请在",
    "还没有", "尚未", "暂时不支持", "暂不支持", "需要先",
    "请打开", "请设置", "请检查", "请联系",
    "未配置视频地址", "已过期", "余额不足",
)


def _looks_like_user_message(raw: str) -> bool:
    """这段文本是否适合直接念给用户听。

    白名单式判定，宁可漏念不可念错：
      1. 含明确的中文业务提示词 → 念（「设备不在允许列表」这类用户需要知道）
      2. 命中技术性关键词         → 不念（错误码/异常堆栈/HTTP 原文）
      3. 含大量英文/代码符号     → 不念（Python 报错、JSON、堆栈）
      4. 其它拿不准              → 不念
    """
    t = str(raw or "").strip()
    if not t:
        return False
    if any(k in t for k in _USER_MESSAGE_MARKERS):
        return True
    if any(h in t for h in _TECHNICAL_ERROR_HINTS):
        return False
    # 英文/代码字符占比过高 → 几乎可以确定是技术性文本
    #（`'dict' object has no attribute 'strip'` 这类，中文音素念出来像方言）
    ascii_n = sum(1 for c in t if c.isascii())
    if len(t) >= 4 and ascii_n / len(t) > 0.3:
        return False
    # 引号包起来的单引号串（Python repr 特征）
    if t.count("'") >= 2 or t.count('"') >= 2:
        return False
    return False


def _get_tool_summary(functions: list) -> str:
    """从工具定义中提取摘要"""
    if not functions:
        return ""
    datas = []
    for func in functions:
        func_info = func.get("function", {})
        name = func_info.get("name", "")
        datas.append(name)
    return "、".join(datas)


_TOOL_WAIT_TIMEOUT_S = 45.0



OLD_TOOL_RESULT_KEEP_CHARS = 300


def _shrink_old_tool_results(messages, keep_chars: int = OLD_TOOL_RESULT_KEEP_CHARS) -> int:
    """把「上一轮及更早」的工具返回原文压成开头摘要（2026-10-12）。

    实测 web_search 一次返回约 1 万字符，留在 dialogue 里之后连续 4 轮请求都带着它（1.9 万字符），
    而主人下一句话基本不再需要原文——LLM 当时已经据此答完了。
    只动「最后一条真实用户消息之前」的 tool 消息：本轮（含递归轮）正在用的结果不受影响。
    幂等；返回压缩的条数。
    """
    last_user = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if getattr(m, "role", "") == "user" and not getattr(m, "is_temporary", False):
            last_user = i
            break
    n = 0
    for m in messages[:max(last_user, 0)]:
        if getattr(m, "role", "") != "tool":
            continue
        c = getattr(m, "content", None)
        if isinstance(c, str) and len(c) > keep_chars + 40:
            m.content = f"{c[:keep_chars]}…（原文共{len(c)}字，已省略）"
            n += 1
    return n


def _wait_tool_result(conn, future, tool_call_data):
    """等工具返回，带超时。

    原先 future.result() 无超时：任何工具卡住（网络、同步插件）会让整轮永远不结束，
    TTS 队列没有 LAST、连接进入「任务进行中」状态，后续插话全被排队。
    超时/异常都转成 Action.ERROR 结果，交给统一的错误话术播报。
    """
    from plugins_func.register import Action, ActionResponse
    import concurrent.futures

    try:
        timeout = float(conn.config.get("tool_call_timeout_seconds", _TOOL_WAIT_TIMEOUT_S))
    except Exception:
        timeout = _TOOL_WAIT_TIMEOUT_S
    name = tool_call_data.get("name", "")
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        conn.logger.bind(tag=TAG).error(f"[工具超时] {name} 超过 {timeout:.0f}s 未返回，已取消")
        return ActionResponse(Action.ERROR, f"工具执行超时: {name}", None)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"[工具异常] {name}: {e!r}")
        return ActionResponse(Action.ERROR, f"Error 工具执行出错: {name}: {e}", None)


def _handle_function_result(conn, tool_results, depth):
    """处理工具调用结果"""
    from plugins_func.register import Action

    need_llm_tools = []

    for result, tool_call_data in tool_results:
        # 【v3.0 P1】dict 结果走模板填充（跳过 LLM 自由生成）
        if isinstance(result.result, dict):
            tool_name = tool_call_data.get('name', '')
            spec = getattr(conn, 'speech_specs', {}).get(tool_name, {})
            template = spec.get('result_template')
            polish_enabled = spec.get('polish', False)
            anchors = spec.get('anchors', [])

            # 【v3.0 P2.5】润色层：polish=true 时先调轻量 LLM 润色
            if polish_enabled and template:
                try:
                    polished = _polish_result(conn, result.result, template, anchors)
                    if polished:
                        text = polished
                        conn.logger.bind(tag=TAG).info(f"[speech_registry] 润色成功: {tool_name} -> {text}")
                    else:
                        text = template.format(**result.result)
                        conn.logger.bind(tag=TAG).info(f"[speech_registry] 润色失败回退模板: {tool_name} -> {text}")
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"[speech_registry] 润色异常回退模板: {e}")
                    text = template.format(**result.result)
            elif template:
                try:
                    text = template.format(**result.result)
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"[speech_registry] 模板填充失败，回退默认: {e}")
                    text = str(result.result)
            else:
                text = str(result.result)

            conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
            conn.dialogue.put(Message(role="assistant", content=text))
            continue

        if result.action in [
            Action.RESPONSE,
            Action.NOTFOUND,
            Action.ERROR,
        ]:
            text = result.response if result.response else result.result
            if result.action == Action.ERROR:
                # 2026-10-06：工具报错**不再把接口原文念给用户听**。
                # 主人反馈「我说播放今天的简报，它回了个 PushPlus 没积分」——
                # send_voice_message 返回的 "PushPlus 返回错误: 积分不足" 被原样播了出来。
                # 技术性报错（含 API/HTTP/超时/积分 等字样）换成有温度的人话，
                # 原文只进日志；业务性提示（如「设备不在允许列表」）仍然照说，用户需要知道。
                _tool_name = tool_call_data.get('name', '')
                _raw = str(text or "")
                conn.logger.bind(tag=TAG).error(f"[工具失败] {_tool_name}: {_raw[:200]}")
                # [2026-10-12 主人实测] 原来只按「技术性关键词黑名单」过滤，漏了一大片：
                # `search_from_ragflow` 报 `'dict' object has no attribute 'strip'`，
                # 黑名单里没有这些词 → 原文被 TTS 直接念，中文音素硬拼英文报错，
                # 主人听到「有点像闽南语」却听不懂。
                #
                # 教训：关键词黑名单**天然漏**（Python 报错形态无穷多）。
                # 改成**白名单**：只有明确判定为「给用户看的中文业务提示」才念，
                # 其余（含所有拿不准的）一律换成话术 —— 宁可不说，不可说错。
                if not _looks_like_user_message(_raw):
                    from core.utils.phrase_pool import pick
                    text = pick(TOOL_ERROR_LINES)
                    conn.logger.bind(tag=TAG).info(f"[工具失败] 已改用话术播报: {text!r}")
            conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
            conn.dialogue.put(Message(role="assistant", content=text))
        elif result.action == Action.REQLLM:
            need_llm_tools.append((result, tool_call_data))
        else:
            pass

    if need_llm_tools:
        all_tool_calls = [
            {
                "id": tool_call_data["id"],
                "function": {
                    "arguments": (
                        "{}"
                        if tool_call_data["arguments"] == ""
                        else tool_call_data["arguments"]
                    ),
                    "name": tool_call_data["name"],
                },
                "type": "function",
                "index": idx,
            }
            for idx, (_, tool_call_data) in enumerate(need_llm_tools)
        ]
        conn.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

        for result, tool_call_data in need_llm_tools:
            text = result.result
            if text is not None and len(text) > 0:
                conn.dialogue.put(
                    Message(
                        role="tool",
                        tool_call_id=(
                            str(uuid.uuid4())
                            if tool_call_data["id"] is None
                            else tool_call_data["id"]
                        ),
                        content=text,
                    )
                )

        # 【插播语义 2026-10-08 主人裁决】插话必须**立即**回答（像插播），
        # 原任务的结果**等插话回答播完再接着播**。
        # 主人原话：「用户插话一定是他特别着急的东西，所以要马上回复。回复完了以后
        # 再继续之前的任务……插播完了以后给回复，回复完了以后，这段就结束了，
        # 然后继续原来的任务。」
        # 为什么在这里等：工具结果已经拿到、接下来要让模型总结并播报；如果此刻
        # 插话的回答还在播，两轮文字会同时往 TTS 文本缓冲里写 → **字会交错**。
        _wait_until_idle(conn, [tc.get("name", "") for _, tc in need_llm_tools])
        chat(conn, None, depth=depth + 1)


# 等插话回答播完的兜底上限（防止 client_is_speaking 卡住把任务线程堵死）
_SPEECH_WAIT_MAX_S = 30.0
# 等待超过这么久才补衔接语（只是擦肩而过就别啰嗦了）
_RESUME_MIN_WAIT_S = 1.0


def _wait_until_idle(conn, tool_names=None) -> None:
    """等当前播报结束（插话的回答播完），再继续原任务的播报。

    只在「工具结果已拿到、准备生成播报」这一步等，任务本身（工具执行）从不等。
    真等到了（> _RESUME_MIN_WAIT_S）就补一句衔接语 —— 主人 2026-10-08：
    「插话的那个回答完了以后，如果要继续原来的对话，应该补一句垫场句子……
    这样就比较合理，不然的话就会感觉衔接上有点别扭。」
    """
    if not getattr(conn, "client_is_speaking", False):
        return
    # 2026-10-11：只有本轮之后确实开了新的一轮（插话）才需要等。
    # client_is_speaking 在本轮第一段音频（含过渡句）发出时就置真、要等本轮 LAST 才复位，
    # 原先把「本轮自己的过渡句还在播」也当成插话等 → 工具早已返回，这一轮却在等自己，
    # 空等到 30 秒上限（实测 10-08 起进入等待 40 次、12 次顶满 30s）。
    import threading as _th
    _my = getattr(conn, "_chat_turn_by_thread", {}).get(_th.get_ident())
    if _my is not None and getattr(conn, "_chat_turn_seq", None) == _my:
        conn.logger.bind(tag=TAG).info("[插播] 本轮之后没有新插话，正在播的是本轮自己的语音，不等")
        return
    t0 = time.time()
    conn.logger.bind(tag=TAG).info("[插播] 插话的回答正在播，等它播完再继续原任务的播报")
    while getattr(conn, "client_is_speaking", False):
        if time.time() - t0 > _SPEECH_WAIT_MAX_S:
            conn.logger.bind(tag=TAG).warning(
                f"[插播] 等待超过 {_SPEECH_WAIT_MAX_S:.0f}s，直接继续")
            break
        time.sleep(0.2)
    waited = time.time() - t0
    conn.logger.bind(tag=TAG).info(f"[插播] 插话播报已结束（等了 {waited:.1f}s），继续原任务")

    if waited < _RESUME_MIN_WAIT_S:
        return
    # 补一句衔接语，避免「突然跳回上一个话题」的别扭感
    try:
        from core.utils.lead_in import pick_resume

        line = pick_resume(tool_names)
        if not line:
            return
        conn.logger.bind(tag=TAG).info(f"[插播] 继续原任务前补衔接语: {line!r}")
        asyncio.run_coroutine_threadsafe(_tts_speak(conn, line), conn.loop)
        # 给它留出先播出来的时间（正文首帧合成本来也要 0.5~1s，这里再加一点保险）
        time.sleep(0.8)
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[插播] 补衔接语失败（忽略）: {e!r}")


def _save_dialogue_history(conn, user_text, ai_text):
    """保存对话历史到 devices.db"""
    import os
    conn_db = None
    try:
        import sqlite3
        # chat_engine.py 位于 core/chat/ 下，需向上两级到 server/homebrain-server/（拆分前在 core/ 下只需一级）
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn_db = sqlite3.connect(db_path)
        cursor = conn_db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dialogue_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                session_id TEXT,
                user_text TEXT,
                ai_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn_db.commit()
        cursor.execute(
            'INSERT INTO dialogue_history (device_id, session_id, user_text, ai_text) VALUES (?, ?, ?, ?)',
            (conn.device_id, getattr(conn, 'session_id', ''), user_text, ai_text)
        )
        conn_db.commit()
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"保存对话历史失败: {e}")
    finally:
        if conn_db:
            conn_db.close()


def _merge_tool_calls(tool_calls_list, tools_call):
    """合并工具调用列表"""
    for tool_call in tools_call:
        tool_index = getattr(tool_call, "index", None)
        if tool_index is None:
            if tool_call.function.name:
                tool_index = len(tool_calls_list)
            else:
                tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0

        if tool_index >= len(tool_calls_list):
            tool_calls_list.append({"id": "", "name": "", "arguments": ""})

        if tool_call.id:
            tool_calls_list[tool_index]["id"] = tool_call.id
        if tool_call.function.name:
            tool_calls_list[tool_index]["name"] = tool_call.function.name
        if tool_call.function.arguments:
            tool_calls_list[tool_index]["arguments"] += tool_call.function.arguments


def _get_system_error_response(config):
    """获取系统错误响应文本"""
    from core.utils.util import get_system_error_response
    return get_system_error_response(config)
