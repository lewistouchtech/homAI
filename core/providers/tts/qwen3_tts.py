# -*- coding: utf-8 -*-
"""Qwen3-TTS provider（走本地 HTTP 服务，见 tts-qwen3/qwen3_tts_server.py）

与 `moss_nano.py` 同构：只负责「把文字发给本地服务、拿回 wav」，
重采样/分帧/流控都交给 `core/providers/tts/base.py` 统一处理。

配置（后台 TTS 页可改，两个 config 文件都要同步）：
    TTS:
      Qwen3TTS-0.6B:
        type: qwen3_tts
        api_url: http://127.0.0.1:8087/api/tts
        model: "0.6B"          # 0.6B | 1.7B | 0.6B-Base
        speaker: Serena        # 中文预置音色：Vivian / Serena / Uncle_Fu / Dylan / Eric
        language: Chinese
        speed: 1.0
        instruct: ""           # 情绪/风格指令（1.7B 支持），如「用温柔一点的语气说」
      Qwen3TTS-1.7B:
        ...（同上，model: "1.7B"）

克隆（mode=clone）与音色设计（mode=design）由后台「Qwen3-TTS 设置页」写入，
provider 只透传。
"""

import asyncio
import json
import sqlite3
import time
import traceback
from pathlib import Path

import requests

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType
from core.utils.foreground_activity import touch as _fg_touch
from core.utils.tts import MarkdownCleaner
from core.utils.util import audio_bytes_to_data_stream, pcm_to_data_stream

TAG = __name__
logger = setup_logging()

# [2026-10-11 B5] tts_config.voice 读取缓存，键为 devices.db 的 (mtime_ns, size)。
# 后台改音色必然写库 → mtime 变 → 立即失效并重读。
_db_voice_cache = {"sig": None, "value": ""}

