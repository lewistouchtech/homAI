"""Apple Music 自动音量调节 + 系统级 ducking。

⚠️ [2026-10-13 音乐不停对话·第三段] 本文件的暂停/降音量 API
（maybe_lower_volume / pause_music_for_dialog / schedule_restore /
schedule_music_resume / restore_volume 等）**已无任何生产调用点**——
双路径 AEC（Process Tap 参考 → VAD，原始音频 → ASR/声纹）上线后，
对话期间音乐不暂停、不降音量。函数保留仅为单测与将来回滚。
生产链路仍在使用的是：is_music_active() / music_paused_since()
（声纹闸门选阈值档）、should_gate_music_text()（已按主人裁决停用）、
note_reference_energy()（新增，Process Tap 参考能量 → 音乐活跃信号）。

（以下为旧流程历史说明）
对话期间降低音量（而非完全暂停），配合系统 AEC 提升语音识别准确率。
策略：VAD 检测到用户开始说话 → 降低音量到 20%；
回复播完 → 延迟几秒恢复原音量。

两层 ducking：
1. Apple Music 精准 ducking（Music 在播时走这层，只调 Music 音量）；
2. 系统级 ducking（Music 未播但 YouTube/其它应用在外放时走这层，
   调 macOS 系统输出音量——2026-09-23 新增，治"主人说话压不住视频声"）。

通过 config.yaml 配置：
music_auto_pause: true
music_volume_speaking: 20  # 说话时音量（0-100）
music_volume_normal: 100   # 正常音量（0-100）
system_duck: true          # 系统级 ducking 开关
"""

import asyncio
import time
from collections import deque

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_state = {"lowered": False, "resume_task": None, "original_volume": None}
# 系统级 ducking 独立状态（与 Music ducking 互斥分支，不会同时 lowered）
_state_sys = {"lowered": False, "resume_task": None, "original_volume": None,
              "last_check_ts": 0.0}

AS_GET_VOLUME = 'tell application "Music" to sound volume'
AS_SET_VOLUME = 'tell application "Music" to set sound volume to'
AS_CHECK_PLAYING = 'tell application "Music" to player state as string'
# 系统输出音量（影响全部应用，含 YouTube 外放）
AS_GET_SYS_VOLUME = 'output volume of (get volume settings)'
AS_SET_SYS_VOLUME = 'set volume output volume {}'
AS_PAUSE = 'tell application "Music" to pause'
AS_PLAY = 'tell application "Music" to play'

# 2026-10-07 主人裁决：对话期间**暂停音乐**（不再用降音量——那个策略已被主人取消：
# 「只要有声音音量就会降低，歌没法放，忽高忽低特别难受」）。
#
# 暂停触发（两者都要，主人选定）：
#   ① 唤醒词命中 → 立刻暂停（第一句话也能说干净）
#   ② 系统决定要回应这次说话 → 保底暂停（闲聊不触发，因为它根本不回应）
# 恢复：本轮回复播完 + 静默 N 秒（有新的说话则取消恢复）。
#
# 只恢复「我们自己暂停的」——主人手动暂停的音乐不会被我们擅自播起来。
_pause_state = {"paused": False, "resume_task": None, "last_pause_ts": 0.0}
MUSIC_RESUME_SILENCE_S = 2.0   # 恢复前要求的静默时长
_NORMAL_FALLBACK = 100        # 拿不到配置时的正常音量兜底


def _enabled(conn) -> bool:
    try:
        return bool(conn.config.get("music_auto_pause", False))
    except Exception:
        return False


def _speaking_volume(conn) -> int:
    if conn is None:
        return 20
    try:
        return int(conn.config.get("music_volume_speaking", 20))
    except Exception:
        return 20


def _normal_volume(conn) -> int:
    if conn is None:
        return 100
    try:
        return int(conn.config.get("music_volume_normal", 100))
    except Exception:
        return 100


