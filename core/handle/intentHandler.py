import json
import re
import uuid
import time
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType
from core.utils import confirm_gate

TAG = __name__

# ┌─────────────────────────────────────────────────────────────────────┐
# │ 暂停指令（2026-10-12 主人裁定）                                      │
# ├─────────────────────────────────────────────────────────────────────┤
# │ 主人在播报过程中说「停」「等等」「等一下」等词，语义是：               │
# │   **停止正在播报的内容**，并把这句话当成控制指令，不要当成新问题丢给   │
# │   大模型，等主人说下一句。                                           │
# │                                                                     │
# │ 原行为（实测日志）：                                                 │
# │   识别文本: 停。  → 提交 LLM chat → LLM 理解为「暂停音乐」           │
# │   发送第一段语音: 好的，已经暂停播放啦                               │
# │ 完全违背语义：播报还在继续，且助手又用新回复打断了主人下一句。        │
# │                                                                     │
# │ 只收短文：带其他内容的句子（「等一下我有事」）仍应走正常对话，         │
# │ 免得把主人的真实提问误吞掉。                                         │
# └─────────────────────────────────────────────────────────────────────┘
_PAUSE_COMMANDS = {
    "停", "停下", "停一下", "停止", "别说了", "不要说了", "安静", "闭嘴",
    "等等", "等一下", "等等一下", "等一下下", "等下", "暂停", "暂停一下",
}


def _is_pause_command(text: str) -> bool:
    """是否「暂停播报」指令（短命令，去掉标点和空白后整句匹配）。"""
    if not text:
        return False
    cleaned = text.strip()
    for ch in "。，！？、,.!?~～ 　":
        cleaned = cleaned.replace(ch, "")
    if not cleaned or len(cleaned) > 6:
        # 太长说明还带别的意图（「等一下我问你」），不该吞掉
        return False
    return cleaned in _PAUSE_COMMANDS


# [2026-10-12 主人实测] 「停」能停，但说「继续」时它播了另外一条，不是从
# 停止位置继续。主人原话：「我说『继续』的时候，它并没有从停止的位置继续
# 播放剩下的内容，而是开始播另外一条了。所以这个逻辑不对。」
# 根因：暂停时 clear_queues() 把未播内容丢了，「继续」只能重新走 LLM。
# 现在暂停会留下断点（abortHandle.salvage_unplayed_text），这里负责续播。
_RESUME_COMMANDS = {
    "继续", "继续说", "继续播", "继续念", "继续讲", "继续读",
    "接着说", "接着播", "接着念", "接着讲", "接着读", "接着来",
    "你继续", "你接着说", "你继续说", "说下去", "读下去", "念下去",
    "继续播放", "接着播放", "往下说", "往下念", "往下读",
}


# 口语变体：「继续播放刚才的新闻」「接着讲那个故事」「请继续说」……
# 只在**有断点**时才会被当成续播（无断点交回 LLM），所以放宽匹配不会吞掉
# 「继续播放音乐」这类没暂停过的正常指令。
_RESUME_LOOSE_RE = re.compile(
    r"^(?:请|你|麻烦你?|帮我)?(?:继续|接着)"
    r"(?:播放?|说|念|讲|读|来|往下说|往下讲|往下念|往下读)?"
    r"(?:刚才|刚刚|刚|上面|前面|之前)?(?:的)?(?:那|这|一)?(?:个|条|段)?"
    r"(?:新闻|消息|故事|内容|文章)?(?:吧|呀|啊|哦)?$"
)


def _is_resume_command(text: str) -> bool:
    """是否「从断点继续播报」指令（与 _is_pause_command 同一套清洗规则）。"""
    if not text:
        return False
    cleaned = text.strip()
    for ch in "。，！？、,.!?~～ 　":
        cleaned = cleaned.replace(ch, "")
    if not cleaned or len(cleaned) > 12:
        return False
    if len(cleaned) <= 6 and cleaned in _RESUME_COMMANDS:
        return True
    return bool(_RESUME_LOOSE_RE.match(cleaned))


