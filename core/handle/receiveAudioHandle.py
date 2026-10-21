import time
import json
import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import (
    handle_user_intent,
    _PAUSE_COMMANDS,
    _RESUME_COMMANDS,
)
from core.providers.asr.dto.dto import InterfaceType
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType
from core.utils.emergency_listener import emergency_listener
from core.providers.tts.dto.dto import ContentType

TAG = __name__


# ┌─────────────────────────────────────────────────────────────────────┐
# │ adaptive interruption（jarvis 3.2 方案 A，2026-10-12 完整落地）        │
# ├─────────────────────────────────────────────────────────────────────┤
# │ 调研原文：                                                           │
# │   interruption={"mode": "adaptive"}                                  │
# │   「用 turn detector 区分『真打断』和 backchannel（"嗯"、"对"），       │
# │     后者不打断。家人在旁边说『嗯』『对』不应该打断播报。」              │
# │                                                                      │
# │ 原实现：播报期间只要 VAD 出人声就 abort —— 家人一句「嗯」就切掉播报。  │
# │                                                                      │
# │ 我们的 turn detector 基础设施 = silero 并行预判写进                    │
# │ `conn._vad_probe_text` 的 ASR 预览文本。用它做内容判定。              │
# │                                                                      │
# │ ⚠️ 保守原则：拿不到预览文本时**保持原行为（打断）**。理由：            │
# │   · 预判有延迟，短促人声可能还没出文本；                             │
# │   · 「没有证据」不等于「是附和」；                                    │
# │   · 漏拦真指令（主人说「停」却没停）比多拦一句附和严重得多。          │
# └─────────────────────────────────────────────────────────────────────┘
# 只收「确定是附和」的：纯应声 / 极短肯定 / 极短否定应答。
# 刻意**不收**「好」「行」这类也是应声但更像指令收尾的词 —— 主人实测
# 「说好不打断」本来是靠能量+时长挡的，这里再叠一层会改变已验证的行为。
_BACKCHANNEL_RE = re.compile(
    r"^[\s，。！？、,.!?~～]*(?:"
    r"嗯+|唔+|呃+|哦+|噢+|诶|哎|啊+"
    r"|对对|对的|对|是是|是的|是啊|是"
    r"|明白了?|了解|知道了?|收到|懂了?"
    r")[\s，。！？、,.!?~～]*$"
)


def _is_backchannel_during_speech(conn) -> bool:
    """播报期间这段人声是不是「附和音」（不打断）。

    Returns:
        True  = 附和音，别打断
        False = 真指令/判断不了，照常打断
    """
    probe = str(getattr(conn, "_vad_probe_text", "") or "").strip()
    if not probe:
        return False  # 没有证据 → 保守，保持原行为
    if len(probe) > 6:
        return False  # 太长的通常是真内容（「对 你说得对」之类）
    return bool(_BACKCHANNEL_RE.match(probe))


# ┌─────────────────────────────────────────────────────────────────────┐
# │ 播报期掐断时机改造（2026-10-13，主人批准）                            │
# ├─────────────────────────────────────────────────────────────────────┤
# │ 坏行为（四个日志文件全历史坐实）：                                    │
# │   ① VAD 层一判到人声就立刻 handleAbortMessage（不带 pause）→          │
# │      clear_queues 把未播内容销毁；                                    │
# │   ② ASR 才出文本「停」→ 断点已丢，「继续」接不上（全历史零条          │
# │      [暂停指令]/[继续指令] 生效日志）；                               │
# │   ③ 附和音「哦」「嗯」只有几百毫秒，旧判据依赖 silero 预判预览文本，  │
# │      文本来不及产出 → 实测 100% 漏过，一句附和就把播报切掉。          │
# │ 修法：把「要不要掐断」的决定挪到 ASR 文字出来之后                     │
# │   （_barge_in_gate 拿文本 + decide_barge_in 四判据）。                │
# │ 代价（主人已接受）：「停」会晚 150~300 毫秒才停。                     │
# └─────────────────────────────────────────────────────────────────────┘

# 预览识别至少需要的音频块数（60ms/块约 300ms；本地 SenseVoice 单次离线
# 识别实测 143.2ms，算上识别总延迟约 0.45s，在主人接受范围内）
_BARGE_IN_PREVIEW_MIN_BLOCKS = 5
# 已判「不打断」后，音频增长多少块才重跑预览（防「附和之后又接真指令」被漏掉）
_BARGE_IN_REEVAL_GROWTH_BLOCKS = 8
# 流式 ASR 等中间结果的上限（秒），超时仍无文本 → 无证据 → 保守掐断
_BARGE_IN_STREAM_WAIT_S = 0.8
# [2026-10-13 闸门改造，主人批准] 本地离线 ASR 的预览识别不再在「刚听到声音」
# 的瞬间跑，而是等声音持续这么久（秒）才跑第一次「阶段性预览」。
# 根因（日志坐实）：闸门在首个人声帧就取 asr_audio 快照，而 asr_audio 此时是
# 「声音出现之前的静音预缓冲」（当前块还没追加进去），预览 RMS 只有 3~1031，
# 识别出 . / Okay. 就按「没证据→掐断」处理 → 咳嗽、嗯、停 100% 被立刻掐断。
# 0.4 秒 = 主人定的延迟预算；真话（有意义的文字）在这一刻就能掐，
# 识别不出东西的（咳嗽/清嗓）等声音说完再判。可调。
_BARGE_IN_PARTIAL_WAIT_S = 0.4
# 阶段性预览仍然识别不出有意义的内容、声音却持续超过这么久（秒）→ 不再等
# 声音结束，按无证据处理（防持续噪声/回声让播报一直续命）。咳嗽短于此值。
_BARGE_IN_JUNK_MAX_S = 2.0
# SenseVoice 声学事件标签里「不是在对助手说话」的几类（咳嗽/打喷嚏/笑/呼吸/哭）。
# 只在识别文字是垃圾/空时才作为「不打断」的正面证据；BGM/Applause 不收
# （BGM 可能是回声，宁可掐断）。
_PAUSE_LIKE_LATIN = frozenset({"ting", "tin", "stop", "wait", "pause"})
_NON_SPEECH_EVENTS = frozenset({"Cough", "Sneeze", "Laughter", "Breath", "Cry"})
# [2026-10-13 主人实测后] 声音说完、识别不出文字（垃圾/空）、又没有事件标签：
# 原先一律掐断，结果咳嗽/清嗓子仍然把播报掐掉（实测 SenseVoice 对真实咳嗽/清嗓子
# 多数只标 Speech，不标 Cough）。改为：按咳嗽/清嗓子处理，**不掐断**。
# 为什么这样安全：① 真指令（停/等等/打开XX）要么在预览里已有有意义的文字（直接掐），
# 要么说完提交后走正式识别——base.py 播报期分支（打断命令/≥4字真插话）还有一次
# 机会；② 持续噪声/回声靠下面两条兜底仍会掐断。
# 兜底：这段播报里「垃圾声」在 _BARGE_IN_JUNK_BURST_S 秒内累计达 _BARGE_IN_JUNK_BURST_N 次
# （咳嗽是偶发的，回声/噪声是连续的）→ 掐断；单段持续超 _BARGE_IN_JUNK_MAX_S 秒同理。
_BARGE_IN_JUNK_BURST_N = 3
_BARGE_IN_JUNK_BURST_S = 6.0