async def _run_applescript(script: str):
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # osascript 目标 App 弹授权框/卡死时会无限阻塞，而本函数处在
    # handleAudioMessage 主链路上，挂起即冻结整个语音管线（2026-09-29 实测），
    # 必须加超时：超时杀进程并抛错，由调用方的 except 分支降级处理
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_APPLESCRIPT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()  # 回收子进程，避免僵尸
        except ProcessLookupError:
            pass
        raise TimeoutError(
            f"osascript 超时（{_APPLESCRIPT_TIMEOUT_S}s）：{script[:60]}"
        )
    return stdout.decode().strip(), stderr.decode().strip()


# AppleScript 超时上限：正常查询/设置 <300ms，3s 已是极宽兜底
_APPLESCRIPT_TIMEOUT_S = 3.0


def is_ducking_active() -> bool:
    """当前是否处于任一 ducking 状态（Music 或系统音量已降低）。
    供 ASR 声纹闸门复用，不产生任何系统调用。"""
    return bool(_state["lowered"] or _state_sys["lowered"])


def _sys_duck_enabled(conn) -> bool:
    try:
        return bool(conn.config.get("system_duck", True))
    except Exception:
        return True


def _cancel_pending_resume_sys():
    t = _state_sys.get("resume_task")
    if t and not t.done():
        t.cancel()
    _state_sys["resume_task"] = None


async def _maybe_lower_system_volume(conn):
    """系统级 ducking：Music 未在播（或 Music 不可用）时，用户说话则压系统音量。

    覆盖 YouTube/腾讯视频等任意应用的外放声——它们的音量不受 Music ducking 控制。
    注意本函数被 VAD 每帧调用，osascript 昂贵，内部 800ms 节流。
    """
    if not _sys_duck_enabled(conn):
        return
    if _state_sys["lowered"]:
        _cancel_pending_resume_sys()  # 用户还在说话，取消挂起的恢复
        return
    now = time.time()
    if now - _state_sys["last_check_ts"] < 0.8:
        return
    _state_sys["last_check_ts"] = now
    _cancel_pending_resume_sys()
    try:
        vol_str, err = await _run_applescript(AS_GET_SYS_VOLUME)
        if err or not vol_str.isdigit():
            return
        curr = int(vol_str)
        speak_vol = _speaking_volume(conn)
        # 已经很低（用户自己调小/会议场景）不动，避免越调越乱
        if curr <= speak_vol:
            return
        _state_sys["original_volume"] = curr
        await _run_applescript(AS_SET_SYS_VOLUME.format(speak_vol))
        _state_sys["lowered"] = True
        conn.logger.bind(tag=TAG).info(
            f"检测到说话，已降低系统音量到 {speak_vol}%（原 {curr}%）")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"降低系统音量失败: {e}")


async def restore_system_volume(conn=None):
    """恢复系统输出音量。"""
    if not _state_sys["lowered"]:
        return
    orig = _state_sys.get("original_volume")
    target = orig if (orig and orig > 0) else 100
    try:
        await _run_applescript(AS_SET_SYS_VOLUME.format(target))
        logger.bind(tag=TAG).info(f"对话结束，已恢复系统音量到 {target}%")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"恢复系统音量失败: {e}")
    finally:
        _state_sys["lowered"] = False
        _state_sys["original_volume"] = None


# 2026-09-21 修复：音乐控制词白名单——这些指令必须能被识别，
# 实测在音乐播放中被闸门误杀最多（"关闭音乐/停止回放/下一首"等）
_MUSIC_CTL_WORDS = (
    "关闭音乐", "关掉音乐", "停止音乐", "停止播放", "停止回放", "别放了", "不要放了",
    "暂停", "暂停播放", "下一首", "上一首", "下一位", "上一位", "切歌",
    "换一个", "换一首", "换歌", "换个歌", "大声", "小声", "音量", "静音",
    "继续播放", "接着放", "播放音乐", "放音乐", "放首歌", "放点音乐", "来点音乐",
)

# 闸门独立阈值默认值：低于识别阈值（0.4），因为音乐回采会整体压低声纹分数
# （实测主人音乐播放中均值 0.378、51% 低于 0.4；非音乐均值 0.478）
_GATE_DEFAULT_THRESHOLD = 0.25