async def handle_user_intent(conn: "ConnectionHandler", text):
    user_intent_start_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] handle_user_intent 开始, ts={user_intent_start_ts}")

    # 预处理输入文本，处理可能的JSON格式
    # [2026-10-13 修复] 这段原来在暂停/继续判断**之后**。语音链路进来的文本是
    # {"speaker": "李威", "content": "继续播放。"} 这种 JSON，整串远超 6 个字，
    # 「停」「继续」永远匹配不上 → 「停」被 LLM 当成别的指令、「继续」重新走 LLM
    # 播了另一条新闻（主人实测；也是「全历史零条 [暂停指令]」的真正原因，
    # 文字通道直接传纯文本所以一直测得通）。必须先解包再匹配。
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # 提取content用于意图分析
                conn.current_speaker = parsed_data.get("speaker")  # 保留说话人信息
    except (json.JSONDecodeError, TypeError):
        pass

    # [2026-10-12 主人裁定] 暂停播报指令优先于一切流程：
    # 「停」「等等」「等一下」= 停止出声、不当新指令、等主人下一句。
    # 放在最前面：连唤醒词/JEV 判停都不该介入（主人正在打断，不是新对话）。
    if _is_pause_command(text):
        conn.logger.bind(tag=TAG).info(f"[暂停指令] 命中，停止播报: {text!r}")
        # 让 TTS 停止合成/播报；不置 standby，会话保持激活，
        # 主人下一句可以接着说（符合「等下一句」的语义）。
        conn.client_abort = True
        try:
            from core.handle.abortHandle import handleAbortMessage
            # pause=True：清队列前先把未播文本留下，主人说「继续」才能接着念
            await handleAbortMessage(conn, pause=True)
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[暂停指令] abort 执行失败: {e!r}")
        return True

    # [2026-10-12] 「继续」= 从暂停的断点接着念剩下的内容。
    # 同样放在最前面：这是接续上一次播报，不是新对话，不该让 LLM 重新理解
    # （重新理解就会「开始播另外一条」，正是主人实测到的错误行为）。
    if _is_resume_command(text):
        residual = str(getattr(conn, "_paused_residual", "") or "").strip()
        if residual:
            conn.logger.bind(tag=TAG).info(
                f"[继续指令] 从断点续播 {len(residual)} 字: {residual[:30]}...")
            conn._paused_residual = ""
            conn.close_after_chat = False
            conn.client_abort = False
            # 重置 TTS 分段状态：processed_chars / tts_text_buff 都是上一轮的
            # 残留，不重置会让 _get_segment_text 从错误位置切、或直接切不出段。
            _tts = getattr(conn, "tts", None)
            if _tts is not None:
                _tts.processed_chars = 0
                _tts.tts_text_buff = []
                _tts.is_first_sentence = True
                _tts.tts_stop_request = False
                _tts._pending_tts_seg = ""
                _tts._tts_first_seg_sent = False
            try:
                speak_txt(conn, residual)
            except Exception as e:
                conn.logger.bind(tag=TAG).warning(f"[继续指令] 续播失败: {e!r}")
            return True
        # 没有断点可续（没暂停过 / 已经续完）→ 交回正常流程，
        # 让 LLM 按上下文理解「继续」（例如继续放下一首歌），不要硬拦。
        conn.logger.bind(tag=TAG).info("[继续指令] 无断点可续，交回正常流程")

    # [2026-10-13 事情 B] 走到这里说明这句话既不是「停/等等」也不是「继续」
    # = 真指令。handleAbortMessage 现在掐断时**默认**抢救断点（VAD 层掐断
    # 那一刻 ASR 还没出文本，分不清暂停还是新指令），真指令到来就必须把
    # 存下的断点丢弃：否则主人下次说「继续」会突然念出上一轮的旧内容。
    if getattr(conn, "_paused_residual", None):
        conn.logger.bind(tag=TAG).info("[断点] 真指令到来，丢弃上一轮掐断留下的未播断点")
        conn._paused_residual = None

    # 危险操作确认闸门：推进对话轮次号（2026-10-04 主人裁决，异步式确认）。
    # 待确认动作会记录创建时的轮次，闸门放行时要求 created_turn < current_turn，
    #以此证明主人中间真的开口说过话，堵死 LLM 在同一轮内自问自答绕过确认。
    confirm_gate.mark_turn(conn)

    # 等待组件初始化完成（修复 detect 文本路径竞态：初始化未完成时 intent_type
    # 仍为默认 nointent，导致 LLM 无工具可用、handle_exit_intent 等工具无法触发）
    wait_deadline = time.time() + 5
    while not getattr(conn, "components_ready", True) and time.time() < wait_deadline:
        await asyncio.sleep(0.1)

    # 检查是否有明确的退出命令
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    # 检查语音切换中央形象（2026-09-24，主人批准）：星球链路说"切换到数字人/数字星球"
    if await check_mode_switch(conn, filtered_text):
        return True

    # 检查是否是唤醒词
    wakeup_check_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 开始检查唤醒词, ts={wakeup_check_ts}")
    wakeup_result = await checkWakeupWords(conn, filtered_text)
    wakeup_done_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 唤醒词检查完成, ts={wakeup_done_ts}, 耗时={wakeup_done_ts - wakeup_check_ts:.0f}ms, result={wakeup_result}")
    if wakeup_result:
        return True

    if conn.intent_type == "function_call":
        # 使用支持function calling的聊天方法,不再进行意图分析
        conn.logger.bind(tag=TAG).info(f"[耗时] function_call 模式, 跳过意图分析")
        return False
    # 使用LLM进行意图分析
    intent_llm_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 开始 LLM 意图分析, ts={intent_llm_ts}")
    intent_result = await analyze_intent_with_llm(conn, text)
    intent_llm_done_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] LLM 意图分析完成, ts={intent_llm_done_ts}, 耗时={intent_llm_done_ts - intent_llm_ts:.0f}ms")
    if not intent_result:
        return False
    # 会话开始时生成sentence_id
    conn.sentence_id = str(uuid.uuid4().hex)
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)


