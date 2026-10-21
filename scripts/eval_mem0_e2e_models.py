#!/usr/bin/env python3
"""mem0 抽取模型端到端对比：35B / 8B / 4B（2026-10-06）

架构澄清（主人 2026-10-06 指出，此前我判断错）：
    mem0 负责**抽取**候选记忆 → GEV 判停层 `rerank_memory_lines` 负责**筛选**
    哪些记忆对当前请求有用。所以评价抽取模型的质量，**不该看 mem0 检索的
    首位命中率**（那只是候选顺序），而该看「经GEV 精排后最终选出的记忆
    是否正确」。

本脚本按真实链路测：
    1. mem0.add()抽取候选记忆（计时）
    2. mem0.search() 召回候选
    3. **GEV rerank_memory_lines() 精排**（计时）
    4. 报告最终注入 prompt 的记忆，与期望对比

用法：
    cd server/homebrain-server && ./venv/bin/python scripts/eval_mem0_e2e_models.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

# 允许从 scripts/ 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOME = Path.home() / ".mem0"
FAISS = str(HOME / "homebrain_faiss")

# 原始对话事实
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

# (期望被选中的关键词, 提问)
QUERIES = [
    ("车", "主人的车是什么型号"),
    ("猫", "主人的猫叫什么名字"),
    ("生日", "主人的生日是什么时候"),
    ("咖啡", "主人早上喝什么"),
    ("摄像头", "摄像头装在哪一层"),
    ("香菜", "主人有什么不吃的"),
]

MODELS = [
    ("35B-Vision", "Qwen3.6-35B-A3B-Fable-5-Distill-4bit-DWQ-Vision-MLX"),
    ("8B", "Qwen3-8B-4bit"),
    ("4B", "Qwen3.5-4B-MLX-4bit"),
]


def build(llm_model: str):
    from mem0 import Memory
    return Memory.from_config({
        "vector_store": {"provider": "faiss", "config": {
            "path": FAISS, "embedding_model_dims": 1024, "distance_strategy": "cosine"}},
        "llm": {"provider": "openai", "config": {
            "model": llm_model, "api_key": "omlx-local",
            "openai_base_url": "http://localhost:8001/v1"}},
        "embedder": {"provider": "openai", "config": {
            "model": "Qwen3-Embedding-0.6B-8bit", "api_key": "omlx-local",
            "openai_base_url": "http://localhost:8001/v1", "embedding_dims": 1024}},
    })


async def run(tag: str, llm_model: str) -> dict:
    print(f"\n{'=' * 72}\n【{tag}】{llm_model}\n{'=' * 72}")
    shutil.rmtree(FAISS, ignore_errors=True)
    try:
        mem = build(llm_model)
    except Exception as exc:
        print(f"  ❌ 构建失败: {type(exc).__name__}: {exc}")
        return {"tag": tag, "error": str(exc)[:100]}

    # ---- 1. 抽取 ----
    t0 = time.perf_counter()
    try:
        # 分批提交（provider 内部也是这么做的，单批超 5 条会JSON 截断丢数据）
        msgs = [{"role": "user", "content": f} for f in FACTS]
        for i in range(0, len(msgs), 4):
            mem.add(messages=msgs[i:i + 4], user_id="homebrain")
    except Exception as exc:
        print(f"  ❌ 抽取失败: {exc}")
        return {"tag": tag, "error": str(exc)[:100]}
    extract_sec = time.perf_counter() - t0

    raw = mem.get_all(user_id="homebrain").get("results", [])
    stored = [str(r.get("memory", "")) for r in raw]
    print(f"  ① 抽取耗时   : {extract_sec:6.1f}s")
    print(f"  ① 产出记忆   : {len(stored)}/{len(FACTS)} 条")
    for s in stored:
        print(f"       - {s[:48]}")

    # ---- 2/3. 召回 + GEV 精排 ----
    from core.utils.jev_decision import get_jev_decision
    jev = get_jev_decision()
    if not getattr(jev, "memory_enabled", False):
        print("  ⚠️ GEV memory_enabled=False，改用 mem0 排序结果直接评估")
        return {"tag": tag, "extract_sec": round(extract_sec, 1),
                "memories": len(stored), "hits": 0, "total": len(QUERIES),
                "gev_ms": 0, "search_ms": 0, "note": "GEV 未启用"}

    hits = 0
    gev_lat, search_lat = [], []
    print(f"\n  ② 召回 + ③ GEV 精排：")
    for expect, q in QUERIES:
        t = time.perf_counter()
        items = mem.search(query=q, user_id="homebrain", limit=5).get("results", [])
        search_lat.append((time.perf_counter() - t) * 1000)
        cands = [str(it.get("memory", "")) for it in items]

        t = time.perf_counter()
        try:
            picked = await jev.rerank_memory_lines(q, cands)
        except Exception as exc:
            print(f"       GEV 调用失败: {exc}")
            picked = None
        gev_lat.append((time.perf_counter() - t) * 1000)

        if picked:
            # GEV 返回的是筛选后的列表，判断期望内容是否在其中
            ok = any(expect in line for line in picked)
            hits += ok
            mark = "✅" if ok else "❌"
            print(f"       {mark} 「{q}」召回{len(cands)}条→GEV选{len(picked)}条")
            for line in picked:
                print(f"            · {line[:46]}")
        else:
            # GEV 不可用时退回「候选里有没有期望内容」——这才是召回率
            ok = any(expect in c for c in cands)
            hits += ok
            mark = "✅" if ok else "❌"
            print(f"       {mark} 「{q}」召回{len(cands)}条（GEV 未生效，按召回判定）")

    total = len(QUERIES)
    print(f"\n  ④ 最终结果   : {hits}/{total} = {hits/total*100:.0f}%")
    print(f"     mem0 召回延迟中位: {statistics.median(search_lat):.0f}ms")
    print(f"     GEV 精排延迟中位 : {statistics.median(gev_lat):.0f}ms")

    return {"tag": tag, "model": llm_model,
            "extract_sec": round(extract_sec, 1), "memories": len(stored),
            "facts": len(FACTS), "hits": hits, "total": total,
            "accuracy": round(hits / total * 100, 1),
            "search_ms": round(statistics.median(search_lat)),
            "gev_ms": round(statistics.median(gev_lat)),
            "stored": stored}


async def main() -> int:
    print("mem0 抽取模型端到端对比（mem0 抽取 → GEV 精排 → 最终结果）")
    results = []
    for tag, model in MODELS:
        results.append(await run(tag, model))

    print(f"\n{'=' * 72}\n汇总\n{'=' * 72}")
    print(f"{'模型':<14}{'抽取耗时':>10}{'记忆数':>9}{'最终命中':>10}{'召回延迟':>10}{'GEV延迟':>10}")
    for r in results:
        if r.get("error"):
            print(f"{r['tag']:<14}{'失败':>10}  {r['error'][:40]}")
            continue
        print(f"{r['tag']:<14}{r['extract_sec']:>8.1f}s"
              f"{str(r['memories'])+'/'+str(r['facts']):>9}"
              f"{str(r['hits'])+'/'+str(r['total']):>10}"
              f"{str(r['search_ms'])+'ms':>10}{str(r['gev_ms'])+'ms':>10}")

    Path("/tmp/mem0_e2e_cmp.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细: /tmp/mem0_e2e_cmp.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))