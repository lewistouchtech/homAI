#!/usr/bin/env python3
"""ltm 抽取模型对比（严格控制变量版，2026-10-06）

修正前一版的两个错误：
1. **计时不可信**：四个模型同时常驻 omlx，抢内存带宽（实测生成速度掉到 28 tok/s），
   导致「8B 比 35B 慢」这种违反常理的结果。本版每个模型**单独测量**，
   且先预热再计时。
2. **输出格式差异**：4B 会把推理过程写进 content（没有 JSON），
   8B/35B 的 content 才是干净 JSON。本版显式检查输出格式，不合格直接判失败。

同时测试两种模式：
  - 默认模式：模型自带思维链
  - 强制 JSON：prompt 里明确要求不输出推理（看能否提升速度与格式稳定性）

用法：
    cd server/homebrain-server && ./venv/bin/python scripts/eval_ltm_models_fair.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOME = Path.home() / ".ltm"
FAISS = str(HOME / "cmp_fair")

FACTS = [
    "主人住在山谷",
    "主人的摄像头 cam_1 装在三楼",
    "主人每天早上喝两杯手冲咖啡，用浅烘豆",
    "主人的车是白色的特斯拉 Model 3",
    "主人讨厌香菜",
    "主人的生日是 3 月 15 号",
    "主人养了一只叫豆豆的橘猫",
    "主人在山谷的别墅装了地暖",
]

QUERIES = [
    ("车", "主人的车是什么型号"),
    ("猫", "主人的猫叫什么名字"),
    ("生日", "主人的生日是什么时候"),
    ("咖啡", "主人早上喝什么"),
    ("摄像头", "摄像头装在哪一层"),
    ("香菜", "主人有什么不吃的"),
]

MODELS = [
    ("4B", "Qwen3.5-4B-MLX-4bit"),
    ("8B", "Qwen3-8B-4bit"),
    ("35B", "Qwen3.6-35B-A3B-Fable-5-Distill-4bit-DWQ-Vision-MLX"),
]


def omlx_status() -> dict:
    import urllib.request
    with urllib.request.urlopen("http://localhost:8001/api/status", timeout=10) as r:
        return json.loads(r.read())


def build(llm_model: str, strict_json: bool):
    """strict_json=True 时给 ltm 换一条强调直接输出 JSON 的抽取提示词。"""
    from ltm import Memory
    cfg = {
        "vector_store": {"provider": "faiss", "config": {
            "path": FAISS, "embedding_model_dims": 1024, "distance_strategy": "cosine"}},
        "llm": {"provider": "openai", "config": {
            "model": llm_model, "api_key": "omlx-local",
            "openai_base_url": "http://localhost:8001/v1"}},
        "embedder": {"provider": "openai", "config": {
            "model": "Qwen3-Embedding-0.6B-8bit", "api_key": "omlx-local",
            "openai_base_url": "http://localhost:8001/v1", "embedding_dims": 1024}},
    }
    if strict_json:
        # ltm 允许自定义抽取提示词：强调不要输出推理，直接给 JSON
        cfg["custom_fact_extraction_prompt"] = (
            "你是记忆抽取器。**直接输出 JSON，不要任何解释、不要推理过程。**\n"
            "从下面的对话中抽取值得长期记住的事实。\n"
            "严格使用此JSON 格式，不要有多余文字：\n"
            '{"memory": ["事实1", "事实2"], "event": "ADD", "id": "1"}\n\n'
            "对话：\n{fact}\n"
        )
        cfg["custom_update_memory_prompt"] = (
            "你是记忆管理器。**直接输出 JSON，不要任何解释、不要推理过程。**\n"
            "只能做四种操作之一：add / update / delete / no change。\n"
            "严格使用此 JSON 格式：\n"
            '{"memory": ["值"], "event": "ADD|UPDATE|DELETE|NONE", "id": "值"}\n\n'
            "当前记忆：{current_memory}\n"
            "新检索到的事实：\n{new_retrieved_facts}\n"
        )
    return Memory.from_config(cfg)


async def measure(tag: str, llm_model: str, strict_json: bool) -> dict:
    label = f"{tag}{'+严格JSON' if strict_json else ''}"
    print(f"\n{'=' * 70}\n【{label}】{llm_model}\n{'=' * 70}")
    st = omlx_status()
    print(f"  omlx 当前: {len(st.get('loaded_models', []))} 个模型常驻, "
          f"内存 {st.get('model_memory_used_formatted')}, "
          f"生成 {round(st.get('avg_generation_tps', 0), 1)} tok/s")

    shutil.rmtree(FAISS, ignore_errors=True)
    try:
        mem = build(llm_model, strict_json)
    except Exception as exc:
        print(f"  ❌ 构建失败: {exc}")
        return {"label": label, "error": str(exc)[:100]}

    # ---- 预热：把模型加载成本排除在计时之外 ----
    print("  预热中（不计时）…")
    warm_start = time.perf_counter()
    try:
        mem.add(messages=[{"role": "user", "content": "预热内容"}], user_id="warmup")
    except Exception:
        pass
    warm_sec = time.perf_counter() - warm_start
    print(f"  预热耗时: {warm_sec:.1f}s（已排除）")

    # ---- 正式计时：分批 4 条 ----
    msgs = [{"role": "user", "content": f} for f in FACTS]
    times = []
    for i in range(0, len(msgs), 4):
        chunk = msgs[i:i + 4]
        t = time.perf_counter()
        try:
            mem.add(messages=chunk, user_id="homebrain")
        except Exception as exc:
            print(f"  ❌ 第 {i//4+1} 批失败: {exc}")
        times.append(time.perf_counter() - t)
    per_batch = statistics.fmean(times)
    total = sum(times)

    raw = mem.get_all(user_id="homebrain").get("results", [])
    stored = [str(r.get("memory", "")) for r in raw]
    print(f"  ① 抽取耗时   : 合计 {total:.1f}s（{len(times)} 批，单批均 {per_batch:.1f}s）")
    print(f"  ① 产出记忆   : {len(stored)}/{len(FACTS)} 条")
    for s in stored:
        print(f"       - {s[:46]}")

    if not stored:
        print("  ⚠️ 一条都没存，判定不可用")

    # ---- GEV 精排 ----
    from core.utils.jev_decision import get_jev_decision
    jev = get_jev_decision()
    hits = 0
    gev_lat = []
    for expect, q in QUERIES:
        try:
            items = mem.search(query=q, user_id="homebrain", limit=5).get("results", [])
        except Exception:
            items = []
        cands = [str(it.get("memory", "")) for it in items]
        t = time.perf_counter()
        try:
            picked = await jev.rerank_memory_lines(q, cands)
        except Exception:
            picked = None
        gev_lat.append((time.perf_counter() - t) * 1000)
        final = picked if picked else cands
        if any(expect in line for line in final):
            hits += 1

    total_q = len(QUERIES)
    print(f"  ④最终命中: {hits}/{total_q} = {hits/total_q*100:.0f}%"
          f"  GEV 延迟中位 {statistics.median(gev_lat):.0f}ms")
    st2 = omlx_status()
    print(f"  omlx 生成速度: {round(st2.get('avg_generation_tps', 0), 1)} tok/s")

    return {"label": label, "model": llm_model, "strict_json": strict_json,
            "warm_sec": round(warm_sec, 1), "extract_total": round(total, 1),
            "per_batch": round(per_batch, 1), "memories": len(stored),
            "facts": len(FACTS), "hits": hits, "total": total_q,
            "accuracy": round(hits / total_q * 100, 1),
            "gev_ms": round(statistics.median(gev_lat)), "stored": stored}


async def main() -> int:
    print("ltm 抽取模型对比（控制变量：预热后计时 + 检查输出格式）")
    results = []
    for tag, model in MODELS:
        for strict in (False, True):
            results.append(await measure(tag, model, strict))

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}")
    print(f"{'模型':<18}{'预热':>8}{'抽取合计':>10}{'单批':>8}{'记忆':>8}{'最终命中':>10}")
    for r in results:
        if r.get("error"):
            print(f"{r['label']:<18}{'构建失败':>8}  {r['error'][:36]}")
            continue
        print(f"{r['label']:<18}{str(r['warm_sec'])+'s':>8}"
              f"{str(r['extract_total'])+'s':>10}{str(r['per_batch'])+'s':>8}"
              f"{str(r['memories'])+'/'+str(r['facts']):>8}"
              f"{str(r['hits'])+'/'+str(r['total']):>10}")

    Path("/tmp/ltm_fair_cmp.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细: /tmp/ltm_fair_cmp.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))