async def check_mode_switch(conn: "ConnectionHandler", text):
    """语音切换中央形象（2026-09-24，主人批准）：星球链路说"切换到数字人/数字星球"。

    与数字人链路（llm_proxy）共用 center_window_api.detect_mode_switch 同源词表
    与否定语境排除，保证两边行为一致（主人要求）。命中→写 center-window 配置
    （只改 mode，保留 avatar/背景）+ TTS 短应答；星球页面轮询发现 mode 变化后
    自动刷新进入数字人。"""
    try:
        from core.api.center_window_api import detect_mode_switch, switch_mode_to, _read_cfg
        target = detect_mode_switch(text or "")
        if not target:
            return False
        conn.logger.bind(tag=TAG).info(f"[切换形象] 星球链路命中: {text} → mode={target}")
        await send_stt_message(conn, text)
        # 时序关键：先播应答、再写配置。若先写配置，前端轮询 2s 内就会
        # reload 并断开 WS（1001 going away），应答语音来不及播放即丢失
        # （2026-09-24 主人实测实锤）。按字长估算播完时长（4.5 字/秒 +
        # 2s 合成与缓冲余量，下限 4s 上限 8s），再落配置触发前端刷新。
        reply = (
            "好嘞，这就切到数字人。"
            if target == "digital_human"
            else "好的，已切回数字星球。"
        )
        await send_tts_message(conn, "sentence_start", reply)
        await send_tts_message(conn, "stop", None)
        await asyncio.sleep(min(8.0, max(4.0, len(reply) / 4.5 + 2.0)))
        ok, msg = switch_mode_to(target)
        if not ok:
            conn.logger.bind(tag=TAG).error(f"[切换形象] 配置写入失败: {msg}")
        return True
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"[切换形象] 星球链路处理失败: {e}")
        return False


