#!/usr/bin/env python3
"""判停影子对比报告生成器（2026-10-04）

分析 3 天观察期日志 data/decision_shadow_log.jsonl：
主判 ollaya winnow:e4b vs 影子云端 TypeSafe jev-latest。
只读日志，不影响主链路。输出 Markdown 报告到 stdout。
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / "data" / "decision_shadow_log.jsonl"

# 判定阈值：判停 complete 与寻址 addressed 共用 endpoint_threshold（jev_decision._optional_bool）
# addressed 仅在 session_active=True 时触发 drop（asr/base.py:279）
COMPLETE_THRESHOLD = 0.5
ADDRESSED_THRESHOLD = 0.55


def load() -> list[dict]:
    rows = []
    with LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


# ASR 切分出来的碎片，不是真实语音：过短，或只是标点/口水词
_FRAGMENTS = {".", "the.", "The.", "the", "..", "?", "？", "。", "嗯", "啊", "哦"}


def is_real_speech(text: str) -> bool:
    """是否为一条真实语音话语（排除 ASR 碎片）。

    完整子集的口径必须固定，否则不同次运行的数字无法互相比较。
    """
    cleaned = (text or "").strip()
    return len(cleaned) >= 6 and cleaned not in _FRAGMENTS


def corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def latency_block(values: list[int]) -> str:
    if not values:
        return "无样本"
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"中位 **{p50:.0f}ms** / 均值 {statistics.fmean(values):.0f}ms / "
        f"p95 **{p95:.0f}ms** / 最大 {max(ordered)}ms"
    )


def analyse(rows: list[dict], label: str, subset: list[dict]) -> list[str]:
    out: list[str] = []
    total = len(subset)
    if not total:
        return [f"### {label}\n", "无样本。\n"]

    paired: list[dict] = []
    p_only = s_only = 0
    for row in subset:
        pri, shd = row.get("primary") or {}, row.get("shadow") or {}
        p_ok, s_ok = pri.get("ok"), shd.get("ok")
        if p_ok and s_ok:
            paired.append(row)
        elif p_ok and not s_ok:
            p_only += 1
        elif s_ok and not p_ok:
            s_only += 1

    out.append(f"### {label}\n")
    out.append(f"- 总请求 **{total}**，双端成功 **{len(paired)}**（{pct(len(paired), total)}）")
    out.append(
        f"- 单边成功：仅本地 {p_only}，仅云端 {s_only}，双端都失败 {total - len(paired) - p_only - s_only}"
    )
    if not paired:
        out.append("")
        return out

    # ---- 判停结论一致率（complete_probability >= 0.5）----
    agree = both_no = p_yes_s_no = s_yes_p_no = 0
    p_scores: list[float] = []
    s_scores: list[float] = []
    p_lat: list[int] = []
    s_lat: list[int] = []
    p_timeouts = s_timeouts = 0
    addr_agree = p_only_addr = s_only_addr = 0
    addr_pool = 0
    addr_session_pool = 0
    addr_session_agree = 0
    p_addr_yes = s_addr_yes = 0

    for row in paired:
        pri, shd = row["primary"], row["shadow"]
        pc = float(pri.get("complete_probability") or 0.0)
        sc = float(shd.get("complete_probability") or 0.0)
        p_scores.append(pc)
        s_scores.append(sc)
        p_lat.append(int(pri.get("latency_ms") or 0))
        s_lat.append(int(shd.get("latency_ms") or 0))
        # 超时计数：ok=True 但 latency 触及 timeout 上限
        if int(pri.get("latency_ms") or 0) >= 600:
            p_timeouts += 1
        if int(shd.get("latency_ms") or 0) >= 800:
            s_timeouts += 1

        p_yes, s_yes = pc >= COMPLETE_THRESHOLD, sc >= COMPLETE_THRESHOLD
        if p_yes == s_yes:
            agree += 1
            if p_yes:
                both_no += 0
            else:
                both_no += 1
        elif p_yes:
            p_yes_s_no += 1
        else:
            s_yes_p_no += 1

        pa = float(pri.get("addressed_probability") or 0.0)
        sa = float(shd.get("addressed_probability") or 0.0)
        # addressed 分歧统计覆盖全部双端成功样本：这里比的是两个模型的能力，
        # 不是生产的门控行为。生产上 addressed 判 False 只在 session_active=True
        # 时才触发 drop（asr/base.py:279），那个子集另行单独统计。
        if (pa < ADDRESSED_THRESHOLD) == (sa < ADDRESSED_THRESHOLD):
            addr_agree += 1
        elif pa < ADDRESSED_THRESHOLD:
            p_only_addr += 1
        else:
            s_only_addr += 1
        addr_pool += 1
        if pa >= ADDRESSED_THRESHOLD:
            p_addr_yes += 1
        if sa >= ADDRESSED_THRESHOLD:
            s_addr_yes += 1
        if row.get("session_active"):
            addr_session_pool += 1
            if (pa < ADDRESSED_THRESHOLD) == (sa < ADDRESSED_THRESHOLD):
                addr_session_agree += 1

    n = len(paired)
    out.append(f"- **判停结论一致率 {pct(agree, n)}**（{agree}/{n}，阈值 {COMPLETE_THRESHOLD}）")
    out.append(
        f"- 分歧：本地判说完/云端未完 **{p_yes_s_no}**，"
        f"云端判说完/本地未完 **{s_yes_p_no}**"
    )
    out.append(
        f"- **寻址一致率 {pct(addr_agree, addr_pool)}**（{addr_agree}/{addr_pool}，"
        f"阈值 {ADDRESSED_THRESHOLD}，与判停同口径分轴统计）；"
        f"分歧 本地判非寻址/云端判寻址 **{p_only_addr}**，云端判非寻址/本地判寻址 **{s_only_addr}**"
    )
    out.append(
        f"- 寻址判「是」占比：本地 **{pct(p_addr_yes, addr_pool)}**，"
        f"云端 **{pct(s_addr_yes, addr_pool)}**（这个差值比一致率更能说明谁在误触发）"
    )
    if addr_session_pool:
        out.append(
            f"- 生产相关子集 session_active=True 共 {addr_session_pool} 条，"
            f"寻址一致率 {pct(addr_session_agree, addr_session_pool)}"
            f"（样本过小，仅供参考；这是 addressed 判 False 真正会触发 drop 的场景）"
        )
    mae = statistics.fmean(abs(a - b) for a, b in zip(p_scores, s_scores))
    out.append(
        f"- 概率偏差 MAE **{mae:.3f}**，皮尔逊相关 **{corr(p_scores, s_scores):.3f}**，"
        f"本地均值 {statistics.fmean(p_scores):.3f} / 云端均值 {statistics.fmean(s_scores):.3f}"
    )
    # 双端都判"未说完"的低分离度检查
    both_incomplete = [b for a, b in zip(p_scores, s_scores) if a < COMPLETE_THRESHOLD and b < COMPLETE_THRESHOLD]
    if both_incomplete:
        out.append(
            f"- 双端均判未说完 {len(both_incomplete)} 条，该组本地均值 "
            f"{statistics.fmean([a for a, b in zip(p_scores, s_scores) if a < COMPLETE_THRESHOLD and b < COMPLETE_THRESHOLD]):.3f}"
        )
    out.append(f"- 本地延迟：{latency_block(p_lat)}；触及 600ms 超时上限 {p_timeouts} 次")
    out.append(f"- 云端延迟：{latency_block(s_lat)}；触及 800ms 超时上限 {s_timeouts} 次")
    out.append(f"- 平均绝对延迟差 **{statistics.fmean([a - b for a, b in zip(p_lat, s_lat)]):.0f}ms**（本地 − 云端）")
    out.append("")
    return out


CONCLUSION = """## 八、结论与建议