# 判据 1b 用的语气词/拟声字集合：咳嗽、清嗓子、敲桌子、碰撞声时 ASR
# **不会**吐出「咳嗽」这种词，只会给空串或随机语气字（实测），所以
# 「无意义字符」只能按「全部由语气字构成」结构性识别，不能靠噪音词表。
# ⚠️ 「好」「行」刻意不在其中（也不在附和词表）——它们更像指令收尾，必须掐断。
_JUNK_FILLER_CHARS = set("呃嗯唔哦噢诶哎呀啊额咳吖呀哇哈哼嘿欸咦哟咯呐呜嘭咚砰喀哒的了着呢吗吧嘛么")


def _is_meaningless_filler(cleaned: str) -> bool:
    """判据 1b：只有一两个无意义字符（全部由语气字/拟声字构成）。

    ⚠️ 短命令不算无意义：「停」必须走判据 4 掐断后由 intentHandler 转成
    暂停（存断点等「继续」），「继续」要消费断点。这里吞掉它们就等于
    主人说「停」不停、说「继续」不续。
    """
    if len(cleaned) > 2:
        return False
    if cleaned in _PAUSE_COMMANDS or cleaned in _RESUME_COMMANDS:
        return False
    return all(ch in _JUNK_FILLER_CHARS for ch in cleaned)


def _is_echo_of_playing(cleaned: str, playing_text: str) -> bool:
    """判据 3：识别结果与正在播报的内容高度重合 = 自激回声。

    AEC 没消干净时助手自己的声音会被 ASR 识别回来。与 startToChat 的
    回声过滤同口径（相似度 ≥0.65 或 被包含且 ≥4 字），保持两层行为一致。
    """
    import difflib
    norm = lambda s: re.sub(r"[\s，。！？,.!?~～、；;：:]", "", s or "")
    a, b = cleaned, norm(playing_text)
    if not a or not b:
        return False
    if a in b and len(a) >= 4:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.65


def decide_barge_in(text, playing_text: str = "") -> bool:
    """播报期间拿到这段人声的识别文字后，决定要不要掐断播报（纯函数，可单测）。

    Args:
        text: 识别文本。**None = 识别失败/拿不到文本**；
              "" = 识别成功但没内容（咳嗽/敲桌子/电视碎片声/**回声**的常态）。
              这两类都按「没有证据」处理 → 掐断（见判据 1）。
        playing_text: 正在播报的内容（判据 3 回声兜底用），空串表示未知。

    Returns:
        True = 掐断；False = 不掐断（播报从头到尾继续）。

    判据顺序（2026-10-13 主人批准；判据 1 同日实测后反转，理由见下）：
      1. 空 / 纯标点 / 只有一两个无意义字符 → 掐断。
         ⚠️ 原设计是「不掐断」，用来兜住咳嗽/清嗓子/敲桌子，已被实测推翻：
         播报期回声送进 ASR 得到的正是这类垃圾文本（实测 He. / う。 / . /
         Okay.）。把「没有证据」当成「不打断」会让播报继续 → 回声持续 →
         VAD 每 1~2 秒切一刀 → 每刀都识别出垃圾 → 又判「不打断」→ 死循环。
         2026-10-13 08:42:05~08:42:13 连续 8 秒全是垃圾，主人真话一句没进去。
         对照历史日志：垃圾识别率 18%~64% 自 2026-09-17 起长期存在，并非本次
         改动引入；但改动前「播报期首次人声即掐断」天然终止了回声期，改动后
         闸门让播报续命才形成循环。
         原则：只有正面认出附和词才可以压制掐断，没有证据必须掐断。
         代价（已如实告知主人）：咳嗽/清嗓子会被掐断 —— 它们在文本层与回声
         不可区分，要兼得必须先解决回声消除，属另一件事。
      2. 命中附和音词表（「哦」「嗯」「对」「明白了」…）→ 不掐断。
         「好」「行」刻意不收。这是唯一的正面证据，可靠。
      3. 识别结果与正在播报的内容高度重合 → 不掐断（回声的正面证据）。
      4. 其余 → 掐断，交给现有流程（intentHandler 再分「停/等等」存
         断点 vs 真指令正常回答）。

    ⚠️ 保守原则（必须保留）：text 为 None（识别异常/拿不到文本）时
    返回 True = 保持原行为掐断。理由：漏拦真指令（主人说「停」却没停）
    比多拦一句附和严重得多。
    """
    return barge_in_verdict(text, playing_text=playing_text, event=None, final=True) != "keep"


