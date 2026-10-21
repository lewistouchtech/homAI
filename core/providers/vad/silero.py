import time
import os
import numpy as np
import opuslib_next  # macOS 兼容性跳过
import onnxruntime
from config.logger import setup_logging
from core.providers.vad.base import VADProviderBase

TAG = __name__
logger = setup_logging()


class VADProvider(VADProviderBase):
    def __init__(self, config):
        logger.bind(tag=TAG).info("SileroVAD", config)

        # 获取项目根目录
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        
        # 使用绝对路径
        model_dir = config.get("model_dir", "models/snakers4_silero-vad")
        if not os.path.isabs(model_dir):
            model_dir = os.path.join(project_root, model_dir)
        
        model_path = os.path.join(
            model_dir, "src", "silero_vad", "data", "silero_vad.onnx"
        )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )

        threshold = config.get("threshold", "0.5")
        threshold_low = config.get("threshold_low", "0.2")
        min_silence_duration_ms = config.get("min_silence_duration_ms", "1000")

        # 解析阈值并确保在有效范围内 [0.0, 1.0]
        threshold_val = float(threshold) if threshold else 0.5
        threshold_low_val = float(threshold_low) if threshold_low else 0.2
        min_silence_ms = int(min_silence_duration_ms) if min_silence_duration_ms else 1000

        self.vad_threshold = max(0.0, min(1.0, threshold_val))
        self.vad_threshold_low = max(0.0, min(1.0, threshold_low_val))

        # 防止阈值过低导致所有帧都被识别为语音
        # 允许更低的阈值（流式ASR需要更敏感的VAD）
        if self.vad_threshold < 0.1:
            logger.bind(tag=TAG).warning(f"VAD threshold {self.vad_threshold} 过低，自动修正为 0.1")
            self.vad_threshold = 0.1
        if self.vad_threshold_low < 0.05:
            logger.bind(tag=TAG).warning(f"VAD threshold_low {self.vad_threshold_low} 过低，自动修正为 0.4")
            self.vad_threshold_low = 0.4

        # 确保高低阈值差值 >= 0.1（滞后带）
        if self.vad_threshold - self.vad_threshold_low < 0.1:
            self.vad_threshold_low = max(0.0, self.vad_threshold - 0.1)

        # 静音阈值范围限制 [200ms, 3000ms]
        # 2026-09-17 对齐上游：下界原为 300ms（本地自加），上游 config.yaml 用 200ms。
        # 本地 300 的钳制会把配置里的 200 抬回 300，导致判停比上游迟钝、且
        # 用户说话中间的换气停顿更容易被判成"说完了"→ 一句话被切成几段分别回复。
        self.silence_threshold_ms = max(200, min(3000, min_silence_ms))

        # 2026-09-23 P0-2 两段式判停（V2 实施基线 §1）：
        # 静音达到 candidate_silence_ms 仅视为「候选结束」（换气/思考停顿不截断），
        # 继续静默 confirmation_window_ms 后才确认说完（client_voice_stop=True）。
        # 期间有声音自动回退，音频持续累积不丢。无新配置键时回退旧单阈值逻辑。
        candidate_ms = config.get("candidate_silence_ms")
        confirm_ms = config.get("confirmation_window_ms")
        if candidate_ms and confirm_ms:
            self.candidate_silence_ms = max(50, min(2000, int(candidate_ms)))
            self.confirmation_window_ms = max(50, min(2000, int(confirm_ms)))
            self.two_stage_endpoint = True
            logger.bind(tag=TAG).info(
                f"两段式判停已启用: 候选={self.candidate_silence_ms}ms + 确认={self.confirmation_window_ms}ms，"
                f"总判停等待={self.candidate_silence_ms + self.confirmation_window_ms}ms"
            )
        else:
            self.candidate_silence_ms = None
            self.confirmation_window_ms = None
            self.two_stage_endpoint = False

        # 2026-10-05 A1：并行预判（speculative probe）。
        # 原两段式的第二段（confirmation_window_ms）是「盲等」——候选结束后什么都不做，
        # 纯粹干等确认窗口到期才提交。而 JEV 干的正是「这句说完没」的判断，两者职责重叠。
        # 改法：候选结束那一刻就并行启动一次预判（预览 ASR + JEV），把结果缓存在 conn 上；
        #       后续每一帧 VAD 都不花代价地读缓存：
        #         · JEV 说完了        → 立刻提交（省掉整个确认窗口）
        #         · JEV 说没说完      → 取消候选标记继续收音（等同 hold，但零额外等待）
        #         · 预判还没回来      → 走原两段式逻辑（超时兜底，防预判卡死拖住判停）
        # 净收益：省 confirmation_window_ms（当前 500ms）；代价是一次可能白跑的预览 ASR。
        # 安全阀：
        #   · endpoint_probe_speculative: false → 完全退回原两段式行为（一行配置回滚）
        #   · 预判异常一律吞掉并退回两段式，绝不因预判失败导致判停失效
        self.endpoint_probe_speculative = bool(
            config.get("endpoint_probe_speculative", True)
        )
        if self.two_stage_endpoint and self.endpoint_probe_speculative:
            logger.bind(tag=TAG).info(
                "并行预判已启用: 候选结束即预判，说完立即提交（省去确认窗口盲等）"
            )

        # 2026-09-18 移除 max_asr_duration_ms：本地自加的「最大 ASR 持续时间兜底」
        # 已删除（上游无此机制），相关配置项一并移除。

        # 2026-09-23 近场爆发切分（YouTube 混合段修复）：
        # 远场背景声（视频/电视）持续占据 VAD 时，近场主人插话能量远高于段内基线，
        # 持续 near_burst_frames 帧超基线×near_burst_ratio → 立即切分当前段单独提交，
        # 让声纹闸门只丢弃视频段、放行主人段。near_burst_ratio=0 禁用。
        self.near_burst_ratio = float(config.get("near_burst_ratio", 3.0) or 0)
        self.near_burst_frames = int(config.get("near_burst_frames", 5) or 0)
        self.near_min_rms = float(config.get("near_min_rms", 1500) or 0)
        # 段首保护（2026-09-23，主人报"整体效果不对"实证修复）：
        # 段建立初期的 EMA 基线≈第一个字的能量（轻声/气音时极低），此时启用
        # 切分会把正常嗓音误判成"近场爆发"→ 正常说话被拦腰切断、识别率暴跌。
        # 段首 near_burst_warmup_frames 帧（25帧≈1.5s）内只攒基线、绝不切分。
        self.near_burst_warmup_frames = int(config.get("near_burst_warmup_frames", 25) or 0)

        self.frame_window_threshold = 3
        self.max_window_size = 10  # 限制滑动窗口最大长度

        # 2026-10-10 确认窗口打断免疫（主人实测「整句被吞」修复）：
        # 候选结束后确认窗口(confirmation_window_ms)内，任何 1 帧 have_voice
        # （环境噪音/电流声造成的假人声毛刺）都会清除候选标记 → 静音重新累计
        # → 再候选 → 再被打断，死循环永不提交（2026-10-10 17:07 实测：人声开始/
        # 候选结束交替出现 65 秒、无一次提交，主人那句话被整个吞掉）。
        # 修复：确认窗口内的人声先累计，未超 confirm_interrupt_ms 视为噪音毛刺
        # ——不清候选、不重置静音计时，确认窗口照常走完提交；持续说话超阈值
        # 才认定为「真的还在说」，打断确认继续收音。0 = 关闭免疫（回滚旧行为）。
        self.confirm_interrupt_ms = max(0, min(1000, int(config.get("confirm_interrupt_ms", 300) or 0)))
        # 累计免疫时长上限：确认窗口内被免疫的人声累计超该时长 → 判定为
        # 「持续真人说话」→ 真打断继续收音。噪音/回声是断断续续的，累计不
        # 到这么久；真人说话（哪怕平缓低音量）持续超 3 秒必打断。
        # 不设能量门槛（2026-10-10 主人裁决删除）：实测主人平缓说话 RMS
        # 1100~1400，拍脑袋的绝对能量值不可靠，只会误杀。
        self.confirm_immune_max_ms = max(
            self.confirm_interrupt_ms,
            min(10000, int(config.get("confirm_immune_max_ms", 3000) or 0)),
        )

        # 【对齐小智原版 2026-09-10】删除"300ms 持续+音量"双条件（6e616d4/ba0d51b 遗留）：
        # 原版窗口 3 帧多数表决即判 have_voice，额外持续条件使语音开始判定多延迟 300ms+，
        # 拖慢整体回复。主人要求对齐小智"说完快速判停回一句"的节奏。

    def _init_connection_state(self, conn):
        """为连接初始化独立的 VAD 状态"""
        if not hasattr(conn, "_vad_opus_decoder"):
            conn._vad_opus_decoder = opuslib_next.Decoder(16000, 1)
        if not hasattr(conn, "_vad_state"):
            conn._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        if not hasattr(conn, "_vad_context"):
            conn._vad_context = np.zeros((1, 64), dtype=np.float32)
        # 确保 client_audio_buffer 存在
        if not hasattr(conn, "client_audio_buffer"):
            conn.client_audio_buffer = bytearray()
        # 确保 last_is_voice 存在
        if not hasattr(conn, "last_is_voice"):
            conn.last_is_voice = False
        # 确保 client_voice_window 存在
        if not hasattr(conn, "client_voice_window"):
            conn.client_voice_window = []
        # 只在首次初始化时清除状态，避免每次 is_vad 调用都重置
        if not hasattr(conn, '_vad_initialized'):
            conn.client_have_voice = False
            conn.client_voice_stop = False
            conn.last_activity_time = 0
            conn._vad_candidate_end = False
            # A1 并行预判缓存：pending=预判在跑 / verdict=预判结论 / text=预判时识别的文本
            conn._vad_probe_pending = False
            conn._vad_probe_verdict = None
            conn._vad_probe_text = None
            conn._vad_probe_audio_sig = None
            conn._vad_voice_seq = 0  # 累计人声帧数：预判结论是否仍可复用的校验依据（静音帧不动它）
            conn._vad_probe_voice_seq = None
            conn._vad_probe_decision = None
            conn._vad_probe_gen = 0  # 预判代际：每次发起/作废 +1，旧协程据此丢弃过期结论
            conn._vad_hold_until = 0.0  # 语义 hold 截止时刻（time.time()），期间不做判停
            conn._vad_hold_expired = False
            # 近场爆发切分启用标志（receiveAudioHandle 打断判定读取：启用时仅爆发打断）
            conn._vad_near_burst_enabled = self.near_burst_ratio > 0 and self.near_burst_frames > 0
            conn._vad_seg_frames = 0  # 当前语音段帧计数（段首保护用）
            if hasattr(conn, '_last_voice_time'):
                delattr(conn, '_last_voice_time')
            if hasattr(conn, '_asr_voice_start_time'):
                delattr(conn, '_asr_voice_start_time')
            conn._vad_initialized = True

    def _confirm_window_immune(self, conn, frame_ms):
        """确认窗口打断免疫。返回 True = 本帧人声应被免疫（不打断确认窗口）。

        机制：候选结束后（_vad_candidate_end=True）的确认窗口内，have_voice
        帧累计进 _vad_confirm_voice_frames 并免疫：不清候选、冻结静音计时
        （调用方刷新 _last_voice_time），噪音/回声毛刺不打断确认；
        累计超 confirm_immune_max_ms（默认 3s）→ 判定持续真人说话（平缓低
        音量讲述也算）→ 真打断继续收音。
        """
        if not getattr(conn, "_vad_candidate_end", False) or self.confirm_interrupt_ms <= 0:
            return False
        total = getattr(conn, "_vad_confirm_voice_frames", 0) + frame_ms
        conn._vad_confirm_voice_frames = total
        # 累计免疫超时 → 持续真人说话，真打断（计数器清零，走原打断逻辑）
        if total >= self.confirm_immune_max_ms:
            conn._vad_confirm_voice_frames = 0
            logger.bind(tag=TAG).info(
                f"[VAD] 确认窗口持续人声累计{total:.0f}ms(≥{self.confirm_immune_max_ms}ms)，判定真人说话，打断确认继续收音"
            )
            return False
        logger.bind(tag=TAG).info(
            f"[VAD] 确认窗口噪音免疫: 人声毛刺累计={total:.0f}ms < {self.confirm_immune_max_ms}ms，不打断确认"
        )
        return True

    def _endpoint_decision_for_silence(self, conn, silence_ms):
        """两段式 + 并行预判的判定核心。纯函数式：只读 conn 状态并返回「是否该提交」。

        抽成独立方法是为了能脱离音频流单测（本项目的 is_vad 依赖真实 PCM）。
        调用方（is_vad 的静音分支）负责把 client_voice_stop 同步到返回值。
        """
        if not self.two_stage_endpoint:
            # 旧单阈值逻辑（无 candidate/confirm 配置时向后兼容）
            return silence_ms >= self.silence_threshold_ms

        # —— 语义 hold：判停模型说「还没说完」后，在截止时刻前不再重复判定 ——
        # 旧实现靠定时器等待，但定时器入口用了粘性的 client_have_voice 永远提前返回，
        # hold 实际失效，下一帧又触发判停（实测 hold 后 1 秒内再次判定约 83%）。
        hold_until = getattr(conn, "_vad_hold_until", 0.0)
        if hold_until:
            if time.time() < hold_until:
                return False
            # 等够了用户仍没再开口 → 不再问模型，直接提交（base 据此跳过判停）
            conn._vad_hold_until = 0.0
            conn._vad_hold_expired = True
            logger.bind(tag=TAG).info(f"[VAD] hold 期满无新语音，直接提交: 静音={silence_ms:.0f}ms")
            return True

        # —— 进入候选结束 ——
        if not getattr(conn, "_vad_candidate_end", False) and silence_ms >= self.candidate_silence_ms:
            conn._vad_candidate_end = True
            conn._vad_confirm_voice_frames = 0  # 确认窗口免疫计数器清零，重新累计
            logger.bind(tag=TAG).info(
                f"VAD 候选结束(进入确认窗口): 静音={silence_ms:.0f}ms >= {self.candidate_silence_ms}ms，"
                f"继续确认 {self.confirmation_window_ms}ms"
            )
            # 并行预判：不等确认窗口，立刻开跑 ASR+JEV
            if self.endpoint_probe_speculative:
                self._start_speculative_probe(conn)

        # —— 读预判结论（每帧一次，纯读缓存，无额外开销）——
        verdict = getattr(conn, "_vad_probe_verdict", None)
        if self.endpoint_probe_speculative and verdict is not None:
            probe_text = str(getattr(conn, "_vad_probe_text", "") or "")[:40]
            if verdict == "complete":
                # JEV 说完了 → 立刻提交，不等确认窗口
                logger.bind(tag=TAG).info(
                    f"VAD 预判完成(跳过确认窗口立即提交): 静音={silence_ms:.0f}ms，预判文本={probe_text!r}"
                )
                # 2026-10-05 A2：这里**不能**清 _vad_probe_text / _vad_probe_audio_sig。
                # 预判文本正是提交路径要复用的对象（省一次 ASR），指纹也是它的安全校验。
                # 完整清理由 base._commit_stopped_audio 在音频提交消费后统一做。
                conn._vad_probe_pending = False
                return True
            if verdict == "incomplete":
                # JEV 说还没说完 → 取消候选标记，继续收音等用户接着说
                conn._vad_candidate_end = False
                logger.bind(tag=TAG).info(
                    f"VAD 预判未完(继续收音): 静音={silence_ms:.0f}ms，预判文本={probe_text!r}"
                )
                # 继续收音意味着音频还会变，本轮预判文本必然过期 → 全清
                self._clear_probe(conn)
                return False
            # verdict == "unknown"：预判跑完但拿不到结论 → 落回两段式等确认窗口

        # —— 落回两段式：确认窗口期满才提交（预判未回来/未知/关闭预判时的兜底）——
        if (
            getattr(conn, "_vad_candidate_end", False)
            and silence_ms >= self.candidate_silence_ms + self.confirmation_window_ms
        ):
            logger.bind(tag=TAG).info(
                f"VAD 确认语音停止(两段式提交): 总静音={silence_ms:.0f}ms >= "
                f"{self.candidate_silence_ms}+{self.confirmation_window_ms}ms"
            )
            return True
        return False

    def _clear_probe(self, conn):
        """清理预判状态（提交后 / 判定继续收音后调用）。代际 +1，在途的旧预判协程写回时会被丢弃。"""
        conn._vad_probe_gen = getattr(conn, "_vad_probe_gen", 0) + 1
        conn._vad_probe_pending = False
        conn._vad_probe_verdict = None
        conn._vad_probe_text = None
        conn._vad_probe_audio_sig = None
        conn._vad_probe_voice_seq = None
        conn._vad_probe_decision = None

    def _reset_utterance_state(self, conn):
        """新一段人声开始时，清掉上一段话遗留的判停状态。"""
        conn._vad_candidate_end = False
        conn._vad_confirm_voice_frames = 0
        conn._vad_hold_until = 0.0
        conn._vad_hold_expired = False
        # 本段人声的声学统计（供播报期打断闸门日志取证：咳嗽/清嗓子与说话的
        # 区别需要真实数据，不能拍脑袋定阈值）：[总帧数, 概率和, 最大概率, 人声帧数]
        conn._vad_seg_stats = [0, 0.0, 0.0, 0]
        self._clear_probe(conn)

    def _start_speculative_probe(self, conn):
        """候选结束那一刻发起并行预判：预览 ASR + JEV，结果写回 conn 供后续帧读取。

        为什么放在 VAD 层发起而不在 ASR 层：VAD 是唯一知道「静音刚达到候选」这一时刻的地方，
        放在这里才能真正做到「不等确认窗口就开始干活」。

        预判本身完全不影响判停主流程——它只写 _vad_probe_verdict，
        真正的提交仍由 client_voice_stop 驱动。任何异常都吞掉退回两段式。
        """
        if getattr(conn, "_vad_probe_pending", False):
            return  # 已有预判在跑，不重复发起
        conn._vad_probe_gen = gen = getattr(conn, "_vad_probe_gen", 0) + 1
        logger.bind(tag=TAG).info(f"[VAD] 发起并行预判 gen={gen}")
        conn._vad_probe_pending = True
        conn._vad_probe_verdict = None
        conn._vad_probe_audio_sig = None
        try:
            loop = getattr(conn, "loop", None)
            if loop is None or loop.is_closed():
                conn._vad_probe_pending = False
                return
            import asyncio

            task = asyncio.run_coroutine_threadsafe(
                self._run_speculative_probe(conn, gen), loop
            )
            conn._vad_probe_task = task
            # 回调在事件循环里执行；这里只是登记，不阻塞 VAD 线程
            task.add_done_callback(lambda _t: None)
        except Exception as exc:
            conn._vad_probe_pending = False
            logger.bind(tag=TAG).warning(f"[VAD] 并行预判发起失败，退回两段式: {exc}")

    async def _run_speculative_probe(self, conn, gen=None):
        """预判协程：跑一次预览 ASR + JEV，把 complete/incomplete/unknown 写回 conn。

        注意：这里刻意不修改 conn.asr_audio / client_voice_stop，
        预判只读，正式提交路径仍走 _commit_stopped_audio（音频不会被提前消费）。
        """
        if gen is None:
            gen = getattr(conn, "_vad_probe_gen", 0)

        def _skip(why):
            # 2026-10-11 观测：预判的每个提前返回点都留 INFO，否则「预判没生效」无从排查
            logger.bind(tag=TAG).info(f"[VAD] 并行预判未出结论: {why}")

        def _alive():
            # 用户已重新出声/已提交 → 代际变了 → 本协程的结论过期，不得写回
            return getattr(conn, "_vad_probe_gen", 0) == gen

        try:
            asr = getattr(conn, "asr", None)
            if asr is None:
                _skip("conn.asr 为空")
                return
            audio_snapshot = list(getattr(conn, "asr_audio", []) or [])
            voice_seq0 = getattr(conn, "_vad_voice_seq", 0)
            if len(audio_snapshot) <= 15:
                # 音频太短（JEV 在 base.py 里同样要求 >15 块），不预判
                _skip(f"音频太短({len(audio_snapshot)}块≤15)")
                return

            # ⚠️ 唤醒路径零判停（主人 2026-10-04 裁决、2026-10-07 再次强调）：
            # 「唤醒词不走判停，只有正常对话才走判停模型」。
            # A1 预判属于判停链路的一环，会话未激活（正在尝试唤醒）时一律不得发起——
            # 它既不该问 JEV，更不该调用预览 ASR（会读走并清空流式会话文本，
            # 把唤醒词那句吃掉 → 唤不醒）。
            if bool(getattr(conn, "idle_guard", False)) and not bool(
                getattr(conn, "_session_active", False)
            ):
                _skip("唤醒路径（会话未激活）")
                if _alive():
                    conn._vad_probe_verdict = "unknown"
                return

            # ⚠️ 2026-10-07 修复：**先判「预判结果有没有用」，再决定要不要跑预览 ASR**。
            #
            # 原顺序是先跑 speech_to_text_wrapper、再检查 JEV 是否可用。这有一个
            # 静默副作用：流式 ASR（doubao_stream）的 speech_to_text 是「读走并清空」
            # 会话文本（sess["text"] = ""）——预览 ASR 会把流式已识别的 definite 文本
            # 吃掉，随后正式提交 handle_voice_stop 拿到空串，表现为
            # 「ASR 明明识别到文本、助手却不回复」。
            #
            # 云端 ASR 时 endpoint_judge_active() 恒为 False（云端自带断句），
            # 预判结果必被丢弃 —— 却照样把文本吃掉。主人实测（Mac Mini）：
            # 10:32:57 识别到「什么呀？你是不」，10:32:59 正式提交 text_length=0。
            # 探针 tmp/probe_probe_steals_text.py 已复现：预判读到文本、正式提交为空。
            from core.providers.decision import get_decision_provider

            provider = get_decision_provider(getattr(conn, "config", None))
            if not provider.enabled or not provider.endpoint_enabled:
                _skip(f"判停未启用 enabled={provider.enabled} endpoint={provider.endpoint_enabled}")
                if _alive():
                    conn._vad_probe_verdict = "unknown"
                return
            from core.utils.model_stack import endpoint_judge_active

            if not endpoint_judge_active(conn.config):
                # 云端 ASR 自己会判停，本地预判无意义（且会误消费流式文本）
                _skip("云端ASR已停用本地判停")
                if _alive():
                    conn._vad_probe_verdict = "unknown"
                return

            # 预览 ASR：与正式识别同一份音频，但结果只给 JEV 当输入，不当最终结果
            preview, _ = await asr.speech_to_text_wrapper(
                audio_snapshot, conn.session_id, conn.audio_format
            )
            if isinstance(preview, dict):
                text = str(preview.get("text", preview.get("content", "")) or "")
            else:
                text = str(preview or "")
            text = text.strip()
            if not _alive():
                return
            if not text:
                _skip("预览识别为空")
                conn._vad_probe_verdict = "unknown"
                return

            idle_guard = bool(getattr(conn, "idle_guard", False))
            session_active = (
                bool(getattr(conn, "_session_active", False)) if idle_guard else True
            )

            # A3 快速通道：句尾规则高置信判定「这句已说完」时跳过 JEV，
            # 省掉 735ms 云端往返。规则保守（宁可漏跳不误跳），拿不准返回
            # None 继续走 JEV。若后续要做阶段 2 的过渡句，这里产出的
            # _vad_probe_text 同样是它需要的输入。
            fp = getattr(conn, "_endpoint_fastpath", None)
            if fp is None:
                from core.utils.endpoint_fastpath import EndpointFastPath

                fp = EndpointFastPath(getattr(conn, "config", None))
                conn._endpoint_fastpath = fp
            fast_rule = fp.check(text) if session_active else None
            if fast_rule:
                conn._vad_probe_text = text[:100]
                conn._vad_probe_audio_sig = (
                    len(audio_snapshot),
                    sum(len(b) for b in audio_snapshot),
                )
                conn._vad_probe_voice_seq = voice_seq0
                conn._vad_probe_verdict = "complete"
                logger.bind(tag=TAG).info(
                    f"[VAD] 快速通道跳过 JEV: rule={fast_rule} text={text[:60]!r}"
                )
                return

            decision = await provider.judge_utterance(text, session_active)
            if not _alive():
                logger.bind(tag=TAG).info("[VAD] 并行预判结论已过期（期间用户又出声/已提交），丢弃")
                return
            if decision is None:
                _skip("判停模型无结论（超时/不可用）")
                conn._vad_probe_verdict = "unknown"
                return

            conn._vad_probe_text = text[:100]
            # 记录预判时看到的音频指纹（块数 + 总字节数）。verdict=complete 时
            # VAD 会在同一帧立刻提交，中间不会有新音频进来，正式提交路径就能
            # 直接复用这份文本，省掉第二次 ASR。若期间音频变了，指纹不一致，
            # base.py 会照旧真识别，文本与音频仍然严格一致。
            conn._vad_probe_audio_sig = (
                len(audio_snapshot),
                sum(len(b) for b in audio_snapshot),
            )
            conn._vad_probe_voice_seq = voice_seq0
            conn._vad_probe_decision = decision
            conn._vad_probe_verdict = "complete" if decision.complete else "incomplete"
            logger.bind(tag=TAG).info(
                f"[VAD] 并行预判完成: verdict={conn._vad_probe_verdict} "
                f"prob={decision.complete_probability} text={text[:60]!r}"
            )
        except Exception as exc:
            if _alive():
                conn._vad_probe_verdict = "unknown"
            logger.bind(tag=TAG).warning(f"[VAD] 并行预判异常，退回两段式: {exc}")
        finally:
            if _alive():
                conn._vad_probe_pending = False

    def release_conn_resources(self, conn):
        """释放连接的 VAD 资源（连接关闭时调用）"""
        for attr in ("_vad_opus_decoder", "_vad_state", "_vad_context"):
            if hasattr(conn, attr):
                try:
                    delattr(conn, attr)
                except Exception:
                    pass

    def is_vad(self, conn, opus_packet):
        """处理 opus 格式的音频数据"""
        # 手动模式：直接返回True，不进行实时VAD检测，所有音频都缓存
        # 参照小智开源服务器实现
        if hasattr(conn, 'client_listen_mode') and conn.client_listen_mode == "manual":
            return True

        try:
            self._init_connection_state(conn)

            pcm_frame = conn._vad_opus_decoder.decode(opus_packet, 960)

            # 诊断：检查解码后的音频内容
            audio_int16 = np.frombuffer(pcm_frame, dtype=np.int16)
            result = self._process_pcm_frames(conn, pcm_frame)
            logger.bind(tag=TAG).debug(f"[VAD诊断-结果] _process_pcm_frames 返回: {result}")
            return result
        except opuslib_next.OpusError as e:
            logger.bind(tag=TAG).warning(f"[VAD-Opus错误] 解码失败: {e}, opus_size={len(opus_packet)}, 前4字节={opus_packet[:4].hex() if len(opus_packet) >= 4 else 'too short'}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"[VAD-Opus异常] {e}")
        
        return False
    
    def is_vad_pcm(self, conn, pcm_data):
        """处理 PCM 格式的音频数据（16kHz, 16-bit, 单声道）"""
        # manual 模式下也进行 VAD 检测，用于判断是否有语音
        # 但不触发语音停止逻辑（由定时触发ASR处理）

        try:
            self._init_connection_state(conn)
            return self._process_pcm_frames(conn, pcm_data)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error processing PCM audio: {e}")
        
        return False
    
    def _process_pcm_frames(self, conn, pcm_frame):
        """处理 PCM 帧，执行 VAD 检测"""
        # 诊断：检查 buffer 状态
        logger.bind(tag=TAG).debug(f"[VAD诊断-buffer] 接收 PCM 帧: {len(pcm_frame)} bytes, buffer 原有: {len(conn.client_audio_buffer)} bytes")

        conn.client_audio_buffer.extend(pcm_frame)

        # 诊断：检查 buffer 扩展后状态
        logger.bind(tag=TAG).debug(f"[VAD诊断-buffer] buffer 扩展后: {len(conn.client_audio_buffer)} bytes, 需要 >= {512 * 2} bytes")

        client_have_voice = False
        frame_count = 0
        while len(conn.client_audio_buffer) >= 512 * 2:
            frame_count += 1
            chunk = conn.client_audio_buffer[: 512 * 2]
            conn.client_audio_buffer = conn.client_audio_buffer[512 * 2 :]

            audio_int16 = np.frombuffer(chunk, dtype=np.int16)
            audio_float32 = audio_int16.astype(np.float32) / 32768.0
            audio_input = np.concatenate(
                [conn._vad_context, audio_float32.reshape(1, -1)], axis=1
            ).astype(np.float32)

            ort_inputs = {
                "input": audio_input,
                "state": conn._vad_state,
                "sr": np.array(16000, dtype=np.int64),
            }
            out, state = self.session.run(None, ort_inputs)

            conn._vad_state = state
            conn._vad_context = audio_input[:, -64:]
            speech_prob = out.item()
            _st = getattr(conn, "_vad_seg_stats", None)
            if _st is not None and getattr(conn, "client_have_voice", False):
                _st[0] += 1
                _st[1] += speech_prob
                _st[2] = max(_st[2], speech_prob)
                if speech_prob >= self.vad_threshold:
                    _st[3] += 1

            # 双阈值判断
            if speech_prob >= self.vad_threshold:
                is_voice = True
            elif speech_prob <= self.vad_threshold_low:
                is_voice = False
            else:
                is_voice = conn.last_is_voice

            # 声音没低于最低值则延续前一个状态，判断为有声音
            conn.last_is_voice = is_voice

            # 更新滑动窗口（限制长度）
            conn.client_voice_window.append(is_voice)
            if len(conn.client_voice_window) > self.max_window_size:
                conn.client_voice_window = list(conn.client_voice_window[-self.max_window_size:])
            
            # 检查最近 N 帧是否有足够多的语音帧（触发起点）
            window_size = min(self.frame_window_threshold, len(conn.client_voice_window))
            if window_size > 0:
                recent_window = conn.client_voice_window[-window_size:]
                recent_voice_count = sum(1 for v in recent_window if v)
                window_voice = recent_voice_count >= self.frame_window_threshold
            else:
                window_voice = False

            # 对齐小智原版：窗口 3 帧多数表决即判 have_voice，无额外持续条件
            client_have_voice = window_voice

            # 诊断日志：恢复 DEBUG 级别（打断排查期间的临时 WARNING 已完成使命）
            logger.bind(tag=TAG).debug(
                f"[VAD诊断] frame={frame_count}, speech_prob={speech_prob:.3f}, is_voice={is_voice}, client_have_voice={client_have_voice}, window={conn.client_voice_window[-5:]}, threshold={self.vad_threshold}"
            )

            # 更新最后活动时间（有声音时）
            if client_have_voice:
                conn._vad_voice_seq = getattr(conn, "_vad_voice_seq", 0) + 1
                if not getattr(conn, "client_have_voice", False):
                    # [2026-10-12] 记录这段人声的采集起点，供声纹闸门判断
                    # 「这段音频采集时音乐是否还在放」（与 music_auto_pause 的
                    # last_pause_ts 比先后）。原先闸门只知道「音乐在放吗」，
                    # 无法区分「暂停前采的（混着音乐）」和「暂停后采的（干净）」。
                    conn._vad_voice_start_ts = time.time()
                    # 2026-10-07 诊断埋点：VAD「人声开始」原先只有 DEBUG 级，
                    # 排查「说话一段时间后助手不响应」时需要确认 VAD 是否还在出人声
                    # （INFO 环境看不到 DEBUG）。每段人声只打一次，不刷屏。
                    # [2026-10-12 打断取证埋点] 主人实测「TTS 播报期间莫名不出声」，
                    # 日志显示三次都是这里触发的 Abort，但**关键判据只在 DEBUG 级**，
                    # INFO 环境看不到 → 拿不到证据就无法定阈值（能量门槛/时长门槛
                    # 都是拍脑袋，主人 2026-10-10 已要求「没用的逻辑不留」，
                    # 不能靠猜改）。
                    # 这里把打断发生时的全部判据提到 INFO：speech_prob、窗口命中数、
                    # 窗口内容、RMS。改完据此定阈值，再把这段降回 DEBUG。
                    try:
                        _rms = float(np.sqrt(np.mean(
                            audio_int16.astype(np.float64) ** 2)))
                    except Exception:
                        _rms = -1.0
                    logger.bind(tag=TAG).info(
                        f"[VAD打断取证] 起始 speech_prob={speech_prob:.3f} "
                        f"阈值={self.vad_threshold} 窗口={conn.client_voice_window[-5:]} "
                        f"命中={recent_voice_count}/{window_size} "
                        f"RMS={_rms:.0f} speaking={bool(getattr(conn, 'client_is_speaking', False))}"
                    )
                    # 新一段话从这里开始：上一段话遗留的候选/确认窗口/预判/hold 状态
                    # 一律作废。否则提交或 drop 后（reset_audio_states 不清这些）下一句
                    # 的首帧会被当成「确认窗口内的毛刺」免疫掉，旧预判结论还可能把新句子
                    # 提前提交（实测：提交后首事件为「噪音免疫」占 62%）。
                    self._reset_utterance_state(conn)
                conn.client_have_voice = True
                conn.last_activity_time = time.time() * 1000
                # 确认窗口打断免疫：低能量的回声/噪音毛刺冻结静音计时（hangover），
                # 既不清候选也不累计到提交；高能量人声（真人开口）真打断正常收音。
                # ⚠️ 免疫帧必须「冻结」静音计时（刷新 _last_voice_time）：
                # 只跳过清除逻辑的话，静音计时会穿过免疫人声帧继续累计 → 在 TTS 播放
                # （回声反复触发人声）期间反复累计到 800ms 误提交，把打断说话
                # 的音频切碎丢弃（主人 2026-10-10 实测「TTS 说话中打断就听不见」）。
                if self._confirm_window_immune(conn, len(audio_int16) / 16.0):
                    conn._last_voice_time = time.time()
                    continue
                # 清除静音计时器与候选标记（回退到说话状态）
                if hasattr(conn, '_last_voice_time'):
                    delattr(conn, '_last_voice_time')
                conn._vad_candidate_end = False
                conn.client_voice_stop = False
                # A1：用户又出声了 → 作废本轮预判（音频已变，结论不再适用）
                if getattr(conn, "_vad_probe_verdict", None) is not None or getattr(
                    conn, "_vad_probe_pending", False
                ):
                    self._clear_probe(conn)
                # 2026-09-18 移除「最大 ASR 持续时间兜底」（本地自加，上游无）：
                # 该兜底在持续有声音（背景音乐/环境噪声）时强制 10s 断句，
                # 导致用户正常说话被拦腰切断、且与上游判停行为不一致。
                # 上游只靠静音阈值判停，这里对齐。
                # 2026-09-23 近场爆发切分：段进行中检测能量突变（近场主人插话
                # vs 远场视频背景声），持续 N 帧超段内基线×倍率 → 立即判停当前段，
                # 切分点以来尾帧保留给新段（base.py 消费 _vad_keep_tail_blocks）。
                if self.near_burst_ratio > 0 and self.near_burst_frames > 0:
                    frame_rms = float(np.sqrt(np.mean(audio_int16.astype(np.float64) ** 2)))
                    conn._vad_seg_frames = getattr(conn, '_vad_seg_frames', 0) + 1
                    seg_ema = getattr(conn, '_vad_seg_rms_ema', None)
                    if seg_ema is None or seg_ema <= 0:
                        # 段首帧：初始化基线，本帧不检测（防段建立瞬间自切）
                        conn._vad_seg_rms_ema = frame_rms
                        conn._vad_burst_frames = 0
                    elif conn._vad_seg_frames <= self.near_burst_warmup_frames:
                        # 段首保护期：只攒基线，不检测不切分（防轻声起句被切碎）
                        conn._vad_seg_rms_ema = seg_ema * 0.9 + frame_rms * 0.1
                        conn._vad_burst_frames = 0
                    else:
                        # 用「旧基线」检测：爆发帧不更新 EMA，否则爆发能量混入基线
                        # 会导致阈值反超、计数器中途清零（2026-09-23 单测抓出的 bug）
                        if frame_rms >= max(seg_ema * self.near_burst_ratio, self.near_min_rms):
                            conn._vad_burst_frames = getattr(conn, '_vad_burst_frames', 0) + 1
                        else:
                            conn._vad_burst_frames = 0
                            conn._vad_seg_rms_ema = seg_ema * 0.9 + frame_rms * 0.1
                        if conn._vad_burst_frames >= self.near_burst_frames:
                            conn.client_voice_stop = True
                            conn._vad_keep_tail_blocks = self.near_burst_frames + 2
                            conn._vad_burst_frames = 0
                            conn._vad_seg_frames = 0  # 新段重新计段首保护
                            conn._vad_seg_rms_ema = frame_rms  # 新段基线从当前能量起
                            logger.bind(tag=TAG).info(
                                f"[VAD] 近场爆发切分: rms={frame_rms:.0f}, 基线={seg_ema:.0f}")
            elif conn.client_have_voice:
                # 之前有声音但现在没有，开始计算静音时间
                if not hasattr(conn, '_last_voice_time'):
                    conn._last_voice_time = time.time()
                    # 新一轮静音计时，候选标记归位（防 reset_audio_states 后的残留状态）
                    conn._vad_candidate_end = False

                silence_ms = (time.time() - conn._last_voice_time) * 1000

                # 两段式 + A1 并行预判的判定核心（抽成独立方法以便单测）
                conn.client_voice_stop = self._endpoint_decision_for_silence(conn, silence_ms)

        return client_have_voice
