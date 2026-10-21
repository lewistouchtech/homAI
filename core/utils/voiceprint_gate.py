"""声纹全线闸门（2026-09-23，主人裁决）：非家庭成员不听不答。

判定语义（两条链路统一）：
- 声纹识别已启用且模型可用时，识别结果未匹配到已录入成员
  （None / 空串 / "未知说话人"）→ 该段语音视为非家庭成员声音，
  丢弃不进对话、不产生回复。
- 声纹不可用（provider 缺失/未启用）→ 闸门旁路放行，防止"声纹挂了全哑"。
- 退出命令白名单放行：播报/噪声环境中说"退出/再见"绝不能被吞。

覆盖：
- 星球链路：core/providers/asr/base.py 提交点（语音专属路径，文字输入天然不受影响）
- 数字人链路：core/api/llm_proxy_api.py /v1/audio/transcriptions 内联判定

配置：config.yaml 的 voiceprint_gate.enabled 总开关（默认开）。
"""
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 与 base.py 的退出命令判定同源：闸门前放行，避免"退出"被当非家庭成员声音吞掉
def _is_exit_command(conn, text: str) -> bool:
    plain = (text or "").strip()
    if not plain:
        return False
    cmds = getattr(conn, "cmd_exit", None) or []
    return any(plain == c or plain.startswith(c) for c in cmds)


def gate_enabled(conn) -> bool:
    """总开关 + 声纹可用性。声纹不可用时旁路（防全哑）。"""
    try:
        cfg = conn.config.get("voiceprint_gate") or {}
    except Exception:
        return False
    if not cfg.get("enabled", False):
        return False
    vp = getattr(conn, "voiceprint_provider", None)
    return bool(vp is not None and getattr(vp, "enabled", False))


# 打断命令白名单（2026-09-23，主人裁决 A+B 方案）：与退出命令同等地位的放行通道，
# 但语义不同——命中即"打断当前播报且不应答"（对齐星球）。两条链路共用本函数，
# 词表来自 config voiceprint_gate.bargein_words；匹配=去标点精确匹配，
# 防止"停止播放音乐"被"停"误伤。命中时调用方自行执行打断（abort/interrupt）。
import re as _re

# 去标点：中英常见标点/空白全剥掉再精确匹配（"停。" "停！" "停，" 都要能命中）
_BARGEIN_PUNCT = _re.compile(r"[\s。，！？、；：~～…—·．,.!?;:]+")


def is_bargein_command(conn, text: str) -> bool:
    plain = _BARGEIN_PUNCT.sub("", (text or "").strip())
    if not plain:
        return False
    try:
        cfg = conn.config.get("voiceprint_gate") or {}
    except Exception:
        cfg = {}
    # 2026-09-24 主人裁决：打断关键词机制整个取消（"停止"等词易与退出混淆，
    # 打断恢复为"有声即打断"）。bargein_words 显式配成空列表 [] = 停用；
    # 不配（None）= 用内置默认词表兜底，保证旧配置环境行为不变。
    words = cfg.get("bargein_words", None)
    if words is None:
        words = [
            "停", "停一下", "停下", "停止", "暂停", "等等", "等一下", "稍等", "稍等下",
            "等下", "别说了", "不要说了", "安静", "闭嘴", "打断",
        ]
    if not words:
        return False
    # 2026-09-23 晚：短句（≤8字）里多字词允许包含匹配——"我说停止"（4字含"停止"）
    # 应打断而不是丢给 LLM 误判成退出（22:01 "我说停止"→handle_exit_intent 实证，
    # 主人感受"聊到一半突然退出"）。单字"停"仍只精确匹配，防"停车/停电"误伤。
    if len(plain) > 8:
        return False
    for w in words:
        if len(w) <= 1:
            if plain == w:
                return True
        elif w in plain:
            return True
    return False