# 默认语气指令（2026-10-07 实测选型）：无指令时音色随文本漂移明显；
# 这句能把 CAM++ 声纹两两相似度从「中位 0.427 / 最低 0.178」提到「中位 0.573 / 最低 0.478」，
# 同时保持亲切自然（不像「新闻播报腔」那么生硬）。
DEFAULT_INSTRUCT = "用温柔亲切但平稳的语气说，语速适中，不要夸张，情绪起伏小一点"


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_url = config.get("api_url", "http://127.0.0.1:8087/api/tts")
        # 连接复用：keep-alive，省掉每句一次 TCP 握手
        self._session = requests.Session()
        self.model = str(config.get("model") or "1.7B")
        self.speaker = str(config.get("speaker") or "Serena")
        self.language = str(config.get("language") or "Chinese")
        # 语气约束（关键）：Qwen3-TTS 无指令时音色会随文本大幅漂移
        # （CAM++ 声纹实测：两两相似度中位 0.427、最低 0.178 ≈ 换个人）。
        # 给一句「温柔亲切但平稳」的指令后升到中位 0.573 / 最低 0.478。
        self.instruct = str(config.get("instruct") or DEFAULT_INSTRUCT)
        self.mode = str(config.get("mode") or "custom")
        # [2026-10-11 音色稳定性] 采样随机性下调。
        # 实测（CAM++ 声纹，5 句不同文本两两相似度）：
        #   temperature=0.9 / top_k=50 → 最低 0.533
        #   temperature=0.5 / top_k=30 → 最低 0.579（最差情况改善=主人听到的"轻微变化"来源）
        # 中位持平（0.677 → 0.657，噪声范围内），音频无截断、时长因尾部静音减少略短。
        # 只动"最差的那几句"，不动整体表现力。
        try:
            self.temperature = float(config.get("temperature", 0.5))
        except (ValueError, TypeError):
            self.temperature = 0.5
        try:
            self.top_k = int(config.get("top_k", 30))
        except (ValueError, TypeError):
            self.top_k = 30
        try:
            self.top_p = float(config.get("top_p", 0.9))
        except (ValueError, TypeError):
            self.top_p = 0.9
        self.ref_audio = str(config.get("ref_audio") or "")
        self.ref_text = str(config.get("ref_text") or "")
        # 语速：Qwen3-TTS 原生支持 0.5~2.0
        try:
            self.speed = float(config.get("speed", 1.0))
        except (ValueError, TypeError):
            self.speed = 1.0
        self.speed = max(0.5, min(2.0, self.speed))
        # 音色可能由后台 devices.db / tts_config 覆盖（跟 MossNano 一致的做法）
        # 采样种子：固定后同一句合成结果完全一致（消除音色随机漂移）
        try:
            self.seed = int(config.get("seed", 1234))
        except (ValueError, TypeError):
            self.seed = 1234
        # 流式合成（2026-10-12）：服务端边生成边吐 PCM，首声从「整句合成时间」降到约 0.3s。
        # stream: false 可退回整句 WAV 模式（调试/回滚用）。
        self.stream_enabled = str(config.get("stream", True)).strip().lower() not in ("0", "false", "no", "off")
        self.stream_url = self.api_url.rstrip("/") + "/stream"
        self._voice = str(config.get("voice") or "").strip()
        self._backend_module = str(config.get("_module_name") or "").strip()
        logger.bind(tag=TAG).info(
            f"Qwen3-TTS 服务地址: {self.api_url}, 模型: {self.model}, "
            f"音色: {self._voice or self.speaker}, 语言: {self.language}, speed: {self.speed}")

    # 克隆音色表（后台「Qwen3-TTS 设置页」维护：上传 3 秒参考音频 + 对应文字）
    # ⚠️ 路径必须走 find_tts_dir()：tts-qwen3/ 在仓库根，不在 homebrain-server 子模块内，
    # 固定层数的 parent 推导会指错目录（历史上就是这样，克隆音色永远认不出来）。
    from core.utils.qwen3_assets import clone_file as _clone_file

    CLONE_FILE = _clone_file()

    def _load_clones(self) -> dict:
        """id → {name, ref_audio, ref_text}。读不到/损坏一律返回空 dict。"""
        try:
            if self.CLONE_FILE.exists():
                items = json.loads(self.CLONE_FILE.read_text(encoding="utf-8"))
                return {str(i.get("id", "")): i for i in items if i.get("id")}
        except Exception as e:
            logger.bind(tag=TAG).warning(f"读取克隆音色表失败: {e}")
        return {}

    def _resolve_voice_params(self, speaker: str):
        """把「当前音色」解析成服务端要的 (mode, model, speaker, ref_audio, ref_text)。

        克隆音色（cloned_voices.json 里的 id）必须走 mode=clone + **0.6B-Base** 模型
        并带上该音色的参考音频与文字 —— 只把 id 当 speaker 发给 custom 模式，
        服务端不认识会报错；模型不对则 0.6B/1.7B 都没有克隆能力。
        """
        clone = self._load_clones().get(speaker)
        if clone:
            return ("clone", "0.6B-Base", speaker,
                    str(clone.get("ref_audio") or ""), str(clone.get("ref_text") or ""))
        return self.mode, self.model, speaker, self.ref_audio, self.ref_text

    def _get_current_voice(self) -> str:
        """给「过渡语缓存」用的音色标识：**引擎+模型+音色**三元组。

        必须带上引擎与模型，否则切引擎/切音色后过渡语会继续用旧缓存，
        听感就是「好几个人在说话」（主人 2026-10-07 实测反馈）。
        """
        return f"qwen3-{self.model}-{self._get_current_speaker()}-{self._synth_signature()}"

    def _synth_signature(self) -> str:
        """影响音色的合成参数指纹（2026-10-12）。

        过渡句/唤醒句是「合成一次、永久缓存」的，缓存键只有 模型+音色 时，
        改了语气指令/温度/top_k/top_p/种子/语速后旧缓存继续被用——同一个目录里
        新旧参数合成的句子混着播，听起来「不像一个人在说话」（实测 qwen3-0.6B-Serena
        目录里 103 句是 10-07~10-11 不同采样参数下陆续合成的）。把这些参数并入缓存键，
        参数一变就自动换目录重新合成。
        """
        import hashlib

        raw = "|".join(str(x) for x in (
            self.mode, self.instruct, self.temperature, self.top_k, self.top_p,
            self.seed, self.speed, self.language))
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]

    def _read_db_voice(self) -> str:
        """读后台 devices.db 的 tts_config.voice（后台「音色选择」写的就是这里）。

        [2026-10-11 B5] 每次合成一句都要开一次 sqlite 连接读一行，而本函数还是
        「过渡语缓存键」的组成部分（`_get_current_voice`），等于每句至少读两次库。
        改为按**文件 mtime + 大小**判定：后台改音色必然写库 → mtime 变 → 缓存失效。
        比 TTL 缓存更准（改了立刻生效，不用等 5 秒），也省掉打开连接的固定开销。
        """
        db_path = Path(__file__).parent.parent.parent.parent / "data" / "devices.db"
        try:
            st = db_path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return ""
        if _db_voice_cache.get("sig") == sig:
            return _db_voice_cache.get("value", "")

        if not db_path.exists():
            return ""
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT config_value FROM tts_config WHERE config_key='voice'")
            row = cur.fetchone()
            value = str(row[0]).strip() if row and row[0] else ""
            _db_voice_cache["sig"] = sig
            _db_voice_cache["value"] = value
            return value
        except Exception as e:
            logger.bind(tag=TAG).warning(f"读取数据库音色配置失败: {e}")
            return ""
        finally:
            if conn:
                conn.close()

    def _get_current_speaker(self) -> str:
        """当前音色。

        优先用后台 tts_config.voice，但**不校验 tts_config.provider**：
        实测 `switch-provider` 只写配置文件、不更新 tts_config.provider
        （切到 Qwen3 后该字段仍是 MossNano），若按 provider 严格校验，
        后台选的音色会被静默忽略 —— 主人改了没反应就是这个原因。
        改为「只要是合法 Qwen3 音色（预置或克隆）就认」，否则回落到配置文件的 speaker。
        """
        db_voice = self._read_db_voice()
        if db_voice and self._is_valid_speaker(db_voice):
            return db_voice
        return self._valid_or_default(self._voice or self.speaker)

    # Qwen3-TTS 的中文预置音色（与后台列表一致）
    PRESET_SPEAKERS = ("Serena", "Vivian", "Uncle_Fu", "Dylan", "Eric")

    def _is_valid_speaker(self, voice: str) -> bool:
        v = str(voice or "").strip()
        if not v:
            return False
        if v in self.PRESET_SPEAKERS:
            return True
        return v in self._load_clones()

    def _valid_or_default(self, voice: str) -> str:
        """旧引擎的音色名（如 MossNano 的 zh_3）不能直接喂给 Qwen3，回落到配置 speaker。"""
        if self._is_valid_speaker(voice):
            return str(voice).strip()
        if voice:
            logger.bind(tag=TAG).info(f"音色 {voice!r} 不是 Qwen3-TTS 音色，回落到 {self.speaker}")
        return self.speaker

    def _request_data(self, text):
        _mode, _model, _speaker, _ref_audio, _ref_text = self._resolve_voice_params(
            self._get_current_speaker())
        return {
            "text": text, "model": _model, "mode": _mode, "speaker": _speaker,
            "language": self.language, "instruct": self.instruct, "speed": self.speed,
            "ref_audio": _ref_audio, "ref_text": _ref_text, "seed": self.seed,
            "temperature": self.temperature, "top_k": self.top_k, "top_p": self.top_p,
        }

    # 中文播报约 0.28 秒/字（实测 37 字 9.2s、20 字 6s），用来估一句话的总时长
    SEC_PER_CHAR = 0.28

    def _iter_pcm24(self, text):
        """请求流式端点，逐块 yield 24kHz/16bit 裸 PCM（不经过 WAV）。生成器关闭时自动断开连接。"""
        resp = self._session.post(self.stream_url, json=self._request_data(text),
                                  stream=True, timeout=(5, 30))
        try:
            if resp.status_code != 200:
                raise Exception(f"Qwen3-TTS 流式服务返回错误: {resp.status_code} {resp.text[:120]}")
            carry = b""
            for chunk in resp.iter_content(chunk_size=None):
                if not chunk:
                    continue
                carry += chunk
                n = len(carry) - (len(carry) % 2)  # int16 对齐
                if n:
                    yield carry[:n]
                    carry = carry[n:]
        finally:
            resp.close()

    @staticmethod
    def _can_start_without_underrun(audio_s: float, first_s: float, gen_s: float, est_total_s: float) -> bool:
        """自适应起播缓冲（防「每两三个字一断」）。

        流式合成快于实时（RTF<1）时边收边播没问题；但机器被别的负载抢占、合成慢于实时时，
        每 0.5 秒一块的音频要隔好几秒才到，听起来就是「两三个字一停、字念一半」。
        所以先看合成速度 r（每秒产出多少秒音频）：r>=1 立刻起播；r<1 则攒到
        「起播后按 1 秒/秒播放也不会断」的量再开始：已缓冲 a ≥ (总长-a)·(1-r)/r。
        合成再慢也退化成「整句合成完再播」，不会比旧方案更差。
        """
        if est_total_s <= audio_s:
            return True
        if gen_s <= 0.05:
            return False  # 样本太少，等第二块再判断
        r = max(audio_s - first_s, 0.0) / gen_s
        if r >= 1.0:
            return True
        if r <= 0.02:
            return False
        # 留 20% + 0.2s 余量：速率是用前两块估的，后面合成可能再慢一点
        need = (est_total_s - audio_s) * (1.0 - r) / r * 1.2 + 0.2
        return audio_s >= need

    def to_tts(self, text):
        """唤醒回复等「一次拿全部音频包」的入口：直接走流式端点收裸 PCM，不经过 WAV。

        修复（2026-10-12）：基类写死 is_opus=True，PCM 客户端（网页端）收到 Opus 包当 PCM 播，
        整句都是「咔啦咔啦」（日志里首个参考信号只有 57 字节 = 一个 Opus 包）。
        """
        if not self.stream_enabled:
            return super().to_tts(text)
        text = MarkdownCleaner.clean_markdown(text)
        if not text:
            return None
        conn = self.conn
        use_opus = getattr(conn, "audio_format", None) != "pcm"
        out_sr = conn.sample_rate
        for attempt in range(2):
            try:
                import numpy as np
                import soxr

                rs = soxr.ResampleStream(24000, out_sr, 1, dtype="int16")
                parts = []
                for raw in self._iter_pcm24(text):
                    parts.append(rs.resample_chunk(np.frombuffer(raw, dtype="<i2")))
                parts.append(rs.resample_chunk(np.zeros(0, dtype="<i2"), last=True))
                pcm = np.concatenate(parts).astype("<i2").tobytes() if parts else b""
                if not pcm:
                    raise Exception("合成未产出音频")
                packets = []
                pcm_to_data_stream(pcm, is_opus=use_opus, callback=packets.append, sample_rate=out_sr,
                                   opus_encoder=None, flush_tail=True)
                return packets
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Qwen3 to_tts 第{attempt + 1}次失败: {e}")
                time.sleep(0.2)
        return None

    def to_tts_stream(self, text, opus_handler=None, flush_tail: bool = True) -> None:
        """流式合成：服务端 `/api/tts/stream` 吐 24kHz PCM → soxr 流式重采样 → 每块立即入播放队列。

        与基类的区别：基类等整句 WAV 回来才开始出帧；这里每收到一块（约 0.5s 音频）就推送，
        首声不必等整句合成完。打断（client_abort）时立刻断开连接，服务端随即释放推理锁。
        合成慢于实时（机器负载高）时自适应多缓冲一会儿再起播，避免断断续续（见 _can_start_without_underrun）。
        """
        if not self.stream_enabled:
            return super().to_tts_stream(text, opus_handler, flush_tail)
        conn = self.conn
        if hasattr(conn, "latency_monitor"):
            conn.latency_monitor.mark("tts_start", text_length=len(text))
        text = MarkdownCleaner.clean_markdown(text)
        if not text:
            return None
        self.tts_audio_queue.put(
            (SentenceType.FIRST, [], text, getattr(self, "current_sentence_id", None)))
        use_opus = getattr(conn, "audio_format", None) != "pcm"
        out_sr = conn.sample_rate
        sent_any = False
        t0 = time.time()
        first_ms = None
        gen = None
        try:
            import numpy as np
            import soxr

            rs = soxr.ResampleStream(24000, out_sr, 1, dtype="int16")
            handler = opus_handler or self._get_audio_handler()

            def _emit(pcm16, last):
                data = pcm16.tobytes()
                if not data and not last:
                    return
                pcm_to_data_stream(
                    data, is_opus=use_opus, callback=handler, sample_rate=out_sr,
                    opus_encoder=self.opus_encoder if use_opus else None,
                    flush_tail=bool(last and flush_tail))

            est_total = max(len(text) * self.SEC_PER_CHAR, 0.5)
            pending, started = [], False
            audio_s = first_s = 0.0
            t_first = None
            n_chunks = 0
            gen = self._iter_pcm24(text)
            for raw in gen:
                # 两种打断信号：client_abort（用户刚打断）；句子 id 已换（新一轮已开始、abort 已被复位）
                _sid = getattr(self, "current_sentence_id", None)
                if (getattr(conn, "client_abort", False) or getattr(self, "tts_stop_request", False)
                        or (_sid is not None and _sid != getattr(conn, "sentence_id", _sid))):
                    logger.bind(tag=TAG).info(f"流式合成被打断，断开连接: {text[:20]!r}")
                    return None
                _fg_touch()
                now = time.time()
                n_chunks += 1
                dur = len(raw) / 2 / 24000.0
                audio_s += dur
                if t_first is None:
                    t_first = now
                    first_s = dur
                    first_ms = (now - t0) * 1000
                    sid = getattr(self, "current_sentence_id", None)
                    if hasattr(conn, "latency_monitor") and getattr(self, "_qwen3_first_sid", None) != sid:
                        self._qwen3_first_sid = sid
                        conn.latency_monitor.mark("tts_first_chunk", first_chunk_ms=int(first_ms))
                out = rs.resample_chunk(np.frombuffer(raw, dtype="<i2"))
                if started:
                    if len(out):
                        _emit(out, False)
                    continue
                pending.append(out)
                if (n_chunks >= 2 or est_total <= audio_s) and (
                        self._can_start_without_underrun(audio_s, first_s, now - t_first, est_total)
                        or now - t0 > 20.0):
                    started = True
                    if (now - t_first) > 0.05 and (audio_s - first_s) / max(now - t_first, 1e-6) < 1.0:
                        logger.bind(tag=TAG).info(
                            f"[Qwen3流式] 合成慢于实时（{(audio_s - first_s) / (now - t_first):.2f}x），"
                            f"已缓冲 {audio_s:.1f}s 后起播 {text[:12]!r}")
                    for o in pending:
                        if len(o):
                            _emit(o, False)
                            sent_any = True
                    pending = []
            for o in pending:  # 流结束仍未起播（短句/整句）：一次性放出
                if len(o):
                    _emit(o, False)
                    sent_any = True
            tail = rs.resample_chunk(np.zeros(0, dtype="<i2"), last=True)
            _emit(tail, True)
            sent_any = True
            logger.bind(tag=TAG).info(
                f"[Qwen3流式] 首块 {int(first_ms or 0)}ms 总 {int((time.time() - t0) * 1000)}ms "
                f"音频 {audio_s:.1f}s {text[:20]!r}")
            return None
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Qwen3 流式合成失败: {e}（已出声={sent_any}）\n{traceback.format_exc()}")
            if sent_any:
                return None  # 已经出了一部分声，重放整句会重复，直接结束这一句
        finally:
            if gen is not None:
                gen.close()
        # 还没出过声：退回整句 WAV 模式，保证至少能播
        try:
            audio_bytes = asyncio.run(self.text_to_speak(text, None))
            if audio_bytes:
                audio_bytes_to_data_stream(
                    audio_bytes, file_type=self.audio_file_type, is_opus=use_opus,
                    callback=opus_handler or self._get_audio_handler(), sample_rate=conn.sample_rate,
                    opus_encoder=self.opus_encoder if use_opus else None, flush_tail=flush_tail)
        except Exception as e2:
            logger.bind(tag=TAG).error(f"Qwen3 整句兜底也失败: {e2}")
        return None

    async def text_to_speak(self, text, output_file):
        """调用本地 Qwen3-TTS 服务合成。"""
        _mode, _model, _speaker, _ref_audio, _ref_text = self._resolve_voice_params(
            self._get_current_speaker())
        request_data = {
            "text": text,
            "model": _model,
            "mode": _mode,
            "speaker": _speaker,
            "language": self.language,
            "instruct": self.instruct,
            "speed": self.speed,
            "ref_audio": _ref_audio,
            "ref_text": _ref_text,
            "seed": self.seed,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
        }
        try:
            response = await asyncio.to_thread(
                self._session.post, self.api_url, json=request_data, timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise Exception(f"Qwen3-TTS 服务请求失败: {e}")
        if response.status_code != 200:
            raise Exception(f"Qwen3-TTS 服务返回错误: {response.status_code} {response.text[:120]}")
        audio_data = response.content
        if output_file:
            with open(output_file, "wb") as f:
                f.write(audio_data)
            logger.bind(tag=TAG).info(f"音频已保存: {output_file}")
        return audio_data