def _gate_threshold(conn) -> float:
    if conn is None:
        return _GATE_DEFAULT_THRESHOLD
    try:
        return float(conn.config.get("music_voiceprint_gate_threshold", _GATE_DEFAULT_THRESHOLD))
    except Exception:
        return _GATE_DEFAULT_THRESHOLD


def should_gate_music_text(conn, speaker_name, text: str = "") -> bool:
    """音乐播放期间声纹闸门：识别文本来自非家庭成员声音时判为歌词残留，应丢弃。

    前提：VAD 触发降音量后音乐在 20% 播放，此时仍能漏进 ASR 的只可能是
    音乐内容本身。

    2026-09-21 修复（误杀真实指令）：
    1. 控制词白名单直接放行——音乐控制指令绝不能丢；
    2. 不再复用 0.4 识别阈值，改用最近一次声纹原始分数 + 独立低阈值
       （music_voiceprint_gate_threshold，默认 0.25）判断；实测音乐播放中
       主人分数被整体压低（均值 0.378，51% < 0.4），复用 0.4 会误杀过半。

    仅在以下条件同时满足时生效：
    - config.music_voiceprint_gate 开启
    - 音乐正在播放（降音量状态激活）
    - 声纹识别已启用，且未匹配到家庭成员、分数低于闸门独立阈值
    """
    if not is_ducking_active():
        return False
    # 2026-10-07 主人裁决：音乐播放期间**不再做声纹判定**（音乐与声纹混在一起判不准）。
    # 本闸门因此停用——保留函数与开关只为将来需要时能一行恢复。
    # 原逻辑：识别文本来自非家庭成员声音时判为歌词残留并丢弃；实测误杀主人真实指令。
    try:
        if not conn.config.get("music_voiceprint_gate_reenable", False):
            logger.bind(tag=TAG).info(
                f"[音乐闸门] 已按主人裁决停用（音乐期间不做声纹判定），放行: {str(text)[:30]}")
            return False
    except Exception:
        return False
    vp = getattr(conn, "voiceprint_provider", None)
    if vp is None or not getattr(vp, "enabled", False):
        return False
    # 已明确识别为家庭成员：放行
    if speaker_name and speaker_name != "未知说话人":
        return False
    # 控制词白名单：音乐控制指令放行
    if text and any(w in text for w in _MUSIC_CTL_WORDS):
        logger.bind(tag=TAG).info(f"[音乐闸门] 控制词放行: {text[:30]}")
        return False
    # 独立低阈值：原始分数达到闸门阈值即视为家庭成员声纹
    last_score = getattr(vp, "last_score", None)
    if isinstance(last_score, (int, float)) and last_score >= _gate_threshold(conn):
        logger.bind(tag=TAG).info(
            f"[音乐闸门] 相似度 {last_score:.3f} ≥ 阈值 {_gate_threshold(conn)}，放行: {text[:30]}"
        )
        return False
    return True


def _cancel_pending_resume():
    t = _state.get("resume_task")
    if t and not t.done():
        t.cancel()
    _state["resume_task"] = None


