import time
import json
from pypinyin import lazy_pinyin
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.utils.wakeword_provider import get_wakeword_detector
from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from core.utils.filler_audio import is_filler_enabled, play_filler_until_ready
from core.utils.util import remove_punctuation_and_length
from core.providers.tools.device_mcp import MCPClient, send_mcp_initialize_message

TAG = __name__

# 强制简短唤醒回复的系统提示词：不自我介绍、不功能介绍、只回应唤醒
# 名字统一取自后台设置（core.utils.assistant_identity），后台改名后自动同步
_WAKEUP_SHORT_REPLY_TEMPLATE = """你是{name}，用户的AI管家。用户刚刚唤醒了你，请用一句简短、自然、温暖的话回应，表示你在听。

重要约束：
- 不要自我介绍（不要说"我是{name}"、"我是你的AI管家"等）。
- 不要介绍功能（不要说"我可以控制家电、查天气、陪你聊天"等）。
- 不要用问句主动询问"有什么可以帮您"，除非用户之前习惯如此。
- 只简单回应，例如："在呢，请说"、"我在"、"嗯，听着呢"。
- 控制在15字以内，越短越好。"""


def get_wakeup_short_reply_prompt() -> str:
    """构建唤醒简短回复提示词（AI 名字从后台设置读取）"""
    # [2026-10-06] 原来这里是 try: get_name() or "伊娃" / except: "伊娃"，
    # 兜底写死人名。改用 safe_name()，异常时也不会念出别人的名字。
    from core.utils.assistant_identity import safe_name

    ai_name = safe_name()
    return _WAKEUP_SHORT_REPLY_TEMPLATE.format(name=ai_name)


def _is_wakeup_only_text(text: str) -> bool:
    """判断用户输入是否只是唤醒词（无实质指令），用于决定是否生成简短回复。

    判定方式（2026-10-05 主人定案改为「剥离法」）
    ------------------------------------------------
    旧实现用**长度门槛**：``len(text) <= len(唤醒词) + 3`` 才算纯唤醒。
    这个门槛在主人连喊多遍唤醒词时必然失效 —— 实测一句
    '你好，小玉你好，小玉你好，小玉你好小玉你好，小玉。'（20 字）被判成
    「包含指令」，于是把 20 个字整个丢给 LLM；而云端 LLM 首token 延迟
    实测 14.4 秒，这期间音频继续粘连、识别串扰，唤醒与对话双双错乱。

    新做法：调用 ``wakeword_provider.strip_wakewords`` 把唤醒词逐个剥离干净，
    剥完只剩标点和空白即视为纯唤醒（下列示例里的名字请换成你后台设置的名字）：
        '你好小玉你好小玉你好小玉'  → ''        → 纯唤醒，播「在呢」
        '你好小玉你好小玉现在几点了' → '现在几点了' → 有指令，丢给 LLM

    保留原有的「常见招呼词」判断作为补充：像「在吗」「喂」这类不含唤醒词的
    招呼，剥离法会原样返回（剥不掉），仍需靠下面的白名单识别。
    """
    if not text:
        return True

    # ===== 主判定：剥离唤醒词后是否为空 =====
    try:
        from core.utils.wakeword_provider import get_wakeword_detector

        wp = get_wakeword_detector()
        if wp and wp.enabled:
            remainder, _count = wp.strip_wakewords(text)
            # 剥得动且剥空了 → 纯唤醒
            if not remainder:
                return True
    except Exception:
        # 剥离不可用时**不能**退化成「有指令」，否则会把纯唤醒当对话送 LLM。
        # 交给下面的白名单兜底判断。
        pass

    # ===== 补充判定：常见招呼词白名单（不含唤醒词的口头禅）=====
    # 常见唤醒词或空泛招呼
    wake_only_patterns = [
        "你好", "您好", "喂", "嘿", "嗨", "hi", "hello",
        "在吗", "在不在", "在呢", "醒了", "小智",
    ]
    # AI 名字/昵称从后台设置读取（后台改名后自动同步）
    # [2026-10-06] except 分支原来 append("伊娃")，兜底写死人名 → 改用 safe_*
    try:
        from core.utils.assistant_identity import safe_name, safe_nickname

        for n in (safe_name(), safe_nickname()):
            if n and n not in wake_only_patterns:
                wake_only_patterns.append(n)
    except Exception:
        pass
    stripped = text.strip().lower()
    for p in wake_only_patterns:
        if p in stripped and len(stripped) <= len(p) + 3:
            return True
    return False


