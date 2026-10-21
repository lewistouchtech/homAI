import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


def salvage_unplayed_text(conn: "ConnectionHandler") -> str:
    """抢救「已收到但尚未合成播报」的文本，供「继续」时从断点续播。

    [2026-10-12 主人实测] 播报中说「停」能立刻停，但接着说「继续」时它
    播了另外一条，不是从停止位置继续 —— 因为 `clear_queues()` 把未播内容
    直接丢了，什么都没留下，「继续」只能重新走一遍 LLM。

    数据流（决定了拼接顺序）：
        LLM 产出 → tts_text_queue → tts_text_buff → _get_segment_text
                → _pending_tts_seg（合段缓冲）→ to_tts_stream → tts_audio_queue → 播放
    所以「尚未合成」的文本按播报先后是：
      ① `_pending_tts_seg`：已切出、攒着还没送进 TTS 的
      ② `tts_text_buff[processed_chars:]`：已收到、还没被切段的
      ③ `tts_text_queue` 里排队的 TEXT 消息：LLM 已产出、TTS 线程还没取的

    已知取舍：`tts_audio_queue` 里**已合成成音频**的部分不保留（重新入队
    opus 包复杂度高），所以断点精度是「句」：
      ④ [2026-10-13] 掐断时**正在播的那一句**整句放在断点最前面，「继续」从
        这一句的开头重新念起，而不是跳到它后面（实测：停在一个 12 秒长句
        中间，续播直接从下一句开始 = 丢了大半句，与主人要求的「从暂停的
        位置继续」不符）。判据 = `client_is_speaking`（有播报会话在进行）+
        `last_tts_text`（sendAudioMessage 逐句登记的当前句，句音频按实时
        节奏外发，所以登记的就是此刻正在播的句子）。没在播（已播完/已
        clearSpeakStatus）不加，避免把已播完的句子再念一遍。
    """
    tts = getattr(conn, "tts", None)
    if tts is None:
        return ""
    parts = []
    # ④ 正在播的那一句（按播报先后排在最前）
    try:
        if getattr(conn, "client_is_speaking", False):
            playing = str(getattr(conn, "last_tts_text", "") or "").strip()
            if playing:
                parts.append(playing)
    except Exception:
        pass
    # ① 合段缓冲
    try:
        pending = getattr(tts, "_pending_tts_seg", "") or ""
        if pending:
            parts.append(pending)
    except Exception:
        pass
    # ② 已收到但未切段的
    try:
        whole = "".join(getattr(tts, "tts_text_buff", []) or [])
        done = int(getattr(tts, "processed_chars", 0) or 0)
        if 0 <= done < len(whole):
            parts.append(whole[done:])
    except Exception:
        pass
    # ③ 队列里排队的（只读快照，不弹出；随后 clear_queues 会清掉）
    try:
        from core.providers.tts.base import ContentType
        for msg in list(getattr(getattr(tts, "tts_text_queue", None), "queue", []) or []):
            if getattr(msg, "content_type", None) == ContentType.TEXT:
                detail = getattr(msg, "content_detail", None)
                if detail:
                    parts.append(detail)
    except Exception:
        pass
    return "".join(parts).strip()


