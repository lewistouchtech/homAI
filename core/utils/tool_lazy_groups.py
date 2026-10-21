"""低频大体积工具组按需暴露（2026-10-11）。

背景：线上每个 LLM 请求带 73 个工具定义共 11.2 万字符（约 3.6 万输入 token），
其中飞书 MCP 的日历/任务/群消息/通讯录 4 组就占 7.9 万字符（单个 `calendar_v4_calendarEvent_patch`
就 1.5 万字符）。这些工具历史 1468 轮对话里**一次都没被调用过**，却每一轮都要预填，
缓存未命中时首 token 要 3~4 秒。

做法：只有当用户原话（及最近几句对话）出现该领域的词时才携带这组工具。
与「全局按场景筛选」不同——那个筛选器历史召回率只有 46~53%（会漏掉 web_search 等常用工具），
而这里只针对明确的低频领域、关键词取宽，漏的代价是用户说「帮我发飞书」时这一句没带上，
所以宽进：词表里有「飞书/日历/任务/群/消息/联系人…」这类宽词，误开只是回到现状的成本。

纯函数，无副作用，便于单测。
"""
from typing import Any, Dict, Iterable, List, Tuple

# 组名 → (工具名前缀, 触发关键词)
LAZY_GROUPS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "feishu": (
        ("calendar_v4_", "task_v2_", "im_v1_", "contact_v3_"),
        ("飞书", "lark", "日历", "日程", "会议", "开会", "参会", "邀请", "预约",
         "空闲", "忙闲", "待办", "任务", "提醒", "群", "消息", "通讯录", "联系人",
         "同事", "共享", "发给", "转发"),
    ),
}


def _tool_name(f: Dict[str, Any]) -> str:
    return str(((f.get("function") if isinstance(f.get("function"), dict) else f) or {}).get("name") or "")


def select_lazy_tools(
    functions: List[Dict[str, Any]],
    text_pool: str,
    opened_groups: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """返回 (保留的工具列表, 本轮打开的组, 被暂时去掉的组)。

    text_pool：用户原话 + 最近几句对话拼成的字符串（跟进语如「改成明天」也能命中）。
    opened_groups：本轮已经打开的组（工具调用后续递归轮次不得中途把工具收走）。
    """
    pool = (text_pool or "").lower()
    opened = set(opened_groups)
    open_now: List[str] = []
    closed: List[str] = []
    drop_prefixes: List[str] = []
    for name, (prefixes, keywords) in LAZY_GROUPS.items():
        if not any(_tool_name(f).startswith(prefixes) for f in functions):
            continue  # 当前没有这组工具，无需处理
        if name in opened or any(k.lower() in pool for k in keywords):
            open_now.append(name)
        else:
            closed.append(name)
            drop_prefixes.extend(prefixes)
    if not drop_prefixes:
        return functions, open_now, closed
    kept = [f for f in functions if not _tool_name(f).startswith(tuple(drop_prefixes))]
    return kept, open_now, closed