async def maybe_lower_volume(conn):
    """VAD 检测到用户说话：若 Apple Music 正在播放则降低音量。

    每帧都可能被调用，已降低时快速返回（同时取消挂起的恢复任务——用户还在说话）。
    """
    if not _enabled(conn):
        return
    if _state["lowered"]:
        _cancel_pending_resume()
        return
    _cancel_pending_resume()
    # 2026-10-11：每个有声帧（PCM 约每 128ms 一次）都会走到这里，原先每次都起一个
    # osascript 子进程查 Music 状态（超时 3 秒），占着音频接收循环。节流到 0.8 秒一次。
    _now = time.monotonic()
    if _now - _state.get("last_check_ts", 0.0) < 0.8:
        return
    _state["last_check_ts"] = _now
    try:
        state, err = await _run_applescript(AS_CHECK_PLAYING)
    except Exception as e:
        conn.logger.bind(tag=TAG).debug(f"检查 Music 播放状态失败: {e}")
        return
    # [2026-10-12] 把这次查到的播放状态缓存下来，供声纹闸门零开销读取。
    # 闸门不能自己起 osascript 子进程（超时 3 秒，会卡住 ASR 链路），
    # 所以复用这里 0.8 秒一次的查询结果。
    _state["playing_cached"] = (state == "playing")
    _state["playing_cached_ts"] = _now
    # App 未运行/无权限时 err 非空或 state 不是 playing
    if state != "playing":
        # Music 没在播 → 兜底系统级 ducking（YouTube 等任意应用外放场景）
        await _maybe_lower_system_volume(conn)
        return
    try:
        # 保存当前音量
        vol_str, _ = await _run_applescript(AS_GET_VOLUME)
        curr = int(vol_str) if vol_str.isdigit() else _normal_volume(conn)
        speak_vol = _speaking_volume(conn)
        # 2026-09-17 修复：只有当前音量高于说话音量才算"真实原音量"。
        # 否则上一轮恢复失败时音量仍停在说话音量(如20%)，会被误记为原音量 → 永久卡低音量。
        _state["original_volume"] = curr if curr > speak_vol else _normal_volume(conn)
        # 降低到说话音量
        target = speak_vol
        await _run_applescript(f"{AS_SET_VOLUME} {target}")
        _state["lowered"] = True
        conn.logger.bind(tag=TAG).info(f"检测到说话，已降低 Apple Music 音量到 {target}%")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"降低 Music 音量失败: {e}")


def bump_original_volume(delta: int) -> None:
    """用户语音调 Music 音量时，若正处于降音量对话中，把增量同步到恢复目标音量。

    场景：音乐被降到说话音量(40)期间主人说「大点声」→ 当前音量 +10，
    同时「本轮回复播完后要恢复到的原音量」也 +10，否则恢复动作会把调节抹掉。
    未在降音量状态时什么都不做（此时直接调的就是真实音量）。
    """
    if not _state["lowered"]:
        return
    try:
        orig = int(_state.get("original_volume") or _NORMAL_FALLBACK)
    except (TypeError, ValueError):
        orig = _NORMAL_FALLBACK
    _state["original_volume"] = max(0, min(100, orig + int(delta)))


# ┌─────────────────────────────────────────────────────────────────────┐
# │ [2026-10-13 音乐不停对话·第三段] 「音乐在放」信号重建                  │
# ├─────────────────────────────────────────────────────────────────────┤
# │ 旧信号源是 maybe_lower_volume 每 0.8 秒 osascript 查 Music 播放状态    │
# │ 写 playing_cached。降音量/暂停流程从生产链路删除后无人更新缓存 →       │
# │ is_music_active() 恒保守 True → 声纹闸门永远走音乐档宽松阈值。        │
# │ 现改用 Process Tap 系统音频参考信号能量（RMS 门限 + 滑动时间窗）判定： │
# │ core/utils/system_aec.py 的采集回调每帧（约 93 次/秒）调              │
# │ note_reference_energy()，节流 0.2 秒写回原缓存字段                    │
# │ （playing_cached / playing_cached_ts），is_music_active() 与          │
# │ 声纹闸门口径不变。                                                    │
# │ 保守方向不变：采集器没跑/缓存过期 → is_music_active() 返回 True        │
# │ （宁可少拦一个陌生人，不能误杀主人）。                                │
# │ ⚠️ 参考信号是「全系统输出音频」：TTS 播报经浏览器外放时也会被算成      │
# │ 「音乐在放」（闸门走宽松档）——方向与旧暂停流程一致（旧流程暂停期间     │
# │ is_music_active 同样为 True），可接受。                               │
# └─────────────────────────────────────────────────────────────────────┘
_MUSIC_RMS_MIN = 200.0               # int16 尺度 RMS 门限（与对齐器 min_rms 同量级）
_MUSIC_ACTIVE_WINDOW_S = 2.0         # 滑动窗口：窗内存在超门限帧 = 音乐在放
_MUSIC_CACHE_WRITE_INTERVAL_S = 0.2  # 缓存写回节流（帧回调约 93 次/秒）
_ref_energy_window = deque()         # (monotonic_ts, rms) 滑动窗口


