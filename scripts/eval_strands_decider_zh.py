#!/usr/bin/env python3
"""Strands Decider 2B 中文判停对照实测（2026-10-04）

动机：Strands Decider 2B（Amazon Strands Agents 团队开源）宣称 JevBench 0.723，
但仓库零中文语料、零中文评测。本脚本用我方真实中文判停测试集直接打
/v1/systemone 端点，验证「中文到底能不能用」。

用法：
    # 1. 起服务
    strands-decider serve <model-path> --port 8000
    # 2. 跑测试
    python eval_strands_decider_zh.py --endpoint http://127.0.0.1:8000

测试集来源：
- A 组：2026-09-29 我方 winnow:e4b 中文判停 6 题（完整句 vs 残句分离度）
- B 组：本次影子日志真实流量抽取的中文残句/完整句（对应 7.2 提前截断风险）
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

# 判定阈值，与我方生产配置一致（config.yaml Decision.ollaya.endpoint）
COMPLETE_THRESHOLD = 0.5

# A 组：2026-09-29 已实测 winnow:e4b 中文判停 6/6 的题（完整句 vs 残句）
SET_A = [
    ("把客厅的灯关掉", True, "完整祈使句"),
    ("我明天早上八点提醒我出门", True, "完整陈述句"),
    ("帮我把卧室的", False, "残句·中途截断"),
    ("那我可以", False, "残句·中途截断"),
    ("我想让你帮我查一下明天的", False, "残句·中途截断"),
    ("然后呢我觉得那个东西", False, "残句·中途截断"),
]

# B 组：从影子日志真实流量抽取的中文话语，标注是否为可安全提交的完整句
# 依据 2026-10-04 报告 7.2：本地 winnow:e4b 在长句中途截断上系统性偏早停
SET_B = [
    ("速度最快，海王星的公转速度最慢，只要掌握行星三大运动定律以及万有引力定律的人都能", False, "长句截断"),
    ("太空望远镜专门盯着一片天空看恒星亮度变化。原理很简单，当行星从恒星前面经过零日会", False, "长句截断"),
    ("大小和地球差不多，位于移居带内不太热，也不太冷，液态水可能存在100亿到200亿", False, "长句截断"),
    ("九大行星体系的这个轨迹。当然我们中央星球不需要这么大。对，就是现在你做的这个太阳", False, "长句截断"),
    ("四五十平米吧，这已经算大的了。对在真实的这个呃居住环境里面，这已经算大的了。那四", False, "长句截断"),
    ("你跟人家请小孩的那种磨牙打呼噜声音差不多，你人家还更像你知道这个打呼噜重你这是新", False, "长句截断"),
    ("素材里边有几个是录像，我在你昨天给我的目录里边，我删了一个，但是还有就是那个韩国", False, "长句截断"),
    ("那14个素材我看过了，我觉得反正一般凑合能用吧。我删了最后一个，最后一个效果不好", True, "完整长句"),
    ("而且这个本身是一个自定义生成的这么一个逻辑。所以我觉得时间并不是一个。", False, "残句结尾"),
    ("我认为你应该是把右。", False, "残句结尾"),
    ("可以这个优化，但是你这个优化的方案。", False, "残句结尾"),
    ("嗯，你现在这个扩的有点大，就是这个线啊，线条轨道的线条扩的有点大，往里缩一缩，往", False, "残句结尾"),
    ("当时选。", False, "极短残句"),
    ("对，所以可能还是。", False, "极短残句"),
    ("嗯，现在不是晚上嘛，其是凌晨就之前。", False, "残句结尾"),
    ("伊娃，帮我看看现在几点了？", True, "完整疑问句"),
    ("伊娃，把客厅的灯关了", True, "完整祈使句"),
    ("嗯", False, "单字·未说完"),
    ("帮我", False, "极短残句"),
]

QUESTION = {
    "type": "noul",
    "instructions": "Is the utterance a complete request or statement?",
    "criteria": {
        "true": "The sentence has a clear ending and can be safely answered or executed.",
        "false": "The sentence is cut off, trails off, or clearly needs more words.",
    },
}


def ask(endpoint: str, utterance: str, session_active: bool, model: str | None = None) -> tuple[float | None, float, str]:
    """打 /v1/systemone，返回 (noul 概率, 延迟ms, 错误信息)。"""
    body: dict = {
        "state": {
            "utterance": utterance,
            "session_active": session_active,
            "assistant_name": "Eva / 伊娃",
        },
        "questions": {"complete": QUESTION},
    }
    # ollaya 这类本地服务要求显式带 model 字段，StartLux 的 server 不需要
    if model:
        body["model"] = model
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/systemone",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency = (time.perf_counter() - started) * 1000
        answer = (body.get("answers") or {}).get("complete") or {}
        return float(answer.get("noul", -1)), latency, ""
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        return None, latency, f"{type(exc).__name__}: {exc}"


def run_group(name: str, cases: list[tuple[str, bool, str]], endpoint: str, model: str | None = None) -> dict:
    print(f"\n{'=' * 78}\n{name}（{len(cases)} 题）\n{'=' * 78}")
    print(f"{'话语':<44} {'期望':<6} {'noul':>7} {'判定':>6} {'延迟':>9}")
    print("-" * 78)
    correct = wrong = 0
    rows = []
    for text, expected, tag in cases:
        prob, latency, err = ask(endpoint, text, session_active=True, model=model)
        if err or prob is None:
            print(f"{text[:42]:<44} {str(expected):<6} {'ERR':>7} {'-':>6} {latency:>8.0f}ms  {err[:40]}")
            wrong += 1
            continue
        decided = prob >= COMPLETE_THRESHOLD
        ok = decided == expected
        correct += ok
        wrong += not ok
        rows.append((text, expected, prob, decided, latency, tag, ok))
        mark = "✓" if ok else "✗"
        print(
            f"{text[:42]:<44} {'完整' if expected else '未完':<6} "
            f"{prob:>7.3f} {mark}{'完整' if decided else '未完':<4} {latency:>8.0f}ms"
        )

    total = correct + wrong
    print("-" * 78)
    print(f"准确率 {correct}/{total} = {correct / total * 100:.1f}%")

    # 分离度：完整句与残句的 noul 均值差
    yes = [r[2] for r in rows if r[1]]
    no = [r[2] for r in rows if not r[1]]
    if yes and no:
        print(
            f"完整句均值 {statistics.fmean(yes):.3f} / 残句均值 {statistics.fmean(no):.3f} "
            f"→ 分离度 {statistics.fmean(yes) - statistics.fmean(no):+.3f}"
        )
    if rows:
        print(f"延迟中位 {statistics.median([r[4] for r in rows]):.0f}ms")
    for text, expected, prob, decided, latency, tag, ok in rows:
        if not ok:
            print(f"  ✗ [{tag}] {text[:36]} 期望{'完整' if expected else '未完'} 实得{prob:.3f}")
    return {"name": name, "correct": correct, "total": total}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=None, help="请求体带 model 字段（ollaya 需要，StartLux 不需要）")
    parser.add_argument("--out", default=None, help="把结果写成 JSON")
    args = parser.parse_args()

    print(f"端点 {args.endpoint}，判定阈值 {COMPLETE_THRESHOLD}")
    try:
        prob, latency, err = ask(args.endpoint, "伊娃，现在几点？", True, args.model)
        if err or prob is None:
            print(f"连通性检查失败：{err}（服务是否已起、模型是否已加载？）")
            return
        print(f"连通性 OK，noul={prob:.3f} 延迟 {latency:.0f}ms")
    except Exception as exc:
        print(f"连通性检查异常：{exc}")
        return

    results = [run_group("A 组 · 定向判停集（2026-09-29 我方 6 题）", SET_A, args.endpoint, args.model)]
    results.append(run_group("B 组 · 真实流量集（影子日志抽取 19 条）", SET_B, args.endpoint, args.model))

    print(f"\n{'=' * 78}\n合计")
    c = sum(r["correct"] for r in results)
    t = sum(r["total"] for r in results)
    print(f"总准确率 {c}/{t} = {c / t * 100:.1f}%")

    # 对照基准：我方 winnow:e4b 2026-09-29 在 A 组为 6/6
    print("\n对照基准：我方 winnow:e4b 在 A 组实测 6/6（2026-09-29，M3 Max Metal）")
    verdict = "可用" if c / t >= 0.8 else ("边缘可用" if c / t >= 0.6 else "不可用")
    print(f"判定：{verdict}（阈值 80% 可用 / 60% 边缘）")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"results": results, "verdict": verdict}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.out}")


if __name__ == "__main__":
    main()