def barge_in_verdict(text, playing_text: str = "", event=None, final: bool = True) -> str:
    """decide_barge_in 的三态版本（纯函数，可单测）。

    返回 "abort"（掐断）/ "keep"（不掐断）/ "wait"（证据不足，再等等）/
    "junk"（声音已说完，识别不出文字也无事件标签：按咳嗽/清嗓子处理，
    是否真放行由 _apply_barge_in_decision 的连续次数/时长兜底决定）。
    "wait" 只会出现在 final=False（声音还没说完的阶段性预览）：识别出垃圾/空，
    但不能下结论——因为此刻拿到的只是声音的前 0.4 秒。

    Args:
        event: SenseVoice 声学事件标签（Speech/Cough/Laughter/…），None=未知。
               识别文字是垃圾/空时，标签属于 _NON_SPEECH_EVENTS = 正面证据 → keep
               （咳嗽/清嗓子这类在文本层与回声分不开，标签是文本之外的信息）。
        final:  True = 声音已说完（或持续太久不再等）。垃圾/空且无正面证据 → junk。
    """
    if text is None:
        return "abort"            # 拿不到证据 → 保守，保持原行为（掐断）
    cleaned = re.sub(r"[\s，。！？、,.!?~～；;：:　]", "", str(text))
    # 正面证据优先（判据 2 必须排在 1b 之前：附和词与无意义字符集合重叠 8 个字）
    if cleaned and len(cleaned) <= 6 and _BACKCHANNEL_RE.match(cleaned):
        return "keep"                                   # 判据 2：附和音词表
    if cleaned and playing_text and _is_echo_of_playing(cleaned, playing_text):
        return "keep"                                   # 判据 3：回声兜底
    # 垃圾文本：空 / 一两个语气字 / 没有一个汉字的短碎片（The. Yeah. Okay. He. う。 그.
    # 这类是 SenseVoice 对咳嗽/键盘/回声等非语音声音的固定兜底输出，2026-10-13
    # 用环境音数据集实测同型）。只用于决定「先暂缓、等声音说完」，不会因此漏掉
    # 真话：暂缓之后 final 阶段仍按 abort 处理。
    # ⚠️ 例外：「停」被 SenseVoice 识别成 Ting. 的概率过半（全历史日志统计：
    # Ting. 6 次 vs 停。5 次），这类拉丁转写必须当作真指令，不能当垃圾放行。
    junk = (not cleaned) or _is_meaningless_filler(cleaned) or (
        len(cleaned) <= 8 and not re.search(r"[\u4e00-\u9fff]", cleaned)
        and cleaned.lower() not in _PAUSE_LIKE_LATIN)
    if not junk:
        return "abort"                                  # 判据 4：真打断
    if event in _NON_SPEECH_EVENTS:
        return "keep"                                   # 判据 1c：咳嗽/笑/呼吸等声学事件
    return "junk" if final else "wait"                  # 判据 1：垃圾/空（见 _apply 的兜底）


def _can_run_local_preview(conn) -> bool:
    """能不能对 conn.asr 跑离线预览识别（speech_to_text_wrapper）。

    ⚠️ 流式 ASR 绝对不能跑：它的 speech_to_text 是「读走并清空」流式会话
    文本（2026-10-07 血泪教训：预览吃掉流式文本 → 正式提交拿到空串 →
    助手不回复），而且云端识别花额度。流式改读 _asr_partial_text 中间结果。
    """
    asr = getattr(conn, "asr", None)
    if asr is None:
        return False
    return getattr(asr, "interface_type", None) != InterfaceType.STREAM


def _collect_barge_in_text(conn):
    """收集**现成的**识别文本证据。返回 None = 此刻拿不到。

    来源（按代价从低到高）：
      ① silero 并行预判写进 _vad_probe_text 的预览文本（现成基础设施）；
      ② 流式 ASR 的中间结果 _asr_partial_text（receive_audio 每帧刷新，
         与闸门进入时的快照不同才算本段人声的新证据）。
    都拿不到时由 _barge_in_gate 决定是否自己跑本地预览识别。
    """
    probe = str(getattr(conn, "_vad_probe_text", "") or "").strip()
    if probe:
        seq0 = getattr(conn, "_vad_probe_voice_seq", None)
        cur = getattr(conn, "_vad_voice_seq", None)
        if seq0 is None or seq0 == cur:
            return probe
        # 预判之后又进了新的人声帧 → 文本过期，不用
    asr = getattr(conn, "asr", None)
    if asr is not None and getattr(asr, "interface_type", None) == InterfaceType.STREAM:
        partial = str(getattr(conn, "_asr_partial_text", "") or "").strip()
        snap = str(getattr(conn, "_barge_in_partial_snapshot", "") or "").strip()
        if partial and partial != snap:
            return partial
    return None


async def _apply_barge_in_decision(conn, text, event=None, final: bool = True,
                                   stage: str = "") -> str:
    """按 barge_in_verdict 执行决定，更新闸门状态，返回 "abort"/"keep"/"wait"。"""
    # 决定之后刷新流式快照：下一轮评估要求「更新的」中间文本才算新证据
    conn._barge_in_partial_snapshot = str(getattr(conn, "_asr_partial_text", "") or "")
    playing = ""
    last = getattr(conn, "last_tts_text", None)
    sent_at = getattr(conn, "last_tts_sent_at", 0) or 0
    if last and (time.time() - sent_at) < 8.0:
        playing = str(last)
    shown = "" if text is None else str(text)[:30]
    started = getattr(conn, "_barge_in_started_at", None)
    elapsed_ms = int((time.time() - started) * 1000) if started else -1
    # 声音已持续太久仍识别不出有意义的内容 → 不再等声音结束，且不再按咳嗽放行
    forced = False
    if not final and elapsed_ms >= _BARGE_IN_JUNK_MAX_S * 1000:
        final = True
        forced = True
    verdict = barge_in_verdict(text, playing_text=playing, event=event, final=final)
    junk_note = ""
    if verdict == "junk":
        now_t = time.time()
        recent = [t for t in getattr(conn, "_barge_in_junk_times", []) or []
                  if now_t - t < _BARGE_IN_JUNK_BURST_S]
        if forced:
            verdict, junk_note = "abort", " [垃圾声持续过久，按噪声/回声掐断]"
        elif len(recent) + 1 >= _BARGE_IN_JUNK_BURST_N:
            verdict, junk_note = "abort", f" [{_BARGE_IN_JUNK_BURST_S:.0f}秒内第{len(recent) + 1}次垃圾声，按连续噪声/回声掐断]"
            conn._barge_in_junk_times = []
        else:
            recent.append(now_t)
            conn._barge_in_junk_times = recent
            verdict, junk_note = "keep", " [识别不出文字的短促声音，按咳嗽/清嗓子处理]"
    tag = f"阶段={stage or '-'} 开口后{elapsed_ms}ms 事件={event or '-'}"
    _ev, _lang, _emo = _asr_meta_for(conn, text)
    _st = getattr(conn, "_vad_seg_stats", None)
    if _st and _st[0]:
        tag += (f" 语种={_lang or '-'} 情感={_emo or '-'} "
                f"VAD人声{_st[3] * 32}ms/共{_st[0] * 32}ms 最大概率{_st[2]:.2f} "
                f"平均概率{_st[1] / _st[0]:.2f}")
    if verdict == "abort":
        conn._barge_in_active = False
        conn._barge_in_resolved = False
        conn.logger.bind(tag=TAG).info(f"[barge-in] 判定真打断，掐断播报: 文本={shown!r} {tag}{junk_note}")
        # handleAbortMessage 默认先抢救未播文本（2026-10-13 事情 B：
        # 断点在掐断前存下，ASR 随后认出「停」时断点是完整的）
        await handleAbortMessage(conn)
    elif verdict == "keep":
        conn._barge_in_resolved = True
        conn.logger.bind(tag=TAG).info(
            f"[barge-in] 判定不打断（附和/咳嗽/回声），播报继续: 文本={shown!r} {tag}{junk_note}")
    else:
        conn._barge_in_resolved = False
        conn.logger.bind(tag=TAG).info(
            f"[barge-in] 暂缓判定（声音还没说完，等说完再判）: 文本={shown!r} {tag}")
    return verdict


