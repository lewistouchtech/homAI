# -*- coding: utf-8 -*-
"""
声纹录制质量打分 API（2026-10-11 新增）

后台「家庭成员 → 🎙️ 录制声纹」标准化流程的服务端校验：
- 前端对每一遍录音做即时检查（时长/音量/有效语音占比），不合格不进下一步
- 两遍都录完后，前端把两段 16kHz/16bit/mono WAV（base64）POST 到本接口
- 本接口做三件事：
  1. CAM++ 能否成功提出 192 维特征（提不出 = 音频格式有问题）
  2. 两遍的余弦相似度 ≥ 0.55 才算合格（主人裁定：强制录两遍，两遍够像才算合格）
  3. 与声纹库中「其他已有成员」的最高相似度必须显著低于本人两遍的相似度
     （防止录进电视声 / 别人的声音）

打分规则（百分制，合格与否由硬性门槛决定，总分反映质量）：
- 每遍质量 20 分 × 2：时长 8 + 音量 6 + 有效语音占比 6
- 两遍一致性 35 分
- 与其他成员的区分度 25 分

硬性门槛（任何一条不过 = 不合格，前端拦截不允许保存）：
- 时长 3~10 秒（5~8 秒最佳，正落在 CAM++ 建议的 3-10 秒区间）
- 音量 RMS ≥ 0.005（更低 = 声音太小）；削波采样占比 ≤ 1% 且 RMS ≤ 0.60（超了 = 破音）
- 有效语音占比 ≥ 60%（掐掉首尾静音后，有效段内有声帧的比例）
- 两遍 CAM++ 余弦相似度 ≥ 0.55
- 其他人最高分 + 0.10 < 两遍相似度（严格大于）

⚠️ 实现注意（项目已踩过的坑）：
- CAM++ 模型加载/推理是同步阻塞的，必须放 run_in_executor 线程里跑，
  不能在 aiohttp handler 里直接调用（会卡死事件循环）
- VoiceprintProvider 有类级模型缓存，服务主进程已加载模型时这里复用是零成本的
- 本模块只调 _extract_embedding（纯特征提取），不调 identify_speaker，
  不会污染识别器的判定状态（_last_confident 等）
"""
import asyncio
import base64
import binascii
import io
import json
import sqlite3
import wave
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from aiohttp import web

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# ==================== 打分阈值（与前端 vpCheckTake 保持同一口径） ====================
# [2026-10-12 实测修正] 原上限 10 秒是照搬 voiceprint_provider 里「建议 3-10 秒」
# 的日志文案拍的，没有依据。主人实测念标准文字自然语速要 12~14 秒却被判「太长」，
# 逼用户加快语速——而语速对声纹识别没有任何帮助（CAM++ 提的是声道/共振峰等生理
# 特征做统计池化，帧越多越稳）。
# 实测（tmp/probe_duration_effect.py，同一音色切 3/6/10/14/20/55.95 秒，
# 各片段与其他片段的平均余弦相似度）：
#   3.0s=0.933  6.0s=0.947  10.0s=0.967  14.0s=0.963  20.0s=0.961  55.95s=0.928
# 10/14/20 秒互相相似度 0.985~0.995，3 秒才是最不稳的那个。
# 故：下限保留 3 秒（勉强及格线），上限放宽到 30 秒，满分区间 8~20 秒。
DURATION_MIN = 3.0          # 时长硬下限（秒）
DURATION_MAX = 30.0         # 时长硬上限（秒）
DURATION_BEST_MIN = 8.0     # 最佳时长下限（满分区间）
DURATION_BEST_MAX = 20.0    # 最佳时长上限（满分区间）
RMS_MIN = 0.005             # 音量硬下限（低于 = 声音太小）
RMS_MAX = 0.60              # 音量硬上限（高于 = 破音）
CLIP_SAMPLE = 0.985         # |采样| ≥ 该值视为削波（约 32300/32768）
CLIP_RATIO_MAX = 0.01       # 削波采样占比硬上限（超了 = 破音）
# [2026-10-12 主人实测推翻] 原 SPEECH_RATIO_MIN = 0.60 是错的，已降到 0.15。
# 主人录两遍都被判「空白太多（有效说话只占 58%）」，他反问：
#   「中间所谓的空白是断句啊，正常说话不可能没有断句的。你想让大家一口气把这个说完，这不可能。」
# 实测（tmp/probe_speech_ratio.py）：用真实 Qwen3-TTS 合成**标准文字本身**（含 3 个句号的
# 自然停顿，15.60s），有效语音占比只有 **44.2%**；把相对阈值从 15% 一路降到 2%，
# 也只从 44.2% 升到 50.6% —— 说明占比低不是阈值问题，而是中文语音的固有特性
# （音节间天然间隙、清辅音 s/sh/x/f 能量低、句间停顿）。
# 主人真人录的 58% 已经比 TTS 还高（念得很连贯），却被 60% 门槛拦掉。
# 结论：占比只能当「极端空白保护」，真正该管的是**有效说话的绝对时长**。
SPEECH_RATIO_MIN = 0.15     # 有效语音占比硬下限（只防「几乎全是空白」）
VOICED_SECONDS_MIN = 2.5    # 有效说话绝对时长下限（防「录 30 秒只说两个字」）
SPEECH_RATIO_FULL = 0.80    # 有效语音占比满分线
PAIR_SIM_MIN = 0.55         # 两遍余弦相似度硬下限
PAIR_SIM_FULL = 0.70        # 两遍一致性满分线
OTHER_MARGIN_MIN = 0.10     # 区分度硬门槛：两遍相似度 - 其他人最高分 必须严格大于该值
OTHER_MARGIN_FULL = 0.30    # 区分度满分线
FRAME_SECONDS = 0.02        # 有声帧判定粒度：20ms
VOICED_REL_THRESHOLD = 0.15  # 有声帧相对阈值：帧 RMS ≥ 最大帧 RMS 的 15%
VOICED_ABS_THRESHOLD = 0.004  # 有声帧绝对底噪门槛