def note_reference_energy(payload: bytes, _now: float | None = None) -> None:
    """Process Tap 参考帧回调（采集线程）：按滑动窗口能量判定「音乐在放」，
    刷新 is_music_active() 读的缓存字段。

    判定口径：窗口（_MUSIC_ACTIVE_WINDOW_S 秒）内任一帧 RMS ≥ _MUSIC_RMS_MIN
    → 音乐在放。系统音频静音时帧仍持续到达（tap 不断流），窗口滑过后
    自动翻回 False；采集器彻底没跑时无人写缓存，is_music_active() 的
    5 秒过期保护兜底为 True（保守）。

    Args:
        payload: int16 LE 单声道 PCM 帧（16kHz，约 170 样本/帧）
        _now: 仅供单测注入时间轴，生产不传
    """
    now = time.monotonic() if _now is None else float(_now)
    try:
        import numpy as np
        arr = np.frombuffer(payload, dtype=np.int16)
        rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2))) if len(arr) else 0.0
    except Exception:
        return
    _ref_energy_window.append((now, rms))
    cutoff = now - _MUSIC_ACTIVE_WINDOW_S
    while _ref_energy_window and _ref_energy_window[0][0] < cutoff:
        _ref_energy_window.popleft()
    # 写回节流：窗口维护每帧都做（廉价），缓存字段 0.2 秒写一次足够
    if now - _state.get("ref_cache_write_ts", 0.0) < _MUSIC_CACHE_WRITE_INTERVAL_S:
        return
    _state["ref_cache_write_ts"] = now
    _state["playing_cached"] = any(
        r >= _MUSIC_RMS_MIN for _, r in _ref_energy_window)
    _state["playing_cached_ts"] = now


def is_music_active() -> bool:
    """「这段音频里可能混着音乐吗」——供声纹闸门判断用（2026-10-12 主人批准方案一）。

    与 is_ducking_active() 的区别：那个只反映「音量被降低了」，
    主人 2026-10-07 改用暂停后就不再是「音乐在放」的可靠信号。
    本函数综合三路信号：
      ① 我们暂停过音乐（_pause_state["paused"]）→ 暂停前那几帧音频必然混着音乐
        （2026-10-13 起暂停流程已删，此路恒 False，保留兼容）；
      ② 音量被降低中（_state["lowered"]）→ Music 正在放
        （2026-10-13 起降音量流程已删，此路恒 False，保留兼容）；
      ③ Process Tap 系统音频参考能量的滑动窗口判定（note_reference_energy
        每 0.2 秒写回缓存；替代旧的 maybe_lower_volume osascript 轮询）。

    ⚠️ 缓存过期（>5 秒）时**保守返回 True**：宁可少拦一个陌生人，
    也不能误杀主人自己（主人 2026-10-07 取舍原话：「听不见 > 偶尔多应答一句」）。
    """
    if _pause_state["paused"] or _state["lowered"]:
        return True
    cached = _state.get("playing_cached")
    cached_ts = _state.get("playing_cached_ts", 0.0)
    if cached is None:
        return True          # 从没查过 → 保守按「有音乐」处理
    if time.monotonic() - float(cached_ts) > 5.0:
        return True          # 缓存过期 → 保守
    return bool(cached)


def music_paused_since() -> float:
    """返回我们暂停音乐的时刻（time.time() 口径）；没在暂停状态返回 None。

    声纹闸门用它与 conn._vad_voice_start_ts 比先后：
    音频起点晚于暂停时刻 = 这句话是在音乐停下之后采的，可以用正常阈值。
    """
    if not _pause_state["paused"]:
        return None
    ts = _pause_state.get("last_pause_ts") or 0.0
    return float(ts) if ts else None


def is_music_paused_by_us() -> bool:
    """当前音乐是不是「因为对话被我们暂停的」。"""
    return bool(_pause_state["paused"])


def cancel_pending_music_resume():
    """有新的说话进来 → 取消挂起的恢复（别在用户说话时把音乐放起来）。"""
    task = _pause_state.get("resume_task")
    if task is not None and not task.done():
        task.cancel()
    _pause_state["resume_task"] = None