def _asr_meta_for(conn, text):
    """取这次识别对应的 SenseVoice 标签 (事件, 语种, 情感)（provider 记在 last_meta；
    文字对不上说明被别的识别覆盖了，宁可当没有）。"""
    meta = getattr(getattr(conn, "asr", None), "last_meta", None)
    if isinstance(meta, dict) and text is not None \
            and str(meta.get("text", "")).strip() == str(text).strip():
        return meta.get("event") or None, meta.get("lang") or None, meta.get("emotion") or None
    return None, None, None


def _asr_event_for(conn, text):
    return _asr_meta_for(conn, text)[0]


async def _barge_in_preview_task(conn, audio_snapshot, stage: str = "final") -> None:
    """本地预览识别协程：跑一次离线 ASR，然后执行判据。

    stage="partial"：声音还在继续时的阶段性预览（只拿到前 0.4 秒左右），
    识别不出东西不下结论（"wait"）；stage="final"：声音已说完，完整音频。

    复用现成的 speech_to_text_wrapper（silero 并行预判同款调用，不是新
    机制）。返回值语义：None = 识别失败（wrapper 内部吞异常后返回 None）→ 保守
    掐断；"" = 识别成功但没内容（咳嗽/敲桌子）→ final 阶段看事件标签。
    """
    text = None
    try:
        result, _path = await conn.asr.speech_to_text_wrapper(
            audio_snapshot, conn.session_id, conn.audio_format
        )
        text = None if result is None else str(result).strip()
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[barge-in] 预览识别异常: {e!r}")
        text = None
    finally:
        conn._barge_in_pending = False
    # 预览期间播报可能已自然结束/已被别的路径处理 → 不再动作
    if not getattr(conn, "_barge_in_active", False):
        return
    if not getattr(conn, "client_is_speaking", False):
        conn._barge_in_active = False
        return
    await _apply_barge_in_decision(
        conn, text, event=_asr_event_for(conn, text),
        final=(stage == "final"), stage=stage)


async def _barge_in_gate(conn, sound_ended: bool = False) -> None:
    """播报期打断闸门：VAD 判到人声不立刻掐断，先拿识别文字再决定。

    每个人声帧都会进来（sound_ended=False）；声音说完（VAD 候选结束）后的静音帧
    也会带 sound_ended=True 进来一次，用完整音频做最终判定。靠状态位保证同一段
    人声里预览识别不重复跑、判「不打断」后只在音频明显增长时才重跑。
    """
    now = time.time()
    asr_audio = getattr(conn, "asr_audio", None) or []
    cur_len = len(asr_audio)

    # 音频缓冲变短 = 上一段已提交/丢弃（reset_audio_states）→ 新的一段人声
    if cur_len < getattr(conn, "_barge_in_last_len", 0):
        conn._barge_in_active = False
    conn._barge_in_last_len = cur_len

    # 闸门状态属于「某一次播报」：新一轮播报（tts_start_time 变了）开始后，上一轮
    # 残留的 active 状态必须作废。否则 abort 后残留的状态会在下一轮助手刚开口
    # 时被静音帧钩子当成「声音说完」，拿旧起点做最终判定，误掐助手自己的新回复
    # （2026-10-13 实测日志：开口后 10815ms 才判定，就是残留状态）。
    _play_id = getattr(conn, "tts_start_time", None)
    if getattr(conn, "_barge_in_active", False) and \
            getattr(conn, "_barge_in_play_id", None) != _play_id:
        conn._barge_in_active = False

    if sound_ended and not getattr(conn, "_barge_in_active", False):
        return  # 没有进行中的闸门，静音帧不必处理

    if not getattr(conn, "_barge_in_active", False):
        conn._barge_in_active = True
        if getattr(conn, "_barge_in_junk_play_id", None) != _play_id:
            conn._barge_in_junk_play_id = _play_id
            conn._barge_in_junk_times = []
        conn._barge_in_play_id = _play_id
        conn._barge_in_started_at = now
        conn._barge_in_eval_len = 0
        conn._barge_in_resolved = False
        conn._barge_in_final_launched = False
        conn._barge_in_partial_snapshot = str(getattr(conn, "_asr_partial_text", "") or "")
        conn.logger.bind(tag=TAG).info(
            "[barge-in] 播报期检测到人声，进入打断闸门（等识别文字再决定掐不掐）")

    if getattr(conn, "_barge_in_pending", False):
        return  # 预览识别在跑，等结果

    if getattr(conn, "_barge_in_resolved", False):
        if sound_ended:
            return
        if cur_len - getattr(conn, "_barge_in_eval_len", 0) < _BARGE_IN_REEVAL_GROWTH_BLOCKS:
            return
        conn._barge_in_resolved = False  # 人声明显变长 → 重新评估
        conn._barge_in_final_launched = False

    if sound_ended and getattr(conn, "_barge_in_final_launched", False):
        return  # 最终预览已跑过

    local = _can_run_local_preview(conn)
    text = _collect_barge_in_text(conn)
    if text is not None:
        # 本地 ASR：预判文本若是垃圾/空只算「暂缓」，继续走自己的预览（带事件标签）
        verdict = await _apply_barge_in_decision(
            conn, text, final=not local, stage="probe")
        if verdict != "wait":
            return

    if local:
        # 离线/本地 ASR：自己跑预览识别（异步任务，不阻塞音频帧处理）
        eval_len = getattr(conn, "_barge_in_eval_len", 0)
        growth = cur_len - eval_len
        elapsed = now - getattr(conn, "_barge_in_started_at", now)
        if sound_ended:
            stage, go = "final", True
        else:
            stage = "partial"
            go = (eval_len == 0 and elapsed >= _BARGE_IN_PARTIAL_WAIT_S) or \
                 (eval_len > 0 and growth >= _BARGE_IN_REEVAL_GROWTH_BLOCKS)
        if go:
            conn._barge_in_eval_len = cur_len
            conn._barge_in_pending = True
            if stage == "final":
                conn._barge_in_final_launched = True
            try:
                asyncio.create_task(
                    _barge_in_preview_task(conn, list(asr_audio), stage))
            except Exception as e:
                conn._barge_in_pending = False
                conn.logger.bind(tag=TAG).warning(
                    f"[barge-in] 预览发起失败: {e!r}，无证据保守掐断")
                await _apply_barge_in_decision(conn, None)
        return

    # 流式 ASR：等中间结果；等不到（超时）→ 无证据 → 保守掐断
    if now - getattr(conn, "_barge_in_started_at", now) >= _BARGE_IN_STREAM_WAIT_S:
        conn.logger.bind(tag=TAG).info("[barge-in] 流式ASR等不到中间文本，无证据，保守掐断")
        await _apply_barge_in_decision(conn, None)