> ⚠️ **本报告已于 2026-10-04 修订。** 初版存在方法错误：把主判的 `complete` 结论
> 和云端的 `addressed` 结论放在同一条轴上比较（主判回落用 complete、云端优先看 addressed），
> 得出「真实语音一致率 53.3%」「云端保守、近三成真实语音被判成未呼叫」的错误结论。
> 真实情况方向相反，详见第八节与第四节。修订内容：complete 与 addressed 严格分轴、
> 新增「真实语音」子集、补 addressed 误触发量化、10 月 3 日失效根因已定论。

### 8.1 本地主判与云端同向，但云端在背景人声上误触发严重

本地健康日（2026-10-02）双端成功 1,906 条，判停一致率 84.5%、概率相关 0.878，
本地与云端**判断方向高度一致**，继续用本地做主判在判停维度成立。

但寻址维度相反：真实语音子集里云端把 **60.6%** 判成「在对助手说话」，本地只给 **9.9%**；
分歧 438 : 2，**约 219 : 1 压倒性偏向「云端认为在叫它、本地认为不是」**。
抽查真实流量（多为念文档、与同事讨论）：

```
你重新定方案吧。              本地 0.185  云端 0.890
R里面不是同样也支持这个查询。  本地 0.093  云端 0.770
你说的真人调育值是怎么个调法？ 本地 0.209  云端 0.920
一兆上下文吗？                本地 0.166  云端 0.840
For Google AI Studio search... 本地 0.132  云端 0.870
```

