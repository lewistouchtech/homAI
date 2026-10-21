# J1：L0 快捷工具直答——时间/日期/星期/农历零 LLM 直答
# 本地取系统时间 + 模板回复，命中即返回文本，跳过 LLM/工具调用全链路
# 由 core/chat/chat_engine.py 在 L1 规则层未命中后调用
from __future__ import annotations

import re
from datetime import datetime

from core.utils.current_time import get_current_lunar_date, get_current_weekday

# 排除词：这些场景必须走 LLM+工具（如"定个闹钟时间是..."）
_EXCLUDE_RE = re.compile(r"闹钟|提醒|定时|设置|天气|广播|倒计时")

# 时间类：几点/几点钟/现在时间（锚定短句，避免误伤长句）
_TIME_RE = re.compile(r"^.{0,8}(几点|几点钟|现在时间|当前时间|什么时间).{0,4}[?？]?$")
# 农历类
_LUNAR_RE = re.compile(
    r"^.{0,2}(农历|阴历|黄历).{0,4}(是多少|多少|几|日期|)[?？]?$|^今天的农历[?？]?$"
)
# 星期类
_WEEKDAY_RE = re.compile(r"^.{0,6}(星期几|周几|礼拜几).{0,3}[?？]?$")
# 日期类
_DATE_RE = re.compile(r"^(今天|现在)?(的)?(日期|几号|几日|几月几号|几月几日|号数)(是多少)?(呀|啊|呢)?[?？]?$")


def _format_time(now) -> str:
    """把 24 小时制时间转成口语化表达，如：现在是下午3点25分"""
    h, m = now.hour, now.minute
    if h < 5:
        period = "凌晨"
    elif h < 9:
        period = "早上"
    elif h < 12:
        period = "上午"
    elif h < 13:
        period = "中午"
    elif h < 18:
        period = "下午"
    else:
        period = "晚上"
    hh = h - 12 if h > 12 else (12 if h == 0 else h)
    if m == 0:
        return f"现在是{period}{hh}点整"
    if m < 10:
        return f"现在是{period}{hh}点零{m}分"
    return f"现在是{period}{hh}点{m}分"


def quick_tool_reply(query: str | None) -> str | None:
    """快捷工具直答：命中返回回复文本，未命中返回 None（继续走 LLM）"""
    if not query:
        return None
    q = query.strip()
    if not q or len(q) > 20:
        return None
    if _EXCLUDE_RE.search(q):
        return None

    now = datetime.now()

    if _TIME_RE.search(q):
        return _format_time(now)

    if _LUNAR_RE.search(q):
        return f"今天是{get_current_lunar_date()}"

    if _WEEKDAY_RE.search(q):
        return f"今天是{get_current_weekday()}"

    if _DATE_RE.search(q):
        return f"今天是{now.year}年{now.month}月{now.day}日，{get_current_weekday()}"

    return None