# ==================== 纯函数（无副作用，便于单测） ====================

def _speech_ratio(data: np.ndarray, sample_rate: int) -> tuple:
    """返回 (有效语音占比, 有效说话秒数)。

    20ms 一帧算帧 RMS，帧 RMS ≥ max(0.004, 最大帧RMS×0.15) 视为有声帧。
    占比 = 掐掉首尾静音后，有效段内「有声帧」的比例。

    ⚠️ [2026-10-12] 占比**不能**当质量主判据：中文语音天然含音节间隙、
    清辅音低能量段和句间停顿，实测连 TTS 合成的标准文字也只有 44.2%。
    真正的有效性判据是 `voiced_seconds`（有声帧折算的绝对秒数）。
    """
    frame_len = max(1, int(sample_rate * FRAME_SECONDS))
    n_frames = len(data) // frame_len
    if n_frames == 0:
        return 0.0, 0.0
    frames = data[:n_frames * frame_len].reshape(n_frames, frame_len)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    thr = max(VOICED_ABS_THRESHOLD, float(frame_rms.max()) * VOICED_REL_THRESHOLD)
    voiced = frame_rms >= thr
    idx = np.where(voiced)[0]
    if len(idx) == 0:
        return 0.0, 0.0
    first, last = int(idx[0]), int(idx[-1])
    ratio = float(voiced[first:last + 1].sum()) / float(last - first + 1)
    voiced_seconds = int(voiced.sum()) * FRAME_SECONDS
    return ratio, voiced_seconds