def _drop_queued_turns(conn, keep_seq: int, logger) -> int:
    """取消连接上「排队中且尚未开始执行」的旧轮次，只保留 keep_seq 这一轮。

    [2026-10-12 P0 修复的必要一环]

    背景：`conn.executor` 是每连接一个 ThreadPoolExecutor，而 `chat()` 是同步阻塞的
    （LLM 60s 超时、ltm 抽取、工具执行全在里面）。abort 只清音频队列，
    **停不掉已经排进线程池的 future**。于是：

        主人连说 4 句 → 4 个 future 排队 → worker 被旧轮占满
        → 第 5 句起永久排队 → 整条链路哑掉（只有 ASR 字幕、无 LLM 无 TTS）

    abort 语义本来就是「只回答最新这句」，所以排队中的旧轮次必然作废，
    直接 cancel 掉是最干净的处理——避免 worker 被无用的积压占死。

    `future.cancel()` 只对「尚未开始执行」的任务有效；已在跑的返回 False，
    由 _should_cancel_previous_turn → handleAbortMessage 正常中止它。

    Returns:
        实际取消的任务数（仅日志用）
    """
    futures = getattr(conn, "_chat_pending_futures", None)
    if not futures:
        return 0

    cancelled = 0
    for old_seq, old_fut in list(futures.items()):
        if old_seq == keep_seq:
            continue
        try:
            if old_fut.cancel():
                futures.pop(old_seq, None)
                cancelled += 1
                conn._chat_turn_seq_backlog = (
                    getattr(conn, "_chat_turn_seq_backlog", 0) + 1
                )
        except Exception:
            pass

    if cancelled:
        try:
            logger.bind(tag=TAG).warning(
                f"[线程池救援] 取消 {cancelled} 个排队中的旧轮次（只保留最新一轮）。"
                "若这句反复出现，说明单轮 chat 执行过慢，需查 LLM 超时/ltm 抽取"
            )
        except Exception:
            pass
    return cancelled