def should_gate_voiceprint(conn, speaker_name, text: str = "") -> bool:
    """返回 True = 该段应丢弃（非家庭成员声音）。

    speaker_name：identify_speaker 的返回值——匹配到录入成员时为人名，
    否则为 None / "" / "未知说话人"。
    """
    if not gate_enabled(conn):
        return False

    # [2026-10-12 改造，主人批准方案一] 不再问「音乐在放吗」，改问「这段音频里有没有音乐」。
    #
    # 原实现（2026-10-07 主人裁决）：is_ducking_active() 为真就无条件放行，
    # 代价是音乐播放期间**任何人说话都被受理**（陌生人、电视声都能打断音乐）。
    # 而且 is_ducking_active() 读的是「音量被降低了」，主人 10-07 改用暂停后
    # 它已不是「音乐在放」的可靠信号。
    #
    # 新实现按「音频采集起点」与「音乐暂停时刻」的先后分三档：
    #   ① 音乐没在放                          → 正常阈值 voiceprint.similarity_threshold（0.45）
    #   ② 音乐已被我们暂停，且这句话在暂停之后  → 同样用 0.45（音频是干净的）← 本次最大改进
    #   ③ 这句话采集时音乐还在放               → music_voiceprint_gate_threshold（0.30）
    # ③ 用 0.30 的依据是本文件下方保留的实测数据：音乐播放中家人声纹均值 0.378、
    # 非音乐均值 0.478——音乐只压低约 0.10（AEC 在起作用），不是「完全无法判断」。
    # 0.30 能放过家人（0.378），同时挡掉陌生人/电视（通常 <0.25）。
    music_mode = False
    try:
        from core.utils.music_auto_pause import is_music_active, music_paused_since

        paused_at = music_paused_since()
        audio_start = getattr(conn, "_vad_voice_start_ts", None)
        if paused_at and audio_start and float(audio_start) > float(paused_at):
            music_mode = False      # ② 暂停之后采的，音频干净
        elif is_music_active():
            music_mode = True       # ③ 采集时音乐在放
        if music_mode:
            # [2026-10-18 P3，主人批准] 运行观察：音乐档判定次数（用于判断
            # 主人「边放歌边说话」被误杀的比例）。
            try:
                from core.utils import observation as _obs
                _obs.bump("voiceprint_music_mode")
            except Exception:
                pass
            logger.bind(tag=TAG).info(
                f"[声纹闸门] 音频含音乐，用音乐档阈值: {str(text)[:30]}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[声纹闸门] 音乐状态查询失败，按干净音频处理: {e!r}")
    if speaker_name and speaker_name != "未知说话人":
        return False  # 识别器内部阈值已匹配，放行
    # 2026-09-23 修复口径矛盾（主人插话无响应实证）：
    # identify_speaker 内部阈值 0.4，音乐闸门独立低阈值 0.25——同一段音频
    # （13:53:49 主人"现在几点了"，相似度 0.325）音乐闸门判定"李威，放行"，
    # 全线闸门却因返回值"未知说话人"丢弃 → 主人插话被误杀。
    # 现改为与音乐闸门同口径：last_score ≥ 闸门阈值即放行（疑似家庭成员），
    # 低于阈值才判非家庭成员丢弃。阈值与音乐闸门共用同一配置项。
    vp = getattr(conn, "voiceprint_provider", None)
    score = getattr(vp, "last_score", None)
    # [2026-10-12] 阈值分两档：音频含音乐用音乐专用低阈值，干净音频用认人阈值。
    # 原先不管有没有音乐都读 music_voiceprint_gate_threshold（默认 0.25），
    # 等于把「安静环境」的门槛也一起拉低了 → 陌生人/电视声容易过线被当成家人。
    # 认人阈值 voiceprint.similarity_threshold 已于 2026-10-12 实测定为 0.45。
    try:
        if music_mode:
            threshold = float(conn.config.get("music_voiceprint_gate_threshold", 0.30) or 0.30)
        else:
            vp_cfg = conn.config.get("voiceprint") or {}
            threshold = float(vp_cfg.get("similarity_threshold", 0.45) or 0.45)
    except Exception:
        threshold = 0.30 if music_mode else 0.45
    if score is not None and score >= threshold:
        return False
    if _is_exit_command(conn, text):
        return False  # 退出命令白名单
    return True