def analyze_wav(audio_bytes: bytes) -> Dict:
    """解析 WAV 并计算质量指标（时长/RMS/削波占比/有效语音占比）。

    解析失败抛 ValueError（中文原因，直接透给前端展示）。
    """
    try:
        with wave.open(io.BytesIO(audio_bytes), 'rb') as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            pcm = wf.readframes(n_frames)
    except Exception as e:
        raise ValueError(f"音频不是有效的 WAV 文件（{e}）")

    if sample_rate != 16000:
        raise ValueError(f"音频采样率必须是 16000Hz，实际是 {sample_rate}Hz")
    if sample_width != 2:
        raise ValueError(f"音频位深必须是 16bit，实际是 {sample_width * 8}bit")
    if n_frames == 0:
        raise ValueError("音频是空的（没有采到任何声音）")

    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        # 多声道混合为单声道（前端理论上只发 mono，这里是兜底）
        data = data[: (len(data) // channels) * channels].reshape(-1, channels).mean(axis=1)

    duration = len(data) / float(sample_rate)
    rms = float(np.sqrt(np.mean(data ** 2)))
    clip_ratio = float(np.mean(np.abs(data) >= CLIP_SAMPLE))
    speech_ratio, voiced_seconds = _speech_ratio(data, sample_rate)
    return {
        "duration": duration,
        "rms": rms,
        "clip_ratio": clip_ratio,
        "speech_ratio": speech_ratio,
        "voiced_seconds": voiced_seconds,
    }


def check_take_quality(metrics: Dict) -> Dict:
    """单遍录音质量打分（满分 20：时长 8 + 音量 6 + 有效语音占比 6）。

    返回 {passed, score, duration, rms, clip_ratio, speech_ratio, voiced_seconds, reasons}。
    reasons 里的文案是说给非程序员主人听的，必须说人话。
    """
    reasons: List[str] = []
    passed = True

    # --- 时长（8 分）---
    dur = float(metrics["duration"])
    if dur < DURATION_MIN:
        passed = False
        dur_pts = 0.0
        reasons.append(f"录音太短了（只有 {dur:.1f} 秒），请把文字完整念完，至少 {DURATION_MIN:.0f} 秒")
    elif dur > DURATION_MAX:
        passed = False
        dur_pts = 0.0
        reasons.append(f"录音太长了（{dur:.1f} 秒），照着文字念一遍就好，最多 {DURATION_MAX:.0f} 秒")
    elif DURATION_BEST_MIN <= dur <= DURATION_BEST_MAX:
        dur_pts = 8.0
    else:
        dur_pts = 6.0

    # --- 音量（6 分）---
    rms = float(metrics["rms"])
    clip = float(metrics["clip_ratio"])
    if rms < RMS_MIN:
        passed = False
        vol_pts = 0.0
        reasons.append("声音太小，请靠近麦克风再录一遍")
    elif clip > CLIP_RATIO_MAX or rms > RMS_MAX:
        passed = False
        vol_pts = 0.0
        reasons.append("声音太大破音了，请离麦克风远一点再录一遍")
    elif rms >= 0.01 and clip == 0.0:
        vol_pts = 6.0
    else:
        vol_pts = 3.0

    # --- 有效说话时长（6 分）---
    # [2026-10-12 主人实测推翻] 原判据是「有效语音占比 ≥60%」，主人两遍都被拦（58%）。
    # 实测：TTS 合成标准文字（韵律标准、含 3 个句号的自然停顿）占比只有 44.2%，
    # 真人 58% 已优于它；把相对阈值从 15% 一路降到 2% 也只升到 50.6% ——
    # 占比低是中文语音固有特性（音节间天然间隙、清辅音 s/sh/x/f 能量低、句间停顿），
    # 不是录音质量问题。**占比不能当主判据。**
    # 改用「实际说话秒数」：这才能防「录 30 秒只说两个字」，又不误杀正常断句。
    vs = float(metrics.get("voiced_seconds", 0) or 0)
    sr = float(metrics.get("speech_ratio", 0) or 0)
    if vs < VOICED_SECONDS_MIN:
        passed = False
        sr_pts = 0.0
        reasons.append(f"实际说话声只有 {vs:.1f} 秒，太少了，请照着文字完整念一遍")
    elif sr < SPEECH_RATIO_MIN:
        # 极端空白保护：几乎没录到声音（TTS 44%、真人 58%，15% 完全安全）
        passed = False
        sr_pts = 0.0
        reasons.append("几乎没有录到说话声，请检查一下麦克风再录一遍")
    elif vs >= 6.0:
        sr_pts = 6.0
    elif vs >= 4.0:
        sr_pts = 4.0
    else:
        sr_pts = 2.0

    return {
        "passed": passed,
        "score": round(dur_pts + vol_pts + sr_pts, 1),
        "duration": round(dur, 2),
        "rms": round(rms, 4),
        "clip_ratio": round(clip, 4),
        "speech_ratio": round(sr, 3),
        "voiced_seconds": round(vs, 2),
        "reasons": reasons,
    }


def check_pair(pair_sim: Optional[float],
               other_max: Optional[float],
               other_name: Optional[str] = None) -> Dict:
    """两遍一致性（35 分）+ 与其他成员区分度（25 分）。

    pair_sim=None 表示 CAM++ 提不出特征（音频格式问题）→ 直接不合格。
    other_max=None 表示库里没有其他成员 → 区分度免检给满分。
    """
    if pair_sim is None:
        return {
            "passed": False,
            "sim_score": 0.0, "other_score": 0.0,
            "pair_similarity": None, "pair_ok": False,
            "other_max": None, "other_name": None, "other_ok": False,
            "reasons": ["声纹模型没能从音频里提取出特征（音频格式有问题），请重新录制"],
        }

    reasons: List[str] = []
    passed = True

    # --- 两遍一致性（35 分）---
    if pair_sim >= PAIR_SIM_MIN:
        pair_ok = True
        if pair_sim >= PAIR_SIM_FULL:
            sim_pts = 35.0
        else:
            sim_pts = 22.0 + (pair_sim - PAIR_SIM_MIN) / (PAIR_SIM_FULL - PAIR_SIM_MIN) * 13.0
    else:
        pair_ok = False
        passed = False
        sim_pts = max(0.0, (pair_sim - 0.15) / (PAIR_SIM_MIN - 0.15)) * 22.0
        reasons.append(
            f"两遍录音听起来不够像同一个人（相似度 {pair_sim:.2f}，要求 ≥ {PAIR_SIM_MIN}），"
            f"请在安静环境里把两遍都重录一次"
        )

    # --- 与其他成员区分度（25 分）---
    if other_max is None:
        other_ok = True
        other_pts = 25.0
    else:
        margin = pair_sim - other_max
        if margin > OTHER_MARGIN_MIN:
            other_ok = True
            if margin >= OTHER_MARGIN_FULL:
                other_pts = 25.0
            else:
                other_pts = 15.0 + (margin - OTHER_MARGIN_MIN) / (OTHER_MARGIN_FULL - OTHER_MARGIN_MIN) * 10.0
        else:
            other_ok = False
            passed = False
            other_pts = max(0.0, margin / OTHER_MARGIN_MIN) * 15.0 if margin > 0 else 0.0
            reasons.append(
                f"这段声音和声纹库里的其他家人太像了（{other_name or '其他成员'} 相似度 {other_max:.2f}，"
                f"与两遍相似度的差距不足 {OTHER_MARGIN_MIN}），"
                f"可能录进了别人的声音或电视声，请在安静环境重录"
            )

    return {
        "passed": passed,
        "sim_score": round(sim_pts, 1),
        "other_score": round(other_pts, 1),
        "pair_similarity": round(float(pair_sim), 4),
        "pair_ok": pair_ok,
        "other_max": None if other_max is None else round(float(other_max), 4),
        "other_name": other_name,
        "other_ok": other_ok,
        "reasons": reasons,
    }


def compose_result(take1: Dict, take2: Dict, pair: Dict) -> Dict:
    """组装最终返回：百分制总分 + 各项明细 + 是否合格 + 不合格原因。"""
    qualified = bool(take1["passed"] and take2["passed"] and pair["passed"])
    total = float(take1["score"]) + float(take2["score"]) + float(pair["sim_score"]) + float(pair["other_score"])

    def _take_detail(t: Dict) -> Dict:
        return {
            "passed": t["passed"],
            "score": t["score"],
            "duration": t.get("duration", 0) or 0,
            "rms": t.get("rms", 0) or 0,
            "clip_ratio": t.get("clip_ratio", 0) or 0,
            "speech_ratio": t.get("speech_ratio", 0) or 0,
            "voiced_seconds": t.get("voiced_seconds", 0) or 0,
        }

    other_member = None
    if pair.get("other_max") is not None:
        other_member = {"name": pair.get("other_name") or "其他成员", "score": pair["other_max"]}

    return {
        "success": True,
        "qualified": qualified,
        "total_score": int(round(total)),
        "reasons": list(take1["reasons"]) + list(take2["reasons"]) + list(pair["reasons"]),
        "details": {
            "take1": _take_detail(take1),
            "take2": _take_detail(take2),
            "pair_similarity": pair["pair_similarity"],
            "pair_ok": pair["pair_ok"],
            "other_member": other_member,
            "other_ok": pair["other_ok"],
            # 库里没有其他成员时区分度免检（前端展示「跳过」而不是渲染 undefined）
            "other_skipped": pair.get("other_max") is None and pair["pair_similarity"] is not None,
        },
    }


def load_other_embeddings(db_path, exclude_member_id: Optional[int] = None) -> List[Dict]:
    """加载声纹库中其他成员的归一化向量。

    exclude_member_id：重录某成员的声纹时，把他自己的旧向量排除掉
    （否则「和自己旧声纹比」会干扰区分度判定）。
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            if exclude_member_id is not None:
                cur.execute(
                    "SELECT name, voice_print_vector FROM family_members "
                    "WHERE voice_print_vector IS NOT NULL AND id != ?",
                    (exclude_member_id,),
                )
            else:
                cur.execute(
                    "SELECT name, voice_print_vector FROM family_members "
                    "WHERE voice_print_vector IS NOT NULL"
                )
            rows = cur.fetchall()
        finally:
            conn.close()

        out = []
        for name, vec_str in rows:
            try:
                vec = np.array(json.loads(vec_str), dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec = vec / norm
                out.append({"name": name, "vector": vec})
            except Exception:
                continue
        return out
    except Exception as e:
        logger.bind(tag=TAG).warning(f"声纹打分：加载声纹库失败: {e}")
        return []


def _decode_audio_field(value) -> Optional[bytes]:
    """解 base64 音频字段：接受 data URL（data:audio/wav;base64,xxx）或裸 base64。"""
    if not value or not isinstance(value, str):
        return None
    b64 = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        return base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return None


# ==================== 同步核心（在线程池里跑，不阻塞事件循环） ====================

def evaluate_sync(wav1: bytes, wav2: bytes,
                  member_id: Optional[int] = None,
                  db_path=None) -> Dict:
    """两遍录音完整打分（同步版，供 handler 放线程池调用，也供单测直接调用）。

    流程：两遍各自解析+质量打分 → 都解析成功才跑 CAM++ → 两遍相似度 + 区分度。
    """
    if db_path is None:
        db_path = DB_PATH

    takes = []
    for wav in (wav1, wav2):
        try:
            metrics = analyze_wav(wav)
            takes.append(check_take_quality(metrics))
        except ValueError as e:
            takes.append({
                "passed": False, "score": 0.0,
                "duration": None, "rms": None, "clip_ratio": None, "speech_ratio": None,
                "voiced_seconds": None,
                "reasons": [str(e)],
            })

    both_parsed = all(t.get("duration") is not None for t in takes)

    if not both_parsed:
        # WAV 都解析不了，跑模型也没意义；不合格原因由 take 的 reasons 说明
        pair = check_pair(None, None)
        pair["reasons"] = []  # 避免和「不是有效 WAV」重复报「提取不出特征」
        return compose_result(takes[0], takes[1], pair)

    # CAM++ 特征提取（局部导入，避免 http_server 启动时拉 torch）
    from core.utils.voiceprint_provider import VoiceprintProvider
    vp = VoiceprintProvider({})
    if not vp.enabled:
        pair = check_pair(None, None)
        pair["reasons"] = ["声纹模型当前不可用（模型未加载或依赖缺失），请稍后重试或检查服务器"]
        return compose_result(takes[0], takes[1], pair)

    emb1 = vp._extract_embedding(wav1)
    emb2 = vp._extract_embedding(wav2)
    if emb1 is None or emb2 is None:
        return compose_result(takes[0], takes[1], check_pair(None, None))

    # 向量已归一化，余弦相似度 = 点积
    pair_sim = float(np.dot(emb1, emb2))

    # 与其他成员比对：用两遍的平均向量（更稳），归一化后与各成员点积
    others = load_other_embeddings(db_path, member_id)
    other_max = None
    other_name = None
    if others:
        mean_emb = (np.asarray(emb1, dtype=np.float32) + np.asarray(emb2, dtype=np.float32)) / 2.0
        norm = float(np.linalg.norm(mean_emb))
        if norm > 0:
            mean_emb = mean_emb / norm
        best_name, best_score = max(
            ((sp["name"], float(np.dot(mean_emb, sp["vector"]))) for sp in others),
            key=lambda kv: kv[1],
        )
        other_max, other_name = best_score, best_name

    result = compose_result(takes[0], takes[1], check_pair(pair_sim, other_max, other_name))
    logger.bind(tag=TAG).info(
        f"[声纹打分] 总分={result['total_score']} 合格={result['qualified']} "
        f"两遍相似度={pair_sim:.3f} 其他人最高={other_name}:{other_max:.3f}"
        if other_max is not None else
        f"[声纹打分] 总分={result['total_score']} 合格={result['qualified']} "
        f"两遍相似度={pair_sim:.3f} 库中无其他成员"
    )
    return result


# ==================== aiohttp handler ====================

async def handle_voiceprint_evaluate(request: web.Request) -> web.Response:
    """POST /api/voiceprint/evaluate

    请求体：{"wav1_b64": "...", "wav2_b64": "...", "member_id": 可选}
      - wav1_b64 / wav2_b64：两遍录音的 base64（支持 data:audio/wav;base64, 前缀）
      - member_id：重录已有成员时传，区分度检查会排除他自己的旧声纹
    返回：{success, qualified, total_score, reasons[], details{...}}
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "请求体必须是合法 JSON"}, status=400)

    wav1 = _decode_audio_field(data.get("wav1_b64"))
    wav2 = _decode_audio_field(data.get("wav2_b64"))
    if not wav1 or not wav2:
        return web.json_response(
            {"success": False, "message": "必须提供两遍录音（wav1_b64 / wav2_b64）"},
            status=400,
        )

    member_id = data.get("member_id")
    try:
        member_id = int(member_id) if member_id is not None else None
    except (TypeError, ValueError):
        member_id = None

    try:
        # ⚠️ CAM++ 加载/推理是同步阻塞的，必须放线程池，别在事件循环里直接跑
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, evaluate_sync, wav1, wav2, member_id)
    except Exception as e:
        logger.bind(tag=TAG).exception(f"[声纹打分] 接口异常: {e}")
        return web.json_response({"success": False, "message": f"打分失败：{e}"}, status=500)

    return web.json_response(result)


def create_voiceprint_eval_routes():
    """路由工厂（与 face_api / plate_api 同款模式，http_server 里 * 展开注册）"""
    return [
        web.post("/api/voiceprint/evaluate", handle_voiceprint_evaluate),
    ]