async def handleAudioMessage(conn: "ConnectionHandler", audio, raw_audio=None):
    """音频帧主链路。

    [2026-10-13 音乐不停对话·双路径] 参数语义：
      · audio：AEC 处理后的音频 → 只喂 VAD/判停/打断链路（音乐下判停可靠）；
      · raw_audio：原始麦克风音频 → 喂 conn.asr.receive_audio（ASR + 声纹，
        音乐下识别/声纹质量优于 AEC 后，实测 CER 0.04/声纹 0.73 vs 0.24/0.42）。
      · raw_audio=None（opus/ESP32 队列路径）：保持旧行为，ASR 与 VAD 同一份。
    """
    conn.logger.bind(tag=TAG).debug(f"[调试-handleAudioMessage] 被调用: {len(audio)} bytes, format={conn.audio_format}")

    # 根据音频格式选择 VAD 处理方法
    if conn.audio_format == "pcm":
        # PCM 格式直接处理
        have_voice = conn.vad.is_vad_pcm(conn, audio)
        conn.logger.bind(tag=TAG).debug(f"[调试-VAD-PCM] 检测结果: have_voice={have_voice}")
    else:
        # opus 格式需要解码
        conn.logger.bind(tag=TAG).debug(f"[调试-VAD-Opus] 开始检测 Opus 音频: {len(audio)} bytes")
        have_voice = conn.vad.is_vad(conn, audio)
        conn.logger.bind(tag=TAG).debug(f"[调试-VAD-Opus] 检测结果: have_voice={have_voice}")
    
    # 延迟监控：VAD 检测到语音
    if have_voice and hasattr(conn, 'latency_monitor'):
        conn.latency_monitor.mark('vad_voice_detected', audio_size=len(audio))

    # [2026-10-13 音乐不停对话] 旧「have_voice → maybe_lower_volume 降音量/系统
    # ducking」整块已删除（主人批准方案 A）：音乐不暂停、不降音量；歌词漏进
    # ASR 的防线改为 VAD 路 AEC + 文本层闸门（回声过滤/声纹闸门/音乐闸门）。
    # ASR/声纹吃的音频：有 raw_audio 用原始音频，否则与 VAD 同一份（旧行为）
    asr_in = audio if raw_audio is None else raw_audio
    
    # 如果设备刚刚被唤醒，短暂忽略VAD检测（防止误打断），但仍接收音频
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复VAD检测
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        # 不调用 handleAbortMessage，但仍然接收音频给 ASR
        await no_voice_close_connect(conn, have_voice)
        await conn.asr.receive_audio(conn, asr_in, have_voice)
        return
    # 音乐自动音量调节：音量降低后不再因音频能量触发打断，
    # 但 VAD 检测到用户真实语音（have_voice）仍可打断（2026-09-15 修复）
    # 2026-09-23 打断加近场判定：仅当 VAD 检测到能量突变（疑似近场插话，连续 2 帧超
    # 段内基线×ratio，复用 silero 近场爆发切分计数器）才打断 TTS；远场稳态声（视频/
    # 音乐外放讲解）不打断——修 YouTube 播放时 TTS 被无差别打断、间歇不播。
    # 近场切分未启用时回退原行为（无脑打断），防打断功能失效。
    # 2026-09-24 恢复初始逻辑（主人裁决）：有声即打断——VAD 检测到有声音且 TTS 正在播
    # 放 → 立即打断。删除 2026-09-23 的近场能量门槛：该门槛在持续说话时会被段内基线
    # 污染而永远触发不了（主人实测插话完全打不断）。媒体/回声误打断的防线在下游
    # ASR 文本层（声纹闸门/回声过滤），不在 VAD 层。
    if have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            # [2026-10-13 掐断时机改造，主人批准] 「要不要掐断」的决定挪到
            # ASR 文字出来之后：闸门先拿到这段人声的识别文字（silero 预判
            # 文本 / 流式中间结果 / 本地 SenseVoice 预览识别 ~143ms），再按
            # decide_barge_in 四判据决定掐不掐；拿不到证据保守掐断（漏拦真
            # 指令比多拦一句附和严重得多）。
            # 旧实现是「除非预览文本是附和，否则立刻 handleAbortMessage」：
            # 短促的「哦」根本来不及产出预览文本，实测 100% 漏过；而且掐断
            # 会把未播内容一起销毁，「停」→「继续」接不上。断点抢救已提前
            # 进 handleAbortMessage（默认先抢救再清队列，事情 B）。
            # 代价（主人已接受）：「停」会晚 150~300 毫秒才停。
            await _barge_in_gate(conn)
    elif (getattr(conn, "_barge_in_active", False)
          and getattr(conn, "_vad_candidate_end", False)
          and conn.client_is_speaking and conn.client_listen_mode != "manual"):
        # 声音说完了（VAD 候选结束 = 静音 ≥300ms）：用完整音频做最终判定。
        # 闸门只在人声帧触发，说完之后的静音帧得单独挂一个钩子。
        await _barge_in_gate(conn, sound_ended=True)
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频（双路径：ASR/声纹吃原始麦克风音频，见函数头注释）
    await conn.asr.receive_audio(conn, asr_in, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # 等待1秒后恢复VAD检测（给用户足够时间说出指令，防止唤醒词回复被误打断）
    await asyncio.sleep(1.0)
    conn.just_woken_up = False


def _should_cancel_previous_turn(conn, text: str = None) -> bool:
    """用户又说话时，要不要取消上一轮的回答？

    【2026-10-07 主人实测：连续短句的回答叠在一起播】
    原判据只看 client_is_speaking（助手「正在出声」）。实测 11:59:48/51/53 三句
    连续短句，每句都开了新一轮：说话时助手还在「思考中」、还没出声，
    client_is_speaking 为 False → 完全不打断 → 三个 LLM 请求并行跑，
    三段回答在 4 秒内连着播出来（谁也没取消谁），听感就是「乱」。
    改为：上一轮**还在处理中**（思考中或出声中）也算，一并取消。
    `_chat_turn_active` 由 startToChat 提交 chat 时置真、chat 结束时复位。
    manual 模式（点按说话）不打断。

    【2026-10-08 例外：任务在跑时不取消，但插话照常立刻回答】
    主人原话：「后台的长任务不应该取消……任务完成后还是要播报。但是，正常聊天过程中
    直接打断的这种，就取消啊。」以及「插话应该立即回答……用户插话一定是他特别着急的
    东西，所以要马上回复。回复完了以后再继续之前的任务。所以，用户插话就相当于一个
    插播。」→ 任务窗口内**不取消**（任务继续跑），插话按普通一轮**立刻回答**；
    原任务的结果由 chat_engine._wait_until_idle() 等插话播完再接着播。
    """
    if getattr(conn, "client_listen_mode", None) == "manual":
        return False
    if _is_task_running(conn):
        return False
    return bool(getattr(conn, "client_is_speaking", False)
                or getattr(conn, "_chat_turn_active", False))


# 任务窗口的兜底上限：卡住超过这个时长就不再特殊对待（否则助手会被憋哑）。
_TASK_DEFER_MAX_S = 90.0


def _is_task_running(conn) -> bool:
    """是否处于「主人在等结果」的任务窗口内（模型已决定调工具，整轮未结束）。

    超时兜底：卡住的任务不能把助手憋哑 —— 超过 _TASK_DEFER_MAX_S 视为已失败，
    不再特殊对待。
    """
    if not getattr(conn, "_task_running", False):
        return False
    started = getattr(conn, "_task_started_at", 0) or 0
    if started and (time.time() - started) > _TASK_DEFER_MAX_S:
        conn.logger.bind(tag=TAG).warning(
            f"[插播] 任务已跑 {time.time() - started:.0f}s（超过 "
            f"{_TASK_DEFER_MAX_S:.0f}s 兜底），不再特殊对待")
        return False
    return True


async def startToChat(conn: "ConnectionHandler", text, speaker: str = None):
    """触发对话。speaker：调用方已识别的说话人（如流式 ASR 路径的声纹结果），
    显式传入时优先于 JSON 解析；传空字符串表示"本次明确无说话人"（清空旧值）。"""
    start_to_chat_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] startToChat 被调用，文本: {text[:50]}..., ts={start_to_chat_ts}")

    # 延迟监控：startToChat 开始
    if hasattr(conn, 'latency_monitor'):
        conn.latency_monitor.mark('start_to_chat', text_length=len(text))
    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    language_tag = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                language_tag = data["language"]
                actual_text = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 直接使用JSON格式的文本，不解析
                actual_text = text
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 显式传入的说话人优先（流式路径声纹结果，2026-09-09）
    if speaker is not None:
        speaker_name = speaker

    # 回声过滤（2026-09-11 一期）：ASR 文本与最近 TTS 播报高度相似且在时间窗内 → 判定自激回声，丢弃
    # 场景：ESP32 固件 AEC 未开 + 网页端播报，助手"听到自己说话"
    _last_tts_text = getattr(conn, "last_tts_text", None)
    _last_tts_at = getattr(conn, "last_tts_sent_at", 0)
    if _last_tts_text and (time.time() - _last_tts_at) < 8.0:
        import difflib, re as _re
        _norm = lambda s: _re.sub(r"[\s，。！？,.!?~～、；;：:]", "", s or "")
        _a, _b = _norm(actual_text), _norm(_last_tts_text)
        if _a and _b:
            sim = difflib.SequenceMatcher(None, _a, _b).ratio()
            # 完全包含也判回声（ASR 只截到播报的一部分）
            if sim >= 0.65 or (_a in _b and len(_a) >= 4):
                conn.logger.bind(tag=TAG).info(
                    f"[回声过滤] 丢弃自激 ASR: '{actual_text[:30]}' ≈ 播报文本 (sim={sim:.2f})")
                return

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    # ===== 全局紧急报警检测（不经过唤醒词） =====
    # 注入 TTS 播报能力与会话循环
    emergency_listener.set_loop(conn.loop)
    device_id = conn.headers.get("device-id", "unknown")

    def _emergency_speak(text):
        # 2026-09-28 修复：用户说"救命"时若正在打断（client_abort=True），
        # 确认提示音会被 TTS 文本线程静默吞掉（主人实测第一遍呼救无确认）。
        # 报警播报属于最高优先级，提交前必须清打断标志。
        conn.client_abort = False
        conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)

    emergency_listener.set_speak_callback(_emergency_speak)

    # 1) 若正处于确认窗口期，优先消费用户回复
    if emergency_listener.handle_reply(actual_text, source=device_id):
        return

    # 2) 检测新文本是否命中紧急关键词；命中则进入确认流程，不再走常规对话
    if emergency_listener.detect(actual_text, source=device_id):
        return
    # ===========================================

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return
    # manual 模式下不打断正在播放的内容
    # 【2026-10-07 主人实测：连续短句的回答叠在一起播】原判据只看
    # client_is_speaking（助手「正在出声」）；助手还在思考时用户插话不会取消它。
    # 判据已抽成 _should_cancel_previous_turn()，含「上一轮还在处理中」。
    _prev_turn_running = bool(getattr(conn, "_chat_turn_active", False))
    if _should_cancel_previous_turn(conn, actual_text):
        if _prev_turn_running and not conn.client_is_speaking:
            conn.logger.bind(tag=TAG).info(
                "[打断] 上一轮还在处理中（尚未出声），取消它并只回答最新这句")
        await handleAbortMessage(conn)

    # 首先进行意图分析，使用实际文本内容
    intent_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 开始意图分析, ts={intent_ts}")
    intent_handled = await handle_user_intent(conn, actual_text)
    intent_done_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 意图分析完成, ts={intent_done_ts}, 耗时={intent_done_ts - intent_ts:.0f}ms, handled={intent_handled}")

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    chat_submit_ts = time.time() * 1000
    conn.logger.bind(tag=TAG).info(f"[耗时] 提交 LLM chat, ts={chat_submit_ts}")
    await send_stt_message(conn, actual_text)

    # 2026-10-11 A8：原来「进行中/任务中」是单个布尔值，插话开新一轮后，旧轮结束的回调
    # 会把新轮的标志一并清掉（反之亦然）→ 新轮明明还在跑却被当成空闲，下一句不取消它。
    # 改为按轮次登记：每轮有自己的序号，结束只注销自己；「进行中」= 还有活跃轮次；
    # 「任务中」只由发起任务的那一轮结束时清除（插话轮结束不影响仍在跑的任务）。

    # ┌─────────────────────────────────────────────────────────────────┐
    # │ [2026-10-12 P0 修复] chat 线程池无限排队，导致整轮彻底哑掉       │
    # ├─────────────────────────────────────────────────────────────────┤
    # │ 事故现象（主人 18:19 反馈）：                                   │
    # │   「最近几轮交互只看到 ASR 文字，没有 LLM 输出和 TTS」            │
    # │   「刷新网页也没有变化」                                        │
    # │                                                                 │
    # │ 日志证据（tmp/server.log）：                                     │
    # │   18:19:08 / 18:19:14 / 18:19:20 / ... 共 10 次「提交 LLM chat」  │
    # │   而 chat() 第一行的「大模型收到用户消息」一次都没出现            │
    # │                                                                 │
    # │ 根因：chat() 是同步阻塞函数（LLM 60s 超时 + ltm 抽取都在里      │
    # │ 面），而 conn.executor 是每连接 ThreadPoolExecutor(max_workers=5)。│
    # │ 主人连说几句时：                                                │
    # │   每句都 submit 一个新任务 → 前面几轮占着 worker 不释放 →       │
    # │   5 个 worker 占满 → 之后所有轮次在队列里无限排队。              │
    # │ abort 只清音频队列（clear_queues），**停不掉已排队的 future**，   │
    # │ 所以刷新网页、重新唤醒都无解。                                  │
    # │                                                                 │
    # │ 修法：① 同连接只允许 1 个 chat 真正在跑；② submit 前把「排队中  │
    # │ 且尚未执行」的旧 future 直接 cancel（abort 的语义本就是「只回    │
    # │ 答最新这句」，排队轮次必然作废）；③ active 集合改为「正在执行」  │
    # │ 才登记，避免排队中的轮次让 _should_cancel_previous_turn 空转。   │
    # └─────────────────────────────────────────────────────────────────┘
    import threading

    seq = getattr(conn, "_chat_turn_seq", 0) + 1
    conn._chat_turn_seq = seq
    active = getattr(conn, "_chat_active_turns", None)
    if active is None:
        active = conn._chat_active_turns = set()
    thread_seq = getattr(conn, "_chat_turn_by_thread", None)
    if thread_seq is None:
        thread_seq = conn._chat_turn_by_thread = {}
    futures = getattr(conn, "_chat_pending_futures", None)
    if futures is None:
        futures = conn._chat_pending_futures = {}

    def _chat_done_callback(fut):
        active.discard(seq)
        conn._chat_turn_active = bool(active)
        if getattr(conn, "_task_turn_seq", None) == seq or not active:
            conn._task_running = False
            conn._task_turn_seq = None
        futures.pop(seq, None)
        try:
            fut.result()
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"LLM chat 执行失败: {e}", exc_info=True)

    def _run_turn():
        tid = threading.get_ident()
        thread_seq[tid] = seq
        # 「真正开始执行」这一刻才算进行中 —— 否则排队中的轮次会让
        # _should_cancel_previous_turn 误判为「上一轮还在跑」而反复 abort。
        active.add(seq)
        conn._chat_turn_active = True
        try:
            return conn.chat(actual_text)
        finally:
            if thread_seq.get(tid) == seq:
                thread_seq.pop(tid, None)

    if conn.executor is None:
        conn.logger.bind(tag=TAG).error("连接线程池已释放，放弃本轮对话")
        return

    # submit 之前先清理排队堆积（详见 _drop_queued_turns 注释）
    _drop_queued_turns(conn, seq, conn.logger)

    # 标记「本轮对话进行中」：思考中也要算，否则用户在你思考时插话不会取消你
    active.add(seq)
    conn._chat_turn_active = True
    # [2026-10-12 根治] LLM 流外部取消探针。
    #
    # 根因（主人 2026-10-12 指出我上一轮没抓住根本原因）：
    #   abort 只置 `client_abort=True` + 清音频队列，**停不掉已经发出去的
    #   LLM 请求**。`client_abort` 只被 TTS 队列消费线程看到（丢弃音频），
    #   而旧 chat() 线程仍卡在 `for chunk in stream:` 等云端返回
    #   （豆包首 token 1~3s）。主人连说几句时每句各占一个 worker，
    #   max_workers=5 被占满 → 之后所有轮次无限排队。
    #   日志证据（18:10:27 起连续 5 轮都是这个模式）：
    #     llm_request_start → [打断] 取消旧轮 → 又 llm_request_start
    #   每次 abort 后旧线程并没有死，还在等云端。
    #
    # 判据：当前线程绑定的轮次序号，已经不是连接上「最新轮次序号」。
    #      → 说明有更新的轮次已经 submit 并接管了 conn，旧的该立刻断开。
    # 不变量：`_chat_turn_by_thread[tid]` 在 `_run_turn` 里登记，
    #      conn._chat_turn_seq 每轮提交时自增。二者不一致 = 本轮已过期。
    def _turn_stale():
        try:
            tid = threading.get_ident()
            my_seq = thread_seq.get(tid)
            return my_seq is not None and my_seq != conn._chat_turn_seq
        except Exception:
            return False

    try:
        if getattr(conn, "llm", None) is not None:
            conn.llm.abort_probe = _turn_stale
    except Exception:
        pass  # provider 不支持外部取消时保持原行为

    future = conn.executor.submit(_run_turn)
    futures[seq] = future
    future.add_done_callback(_chat_done_callback)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False

            # 会话结束前生成对话摘要
            try:
                from core.utils.conversation_summary import generate_summary_for_session
                summary = generate_summary_for_session(
                    session_id=getattr(conn, 'session_id', 'unknown'),
                    conn=conn
                )
                if summary:
                    conn.logger.bind(tag=TAG).info(f"[摘要] 会话结束摘要: {summary[:100]}...")
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"生成会话摘要失败: {e}")

            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)

    # ┌─────────────────────────────────────────────────────────────────┐
    # │ [2026-10-12 主人纠正] 唤醒会话超时 → **主动**推待机，不用等人说话 │
    # ├─────────────────────────────────────────────────────────────────┤
    # │ 主人原话：「超过 120 秒自动就会变成待对话状态」，我上一版却写成    │
    # │ 「超时后还要再说一句不带唤醒的话才复位」——那是我实现错了。        │
    # │                                                                 │
    # │ 正确行为：`session_active_until` 一到点，服务端**主动**给前端发  │
    # │ `state: sleep`，前端把「对话中」按钮复位回待点击。全程不需要主人  │
    # │ 再开口。                                                         │
    # │                                                                 │
    # │ 为什么放这里：本函数每个音频帧（32ms）都会被调用，是最可靠的      │
    # │ 「时钟」。但没必要每帧发，用时间戳节流到 1 秒一次。               │
    # │                                                                 │
    # │ 幂等：`_standby_notified` 保证只发一次；唤醒词命中时清零。        │
    # └─────────────────────────────────────────────────────────────────┘
    if not have_voice:
        import time as _time
        _now = _time.time()
        _last = getattr(conn, "_standby_check_ts", 0.0)
        if _now - _last >= 1.0:
            conn._standby_check_ts = _now
            await _push_standby_if_session_expired(conn)