def _generate_short_wakeup_reply(conn: "ConnectionHandler") -> str:
    """生成唤醒回复 - 使用规则层，不调用 LLM（避免 4-5 秒延迟）"""
    import random

    # 预定义的唤醒回复列表（简短、自然、温暖）
    # 2026-10-12：口语化（「请说」是客服腔，真人被叫一声会说「哎，在呢」「嗯，你说」）
    wakeup_replies = [
        "在呢",
        "哎，在呢",
        "我在呢",
        "嗯，我在",
        "在呢，你说",
    ]

    # 随机选择一个回复
    reply = random.choice(wakeup_replies)
    conn.logger.bind(tag=TAG).info(f"[L1唤醒回复] {reply}")
    return reply


async def handleHelloMessage(conn: "ConnectionHandler", msg_json):
    """处理hello消息"""
    # 保存 hello 消息中的 device_id（用于 ASR session key）
    hello_device_id = msg_json.get("device_id")
    if hello_device_id:
        conn.hello_device_id = hello_device_id
        conn.logger.bind(tag=TAG).debug(f"hello device_id: {hello_device_id}")

    audio_params = msg_json.get("audio_params")
    if audio_params:
        format = audio_params.get("format")
        conn.logger.bind(tag=TAG).debug(f"客户端音频格式: {format}")
        conn.audio_format = format
        conn.welcome_msg["audio_params"] = audio_params
    features = msg_json.get("features")
    if features:
        # 兼容数组格式（旧客户端可能发 features: ["aec", "idle_guard"]）
        if isinstance(features, list):
            features = {f: True for f in features if isinstance(f, str)}
            conn.logger.bind(tag=TAG).info(f"客户端特性从数组转为对象: {features}")
        conn.logger.bind(tag=TAG).debug(f"客户端特性: {features}")
        conn.features = features
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).debug("客户端支持MCP")
            conn.mcp_client = MCPClient()
            # 发送初始化
            asyncio.create_task(send_mcp_initialize_message(conn))

        # AEC 回声消除支持（参照小智 kAecOnServerSide）
        if features.get("aec"):
            conn.logger.bind(tag=TAG).info("客户端支持 AEC，初始化回声消除器")
            conn.aec_enabled = True
            from core.utils.aec import AECProcessor
            sample_rate = audio_params.get("sample_rate", 16000) if audio_params else 16000
            # 回声延迟：网页端播放器要先缓冲 600ms 才出声（pcm-player.js
            # BUFFER_THRESHOLD_MS），参考信号必须按这个延迟往回对齐，否则 AEC 取到
            # 的是「还没播出来的未来音频」。可用 aec_echo_delay_ms 配置微调。
            try:
                _delay = int(conn.config.get("aec_echo_delay_ms", 600) or 600)
            except Exception:
                _delay = 600
            conn.aec = AECProcessor(sample_rate=sample_rate, echo_delay_ms=_delay)

        # 免唤醒守卫：常开推流的连接（ESP32 设备 / Web 终端）一律启用，
        # 会话未激活时只放行含唤醒词的语音，其余静默丢弃。
        # 2026-09-28 主人裁决：Web 终端不再 ASR 直通。此前 web-terminal 跳过
        # 门控，待机时误拾音（如 Mac FN 听写、对他人说话）直通 LLM 触发回答。
        # 激活方式：点"开始对话"按钮（listen start）或唤醒词"你好伊娃"命中。
        if features.get("idle_guard"):
            conn.idle_guard = True
            conn.logger.bind(tag=TAG).info("连接启用 idle_guard（免唤醒守卫，含 Web 终端）")

    await conn.websocket.send(json.dumps(conn.welcome_msg))

    # 在欢迎消息后下发唤醒词配置（支持实时更新）
    try:
        wakeword_detector = get_wakeword_detector()
        if wakeword_detector and wakeword_detector.wake_word:
            # 设备端 MultiNet 只接受拼音/ASCII 命令词
            # 兼容中英文逗号，下发用户配置 + 基础唤醒词
            normalized = wakeword_detector.wake_word.replace('，', ',')
            wake_words = [w.strip() for w in normalized.split(',') if w.strip()]
            # AI 名字转拼音（设备端 MultiNet 只认拼音命令词，如「小玉」→「xiao yu」）
            # 名字统一取自后台设置，改名后自动同步。
            # [2026-10-06] 原来兜底写死 'yi wa'（= 伊娃的拼音），换部署就是错的唤醒词。
            # 现在兜底从后台名实时转拼音，取不到才退到通用默认名。
            try:
                ai_name = (getattr(wakeword_detector, 'ai_name', '') or '').strip()
                name_pinyin = ' '.join(lazy_pinyin(ai_name)).lower() if ai_name else ''
            except Exception:
                name_pinyin = ''
            if not name_pinyin:
                from core.utils.assistant_identity import safe_name

                name_pinyin = ' '.join(lazy_pinyin(safe_name())).lower()
            base_wake_word = f'ni hao {name_pinyin}'
            if base_wake_word not in wake_words:
                wake_words.append(base_wake_word)
            if wake_words:
                config_msg = {
                    "type": "config",
                    "config": {
                        "wake_words": wake_words,
                    },
                    "session_id": conn.session_id,
                }
                await asyncio.sleep(0.05)
                await conn.websocket.send(json.dumps(config_msg))
                conn.logger.bind(tag=TAG).info(f"已下发唤醒词配置: {wake_words}")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"下发唤醒词配置失败: {e}")