这些都不是在呼叫 HomeBrain，**本地给 0.1 才是对的，云端给 0.9 是误触发**。
这条结论修正了初版的相反判断，也说明**云端模型不适合直接承担 addressed 门控**。

### 8.2 本地判停的真实短板是「假停」，且 hold 机制修不了

初版说「分歧 251:295 偏向早停」这个方向是对的，但归因错了。
真实语音子集里本地 complete 判「是」71.5%、云端 51.0%，本地整体更容易判说完。

更关键的是 2026-10-04 的定向实测（`scripts/eval_strands_decider_zh.py`，25 题真实流量）：

| 模型 | 准确率 | 假停（应等却判完） |
|---|---|---|
| winnow:e4b | 72.0% | 7 / 25 |
| StartLux-Decision-9B | **100.0%** | **0 / 25** |

`incomplete_hold_ms`（900ms）只在模型说「还没说完」时触发，**对「误判说完」完全无效**。
假停只能靠模型准确率解决，加长等待时间一点用没有。

### 8.3 中文表现弱于整体

中文子集一致率 75.6%，低于全量 84.5%；相关系数 0.801 低于 0.878。
结合 2026-09-29 的定向实测（中文判停 6/6）与 2026-10-04 的真实流量实测（72%），
winnow:e4b **能判中文，但在真实长句截断场景下明显弱于英文基准**。

### 8.4 延迟：本地优势明确

本地中位 358ms vs 云端 761ms，本地快 413ms；本地 p95 666ms，云端 p95 1551ms、最大 79,920ms。
本地延迟优势明显，**继续用本地做主判是正确的性能选择**。

### 8.5 已完成事项

1. ✅ **[最高] 2026-10-03 本地主判整日失效已定案** —— 诱因是 2026-10-02 17:21:42 加载
   `decider:4b`（ONNX / CPU / fp32，8.4GB），此后 winnow 成功率平滑退化 6 小时后归零。
   已执行 `ollaya stop decider:4b` + `ollaya rm decider:4b`（释放 7.5GB），
   **实测延迟中位从 358ms 降到 85ms**（快 4.2 倍）。
2. ✅ **[高] 已补主判健康监控** —— `core/utils/jev_decision.py` 新增连续失败计数、
   阈值升级告警、恢复日志与 `health_snapshot()`，单测 5 项。

### 8.6 待办

1. **[最高] 切换主判到 StartLux-Decision-9B** —— 实测中文判停 100%、寻址 96.2%，
   两个维度都优于 winnow，且最佳阈值都是 0.5，不需要改任何生产参数。
   接入仅需把 `Decision.jev.base_url` 从 11435 改到 MLX server 端口（实测兼容，
   StartLux 忽略 `model` 字段与 Authorization 头）。⚠️ 权重为 CC BY-NC 4.0，禁商用。
