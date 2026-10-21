# -*- coding: utf-8 -*-
"""车牌三段式反馈协调器（T4-T7）

主人 2026-09-26 定的设计：
    ① 预欢迎   —— 车牌识别完成就先把空调/灯/音乐打开（**不等身份确认**，
                  因为这些动作是通用的，早做体验更好）
    ② 多设备联合判断 —— 人进门后靠摄像头人脸（可叠加声纹/毫米波）确认**到底是谁**
    ③ 个性化播报 —— 确认之后才按"谁 + 情境"生成有温度的欢迎语

为什么必须分三段（核心约束）：
    车牌只能回答"哪台车回来了"，回答不了"谁回来了"。
    一台车多人开时，仅凭车牌就喊"主人回来了"会认错人。
    所以：预欢迎可以早做（不点名），个性化播报必须等确认（点名）。

复用而非重写：
    - 设备控制走 plugins_func.functions.ha_control.apply_entities（自带允许清单校验）
    - 欢迎语生成走调用方注入的 generate_welcome 回调（即 security_listener 里
      既有的 LLM 情境化欢迎语，含称呼/分寸/防机械罗列等已调教好的提示词）
    - 广播走调用方注入的 broadcast 回调（即 security_listener._broadcast，含 TTS）

本模块不 import security_listener，避免循环依赖；依赖全部构造时注入，便于单测。
"""
import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config.logger import setup_logging
from core.utils import data_collector, plate_bindings

TAG = __name__
logger = setup_logging()

# 注入的回调类型别名
BroadcastFn = Callable[..., Awaitable[None]]
GenWelcomeFn = Callable[[str, str], Awaitable[Optional[str]]]