async def checkWakeupWords(conn: "ConnectionHandler", text):
    # 方案 A：跳过 TTS 回复，直接激活会话，让设备立即进入录音模式
    # 这样可以避免 TTS 播放导致的延迟，让用户的第一句话能被识别

    _, filtered_text = remove_punctuation_and_length(text)
    wakeword_detector = get_wakeword_detector()
    is_wakeup, matched_word, confidence = wakeword_detector.detect(filtered_text)
    if not is_wakeup:
        return False

    # ===== 2026-10-05 修复（主人裁定）：唤醒通知无条件下发 =====
    # 背景：本函数原先把「通知前端进入 listening」放在两处 return False 之后，
    # 于是只要文本不满足 _is_wakeup_only_text 的长度门槛
    #（len(stripped) <= len(词) + 3），这条 listen/wakeup 就永远发不出去，
    # 前端按钮停在 IDLE —— 主人喊了 4 遍"你好伊娃"却看不到唤醒，
    # 日志显示 detect 已命中（confidence=0.41）但 result=False，界面毫无反应。
    #
    # 主人定案的核心原则：**只要有唤醒词就一定要唤醒**，句子多长、有没有别的
    # 内容都不影响唤醒。唤醒是前提，唤不醒后面一切都没有意义。
    # 「是否继续当正常对话处理」与「要不要唤醒」必须解耦：
    #   - 唤醒通知：有唤醒词就发，且只发一条（多个唤醒词也只唤醒一次）
    #   - 后续对话：由文本内容决定，剩余的唤醒词照常丢给 LLM，
    #     它会自己判断怎么回（主人原话：可能会回"你叫我这么多遍干嘛呀"）
    #
    # 注意：这条消息只下发一次，下方不再重复发送。
    try:
        await conn.websocket.send(json.dumps({"type": "listen", "state": "wakeup"}))
        conn.logger.bind(tag=TAG).info(
            f"唤醒词命中，已通知前端进入对话态: {matched_word}"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"下发唤醒通知失败: {e}")

    # 如果文本里不只是唤醒词，还包含实质指令，走正常对话
    if not _is_wakeup_only_text(filtered_text):
        conn.logger.bind(tag=TAG).info(
            f"唤醒后文本包含指令，不再短回复，走正常对话: {filtered_text}"
        )
        return False

    # [2026-10-11 主人实测反馈] 唤醒后必须有回应。
    # 原代码在这里「跳过TTS回复，直接激活会话」，导致本地 ASR（SenseVoice）路径
    # 唤醒后完全静默——主人 23:12 实测「你好，汤圆。」，日志显示命中并激活，
    # 但没有任何音频下发。而云端流式 ASR 路径（asr/base.py:992）本来就调
    # play_short_wakeup_reply，所以主人说「之前是有的」= 切本地 ASR 后才丢的。
    #
    # 回应走规则层（_generate_short_wakeup_reply，不调 LLM，零延迟），
    # 播放期间不置 just_woken_up：保持「唤醒后可立即说话」的原有体验。
    conn.logger.bind(tag=TAG).info(f"唤醒词检测成功，播放唤醒回应: {matched_word}")
    try:
        await play_short_wakeup_reply(conn)
    except Exception as e:
        # 回应失败不能挡住会话激活，否则唤醒等于失效
        conn.logger.bind(tag=TAG).warning(f"播放唤醒回应失败（会话仍激活）: {e}")

    return True


