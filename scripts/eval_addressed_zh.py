#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测本地判停模型的 addressed（寻址）能力。

与 eval_strands_decider_zh.py（测 complete 判停）平行：同一个 /v1/systemone 端点，
同一套 state 结构，但问的是「这句话是对我说的吗」，对应
core/utils/jev_decision.py:219-225 里的生产提问，原文照抄。

用法：
    # ollaya 端点（必须带 --model）
    venv/bin/python scripts/eval_addressed_zh.py \
        --endpoint http://127.0.0.1:11435/v1/systemone --model winnow:e4b --name winnow:e4b

    # StartLux 端点（不能带 --model）
    venv/bin/python scripts/eval_addressed_zh.py \
        --endpoint http://127.0.0.1:8091/v1/systemone --name StartLux-9B
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

# 与 core/utils/jev_decision.py:219-225 完全一致，勿改
QUESTION = {
    "type": "noul",
    "instructions": "Is the utterance addressed to the assistant?",
    "criteria": {
        "true": "The user is speaking to the assistant or continuing an active request.",
        "false": "The text is background speech, narration, TV audio, or a conversation with someone else.",
    },
}

# ---------------------------------------------------------------------------
# 题库：expect=True 表示「应该应答」，expect=False 表示「应该忽略」
# session_active=True 表示当前已有活跃会话，此时延续请求也算 addressed
# 来源：2026-09-29 手工构造 + data/decision_shadow_log.jsonl 真实流量
# ---------------------------------------------------------------------------
CASES: list[dict] = [
    # --- A. 会话中，明确喊伊娃/直接下达指令 → 应答 ---
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "伊娃，帮我看看现在几点了？", "src": "真实流量 winnow=0.98"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "打开主入口摄像头。", "src": "真实流量 winnow=0.83"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "不对啊，这个摄像头已经更新了，你查一下。", "src": "真实流量 winnow=0.79"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "退下。", "src": "真实流量 winnow=0.52"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "伊娃，把客厅的灯关了", "src": "构造"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "嗯，那个射灯也打开吧", "src": "真实流量变体"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "刚才那个摄像头，能左右转吗", "src": "构造 延续追问"},
    {"group": "A-明确呼叫", "expect": True, "session_active": True,
     "utterance": "嗯嗯，说吧我在听", "src": "构造 最小应答"},

    # --- B. 会话中，但明显不是在跟 Eva 说 → 忽略（最容易误判的一组）---
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "你吃了吗", "src": "构造 问家人"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "老公你把垃圾拿下去", "src": "构造 喊配偶"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "妈妈我回来了", "src": "构造 孩子喊妈"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "小王你过来一下", "src": "构造 喊同事 非伊娃"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "这电视剧真好看", "src": "构造 电视旁白"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "明天记得交房租", "src": "构造 自言自语"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "他怎么还不来", "src": "构造 抱怨他人"},
    {"group": "B-会话中他人对话", "expect": False, "session_active": True,
     "utterance": "你看这个是不是破了", "src": "构造 跟伴侣说话"},

    # --- C. 无会话，电视/背景音 → 忽略 ---
    {"group": "C-无会话背景音", "expect": False, "session_active": False,
     "utterance": "本次节目即将开始", "src": "构造 电视字幕"},
    {"group": "C-无会话背景音", "expect": False, "session_active": False,
     "utterance": "订购请拨打四零零八八八八八八八", "src": "构造 广告"},
    {"group": "C-无会话背景音", "expect": False, "session_active": False,
     "utterance": "宝宝快来吃饭了", "src": "构造 电视里的家长喊孩子"},
    {"group": "C-无会话背景音", "expect": False, "session_active": False,
     "utterance": "下面请欣赏音乐", "src": "构造 电台"},
    {"group": "C-无会话背景音", "expect": False, "session_active": False,
     "utterance": "第两百三十七集完", "src": "构造 剧集结束语"},

    # --- D. 无会话，必须喊名字才应答 ---
    {"group": "D-无会话唤醒", "expect": True, "session_active": False,
     "utterance": "伊娃，开灯", "src": "构造"},
    {"group": "D-无会话唤醒", "expect": True, "session_active": False,
     "utterance": "Eva，把窗帘拉上", "src": "构造 英文名"},
    {"group": "D-无会话唤醒", "expect": True, "session_active": False,
     "utterance": "伊娃现在几点了", "src": "构造"},

    # --- E. 疑难：名字被 ASR 听错（真实流量里 winnow 就栽在这）---
    {"group": "E-疑难", "expect": True, "session_active": True,
     "utterance": "你好，伊万，打开主入口摄像头。", "src": "真实流量 winnow=0.14 误判"},
    {"group": "E-疑难", "expect": True, "session_active": False,
     "utterance": "伊娃，把空调调到二十四度", "src": "构造 连续指令"},
]