async def _speak_farewell_before_standby(conn):
    """回待机前播一句极短告别（主人 2026-10-08 明确要求）。

    主人原话：「另外说退出、停止什么的，退回到待机状态的话，只需要简单回复两个字就行，
    比如『好的啊』；甚至不回复也可以，或者回复『下次见』、『一会儿见』，类似这样的话就可以。」

    ⚠️ 必须在 handleAbortMessage 之后调用：abort 会置 client_abort 并清空 TTS 队列，
    所以这里要重新清标志 + 换新 sentence_id 才能出声（与 chat_engine._send_quick_reply 同理）。
    播完再返回，避免紧接着的 standby 界面切换把这句话切掉。
    """
    try:
        from core.utils.lead_in import pick_farewell

        text = pick_farewell()
        if not text:
            return
        from core.chat.chat_engine import _send_quick_reply

        _send_quick_reply(conn, text)
        conn.logger.bind(tag=TAG).info(f"回待机前告别: {text!r}")
        # 极短句（2~4 字）约 0.8~1.2 秒；给足播完的时间再切待机界面
        await asyncio.sleep(min(2.0, 0.35 * len(text) + 0.7))
    except Exception as e:
        conn.logger.bind(tag=TAG).debug(f"告别语播报失败（忽略）: {e!r}")


async def check_direct_exit(conn: "ConnectionHandler", text):
    """检查是否有明确的退出命令"""
    # ========================================================================
    # 禁止修改：退出命令处理与会话停用
    # 功能：检测用户说"退出"等命令，停用会话并关闭连接
    # 负责人：bot-eva
    # 修改需获得 bot-eva 同意
    # 实现要点：
    # 1. 检测到退出命令后，设置 conn._session_active = False
    # 2. 发送 STT 消息到设备
    # 3. 关闭连接
    # ------------------------------------------------------------------------
    # 2026-10-08：按主人明确要求，在「回待机/关连接」前补一句极短告别
    # （「好的」「下次见」「一会儿见」）。除新增这一句外未改动原有逻辑。
    # ========================================================================
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    from core.utils.exit_intent import is_media_stop_command, is_bare_stop_command

    # [2026-10-11] 裸「停止」只打断播报，不当退出。
    # 主人实测：说「停止，关闭窗口」→ 被当退出命令直接进 standby，
    # 后面的话全被 idle_guard 吞掉。打断不碰会话激活态（2026-09-29 裁决）。
    if is_bare_stop_command(text):
        conn.logger.bind(tag=TAG).info(
            f"『{text}』是纯停止词：只打断当前播报，不退出会话")
        from core.handle.abortHandle import handleAbortMessage
        await handleAbortMessage(conn)
        return True

    for cmd in cmd_exit:
        # 宽松匹配：精确命中或以退出命令开头（"退出"/"退出吧"/"退下了"等均可）
        if text == cmd or text.startswith(cmd):
            # 2026-10-07 主人实测：退出词表里的「停止」会把「停止播放音乐」
            # 也吃掉（规则是「以退出命令开头」），于是让他停音乐却退出了会话。
            # 含媒体词的「停止/关闭」一律不当退出，交给 LLM 去控制媒体。
            if is_media_stop_command(text):
                conn.logger.bind(tag=TAG).info(
                    f"『{text}』含媒体词，不当退出命令处理（转由 LLM 控制媒体）")
                break
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            # 小智方案：退出时停用会话
            if hasattr(conn, '_session_active'):
                conn._session_active = False
            await send_stt_message(conn, text)
            # Web 终端（device_id 以 web-terminal 开头）：不关闭连接——
            # 关闭后前端 3 秒自动重连，等于没退出。
            # 改为进入待机：启用 idle_guard 门控（此后仅唤醒词/开始对话可激活）、
            # 停止当前播报，并通知前端切换待机界面。
            device_id = getattr(conn, 'device_id', '') or ''
            if device_id.startswith('web-terminal'):
                conn.idle_guard = True
                # 2026-09-28 修复：停止播报必须立即生效。原实现走
                # send_tts_message("stop")，其 _wait_for_audio_completion 会按
                # 实时播放速度等待音频队列排空（实测积压时卡 74 秒，主人说
                # "停止播放"期间 TTS 全程继续输出，且点击"结束"的 abort 也被
                # 拖延处理）。改为 handleAbortMessage：立即置打断标志、清空
                # TTS 文本/音频队列、复位流控器并下发 tts stop。
                from core.handle.abortHandle import handleAbortMessage
                await handleAbortMessage(conn)
                # 回待机前补一句极短告别（主人 2026-10-08 要求）
                await _speak_farewell_before_standby(conn)
                # [2026-10-13 音乐不停对话] 旧「退出待机 → restore_volume 恢复被
                # 自动降音量/暂停的音乐」已删除：该流程已不存在，无恢复对象。
                try:
                    await conn.websocket.send(json.dumps({"type": "state", "state": "sleep"}))
                except Exception:
                    pass
                conn.logger.bind(tag=TAG).info("Web 终端进入待机模式，等待唤醒词")
            else:
                # 设备端同样先道个别再关连接（主人 2026-10-08 要求）
                await _speak_farewell_before_standby(conn)
                await conn.close()
            return True
    return False


