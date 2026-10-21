"""网页端麦克风看门狗事件处理器（2026-10-16 主人批准新增）

【为什么要这个处理器】
网页端（LED 星球 / 数字星球）从 2026-10-13 起会在麦克风异常时发
`{"type": "mic_watchdog_event", ...}`，注释里写的是「上报给服务端日志便于远程观察」。
但服务端注册表里**没有这个类型**，消息落到 textMessageProcessor 的兜底分支，
被打成「收到未知类型消息」的 error（还是两遍）——上报等于没上报，
排查时只能靠主人在现场描述「喊不醒了」。

本处理器把事件转成一条**结构化中文日志**，并附上服务端自己算的音频活性快照
（core/utils/mic_liveness.py 的实时率/全零时长），两边对读就能判断：
  · 页面说掉速、服务端也低 → 音频真的供不上来（设备/USB/浏览器采集侧）
  · 页面说掉速、服务端正常 → 页面侧统计或发送链路问题

【不改行为】
只打日志。不触碰 VAD/ASR/判停，也不回消息给页面。
"""

from typing import Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.mic_liveness import MicLiveness

TAG = __name__

# 各 action 的中文说明。未知 action 不丢，按原文打出来（页面以后加动作也不会漏）。
_ACTION_TEXT = {
    "watchdog_restart": "回调中断超3秒，页面已重新申请麦克风",
    "watchdog_restart_failed": "回调中断后重新申请麦克风失败",
    "retry_needed": "开麦失败，已标记待恢复",
    "rate_low_rebuild": "音频实时率不达标，页面自动重建麦克风",
    "rate_rebuild_ok": "自动重建麦克风成功",
    "rate_rebuild_failed": "自动重建麦克风失败",
    # [2026-10-17] 页面周期性上报「页面侧速率 + 音频图时钟 + 流轨道状态」，
    # 用于远程区分「音频图被节流」与「输入流供数不足」两类根因。
    "rate_report": "周期上报音频实时率（含音频图时钟与流轨道状态）",
    # [2026-10-17] 干净并行采集自测：在退化中的页面旁边新起一条独立采集链路，
    # 用来区分「这条链路/这个 AudioContext 坏了」与「整个 Chrome 实例坏了」
    "clean_capture_test": "干净并行采集自测结果（根因取证）",
    # [2026-10-17] 连续重建救不回速率 → 页面自动刷新（把主人手动刷新自动化）
    "rate_reload_page": "连续重建无效，页面自动刷新",
    # [2026-10-17] 根因修复：只重建麦克风流救不回来时，连 AudioContext 一起重建
    "ctx_reset_rebuild": "重建麦克风流无效，连 AudioContext 一起重建",
}


class MicWatchdogMessageHandler(TextMessageHandler):
    """网页端麦克风看门狗/实时率自愈事件 → 结构化日志"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MIC_WATCHDOG_EVENT

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        try:
            action = str(msg_json.get("action") or "unknown")
            desc = _ACTION_TEXT.get(action, action)
            detail = {k: v for k, v in msg_json.items() if k not in ("type", "action")}
            snap = MicLiveness.snapshot(conn)
            dev = (getattr(conn, "hello_device_id", "")
                   or getattr(conn, "device_id", "") or "?")
            conn.logger.bind(tag=TAG).warning(
                f"[麦克风看门狗] {desc} device={dev} "
                f"页面={detail} 服务端={snap}"
            )
        except Exception:
            # 观测代码绝不能反过来影响主链路：连 logger 都是坏的也照样吞掉。
            # （此处再调 logger 兜底同样会抛，所以只能静默）
            pass