async def play_short_wakeup_reply(conn: "ConnectionHandler", reply_text: str = None):
    """播放一句简短唤醒回复（供 checkWakeupWords 或 ASR 兜底调用）。"""
    if reply_text is None:
        reply_text = _generate_short_wakeup_reply(conn)
    # 不再发送 TTS start 消息，避免工具调用时的"咔咔"杂音
    # TTS start 应该在有实际音频时才发送（由 sentence_start 触发）

    # 将唤醒词回复视为新会话，生成新的 sentence_id，确保流控器重置
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.client_abort = False

    conn.logger.bind(tag=TAG).info(f"播放唤醒词回复: {reply_text}")

    # J6 等待短语：TTS 合成期间循环播放预缓存填充音，掩盖起播延迟
    # 音频包按序进入流控队列，设备依次播放，正式 TTS 包入队后无缝衔接
    tts_result = None
    if is_filler_enabled(conn):
        tts_done = asyncio.Event()

        async def _run_tts():
            nonlocal tts_result
            tts_result = await asyncio.to_thread(conn.tts.to_tts, reply_text)
            tts_done.set()

        tts_task = asyncio.create_task(_run_tts())
        try:
            await play_filler_until_ready(conn, tts_done.is_set)
        finally:
            # 确保填充结束后 TTS 任务已完成（被打断时也等待其结束，避免悬挂）
            await asyncio.shield(tts_task)
        if tts_result is None:
            conn.logger.bind(tag=TAG).warning("唤醒回复 TTS 生成失败")
            return
    else:
        # 生成TTS音频
        tts_result = await asyncio.to_thread(conn.tts.to_tts, reply_text)
    if tts_result:
        if isinstance(tts_result, list):
            opus_packets = tts_result
        else:
            opus_packets = await audio_to_data(tts_result, use_cache=False)
        await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, reply_text)
        await sendAudioMessage(conn, SentenceType.LAST, [], None)
    else:
        conn.logger.bind(tag=TAG).warning("唤醒回复 TTS 生成失败")

    # 补充对话
    conn.dialogue.put(Message(role="assistant", content=reply_text))
