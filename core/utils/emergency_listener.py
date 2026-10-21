# -*- coding: utf-8 -*-
"""全局语音紧急报警监听器

在 ASR 识别结果出来后、进入 LLM 常规对话之前，先判断文本是否命中后台配置的
紧急关键词。命中后：
1. 立即打断当前流程，进入确认状态；
2. 向用户 TTS 确认是否真的需要帮助；
3. 在超时时间内等待用户回复；
4. 若用户确认需要或超时未回复，则按后台配置的 alert_order 拨打电话/发短信/发微信。

设计约束：
- 不依赖唤醒词，任何设备输入都生效；
- 任何失败只记日志，不影响主对话链路；
- 报警有冷却时间，防止同一事件反复触发。
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Callable, Optional

from config.logger import setup_logging

logger = setup_logging()
TAG = "EmergencyListener"

# 避免循环导入：同步读取配置的辅助函数在 emergency_api.py 中
def _api():
    from core.api import emergency_api
    return emergency_api


class EmergencySession:
    """一次紧急报警会话的状态机。"""

    def __init__(self, text: str, keyword: str, source: str = "unknown"):
        self.text = text
        self.keyword = keyword
        self.source = source
        self.created_at = time.time()
        self.confirmed = None  # True / False / None（未答复）
        self.alert_started = False
        self.alert_task: Optional[asyncio.Task] = None


class EmergencyListener:
    """全局紧急报警监听器（单例）。"""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._sessions: dict[str, EmergencySession] = {}
        self._speak_callback: Optional[Callable[[str], None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_speak_callback(self, callback: Callable[[str], None]):
        """设置 TTS 播报回调（由上层语音链路注入）。"""
        self._speak_callback = callback

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """取可用的事件循环：优先注入的 loop，其次当前线程已有循环，
        都没有就新建一个。asyncio.get_event_loop() 在无循环线程会直接抛
        RuntimeError（测试并发/异常路径实测踩中），不能裸调。"""
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def has_pending_session(self) -> bool:
        """是否存在等待用户确认（或正在执行）的报警会话。
        用于免唤醒门控放行确认答复——待机时用户回复"是/否"也必须能到达本链路。"""
        now = time.time()
        for sid, session in list(self._sessions.items()):
            if now - session.created_at > 300:
                self._cleanup(sid)
                continue
            if session.confirmed is None or session.alert_started:
                return True
        return False

    def _speak(self, text: str):
        """触发 TTS 播报。"""
        try:
            if self._speak_callback:
                self._speak_callback(text)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"紧急报警 TTS 播报失败: {e}")

    def detect(self, text: str, source: str = "unknown") -> Optional[EmergencySession]:
        """检测文本是否命中紧急关键词。命中则创建会话并返回；否则返回 None。

        此函数设计为同步调用，便于 ASR 后处理链路快速判断。
        """
        try:
            keyword = _api().match_emergency_keywords_sync(text)
            if not keyword:
                return None

            settings = _api().get_emergency_settings_sync()
            if not settings.get("enabled", True):
                return None

            # 冷却检查
            last_alert = int(settings.get("last_alert_at", 0))
            cooldown = int(settings.get("cooldown_seconds", 60))
            if time.time() - last_alert < cooldown:
                logger.bind(tag=TAG).info(
                    f"[紧急报警] 命中关键词'{keyword}'，但处于冷却期（{cooldown}s），跳过"
                )
                return None

            session_id = f"{source}_{int(time.time()*1000)}"
            session = EmergencySession(text, keyword, source)
            self._sessions[session_id] = session

            logger.bind(tag=TAG).warning(
                f"[紧急报警] 检测到关键词'{keyword}'，来自{source}，文本：{text[:50]}"
            )

            # 立即播报确认提示
            prompt = settings.get("confirm_prompt", "听到您说紧急词汇，请问您是否真的需要帮助？请回答是或否。")
            self._speak(prompt)

            # 启动超时报警任务
            timeout = int(settings.get("confirmation_timeout_seconds", 30))
            loop = self._get_loop()
            session.alert_task = loop.create_task(
                self._timeout_alert(session_id, timeout)
            )

            return session
        except Exception as e:
            logger.bind(tag=TAG).error(f"[紧急报警] 检测流程异常: {e}")
            return None

    def handle_reply(self, text: str, source: str = "unknown") -> bool:
        """处理用户在确认窗口期内的回复。

        返回 True 表示本次回复被紧急报警链路消费（不再走普通对话）。
        """
        try:
            now = time.time()
            for sid, session in list(self._sessions.items()):
                # 只处理 5 分钟内未关闭的会话
                if now - session.created_at > 300:
                    self._cleanup(sid)
                    continue
                if session.confirmed is not None:
                    continue

                lower = text.lower()
                # 否定答复
                if any(w in lower for w in ["否", "不", "不用", "没事", "误报", "误触", "cancel", "no"]):
                    session.confirmed = False
                    self._speak("已取消报警，请放心。")
                    self._cleanup(sid)
                    logger.bind(tag=TAG).info(f"[紧急报警] 用户取消报警：{text[:30]}")
                    return True

                # 肯定答复
                if any(w in lower for w in ["是", "要", "需要", "救命", "救我", "yes", "help"]):
                    session.confirmed = True
                    if session.alert_task:
                        session.alert_task.cancel()
                    self._trigger_alert(sid)
                    return True

                # 其他答复视为未确认，继续等待超时
                return True

            return False
        except Exception as e:
            logger.bind(tag=TAG).error(f"[紧急报警] 处理回复异常: {e}")
            return False

    async def _timeout_alert(self, session_id: str, timeout: int):
        """超时后自动触发报警。"""
        try:
            await asyncio.sleep(timeout)
            session = self._sessions.get(session_id)
            if not session or session.confirmed is not None or session.alert_started:
                return
            logger.bind(tag=TAG).warning(f"[紧急报警] 等待 {timeout}s 无回复，自动触发报警")
            await self._trigger_alert_async(session_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.bind(tag=TAG).error(f"[紧急报警] 超时报警任务异常: {e}")

    def _trigger_alert(self, session_id: str):
        """同步入口：触发报警。"""
        loop = self._get_loop()
        loop.create_task(self._trigger_alert_async(session_id))

    async def _trigger_alert_async(self, session_id: str):
        """执行报警：按 alert_order 依次通知紧急联系人。"""
        session = self._sessions.get(session_id)
        if not session or session.alert_started:
            return
        session.alert_started = True

        try:
            settings = _api().get_emergency_settings_sync()
            alert_order = settings.get("alert_order", ["voice", "sms", "wechat"])

            # 更新最后报警时间（冷却）
            settings["last_alert_at"] = int(time.time())
            _api().save_settings(settings)

            # 读取紧急联系人
            contacts = self._load_emergency_contacts()
            if not contacts:
                logger.bind(tag=TAG).error("[紧急报警] 未配置紧急联系人，无法报警")
                self._speak("紧急联系人未配置，请先在后台设置。")
                self._cleanup(session_id)
                return

            message = self._build_alert_message(session)
            logger.bind(tag=TAG).warning(f"[紧急报警] 开始执行报警：{message}")

            # 按顺序执行通知
            from core.utils import notify_service

            alerted = False
            for channel in alert_order:
                if channel == "voice":
                    ok, msg = await notify_service.send_voice(message[:100])
                    if ok:
                        alerted = True
                    logger.bind(tag=TAG).info(f"[紧急报警] 语音电话结果：{ok}, {msg}")
                elif channel == "sms":
                    for c in contacts:
                        phone = c.get("phone")
                        if phone:
                            ok, msg = await notify_service.send_sms(message, phone)
                            logger.bind(tag=TAG).info(f"[紧急报警] 短信→{phone}：{ok}, {msg}")
                            if ok:
                                alerted = True
                elif channel == "wechat":
                    ok, msg = await notify_service.send_wechat(message)
                    if ok:
                        alerted = True
                    logger.bind(tag=TAG).info(f"[紧急报警] 微信结果：{ok}, {msg}")

            if alerted:
                self._speak("已为您联系紧急联系人，请保持冷静。")
            else:
                self._speak("报警发送失败，请尝试其他方式联系家人。")

        except Exception as e:
            logger.bind(tag=TAG).error(f"[紧急报警] 执行报警异常: {e}")
        finally:
            self._cleanup(session_id)

    def _load_emergency_contacts(self) -> list:
        """从通知通道配置读取紧急联系人。"""
        try:
            from core.api.notification_api import CONFIG_FILE
            if not CONFIG_FILE.exists():
                return []
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            phone_cfg = cfg.get("phone", {})
            raw = phone_cfg.get("emergency_contacts", "[]")
            if isinstance(raw, str):
                return json.loads(raw) or []
            return raw or []
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[紧急报警] 读取紧急联系人失败: {e}")
            return []

    def _build_alert_message(self, session: EmergencySession) -> str:
        """生成报警内容。"""
        from datetime import datetime
        now_str = datetime.now().strftime("%m月%d日 %H:%M")
        return (
            f"紧急报警：{now_str}，设备{session.source}检测到关键词"
            f"“{session.keyword}”，用户原话：{session.text[:40]}。请立即联系确认。"
        )

    def _cleanup(self, session_id: str):
        """清理会话。"""
        session = self._sessions.pop(session_id, None)
        if session and session.alert_task and not session.alert_task.done():
            session.alert_task.cancel()


# 全局单例
emergency_listener = EmergencyListener()


def detect_emergency(text: str, source: str = "unknown") -> bool:
    """便捷函数：返回是否命中紧急关键词并触发确认流程。"""
    return emergency_listener.detect(text, source) is not None


def handle_emergency_reply(text: str, source: str = "unknown") -> bool:
    """便捷函数：处理用户在确认窗口期的回复。"""
    return emergency_listener.handle_reply(text, source)