def ask(endpoint: str, utterance: str, session_active: bool,
        model: str | None, timeout: float) -> tuple[float, int] | None:
    body = {
        "state": {
            "utterance": utterance,
            "session_active": bool(session_active),
            "assistant_name": "Eva / 伊娃",
        },
        "questions": {"addressed": QUESTION},
    }
    if model:
        body["model"] = model
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    latency = int((time.perf_counter() - started) * 1000)
    ans = (payload.get("answers") or {}).get("addressed")
    if not isinstance(ans, dict):
        return None
    v = ans.get("noul")
    return float(v) if isinstance(v, (int, float)) else None, latency


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", default=None, help="ollaya 端点必填；StartLux 端点不要填")
    ap.add_argument("--name", default="model")
    ap.add_argument("--thresholds", default="0.3,0.4,0.5,0.55,0.6,0.7,0.8,0.9")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        probe = ask(args.endpoint, "测试", False, args.model, args.timeout)
        if probe is None:
            print(f"[错误] 端点不可用或响应格式不对: {args.endpoint}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 连通性检查失败: {exc}", file=sys.stderr)
        return 2

    print(f"=== addressed（寻址）评测：{args.name} ===")
    print(f"端点 {args.endpoint}｜题数 {len(CASES)}\n")

    rows, latencies = [], []
    for idx, case in enumerate(CASES, 1):
        got = ask(args.endpoint, case["utterance"], case["session_active"],
                  args.model, args.timeout)
        if got is None:
            rows.append({**case, "prob": None, "latency_ms": None, "failed": True})
            print(f"[{idx:2d}/{len(CASES)}] 请求失败  {case['utterance'][:24]}")
            continue
        prob, lat = got
        latencies.append(lat)
        rows.append({**case, "prob": round(prob, 4), "latency_ms": lat, "failed": False})
        mark = "✓" if (prob >= 0.5) == case["expect"] else "✗"
        print(f"[{idx:2d}/{len(CASES)}] {mark} {prob:.4f}  "
              f"{'应答' if case['expect'] else '忽略'}  "
              f"sa={int(case['session_active'])}  {case['utterance'][:24]}")

    ok = [r for r in rows if not r["failed"]]
    print(f"\n失败 {len(rows) - len(ok)} 题｜延迟中位 "
          f"{statistics.median(latencies) if latencies else 0}ms")

    thr_list = [float(x) for x in args.thresholds.split(",")]
    print(f"\n{'阈值':>6} {'准确率':>8} {'误丢弃':>8} {'误触发':>8} "
          f"{'应答召回':>10} {'忽略召回':>10}")
    print("-" * 62)
    best = None
    for th in thr_list:
        tp = sum(1 for r in ok if r["expect"] and r["prob"] >= th)
        fn = sum(1 for r in ok if r["expect"] and r["prob"] < th)
        fp = sum(1 for r in ok if not r["expect"] and r["prob"] >= th)
        tn = sum(1 for r in ok if not r["expect"] and r["prob"] < th)
        total = tp + fn + fp + tn
        if total == 0:
            continue
        recall_yes = tp / (tp + fn) if (tp + fn) else 0.0
        recall_no = tn / (tn + fp) if (tn + fp) else 0.0
        acc = (tp + tn) / total
        print(f"{th:>6.2f} {acc * 100:>7.1f}% {fn:>8} {fp:>8} "
              f"{recall_yes * 100:>9.1f}% {recall_no * 100:>9.1f}%")
        if best is None or acc > best[1]:
            best = (th, acc, fn, fp)

    yes = [r["prob"] for r in ok if r["expect"]]
    no = [r["prob"] for r in ok if not r["expect"]]
    print(f"\n应答组概率均值 {statistics.mean(yes):.3f}（n={len(yes)}）"
          f"｜忽略组概率均值 {statistics.mean(no):.3f}（n={len(no)}）")
    print(f"分离度 {statistics.mean(yes) - statistics.mean(no):+.3f}")

    if best:
        th, acc, fn, fp = best
        print(f"\n最佳阈值 {th}｜准确率 {acc * 100:.1f}%｜误丢弃 {fn}｜误触发 {fp}")
        print(f"生产当前 endpoint_threshold=0.55 处的表现：", end="")
        fp55 = sum(1 for r in ok if not r["expect"] and r["prob"] >= 0.55)
        fn55 = sum(1 for r in ok if r["expect"] and r["prob"] < 0.55)
        acc55 = sum(1 for r in ok if (r["prob"] >= 0.55) == r["expect"]) / len(ok)
        print(f"准确率 {acc55 * 100:.1f}%｜误丢弃 {fn55}｜误触发 {fp55}")

    print("\n--- 逐组明细（阈值 0.5）---")
    for g in dict.fromkeys(r["group"] for r in rows):
        sub = [r for r in ok if r["group"] == g]
        if not sub:
            continue
        hit = sum(1 for r in sub if (r["prob"] >= 0.5) == r["expect"])
        print(f"  {g}: {hit}/{len(sub)}")
        for r in sub:
            mark = "✓" if (r["prob"] >= 0.5) == r["expect"] else "✗"
            print(f"    {mark} {r['prob']:.4f} {r['utterance'][:28]}")

    if args.out:
        payload = {"name": args.name, "endpoint": args.endpoint,
                   "rows": rows, "best_threshold": best[0] if best else None,
                   "latency_median": statistics.median(latencies) if latencies else None}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())