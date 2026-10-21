"""危险操作确认闸门（服务端强制版）

背景
----
给主人自己发的通知（短信/语音/微信/邮件通知）不需要确认——那些通道的收件人
固定是主人自己，LLM 误触发最坏后果只是主人手机响一下。

真正需要闸门的是「发给第三方、不可逆」的两类操作：
  - mac_message（iMessage，target 必填，可发任意第三方）
  - mail_action 的 send-email（走 apple-mail-mcp，可发任意地址）

为什么改造原有的 confirmed 机制
-------------------------------
mac_control 原本已有 ``confirmed: bool = False`` 参数 + ``_confirm_needed()``，
但那是「君子协定」：服务端不校验，LLM 只要直接传 ``confirmed=true``
就能把消息发出去，闸门形同虚设。

本模块的职责是把「协定」变成「强制」：
  1. 第一次调用（confirmed=false）时，服务端记住这次待确认的动作
     （工具名 + 关键参数 + 发生在第几轮对话），并向前端推确认条
  2. 第二次调用（confirmed=true）时，服务端校验：
       · 存在对应的待确认动作
       · 工具名与关键参数完全一致（防止 LLM 中途换内容）
       · 发生在**更晚的一轮对话**（证明主人中间真的说过话）
       · 主人在网页端没有点过「取消」
     全部满足才放行，否则拒绝执行
  3. 网页端确认条点击 → :func:`resolve_by_request_id`，与语音路径并存

两条确认路径（2026-10-04 主人裁决）
------------------------------------
  - 看：网页端确认条点「发送」/「取消」
  - 说：下一轮直接说「发吧」「取消」，由 LLM 再次调用带 confirmed=true，
        服务端的轮次校验保证这中间隔了真实的一轮对话

轮次机制（防「同一轮绕过」）
----------------------------
:func:`mark_turn` 由意图层每轮对话调用一次。待确认动作记录创建时的轮次，
校验时要求 ``created_turn < current_turn``。这样即使 LLM 在同一轮里连续
调用两次（第一次 confirmed=false、第二次 confirmed=true），也会因为轮次
没推进而被拒绝——只有主人真的开口说过话才放行。
"""

import json
import time
import uuid
from typing import Any, Callable, Optional, Tuple

from config.logger import setup_logging

logger = setup_logging()

# 待确认动作有效期（秒）。超时自动作废，避免主人几小时后随口一句「发送」
# 就把一条早就过期的消息真发出去。
CONFIRM_TTL = 180

# 归一化后的动作描述，用作前端确认条正文与语音播报内容
DEFAULT_DETAIL = "该操作"


def _get_state(conn) -> dict:
    """取连接上的确认闸门状态（懒初始化，避免改动 core/connection.py）。

    ``core/connection.py`` 属稳定模块，此处不改它，改为按需挂属性。
    """
    state = getattr(conn, "_confirm_gate_state", None)
    if state is None:
        state = {"pending": None, "turn": 0}
        conn._confirm_gate_state = state
    return state


def mark_turn(conn) -> int:
    """标记一次新的用户对话轮次。由意图层每轮调用一次。返回当前轮次号。"""
    state = _get_state(conn)
    state["turn"] += 1
    return state["turn"]


def current_turn(conn) -> int:
    """当前对话轮次号。"""
    return _get_state(conn)["turn"]


def _pending(conn) -> Optional[dict]:
    return _get_state(conn)["pending"]


def clear(conn) -> None:
    """清除待确认动作（会话结束、超时、工具内主动作废时调用）。"""
    _get_state(conn)["pending"] = None


async def request_confirm(
    conn,
    tool: str,
    key: str,
    detail: str = DEFAULT_DETAIL,
    on_web_approve: Optional[Callable] = None,
) -> None:
    """登记一次待确认动作，并向前端推确认条。

    :param tool:    工具名，如 ``mac_message``
    :param key:     关键参数指纹，如 ``target|message``；第二次调用必须完全一致
    :param detail:  展示/播报用的动作描述
    :param on_web_approve: 网页端点「发送」时的回调（可选，异步或同步均可）。
                           语音路径不走这里，由 :func:`consume` 直接执行。
    """
    state = _get_state(conn)
    old = state["pending"]
    if old and not old.get("consumed"):
        logger.bind(tag=tag()).info(
            f"确认闸门：旧的待确认动作被新动作取代 tool={old.get('tool')}"
        )
        _push_tts_hint(conn, old.get("detail", ""), replaced=True)

    request_id = uuid.uuid4().hex[:12]
    state["pending"] = {
        "request_id": request_id,
        "tool": tool,
        "key": key,
        "detail": detail,
        "created_turn": state["turn"],
        "created_at": time.time(),
        "declined": False,
        "consumed": False,
        "on_web_approve": on_web_approve,
    }
    logger.bind(tag=tag()).info(f"确认闸门：已登记待确认动作 tool={tool} key={key}")
    await _push_confirm_bar(conn, request_id, tool, detail)