class PlateWelcomeCoordinator:
    """车牌三段式协调器。

    一个实例对应一条 Frigate 事件监听链路（与 security_listener 同生命周期）。
    """

    def __init__(self, cfg: Dict[str, Any], broadcast: BroadcastFn,
                 generate_welcome: GenWelcomeFn):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        # 车牌 → 人脸 的融合等待窗（秒）。人从车库走到门口通常需要这么久
        self.window_seconds = float(cfg.get("window_seconds", 120))
        # 同一车牌的预欢迎冷却，防止 Frigate 反复上报同一辆车导致重复动作
        self.cooldown_seconds = float(cfg.get("cooldown_seconds", 600))
        self.prewelcome_cfg = cfg.get("prewelcome") or {}
        self.unknown_cfg = cfg.get("unknown_plate") or {}
        self.unconfirmed_cfg = cfg.get("unconfirmed") or {}

        self._broadcast = broadcast
        self._generate_welcome = generate_welcome

        # 待确认的车牌事件：plate -> pending dict
        self._pending: Dict[str, Dict[str, Any]] = {}
        # 预欢迎冷却：plate -> 上次触发时间戳
        self._last_prewelcome: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # ① 车牌事件入口
    # ------------------------------------------------------------------
    async def on_plate(self, plate: str, camera_id: str = "",
                       camera_name: str = "") -> Dict[str, Any]:
        """Frigate 识别到车牌时调用。返回处理结果（便于日志与单测断言）。"""
        plate_n = plate_bindings.normalize_plate(plate)
        if not plate_n:
            return {"handled": False, "reason": "empty_plate"}
        if not self.enabled:
            return {"handled": False, "reason": "disabled"}

        candidates = plate_bindings.get_candidates(plate_n)
        known = len(candidates) > 0
        ambiguous = len(candidates) > 1

        # 已有待确认事件：只刷新时间，不重复预欢迎
        if plate_n in self._pending:
            self._pending[plate_n]["ts"] = time.time()
            return {"handled": True, "stage": "dedup", "plate": plate_n,
                    "known": known, "ambiguous": ambiguous}

        now = time.time()
        if now - self._last_prewelcome.get(plate_n, 0) < self.cooldown_seconds:
            logger.bind(tag=TAG).info(
                f"[车牌] {plate_n} 在预欢迎冷却内，跳过本次")
            return {"handled": False, "reason": "cooldown", "plate": plate_n}
        self._last_prewelcome[plate_n] = now

        # ── ① 预欢迎：只认车、不认人，所以动作必须是通用的 ──
        pre_result = await self._do_prewelcome(plate_n, known, camera_name)

        # 采集：车牌事件落库（旁路统计）
        data_collector.record_event(
            "plate", camera_id=camera_name or camera_id, plate=plate_n,
            label="known" if known else "visitor",
            extra={"candidates": len(candidates), "ambiguous": ambiguous,
                   "prewelcome": pre_result})

        if not known:
            # 访客车：不进融合流程，直接按访客提示（不点名、不做家庭预欢迎）
            await self._announce(self.unknown_cfg.get("announce", ""),
                                 camera_name, level="warn")
            return {"handled": True, "stage": "unknown_plate",
                    "plate": plate_n, "prewelcome": pre_result}

        # ── ② 开融合窗，等人进门后的人脸确认 ──
        task = asyncio.ensure_future(self._await_face(plate_n, camera_id,
                                                      camera_name))
        self._pending[plate_n] = {
            "plate": plate_n, "candidates": candidates,
            "camera_id": camera_id, "camera_name": camera_name,
            "ts": now, "task": task, "prewelcome": pre_result,
        }
        logger.bind(tag=TAG).info(
            f"[车牌] {plate_n} 已预欢迎，进入 {self.window_seconds:.0f}s 身份融合窗"
            f"（候选 {len(candidates)} 人{'，一车多人需人脸确认' if ambiguous else ''}）")
        return {"handled": True, "stage": "prewelcomed", "plate": plate_n,
                "known": True, "ambiguous": ambiguous,
                "candidates": [c.get("call_name") for c in candidates],
                "prewelcome": pre_result}

    # ------------------------------------------------------------------
    # ② 人脸事件入口（由 security_listener 在识别到家人时调用）
    # ------------------------------------------------------------------
    async def on_face(self, name_key: str, display_name: str,
                      camera_id: str = "", camera_name: str = "") -> Optional[Dict[str, Any]]:
        """识别到人脸时调用，尝试与待确认的车牌事件融合。

        返回 None 表示"没有待融合的车牌事件"，调用方应按原有逻辑自己播报欢迎语；
        返回 dict 表示本模块已完成播报，调用方**应跳过**自己的欢迎语，避免重复。
        """
        if not self.enabled or not self._pending:
            return None

        pending = self._find_pending(camera_id)
        if not pending:
            return None

        plate = pending["plate"]
        candidates = pending.get("candidates") or []
        cand_keys = {str(c.get("name") or "").strip().lower() for c in candidates}
        cand_keys |= {str(c.get("call_name") or "").strip().lower() for c in candidates}
        cand_keys.discard("")

        matched = next((c for c in candidates
                        if str(c.get("name") or "").strip().lower() == name_key
                        or str(c.get("call_name") or "").strip().lower() == name_key),
                       None)
        if matched:
            relation = matched.get("relation") or ""
            call_name = matched.get("call_name") or display_name
            outcome = "confirmed_by_plate"
        else:
            # 人脸与车牌候选不一致：**以人脸为准**（人脸可信度高于车牌），
            # 但记一条 mismatch 供事后核查（可能是别人开回家的车）
            relation, call_name = "", display_name
            outcome = "face_override_mismatch"
            logger.bind(tag=TAG).warning(
                f"[车牌] 人脸({display_name}) 与车牌 {plate} 的候选"
                f"({[c.get('call_name') for c in candidates]}) 不一致，以人脸为准")

        self._clear_pending(plate)

        # ── ③ 个性化播报：此时身份已确认，可以点名 ──
        text = await self._generate_welcome(call_name, relation)
        if not text:
            text = f"{call_name}回来啦"
        # 家人车+人脸确认：个性化欢迎，用后台主音色（不传 voice）
        await self._announce(text, pending.get("camera_name") or camera_name,
                             level="info")
        data_collector.record_event(
            "face", camera_id=pending.get("camera_name") or camera_name,
            label=call_name, plate=plate,
            greeting_triggered=True, greeting_text=text,
            extra={"outcome": outcome})
        logger.bind(tag=TAG).info(
            f"[车牌] 三段式完成：车牌 {plate} + 人脸 {call_name} → {outcome}")
        return {"fused": True, "plate": plate, "call_name": call_name,
                "outcome": outcome, "text": text}

    # ------------------------------------------------------------------
    # 内部：预欢迎动作
    # ------------------------------------------------------------------
    async def _do_prewelcome(self, plate: str, known: bool,
                             camera_name: str) -> Dict[str, Any]:
        """执行通用预欢迎动作（开灯/空调/场景）。**只对已登记车辆执行**。

        支持两种写法（可并存）：
          - `entities`: 直接给设备目标状态，如 {"light.x": "on"}
          - `scene`:    指定一个 HA scene/script 实体，推荐用它承载"回家预欢迎"
                        （可以包含音乐——音乐走 HA 场景比在这里耦合播放插件更干净）
        """
        if not known:
            return {"skipped": "unknown_plate"}

        entities = dict(self.prewelcome_cfg.get("entities") or {})
        scene = str(self.prewelcome_cfg.get("scene") or "").strip()
        if scene:
            entities[scene] = "on"
        if not entities:
            return {"skipped": "no_action_configured"}

        try:
            # 懒加载：ha_control 依赖 core.utils.ha_expose，模块级导入有环风险
            from plugins_func.functions.ha_control import apply_entities
            res = await apply_entities(entities, trusted=True)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[车牌] 预欢迎控制失败（忽略）: {e}")
            return {"error": str(e)[:200]}

        if isinstance(res, dict) and res.get("error"):
            logger.bind(tag=TAG).warning(f"[车牌] 预欢迎被拒: {res['error']}")
            return {"error": str(res["error"])[:200]}

        ok = (res or {}).get("ok", 0)
        total = (res or {}).get("total", len(entities))
        failed = (res or {}).get("failed") or {}
        logger.bind(tag=TAG).info(
            f"[车牌] ① 预欢迎已执行 {ok}/{total} 项"
            + (f"，失败: {list(failed)}" if failed else ""))

        # 预欢迎播报：不含人名（身份还没确认），空串表示静默执行
        await self._announce(self.prewelcome_cfg.get("announce", ""),
                             camera_name, level="info")
        return {"ok": ok, "total": total, "failed": list(failed)}

    # ------------------------------------------------------------------
    # 内部：融合窗超时
    # ------------------------------------------------------------------
    async def _await_face(self, plate: str, camera_id: str, camera_name: str):
        """等待融合窗超时。窗口内收到人脸则由 on_face 取消本协程。"""
        try:
            await asyncio.sleep(self.window_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending.pop(plate, None)
        if not pending:
            return
        cands = pending.get("candidates") or []
        names = [c.get("call_name") for c in cands if c.get("call_name")]
        logger.bind(tag=TAG).info(
            f"[车牌] {plate} 融合窗超时，未确认驾驶员（候选 {names}）→ 降级为不点名播报")
        # 降级：**绝不点名**。宁可少说一句，也不能认错人。
        text = self.unconfirmed_cfg.get("announce", "")
        await self._announce(text, camera_name, level="info")
        data_collector.record_event(
            "plate", camera_id=camera_name or camera_id, plate=plate,
            label="unconfirmed", greeting_triggered=bool(text),
            greeting_text=text, extra={"outcome": "window_timeout",
                                       "candidates": names})

    # ------------------------------------------------------------------
    # 内部：工具
    # ------------------------------------------------------------------
    def _find_pending(self, camera_id: str = "") -> Optional[Dict[str, Any]]:
        """挑一个待确认事件。优先同摄像头，其次最近的一个。"""
        if not self._pending:
            return None
        if camera_id:
            for p in self._pending.values():
                if p.get("camera_id") == camera_id:
                    return p
        return max(self._pending.values(), key=lambda p: p.get("ts", 0))

    def _clear_pending(self, plate: str):
        pending = self._pending.pop(plate, None)
        if pending:
            task = pending.get("task")
            if task and not task.done():
                task.cancel()

    def cancel_pending(self, camera_id: str = "", announce: bool = False):
        """静默取消待确认的车牌事件（不播报）。

        用途：人脸欢迎语处于防骚扰冷却内时，人已经被招呼过了，
        此时不该再补一句"您的车回来了"，直接丢弃待确认事件即可。
        """
        if not self._pending:
            return 0
        targets = ([p["plate"] for p in self._pending.values()
                    if p.get("camera_id") == camera_id] if camera_id
                   else list(self._pending))
        for plate in targets:
            self._clear_pending(plate)
        return len(targets)

    async def _announce(self, text: str, camera_name: str = "",
                        level: str = "info", voice: str = None):
        """统一播报出口。text 为空则完全静默（不打扰）。

        voice=None 表示「用后台主音色」（主人 2026-10-06 定案：车牌相关播报
        都是家人车到家的场景，不是安防报警，不需要威慑音）。
        """
        text = str(text or "").strip()
        if not text:
            return
        try:
            source = f"摄像头·{camera_name}" if camera_name else "车牌识别"
            speak = level in ("info", "warn")
            await self._broadcast({"level": level, "source": source,
                                   "text": text}, speak=speak, voice=voice)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[车牌] 播报失败（忽略）: {e}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def shutdown(self):
        """停止所有等待中的融合窗（服务退出时调用，避免遗留协程）。"""
        for plate in list(self._pending):
            self._clear_pending(plate)
        self._pending.clear()

    def pending_plates(self) -> List[str]:
        """当前待确认的车牌（调试/测试用）。"""
        return list(self._pending)