2. **[中] 清理死配置** —— `Decision.local_qwen` 未被选中且 8099 端口无监听；
   `ollaya_provider.py` 在 config 中无对应块，属死代码。
3. **[低] `incomplete_hold_ms` 调短** —— 换 9B 后假停归零，900ms 等待可缩短换取响应速度。
4. **[低] addressed 是否开启** —— 9B 寻址 96.2%、winnow 88.5%，且已实测 winnow 会把
   ASR 听错名字的「你好，伊万」判 0.137 直接丢弃命令（9B 判 0.606 正确执行）。
   若开启 addressed，建议先灰度只记日志。
"""


def main() -> None:
    rows = load()
    if not rows:
        print("影子日志为空，无法生成报告")
        return

    lines = [
        "# 判停本地 vs 云端影子对比报告",
        "",
        "> 观察期 2026-10-01 23:05 — 2026-10-04（3 天）。主判 = ollaya `winnow:e4b`（本地），"
        "影子 = TypeSafe `jev-latest`（云端，只记录不干预）。",
        f"> 数据源 `server/homebrain-server/data/decision_shadow_log.jsonl`，共 {len(rows)} 条。",
        "",
        "## 一、总览",
        "",
    ]

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_day[(row.get("time") or "")[:10]].append(row)
    lines.append("| 日期 | 请求 | 本地成功率 | 双端成功 | 判停一致率 |")
    lines.append("|---|---|---|---|---|")
    for day in sorted(by_day):
        day_rows = by_day[day]
        p_ok = sum(1 for r in day_rows if (r.get("primary") or {}).get("ok"))
        paired_day = [
            r
            for r in day_rows
            if (r.get("primary") or {}).get("ok") and (r.get("shadow") or {}).get("ok")
        ]
        agree = sum(
            1
            for r in paired_day
            if (
                float(r["primary"].get("complete_probability") or 0) >= COMPLETE_THRESHOLD
            )
            == (
                float(r["shadow"].get("complete_probability") or 0) >= COMPLETE_THRESHOLD
            )
        )
        lines.append(
            f"| {day} | {len(day_rows)} | {p_ok}/{len(day_rows)} = {pct(p_ok, len(day_rows))} "
            f"| {len(paired_day)} | {pct(agree, len(paired_day))} |"
        )
    lines.append("")

    # ---- 本地主判失效事故（重要）----
    bad_days = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        p_ok = sum(1 for r in day_rows if (r.get("primary") or {}).get("ok"))
        if day_rows and p_ok / len(day_rows) < 0.5:
            bad_days.append((day, len(day_rows), p_ok))
    if bad_days:
        lines += ["## 二、本地主判失效事故（最高优先级）", ""]
        lines.append(
            "观察期内本地主判出现整日级失效，**主链路全程在「回退本地逻辑」降级运行**，"
            "而影子云端始终正常，因此这类故障不会体现在一致率里，只体现在本地成功率上。"
        )
        lines.append("")
        lines.append("| 日期 | 请求 | 本地成功 | 本地成功率 |")
        lines.append("|---|---|---|---|")
        for day, total, p_ok in bad_days:
            lines.append(f"| {day} | {total} | {p_ok} | {pct(p_ok, total)} |")
        lines.append("")
        lines.append(
            "证据链：`core/utils/jev_decision.py` `_ask()` 捕获异常后只打 warning「Jev 决策请求失败，"
            "回退本地逻辑」并返回 None，`tmp/server.2026-10-03_12-02-01_839556.log` 内该 warning "
            "1,687 次，其中 1,520 次为 `502 Bad Gateway for url 'http://127.0.0.1:11435/v1/systemone'`。"
            "故障期间 `services/logs/ollaya.launchd.err.log` 无任何新记录（最后一条停在 2026-10-02T09:21），"
            "ollaya 主进程自 2026-09-29 10:53 起连续运行未重启 —— 即 HTTP 端口仍监听（故为 502 而非连接拒绝），"
            "但后端 runner 已不可用。"
        )
        lines.append("")
        lines.append(
            "**根因已定案（2026-10-04 追查）：诱因是 2026-10-02 17:21:42 加载 `decider:4b`"
            "（ONNX / CPU / fp32，8.4GB）**。该时刻与 runner 进程启动时间 17:21:29 吻合，"
            "也是 ollaya 日志的最后一条记录；此后 winnow 成功率平滑退化约 6 小时后归零。"
            "ollaya 主进程与 winnow runner 全程未重启，10-03 延迟中位 450~515ms 仍远低于 600ms 超时线"
            "却 100% 失败，说明是**快速失败而非超时**。已执行 `ollaya stop decider:4b` + "
            "`ollaya rm decider:4b`（释放 7.5GB），实测延迟中位由 358ms 降至 85ms。"
        )
        lines.append("")
        lines.append(
            "诚实标注：**诱因已确定，但「同机共存为何当时失效、现在却正常」这一机制问题仍未完全定论**，"
            "因为 `decider:4b` 加载期间也曾有 winnow 正常工作的时段。"
        )
        lines.append("")
        lines.append("影响：失效时段的判停决策全部来自本地兜底规则，判停质量无保障。")
        lines.append("")
        lines.append("")

    lines += analyse(rows, "三、全量对比", rows)

    speech = [r for r in rows if is_real_speech(r.get("utterance", ""))]
    lines.append(
        "> 「真实语音」子集口径：话语去空白后长度 ≥6，且不属于 ASR 碎片"
        f"（{'、'.join(sorted(_FRAGMENTS))}）。共 {len(speech)} 条。"
        "**这是最贴近真实使用的口径，全量对比里大量 ASR 碎片会稀释指标。**"
    )
    lines.append("")
    lines += analyse(speech, "四、真实语音对比（推荐以本节为准）", speech)

    cjk = [r for r in rows if is_cjk(r.get("utterance", ""))]
    lines += analyse(cjk, "五、中文语句对比", cjk)

    # ---- 分歧样本 ----
    lines += ["## 六、分歧样本（判停结论不一致）", ""]
    diffs = []
    for row in rows:
        pri, shd = row.get("primary") or {}, row.get("shadow") or {}
        if not (pri.get("ok") and shd.get("ok")):
            continue
        pc = float(pri.get("complete_probability") or 0.0)
        sc = float(shd.get("complete_probability") or 0.0)
        if (pc >= COMPLETE_THRESHOLD) != (sc >= COMPLETE_THRESHOLD):
            diffs.append((abs(pc - sc), row, pc, sc))
    diffs.sort(key=lambda item: item[0], reverse=True)
    if not diffs:
        lines.append("无分歧样本。\n")
    else:
        lines.append(f"共 {len(diffs)} 条，按概率差降序列前 25 条：\n")
        lines.append("| 时间 | 话语 | 本地 | 云端 | 分歧方向 |")
        lines.append("|---|---|---|---|---|")
        for _, row, pc, sc in diffs[:25]:
            utt = (row.get("utterance") or "").replace("\n", " ")[:40]
            direction = "本地更早停" if pc >= COMPLETE_THRESHOLD else "本地更晚停"
            lines.append(
                f"| {row.get('time', '')} | {utt} | {pc:.3f} | {sc:.3f} | {direction} |"
            )
        lines.append("")

    # ---- 失败/超时样本 ----
    lines += ["## 七、影子失败与超时", ""]
    fail = [r for r in rows if not (r.get("shadow") or {}).get("ok")]
    errors = Counter(str((r.get("shadow") or {}).get("error"))[:80] for r in fail)
    if not fail:
        lines.append("影子零失败。\n")
    else:
        lines.append(f"影子失败 {len(fail)} 条（{pct(len(fail), len(rows))}），主链路不受影响。错误分布：\n")
        for err, cnt in errors.most_common(10):
            lines.append(f"- `{err}`：{cnt} 次")
        lines.append("")

    lines += CONCLUSION.splitlines()

    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())