async def _push_standby_if_session_expired(conn: "ConnectionHandler") -> None:
    """唤醒会话过期时主动给网页端推待机通知（不需要主人再说话）。

    [2026-10-12 主人纠正] 我上一版把这个逻辑放在「说话被 idle_guard 拦掉」
    的分支里，等于要求主人再开口一句才复位 —— 主人明确指出不对：
    「超过 120 秒自动就会变成待对话状态」。

    本函数由 `no_voice_close_connect` 每 1 秒调用一次，满足「自动」。
    放在 `asr/base.py` 的 idle_guard 分支里那份保留作兜底（更快响应），
    两份都靠同一个 `_standby_notified` 标记，不会双发。
    """
    device_id = getattr(conn, "device_id", "") or ""
    if not device_id.startswith("web-terminal"):
        return

    try:
        from core.utils.wakeword_provider import get_wakeword_detector
        detector = get_wakeword_detector()
        if not detector:
            return
        if detector.is_session_active():
            # 会话还活着（含刚被唤醒词激活）：清掉「已通知待机」标记，
            # 让下一次会话过期时还能再复位一次。
            # 这个判据覆盖所有激活路径（唤醒词/注视激活/idle_guard 激活），
            # 不必去改稳定模块 helloHandle.py。
            if getattr(conn, "_standby_notified", False):
                conn._standby_notified = False
                conn._standby_notified_ts = 0.0
            return
    except Exception:
        return

    # 播报中不要打断：一段长回复可能超过 session_timeout，此刻推 sleep
    # 会直接把正在播放的语音掐掉。只在「确实还有音频没发完」时才让路；
    # 若 client_is_speaking 卡住但队列已空（异常/打断后残留），照常判定待机，
    # 否则会永久卡在对话态 —— 本次要修的就是这类「标记卡死」。
    if getattr(conn, "client_is_speaking", False):
        _q = getattr(getattr(conn, "tts", None), "tts_audio_queue", None)
        try:
            _pending = len(getattr(_q, "queue", []) or [])
        except Exception:
            _pending = 0
        if _pending:
            return

    # 到这里：会话确实已过期 → 真正回到「要喊唤醒词」的待机态。
    # [2026-10-20 主人批准] 原先只给前端发 sleep，conn._session_active 永远
    # 不复位 → 服务端一直放行任意说话（电视声音全被识别处理）。
    conn._session_active = False

    # 幂等 + 自愈：正常只发一次（每秒都在查，不能刷屏）；但若 60 秒后仍处于
    # 过期态，说明前端没收到 / 某条唤醒路径漏清标记，再补发一次。
    _now = time.time()
    _last_ts = getattr(conn, "_standby_notified_ts", 0.0)
    if getattr(conn, "_standby_notified", False) and (_now - _last_ts) < 60.0:
        return

    conn._standby_notified = True
    conn._standby_notified_ts = _now
    try:
        await conn.websocket.send(
            json.dumps({"type": "state", "state": "sleep"})
        )
        conn.logger.bind(tag=TAG).info(
            "[待机] 唤醒会话已过期，主动推送待机状态（网页端对话按钮复位）"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).debug(f"[待机] 推送失败: {e}")


async def max_out_size(conn: "ConnectionHandler"):
    # 播放超出最大输出字数的提示
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 播放未绑定提示
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
