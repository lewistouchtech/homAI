import os
import io
import time
import json
import sqlite3
import wave
import numpy as np
from typing import Optional, List, Dict
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 尝试导入必要的库
try:
    import torch
    from scipy.io import wavfile
    from modelscope.pipelines import pipeline
    from modelscope.utils.constant import Tasks
    HAS_TORCH = True
except ImportError as e:
    HAS_TORCH = False
    logger.bind(tag=TAG).error(f"声纹识别依赖缺失: {e}")

class VoiceprintProvider:
    """本地声纹识别提供者 - 基于 3D-Speaker CAM++ 模型"""

    # 类级模型缓存：CAM++ 加载 2-5s，WS 频繁重连时每次 hello 都会新建实例，
    # 必须复用已加载的 pipeline，否则每次重连都阻塞并重新 initiate
    _MODEL_CACHE = {"pipeline": None, "loaded": False}

    def __init__(self, config: dict):
        self.enabled = False
        self.pipeline = None
        self.model = None
        self.db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "devices.db")
        if not os.path.isabs(self.db_path):
            self.db_path = os.path.abspath(self.db_path)

        # 配置参数
        self.similarity_threshold = float(config.get("similarity_threshold", 0.45))
        self._config = config or {}
        self._last_confident = None
        from collections import deque
        self._recent_confident = deque(maxlen=30)

        if not HAS_TORCH:
            logger.bind(tag=TAG).error("声纹识别依赖 torch 和 modelscope，请安装后重试")
            return

        try:
            # 复用类级缓存的模型（WS 重连时避免重复加载 2-5s）
            cls = type(self)
            if cls._MODEL_CACHE["loaded"]:
                self.pipeline = cls._MODEL_CACHE["pipeline"]
                self.model = self.pipeline.model
                self.model.eval()
                self.enabled = True
                logger.bind(tag=TAG).info("声纹识别已启用 (CAM++, 复用已缓存模型)")
                return

            # 初始化 CAM++ 声纹模型
            # 模型会自动下载到 ~/.cache/modelscope
            logger.bind(tag=TAG).info("正在加载 CAM++ 声纹模型...")
            self.pipeline = pipeline(
                task=Tasks.speaker_verification,
                model="damo/speech_campplus_sv_zh-cn_16k-common"
            )
            self.model = self.pipeline.model
            self.model.eval()
            cls._MODEL_CACHE["pipeline"] = self.pipeline
            cls._MODEL_CACHE["loaded"] = True
            self.enabled = True
            logger.bind(tag=TAG).info("声纹识别已启用 (CAM++)")
        except Exception as e:
            logger.bind(tag=TAG).error(f"声纹模型加载失败: {e}")
            self.enabled = False

    def _extract_embedding(self, audio_data: bytes) -> Optional[np.ndarray]:
        """从 WAV 音频数据提取 192 维声纹特征向量"""
        if not self.model:
            return None

        try:
            # 解析 WAV 数据
            wav_io = io.BytesIO(audio_data)
            with wave.open(wav_io, 'rb') as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                pcm_data = wf.readframes(n_frames)

            duration = n_frames / sample_rate if sample_rate > 0 else 0
            logger.bind(tag=TAG).info(f"声纹音频: {duration:.2f}s, {sample_rate}Hz, {channels}ch, {sample_width*8}bit")

            if sample_rate != 16000:
                logger.bind(tag=TAG).warning(f"声纹识别期望 16kHz，实际 {sample_rate}Hz")
                return None

            if duration < 1.0:
                logger.bind(tag=TAG).warning(f"声纹音频太短 ({duration:.2f}s)，建议 3-10 秒")

            # 转换为 float32 numpy 数组
            if sample_width == 2:
                audio_np = np.frombuffer(pcm_data, dtype=np.int16)
                waveform = torch.from_numpy(audio_np.astype(np.float32) / 32768.0).unsqueeze(0)
            else:
                return None

            # 模型推理 (Batch, Time) -> (Batch, 192)
            with torch.no_grad():
                embedding = self.model(waveform)

            # 归一化
            embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
            return embedding.squeeze(0).numpy()

        except Exception as e:
            logger.bind(tag=TAG).error(f"声纹特征提取失败: {e}")
            return None

    # 声纹库缓存：每句话都读一次 sqlite 没必要，5 秒 TTL 足够让后台重录声纹快速生效
    _SPEAKER_CACHE_TTL = 5.0

    def _load_all_embeddings(self) -> List[Dict]:
        """从数据库加载所有家庭成员的声纹特征（带 5 秒缓存）"""
        now = time.monotonic()
        cache = getattr(self, "_speaker_cache", None)
        if cache is not None and (now - cache[0]) < self._SPEAKER_CACHE_TTL:
            return cache[1]
        speakers = self._read_embeddings_from_db()
        self._speaker_cache = (now, speakers)
        return speakers

    def _read_embeddings_from_db(self) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT name, voice_print_vector FROM family_members WHERE voice_print_vector IS NOT NULL")
                rows = cursor.fetchall()
            finally:
                conn.close()

            speakers = []
            for name, vec_str in rows:
                try:
                    vec = np.array(json.loads(vec_str))
                    speakers.append({"name": name, "vector": vec})
                except Exception:
                    continue
            return speakers
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载声纹数据库失败: {e}")
            return []

    # ---------- 判定逻辑（纯函数式，便于单测） ----------

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _decide(self, scores: List[tuple], now: float) -> str:
        """根据全部成员得分给出说话人。

        scores: [(name, score), ...] 已按分数降序。
        规则：
          1. 最高分低于阈值 → 未知说话人
          2. 最高分领先第二名 ≥ margin → 直接取最高分
          3. 领先不足（声音相近的两个人打平）→ 先看「刚才这轮对话是谁」
             （连续性），再看「最近确认过的说话人里谁出现得多」（先验），
             都没有依据就返回「未知说话人」——宁可不称呼，也不叫错人
        """
        top_name, top_score = scores[0]
        if top_score < self.similarity_threshold:
            return "未知说话人"
        second_score = scores[1][1] if len(scores) > 1 else -1.0
        margin = self._cfg_float("margin", 0.08)
        if top_score - second_score >= margin:
            self._remember_confident(top_name, now)
            return top_name

        tied = [n for n, s in scores
                if top_score - s < margin and s >= self.similarity_threshold]
        last = self._last_confident
        if last and (now - last[1]) <= self._cfg_float("continuity_seconds", 20.0) and last[0] in tied:
            return last[0]

        counts = {}
        for n in self._recent_confident:
            if n in tied:
                counts[n] = counts.get(n, 0) + 1
        if counts:
            ordered = sorted(counts.items(), key=lambda kv: -kv[1])
            best_n, best_c = ordered[0]
            next_c = ordered[1][1] if len(ordered) > 1 else 0
            if best_c >= 5 and best_c >= 2 * next_c:
                return best_n
        return "未知说话人"

    def _remember_confident(self, name: str, now: float) -> None:
        self._last_confident = (name, now)
        self._recent_confident.append(name)

    def _write_trace(self, embedding, scores, result, duration) -> None:
        """把本次全部得分（含向量）落 jsonl，供离线分析/调参。保留最近 3000 行。"""
        if not self._config.get("trace", True):
            return
        try:
            path = os.path.join(os.path.dirname(self.db_path), "voiceprint_trace.jsonl")
            rec = {
                "ts": round(time.time(), 2),
                "dur": round(duration, 2),
                "result": result,
                "scores": {n: round(float(s), 4) for n, s in scores},
                "emb": [round(float(x), 4) for x in embedding],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._trace_count = getattr(self, "_trace_count", 0) + 1
            if self._trace_count % 200 == 0:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > 3000:
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(lines[-3000:])
        except Exception as e:
            logger.bind(tag=TAG).warning(f"声纹 trace 写入失败: {e!r}")

    async def identify_speaker(self, audio_data: bytes, session_id: str = None) -> Optional[str]:
        """
        识别说话人 (异步)
        """
        if not self.enabled:
            return None

        # 1. 提取特征 (在后台线程执行以防阻塞事件循环)
        import asyncio
        embedding = await asyncio.get_event_loop().run_in_executor(
            None, self._extract_embedding, audio_data
        )
        if embedding is None:
            return None

        # 2. 加载数据库中的声纹
        speakers = await asyncio.get_event_loop().run_in_executor(
            None, self._load_all_embeddings
        )
        if not speakers:
            return None

        # 3. 与每位成员比对余弦相似度（向量已归一化，直接点积）
        scores = sorted(
            ((sp["name"], float(np.dot(embedding, sp["vector"]))) for sp in speakers),
            key=lambda kv: -kv[1],
        )
        best_name, best_score = scores[0]
        result = self._decide(scores, time.time())

        all_txt = " ".join(f"{n}={s:.3f}" for n, s in scores)
        logger.bind(tag=TAG).info(
            f"声纹识别结果: {best_name} (相似度: {best_score:.3f}) | 判定: {result} | 全部: {all_txt}"
        )
        # 记录最近一次最高相似度，供音乐声纹闸门/全线闸门使用（它们用独立的低阈值）
        self.last_score = best_score
        self.last_scores = scores
        self.last_result = result
        self._write_trace(embedding, scores, result, len(audio_data) / 32000.0)
        return result