async def handleAbortMessage(conn: "ConnectionHandler", standby: bool = False,
                             pause: bool = False):
    # pause=True：主人说「停」「等等」这类**暂停播报**指令（2026-10-12 裁定）。
    #   2026-10-13 起「清队列前抢救未播文本」已是**所有打断的默认行为**
    #   （事情 B：VAD 层掐断发生在 ASR 出文本之前，掐断那一刻分不清是暂停
    #   还是新指令），pause=True 仅作暂停路径的显式标记与日志语义。
    # standby=True：用户显式"结束对话"（网页端"结束"按钮）——打断之外还要复位
    # 会话激活态，回到待机只认唤醒词。VAD 插话打断等内部路径不传，保持会话
    # 连续（2026-09-29 主人裁决：打断不碰会话，结束对话请说"再见"或点"结束"）。
    conn.logger.bind(tag=TAG).info(f"Abort message received, standby={standby}")
    # [2026-10-18 P4，主人批准] 运行观察：打断（barge-in）计数 + 逐条落 feedback_log，
    # 报告里用它看「打断频率」与「打断后是否接得住」。
    try:
        from core.utils import observation as _obs
        from core.utils import data_collector as _dc
        _obs.bump("barge_in")
        _dc.record_feedback(
            None, "interrupt",
            detail=f"standby={standby} pause={pause} device={getattr(conn, 'device_id', '')}",
        )
    except Exception:
        pass
    # 设置成打断状态，会自动打断llm、tts任务
    # 对齐上游：打断即复位 close_after_chat。缺失会导致告别语播报中被插话后，
    # 下一轮回复播完仍触发 conn.close() 断连（2026-09-17 补回）
    conn.close_after_chat = False
    conn.client_abort = True

    # 在清空队列前，等待一小段时间让当前音频播放到自然停顿点
    # 但不超过0.2秒，以免影响用户体验
    # 注意：实测缩短到0.03s会导致设备收到半截音频流、出现咔嗒咔嗒的截断杂音，
    # 此等待是给设备端在途缓冲的排空窗口，勿再缩短。
    # 2026-09-17 排查结论：咔嗒声的真正根因在客户端硬切停播放（Web 终端
    # stopPlayback 无淡出），已在终端页加 10ms 淡出修复；本窗口保留待主人
    # 实测确认后可与上游对齐摘除（当前保留是为稳住已验证的现状）。
    import asyncio
    await asyncio.sleep(0.1)  # 短暂等待，让音频播放接近句子边界

    # ⚠️ 抢救必须发生在 clear_queues() 之前 —— 清完就什么都没了
    # [2026-10-13 事情 B，主人批准] 打断**默认就抢救**，不再只有 pause=True
    # 才抢救。根因（日志坐实）：VAD 层掐断发生在 ASR 出文本之前——掐断那
    # 一刻还不知道主人说的是「停」还是真指令；旧实现不带 pause 的打断把
    # 未播内容随 clear_queues 直接销毁，等 ASR 认出「停」命中暂停指令时
    # 已无东西可抢（四个日志文件全历史零条 [暂停指令] 生效日志）。
    # 后续分流交给 intentHandler：
    #   · 认出「停/等等」→ 保留断点，等「继续」；
    #   · 认出别的真指令 → 丢弃断点（_paused_residual = None），防止
    #     主人下次说「继续」突然念出上一轮的旧内容。
    try:
        residual = salvage_unplayed_text(conn)
    except Exception as e:
        residual = ""
        conn.logger.bind(tag=TAG).warning(f"[断点] 抢救未播文本失败: {e!r}")
    old_residual = str(getattr(conn, "_paused_residual", "") or "")
    if residual:
        if old_residual and (residual in old_residual or old_residual in residual):
            # 幂等：同一轮里重复抢救（VAD 层掐断 + 暂停指令掐断先后发生）
            # 取更完整的一份，绝不拼接，防止「继续」时念出双倍内容。
            conn._paused_residual = (
                residual if len(residual) >= len(old_residual) else old_residual
            )
        else:
            conn._paused_residual = residual
    elif old_residual:
        # 队列已被上一次 abort 清空 → 抢救不到新内容，保留既有断点不覆盖
        # （否则第二次 abort 会把第一次存下的断点冲成空）。
        pass
    else:
        conn._paused_residual = ""
    if pause or conn._paused_residual:
        conn.logger.bind(tag=TAG).info(
            f"[断点] 已留下断点，未播 {len(conn._paused_residual)} 字，说「继续」可接着念")

    conn.clear_queues()
    # 打断客户端说话状态
    # [2026-10-11 B6] 加 abort 标记：前端据此区分「打断（硬停）」与「自然播完（播完剩余）」。
    # 自然结束的 tts stop 由 send_tts_message 发出、不带此字段，走前端 PcmPlayer.finish()。
    await conn.websocket.send(
        json.dumps(
            {"type": "tts", "state": "stop", "session_id": conn.session_id, "abort": True}
        )
    )
    conn.clearSpeakStatus()
    # 2026-09-29 主人裁决：打断不碰会话激活态。会话连续性按约定管理
    # （VAD 断句 + 每轮对话自动续期，说"再见"才结束）——6 月基线行为。
    # 此前 9/7 引入"打断即复位"、9/10 叠加"15s 宽限"补丁，与约定冲突
    # （打断后 20s 长句会被静默丢弃，主人实测"聊着聊着自己 standby"），
    # 两处均已移除。例外：standby=True 的显式结束（网页"结束"按钮）。
    if standby and hasattr(conn, '_session_active'):
        conn._session_active = False
        conn.logger.bind(tag=TAG).info("[idle_guard] 用户显式结束对话，会话复位进入待机")
    conn.logger.bind(tag=TAG).info("Abort message received-end")
