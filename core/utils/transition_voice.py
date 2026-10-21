# -*- coding: utf-8 -*-
"""过渡语 2.0：判停后立刻用**主音色**出声，填满等待（2026-10-06）

## 为什么重做

原来的「前置过渡句」只发字幕不出声（`_tts_speak` 只调 `send_tts_message`）；
真正出声的是 `filler_audio.play_transition_once` —— 3 个 2026-09-17 录的 24kHz
固定句，而且在 LLM 返回 tool_calls 之后（约 +4s）才播。三个后果：

1. 等待的前 4 秒仍然静音，过渡语没抢到任何时间；
2. 过渡语是另一个音色（老引擎 24kHz vs 主音色 MossNano 48kHz）；
3. 屏幕字幕（"我看看天气"）与耳朵听到的（"让我查一下"）不是同一句。

## 本模块做什么

用**当前后台音色**把候选过渡语合成一次并落盘缓存，运行期直接取包播放：

- 音色与正文一致（同一个 TTS provider、同一套音量归一化 -3dBFS）
- 字幕与语音同源（同一段 text，先发 `sentence_start` 再送音频）
- 不再依赖那 3 个老录音

缓存按「音色」分目录：后台换音色后，下次用到会重新合成并缓存，自动跟随。

⚠️ 刻意**不做启动预热**：MossNano 是单实例，预热请求会和正文合成抢同一个服务，
反而可能拖慢真正的回答。改为「首次用到才合成一次，之后永久走缓存」。
"""

import os
import hashlib
import threading

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 候选过渡语：直接取自 `core/utils/lead_in.py` 的文案池（单一真源，避免两处漂移）。
# 池里每句都会预生成缓存，运行期零延迟取包。
# 2026-10-06 起每个主题有多个变体、随机轮换不连续重复（主人反馈「太僵化」）。
from core.utils.lead_in import ALL_LEAD_INS

CANDIDATE_PHRASES = tuple(ALL_LEAD_INS)

_CACHE_DIR = os.path.join("config", "assets", "transition_cache")
_packets_cache: dict = {}
_cache_lock = threading.Lock()


def _server_root() -> str:
    """core/utils/transition_voice.py → 服务根目录"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _voice_key(conn) -> str:
    """当前 TTS 音色（缓存分区键）。取不到时用 default，保证功能可用。"""
    try:
        voice = conn.tts._get_current_voice()
        voice = str(voice or "").strip()
        return voice or "default"
    except Exception:
        return "default"


def _wav_path(voice: str, text: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
    return os.path.join(_server_root(), _CACHE_DIR, voice, f"t_{digest}.wav")


async def _ensure_wav(conn, text: str):
    """确保该句在当前音色下有缓存音频；没有就合成一次。返回路径或 None。"""
    voice = _voice_key(conn)
    path = _wav_path(voice, text)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先写临时文件再改名：避免合成中断留下半个文件被当成有效缓存
        tmp = path + ".part"
        await conn.tts.text_to_speak(text, tmp)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, path)
            logger.bind(tag=TAG).info(f"[过渡语] 合成并缓存: {text!r}（音色 {voice}）")
            return path
        logger.bind(tag=TAG).warning(f"[过渡语] 合成产物为空: {text!r}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[过渡语] 合成失败（本次跳过过渡语）: {text!r} {e!r}")
    return None


async def get_transition_packets(conn, text: str) -> list:
    """取过渡语音频包（按客户端音频格式编码，带内存缓存）。"""
    path = await _ensure_wav(conn, text)
    if not path:
        return []
    is_pcm = getattr(conn, "audio_format", None) == "pcm"
    key = (path, is_pcm)
    with _cache_lock:
        if key in _packets_cache:
            return _packets_cache[key]
    try:
        from core.utils.util import audio_to_data

        packets = await audio_to_data(path, is_opus=not is_pcm, use_cache=True)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[过渡语] 音频转码失败: {path} {e!r}")
        packets = []
    with _cache_lock:
        _packets_cache[key] = packets or []
    return _packets_cache[key]


async def speak_transition(conn, text: str) -> None:
    """播一句过渡语：先发字幕再送音频，保证「看到的」和「听到的」是同一句。

    刻意**不发 tts stop**：stop 会把前端 `_ignorePcm` 置真、并停掉流控发送器，
    导致紧随其后的正文音频被丢弃。过渡语只发 sentence_start。
    """
    text = (text or "").strip()
    if not text or conn.client_abort:
        return
    packets = await get_transition_packets(conn, text)
    if not packets:
        return
    try:
        from core.handle.sendAudioHandle import send_tts_message, sendAudio

        await send_tts_message(conn, "sentence_start", text)
        await sendAudio(conn, packets)
        logger.bind(tag=TAG).info(f"[过渡语] 已播报: {text!r}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[过渡语] 播报失败: {e!r}")


def looks_like_template(text: str) -> bool:
    """是否还有未替换的占位符（如 `查下{location}天气`）。

    这类文本念出来就是噪音，宁可不说。B 类衔接语模板目前没有变量替换逻辑。
    """
    return "{" in (text or "") or "}" in (text or "")
