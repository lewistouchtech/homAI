import os
import asyncio

from core.utils.util import audio_to_data

TAG = __name__

import logging

logger = logging.getLogger("filler_audio")

_filler_lock = asyncio.Lock()

# 填充音目录（多个自然衔接词轮换，避免每次都是同一个"嗯"）
FILLER_AUDIO_DIR = "config/assets/fillers"
# 单文件回退（目录不存在时使用）
FILLER_AUDIO_FILE = "config/assets/filler_mm.wav"

# 循环填充上限：防止 TTS 长时间不就绪导致无限填充
MAX_FILLER_LOOPS = 12

# 模块级缓存：{文件路径: opus 包列表}（跨连接复用，与具体连接无关）
_filler_cache: dict = {}
# 轮换游标：每次唤醒换一个填充词
_filler_index = [0]

# 过渡句目录（检测到工具调用时播放，填充"工具执行+TTS 首句合成"的静默期）
TRANSITION_AUDIO_DIR = "config/assets/transition"
# 过渡句缓存与轮换游标（与填充音独立）
_transition_cache: dict = {}
_transition_index = [0]


def is_filler_enabled(conn) -> bool:
    """检查唤醒等待短语是否启用（配置开关，默认开启）

    2026-10-12：当前 TTS 是流式（首块约 0.3s）时不再播「哦哦/嗯呢/好嘞」这批填充音。
    它们是 2026-09-17 用老引擎录的固定音频，音色与现在的主音色不是同一个人
    （主人反馈：打招呼的音色与正常播报「听起来不像一个人在说话」）；
    而流式合成起播已足够快，不需要再用填充音掩盖等待。
    """
    try:
        tts = getattr(conn, "tts", None)
        if getattr(tts, "stream_enabled", False):
            return False
        return bool(conn.config.get("enable_wakeup_filler", True))
    except Exception:
        return True


def _resolve_dir_paths(conn, dir_name: str) -> list:
    """解析音频目录下全部 wav 路径（服务根目录优先，cwd 兜底）"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for base in (root, os.getcwd()):
        d = os.path.join(base, dir_name)
        if os.path.isdir(d):
            wavs = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
            return [os.path.join(d, f) for f in wavs]
    return []


def _resolve_filler_paths(conn) -> list:
    """解析填充音文件绝对路径列表（目录扫描多个轮换，兼容不同工作目录启动）"""
    paths = _resolve_dir_paths(conn, FILLER_AUDIO_DIR)
    if paths:
        return paths
    # 回退：单文件
    candidates = []
    custom = None
    try:
        custom = conn.config.get("wakeup_filler_audio")
    except Exception:
        pass
    if custom:
        candidates.append(custom)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates.append(os.path.join(root, FILLER_AUDIO_FILE))
    candidates.append(FILLER_AUDIO_FILE)
    return [c for c in candidates if c and os.path.exists(c)]


async def _load_dir_packets(conn, cache: dict, index: list, paths: list, tag: str) -> list:
    """懒加载未缓存文件 + 轮换选一个，返回选中文件的音频包列表。

    2026-09-17 修：原先一律按 Opus 预编码（is_opus=True），但 PCM 客户端
    （Web 终端）拿到的是被直接下发的字节——Opus 包被当成 16kHz PCM 播放，
    就是"播报中夹咔咔声"的根因（59 字节/包的随机数据）。这里按客户端
    音频格式编码：PCM 客户端出 PCM，Opus 客户端出 Opus。
    """
    is_pcm = getattr(conn, "audio_format", None) == "pcm"
    # 缓存键必须区分格式，否则同一文件会被 PCM/Opus 两种产物互相覆盖
    key_suffix = ":pcm" if is_pcm else ":opus"
    async with _filler_lock:
        for path in paths:
            ck = path + key_suffix
            if ck in cache:
                continue
            try:
                # audio_to_data 自带缓存管理器，此处再缓存包列表
                packets = await audio_to_data(path, is_opus=not is_pcm, use_cache=True)
                cache[ck] = packets or []
                logger.info(f"[{tag}] 加载成功: {path} ({len(cache[ck])} 个{'PCM帧' if is_pcm else 'opus包'})")
            except Exception as e:
                logger.error(f"[{tag}] 加载失败: {e}")
                cache[ck] = []
        # 轮换选择：每次调用换下一个（过滤加载失败/空包的文件）
        usable = [p for p in paths if cache.get(p + key_suffix)]
        if not usable:
            return []
        idx = index[0] % len(usable)
        index[0] = (idx + 1) % len(usable)
        logger.info(f"[{tag}] 本次选用: {os.path.basename(usable[idx])} ({idx+1}/{len(usable)})")
        return cache[usable[idx] + key_suffix]


async def get_filler_packets(conn) -> list:
    """获取本次唤醒使用的填充音 opus 包（懒加载全部候选 + 轮换选择）"""
    paths = _resolve_filler_paths(conn)
    if not paths:
        logger.warning(f"[填充音] 无可用文件: {FILLER_AUDIO_DIR}，等待短语不可用")
        return []
    return await _load_dir_packets(conn, _filler_cache, _filler_index, paths, "填充音")


def is_transition_enabled(conn) -> bool:
    """检查工具调用过渡句是否启用（配置开关，默认开启）"""
    try:
        return bool(conn.config.get("enable_transition_phrase", True))
    except Exception:
        return True


async def get_transition_packets(conn) -> list:
    """获取本次使用的过渡句 opus 包（懒加载全部候选 + 轮换选择）"""
    paths = _resolve_dir_paths(conn, TRANSITION_AUDIO_DIR)
    if not paths:
        logger.warning(f"[过渡句] 目录无文件: {TRANSITION_AUDIO_DIR}")
        return []
    return await _load_dir_packets(conn, _transition_cache, _transition_index, paths, "过渡句")


async def play_transition_once(conn):
    """播放一次过渡句（工具调用前的静默期填充，进流控队列与后续 TTS 无缝衔接）"""
    from core.handle.sendAudioHandle import sendAudio

    if conn.client_abort:
        return
    packets = await get_transition_packets(conn)
    if not packets:
        return
    try:
        await sendAudio(conn, packets)
    except Exception as e:
        logger.error(f"[过渡句] 播放失败: {e}")



async def play_filler_until_ready(conn, ready_check, max_loops: int = MAX_FILLER_LOOPS):
    """
    循环播放填充音直到 ready_check 返回 True（TTS 就绪）。

    音频包按序进入流控队列，设备依次播放，正式 TTS 包入队后无缝衔接。

    Args:
        conn: 连接对象
        ready_check: 无参可等待 callable（同步函数或 async 函数），返回 True 停止填充
        max_loops: 最大循环轮数（每轮约 280ms）
    """
    from core.handle.sendAudioHandle import sendAudio

    packets = await get_filler_packets(conn)
    if not packets:
        return

    # 【修复 2026-09-11】原 bug：每循环轮都重新灌整包列表且无 sleep，
    # 合成仅 ~1.3s 而 12 轮瞬间完成，同句入队列 12 份，端挨放 12 份
    await sendAudio(conn, packets)
    for _ in range(max_loops):
        if conn.client_abort:
            break
        try:
            result = ready_check()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                break
        except Exception as e:
            logger.error(f"[填充音] ready_check 异常: {e}")
            break
        # 等一小段时间再查（~280ms/轮，约一段播完），不重灌
        await asyncio.sleep(0.28)
