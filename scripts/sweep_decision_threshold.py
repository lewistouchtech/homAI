#!/usr/bin/env python3
"""判停阈值敏感度分析：同一批中文题，在多个阈值下统计准确率。

用途：换判停模型后判断 complete_threshold 是否需要跟着调。
题库复用 eval_strands_decider_zh.py 的 A/B 组。
"""
import argparse
import importlib.util
import json
import pathlib
import statistics
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "eval_zh", _HERE / "eval_strands_decider_zh.py"
)
_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eval)

THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def collect(endpoint: str, model: str | None) -> list[tuple[str, bool, float, float]]:
    """跑完 A+B 两组，返回 (话语, 期望完整, noul, 延迟ms)。"""
    rows = []
    for text, expected, _tag in _eval.SET_A + _eval.SET_B:
        prob, latency, err = _eval.ask(endpoint, text, session_active=True, model=model)
        if err or prob is None:
            print(f"  请求失败 {text[:24]}: {err}", file=sys.stderr)
            continue
        rows.append((text, expected, prob, latency))
    return rows


def sweep(rows: list[tuple[str, bool, float, float]]) -> dict:
    out = {}
    for th in THRESHOLDS:
        correct = sum((p >= th) == exp for _t, exp, p, _l in rows)
        # 假停（把未完判成完）vs 假等（把完整判成未完）
        false_stop = sum(p >= th and not exp for _t, exp, p, _l in rows)
        false_wait = sum(p < th and exp for _t, exp, p, _l in rows)
        out[th] = {
            "accuracy": correct / len(rows),
            "correct": correct,
            "total": len(rows),
            "false_stop": false_stop,
            "false_wait": false_wait,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:11435")
    parser.add_argument("--model", default=None, help="请求体带 model 字段（ollaya 需要）")
    parser.add_argument("--name", default="model")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    print(f"采集 {args.name}（{args.endpoint}）…")
    rows = collect(args.endpoint, args.model)
    if not rows:
        print("没有可用样本，退出")
        return

    table = sweep(rows)
    print(f"\n{'阈值':>6} {'准确率':>9} {'正确':>7} {'假停':>6} {'假等':>6}")
    print("-" * 40)
    for th, r in table.items():
        print(
            f"{th:>6.1f} {r['accuracy'] * 100:>8.1f}% "
            f"{r['correct']:>3}/{r['total']:<3} {r['false_stop']:>6} {r['false_wait']:>6}"
        )

    best = max(table.items(), key=lambda kv: kv[1]["accuracy"])
    print(f"\n最佳阈值 {best[0]:.1f} → {best[1]['accuracy'] * 100:.1f}%")
    print(f"完整句均值 {statistics.fmean([p for _t, e, p, _l in rows if e]):.3f}"
          f" / 残句均值 {statistics.fmean([p for _t, e, p, _l in rows if not e]):.3f}")
    print(f"延迟中位 {statistics.median([l for _t, _e, _p, l in rows]):.0f}ms")

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(
                {"name": args.name, "endpoint": args.endpoint, "sweep": table,
                 "rows": [{"text": t, "expected": e, "prob": p, "latency": l}
                          for t, e, p, l in rows]},
                ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"结果已写入 {args.out}")


if __name__ == "__main__":
    main()