async def _push_confirm_bar(conn, request_id: str, tool: str, detail: str) -> None:
    """向前端推确认条消息。推送失败不影响语音确认路径，只记警告。"""
    try:
        payload = {
            "type": "confirm",
            "request_id": request_id,
            "session_id": getattr(conn, "session_id", None),
            "tool": tool,
            "detail": detail,
            "ttl": CONFIRM_TTL,
        }
        await conn.websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — 前端不可用时不能连带阻断语音路径
        logger.bind(tag=tag()).warning(f"确认闸门：推送网页确认条失败（语音路径仍可用）: {e}")


async def _push_tts_hint(conn, detail: str, replaced: bool = False) -> None:
    """网页端确认条出现/被取代时补一条轻量提示，避免主人以为没反应。"""
    if replaced:
        text = "上一个待确认的操作已作废"
    else:
        return
    try:
        from core.handle.sendAudioHandle import send_tts_message

        await send_tts_message(conn, "TTS_FINISH", text)
    except Exception as e:  # noqa: BLE001
        logger.bind(tag=tag()).debug(f"确认闸门：补发提示失败（可忽略）: {e}")


async def consume(conn, tool: str, key: str) -> Tuple[bool, str]:
    """校验并消费一次待确认动作。

    返回 ``(是否放行, 说明)``。放行条件见模块文档，全部满足才会返回 True。

    :param tool: 本次要执行的工具名，必须与登记时一致
    :param key:  关键参数指纹，必须与登记时完全一致
    """
    state = _get_state(conn)
    pending = state["pending"]

    if not pending:
        return False, "未找到待确认动作：本次请求没有先向用户复述并征求确认，拒绝执行"

    if pending.get("consumed"):
        state["pending"] = None
        return False, "该待确认动作已被使用过一次，拒绝重复执行"

    if time.time() - pending["created_at"] > CONFIRM_TTL:
        state["pending"] = None
        return False, f"确认已超时（超过 {CONFIRM_TTL} 秒），操作已作废，请重新发起"

    if pending["tool"] != tool:
        return False, (
            f"确认对象不一致：待确认的是 {pending['tool']}，本次调用是 {tool}，拒绝执行"
        )

    if pending["key"] != key:
        return False, (
            f"收件人与用户确认时的不一致（确认时是「{pending['key']}」，"
            f"本次是「{key}」），为安全起见拒绝执行。"
            "请先向用户复述新的收件人并再次征求确认。"
        )

    if pending.get("declined"):
        state["pending"] = None
        return False, "用户已明确取消该操作，拒绝执行"

    if pending["created_turn"] >= state["turn"]:
        return False, (
            "本轮对话内重复调用，用户尚未开口确认，拒绝执行"
            "（请先向用户复述并等待用户回复）"
        )

    state["pending"] = None
    return True, "已确认"


async def resolve_by_request_id(
    conn, request_id: str, approved: bool
) -> Tuple[bool, str]:
    """网页端确认条点击入口。

    :param request_id: 前端回传的请求号，必须与登记时一致（防迟到/伪造消息）
    :param approved:   True=点「发送」，False=点「取消」
    """
    state = _get_state(conn)
    pending = state["pending"]

    if not pending:
        return False, "收到网页确认回复但没有待确认动作（可能已超时或已处理）"

    if pending["request_id"] != request_id:
        logger.bind(tag=tag()).warning(
            f"确认闸门：request_id 不匹配，回传={request_id}，当前={pending['request_id']}"
        )
        return False, "确认请求已过期，请重新发起操作"

    if time.time() - pending["created_at"] > CONFIRM_TTL:
        state["pending"] = None
        return False, "确认已超时，操作已作废，请重新发起"

    if not approved:
        pending["declined"] = True
        state["pending"] = None
        logger.bind(tag=tag()).info("确认闸门：用户在网页端取消了操作")
        return True, "用户已取消该操作"

    # 网页端已点「发送」：放行并执行回调（若有）
    state["pending"] = None
    cb = pending.get("on_web_approve")
    if cb:
        try:
            result = cb()
            if hasattr(result, "__await__"):
                await result
        except Exception as e:  # noqa: BLE001
            logger.bind(tag=tag()).exception(f"确认闸门：网页确认后执行失败: {e}")
            return False, f"执行失败：{type(e).__name__}: {e}"
    return True, "已确认"


def get_pending_detail(conn) -> Optional[str]:
    """给意图层/提示词用：当前是否有待确认动作及其描述。"""
    pending = _pending(conn)
    if not pending:
        return None
    if time.time() - pending["created_at"] > CONFIRM_TTL:
        return None
    return pending["detail"]


def tag() -> str:
    return __name__