async def analyze_intent_with_llm(conn: "ConnectionHandler", text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None

    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")

    return None


async def process_intent_result(
    conn: "ConnectionHandler", intent_result, original_text
):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)

        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            conn.logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False

                def process_context_result():
                    conn.dialogue.put(Message(role="user", content=original_text))

                    from core.utils.current_time import get_current_time_info

                    current_time, today_date, today_weekday, lunar_date = (
                        get_current_time_info()
                    )

                    # 构建带上下文的基础提示
                    context_prompt = f"""当前时间：{current_time}
                                        今天日期：{today_date} ({today_weekday})
                                        今天农历：{lunar_date}

                                        请根据以上信息回答用户的问题：{original_text}"""

                    response = conn.intent.replyResult(context_prompt, original_text)
                    speak_txt(conn, response)

                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # 使用executor执行函数调用和结果处理
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))

                # 使用统一工具处理器处理所有工具调用
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        conn.func_handler.handle_llm_function_call(
                            conn, function_call_data
                        ),
                        conn.loop,
                    ).result()
                except Exception as e:
                    conn.logger.bind(tag=TAG).error(f"工具调用失败: {e}")
                    result = ActionResponse(
                        action=Action.ERROR, result=str(e), response=str(e)
                    )

                if result:
                    if result.action == Action.RESPONSE:  # 直接回复前端
                        text = result.response
                        if text is not None:
                            speak_txt(conn, text)
                    elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
                        text = result.result
                        conn.dialogue.put(Message(role="tool", content=text))
                        llm_result = conn.intent.replyResult(text, original_text)
                        if llm_result is None:
                            llm_result = text
                        speak_txt(conn, llm_result)
                    elif (
                        result.action == Action.NOTFOUND
                        or result.action == Action.ERROR
                    ):
                        text = result.result
                        if text is not None:
                            speak_txt(conn, text)
                    elif function_name != "play_music":
                        # For backward compatibility with original code
                        # 获取当前最新的文本索引
                        text = result.response
                        if text is None:
                            text = result.result
                        if text is not None:
                            speak_txt(conn, text)

            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False


def speak_txt(conn: "ConnectionHandler", text):
    # 记录文本
    conn.tts_MessageText = text

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
