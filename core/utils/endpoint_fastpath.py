"""A3：JEV 按需跳过 —— 句尾规则快速通道。

## 为什么需要

A1+A2 把判停从 800ms 降到 ~400ms，但剩下的瓶颈是 **JEV 云端往返 735ms**。
这一步省不掉就没法再快，而它是纯网络等待。

## 什么时候可以跳过

JEV 的职责是判断「这句话说完没」。但有一类句子**在语法上已经不可能是半句话**：
以收束语气词结尾的完整短句（"…吧"、"…好了"、"谢谢"），
或极短的祈使指令（"开灯"、"几点了"）。这类句子走云端 JEV 几乎必然返回
complete，白白等 735ms。

## 设计原则：宁可漏跳，绝不误跳

误跳的代价是把半句话送给 LLM（主人 2026-10-04 抱怨过"你怎么喊了我这么多遍"）；
漏跳的代价只是慢 735ms。两者不对称，所以规则一律保守：

1. 长度超限不跳（长句可能还在列举）
2. 命中「悬挂结构」不跳（以「把/将/帮我/给」等介词结尾 = 宾语还没说出来）
3. 拿不准一律返回 None，交给 JEV

配置开关（`Decision.jev.fastpath_*`，全部可单独关）：
- `fastpath_enabled: true`
- `fastpath_max_chars: 14`  超过这个长度一律不跳
- `fastpath_imperative_min_chars: 2` 祈使句下限
"""

from __future__ import annotations

import re
from typing import Optional

# 以这些词结尾 = 宾语/从句还没说出来，句子必然未完成
_HANGING_TAIL = (
    "把",
    "将",
    "帮",
    "替",
    "给",
    "跟",
    "向",
    "对",
    "用",
    "在",
    "从",
    "和",
    "与",
    "要",
    "想",
    "需",
    "请",
    "让",
    "被",
    "把儿",
)

# 句末语气词：出现即强烈提示句子已收束
_TRAILING_PARTICLES = (
    "吧",
    "啦",
    "呢",
    "啊",
    "哦",
    "嘛",
    "咯",
    "哟",
    "噢",
)

# 收束词：整句结束语
_CLOSERS = (
    "谢谢",
    "谢了",
    "好了",
    "完事",
    "完毕",
    "结束",
    "搞定",
    "完成",
    "没问题",
    "可以了",
    "就这样",
    "就这些",
    "搞定啦",
    "好了吧",
    "做完了",
    "处理完了",
    "弄好了",
    "搞定了",
    "结束了",
)

# 短祈使指令：动词开头 + 总长够短 -> 必然是完整指令
_IMPERATIVE_VERBS = (
    "开",
    "关",
    "查",
    "看",
    "搜",
    "播",
    "放",
    "调",
    "打",
    "换",
    "停",
    "锁",
    "关掉",
    "打开",
    "关上",
    "启动",
    "停止",
    "查看",
    "搜索",
    "播放",
    "暂停",
    "切到",
    "换成",
    "调高",
    "调低",
    "调大",
    "调小",
)

_TIME_QUERIES = (
    "几点了",
    "几时了",
    "什么时候",
    "今天几号",
    "今天星期几",
    "星期几",
    "几月几号",
    "报时",
)

_PUNCT = "，。！？、,.!?;；:：…~～ 　\"'“”‘’()（）【】"

_MIN_CONTENT_CHARS = 2


def _normalize(text: str) -> str:
    return re.sub(rf"[{re.escape(_PUNCT)}]+", "", (text or "").strip().lower())


def is_definitely_complete(
    text: str,
    max_chars: int = 14,
    imperative_min_chars: int = 2,
) -> Optional[str]:
    """高置信「这句已说完」判定。

    返回命中的规则名（便于日志排查），拿不准返回 None —— 调用方应继续走 JEV。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    t = _normalize(raw)
    if not t:
        return None
    # 太短：「嗯」「啊」这种是应声，不是句子，交给 JEV
    if len(t) < _MIN_CONTENT_CHARS:
        return None
    # 太长：可能还在列举或从句未完
    if len(t) > max_chars:
        return None

    # 悬挂结构优先否决：哪怕它同时以语气词结尾，宾语缺失也说明没说完
    for w in _HANGING_TAIL:
        if t.endswith(w):
            return None

    # 规则 1：句末语气词
    for p in _TRAILING_PARTICLES:
        if t.endswith(p):
            # 「好吧」「行吧」这类也命中，符合预期
            return "trailing_particle"

    # 规则 2：收束词
    for c in _CLOSERS:
        if t.endswith(c):
            return "closer"

    # 规则 3：时间类问答（必然说完）
    for q in _TIME_QUERIES:
        if q in t:
            return "time_query"

    # 规则 4：短祈使指令
    if imperative_min_chars <= len(t) <= max_chars:
        for v in _IMPERATIVE_VERBS:
            if t.startswith(v):
                return "imperative"

    return None


class EndpointFastPath:
    """把配置读好缓存起来，供 silero 预判反复调用。"""

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("Decision", {}).get("jev", {}) or {}
        self.enabled = bool(cfg.get("fastpath_enabled", True))
        self.max_chars = int(cfg.get("fastpath_max_chars", 14))
        self.imperative_min_chars = int(cfg.get("fastpath_imperative_min_chars", 2))
        self.stats = {"hit": 0, "miss": 0}

    def check(self, text: str) -> Optional[str]:
        if not self.enabled:
            return None
        rule = is_definitely_complete(
            text,
            max_chars=self.max_chars,
            imperative_min_chars=self.imperative_min_chars,
        )
        self.stats["hit" if rule else "miss"] += 1
        return rule