async def pause_music_for_dialog(conn, reason: str = ""):
    """对话需要安静环境：暂停 Apple Music。

    - Music 没在播（或未授权/未运行）→ 什么都不做；
    - 已经被我们暂停过 → 只取消挂起的恢复；
    - 只暂停我们自己控制的那次播放。
    """
    if not _enabled(conn):
        return
    cancel_pending_music_resume()
    if _pause_state["paused"]:
        return
    try:
        state, err = await _run_applescript(AS_CHECK_PLAYING)
    except Exception as e:
        conn.logger.bind(tag=TAG).debug(f"检查 Music 播放状态失败: {e}")
        return
    if state != "playing":
        return
    try:
        await _run_applescript(AS_PAUSE)
        _pause_state["paused"] = True
        _pause_state["last_pause_ts"] = time.time()
        conn.logger.bind(tag=TAG).info(f"对话期间暂停音乐（{reason or '对话中'}）")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"暂停 Music 失败: {e}")


def schedule_music_resume(conn=None, delay: float = None):
    """本轮对话结束：挂一个「静默 delay 秒后恢复播放」的任务。

    有新的说话进来时会调 cancel_pending_music_resume() 取消它，
    所以不会出现「用户还在说，音乐先响了」。
    """
    if not _pause_state["paused"]:
        return
    cancel_pending_music_resume()
    wait = MUSIC_RESUME_SILENCE_S if delay is None else float(delay)

    async def _delayed():
        try:
            await asyncio.sleep(wait)
            await _do_resume(conn)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if conn is not None:
                conn.logger.bind(tag=TAG).warning(f"恢复 Music 播放失败: {e}")

    try:
        loop = asyncio.get_event_loop()
        _pause_state["resume_task"] = loop.create_task(_delayed())
    except Exception as e:
        if conn is not None:
            conn.logger.bind(tag=TAG).warning(f"挂起恢复任务失败: {e}")


async def _do_resume(conn=None):
    if not _pause_state["paused"]:
        return
    try:
        await _run_applescript(AS_PLAY)
        if conn is not None:
            conn.logger.bind(tag=TAG).info("对话结束，已恢复音乐播放")
    except Exception as e:
        if conn is not None:
            conn.logger.bind(tag=TAG).warning(f"恢复 Music 播放失败: {e}")
    finally:
        _pause_state["paused"] = False
        _pause_state["resume_task"] = None


async def restore_volume(conn=None):
    """恢复音乐音量。

    conn 可选：传入时才能读到 config.yaml 里的 music_volume_normal/speaking，
    不传则退回默认值（100/20）。旧调用点 intentHandler 已改为传 conn。
    """
    if not _state["lowered"]:
        return
    orig = _state.get("original_volume")
    # 2026-09-17 修复：二次校验，避免把说话音量(如20%)当成原音量恢复
    target = orig if (orig and orig > _speaking_volume(conn)) else _normal_volume(conn)
    try:
        await _run_applescript(f"{AS_SET_VOLUME} {target}")
        logger.bind(tag=TAG).info(f"对话结束，已恢复 Apple Music 音量到 {target}%")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"恢复 Music 音量失败: {e}")
    finally:
        _state["lowered"] = False
        _state["original_volume"] = None


def schedule_restore(conn, delay: float = 3.0):
    """回复播完后延迟恢复音量；期间用户再说话会被 maybe_lower_volume 取消。"""
    # 2026-09-23 修复：Music 未播时压低走的是系统级 ducking（_state_sys），
    # 原条件只看 _state 会在「Music 没播、只压了系统音量」时直接 return，
    # 导致系统音量永不恢复。两个都未压低才无需恢复。
    if not _state["lowered"] and not _state_sys["lowered"]:
        return
    _cancel_pending_resume()

    async def _delayed():
        await asyncio.sleep(delay)
        try:
            await restore_volume(conn)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"恢复 Music 音量失败: {e}")

    _state["resume_task"] = asyncio.create_task(_delayed())
    # 系统级 ducking 同样延迟恢复
    if _state_sys["lowered"]:
        _cancel_pending_resume_sys()

        async def _delayed_sys():
            await asyncio.sleep(delay)
            try:
                await restore_system_volume(conn)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"恢复系统音量失败: {e}")

        _state_sys["resume_task"] = asyncio.create_task(_delayed_sys())
