#!/usr/bin/env python3
"""mem0 抽取模型对比：35B 视觉 vs 8B（2026-10-06）

主人的疑问：记忆抽取用 35B 是不是大材小用、能不能换 8B。
本脚本用同一批中文记忆、同样的题库，实测两个模型的：
  1. 抽取耗时
  2. 抽取质量（检索命中率）
  3. 记忆改写程度（原文 vs mem0 改写后的文本）

用法：
    cd server/homebrain-server && ./venv/bin/python scripts/eval_mem0_extract_models.py
"""
from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
from pathlib import Path

BASE_URL = "http://localhost:8001/v1"
API_KEY = "omlx-local"
EMBED_MODEL = "Qwen3-Embedding-0.6B-8bit"
HOME = Path.home() / ".mem0"

# 候选抽取模型：35B（当前）vs 8B（主人提议）
CANDIDATES = [
    ("35B-A3B-Vision", "Qwen3.6-35B-A3B-Fable-5-Distill-4bit-DWQ-Vision-MLX"),
    ("8B-4bit", "Qwen3-8B-4bit"),
]

MSGS = [
    {"role": "user", "content": "主人住在山谷"},
    {"role": "user", "content": "主人的摄像头 cam_1 装在三楼"},
    {"role": "user", "content": "主人每天早上喝两杯手冲咖啡，用浅烘豆"},
    {"role": "user", "content": "主人的车是白色的特斯拉 Model 3"},
    {"role": "user", "content": "主人讨厌香菜"},
    {"role": "user", "content": "主人的生日是 3 月 15 号"},
    {"role": "user", "content": "主人养了一只叫豆豆的橘猫"},
    {"role": "user", "content": "主人在山谷的别墅装了地暖"},
]

QUERIES = [
    ("主人住在山谷", "主人住在哪里"),
    ("主人的摄像头 cam_1 装在三楼", "摄像头装在哪"),
    ("主人每天早上喝两杯手冲咖啡，用浅烘豆", "主人早上喝什么咖啡"),
    ("主人的车是白色的特斯拉 Model 3", "主人的车是什么型号"),
    ("主人讨厌香菜", "主人不喜欢什么蔬菜"),
    ("主人的生日是 3 月 15 号", "主人什么时候过生日"),
    ("主人养了一只叫豆豆的橘猫", "主人的猫叫什么"),
    ("主人在山谷的别墅装了地暖", "别墅有什么设施"),
]


def build(llm_model: str, tag: str):
    from mem0 import Memory
    path = str(HOME / f"cmp_{tag}")
    shutil.rmtree(path, ignore_errors=True)
    return Memory.from_config({
        "vector_store": {"provider": "faiss", "config": {
            "path": path, "embedding_model_dims": 1024, "distance_strategy": "cosine"}},
        "llm": {"provider": "openai", "config": {
            "model": llm_model, "api_key": API_KEY, "openai_base_url": BASE_URL}},
        "embedder": {"provider": "openai", "config": {
            "model": EMBED_MODEL, "api_key": API_KEY, "openai_base_url": BASE_URL,
            "embedding_dims": 1024}},
    })


def covered(fact: str, text: str) -> bool:
    """判断检索结果是否覆盖了原始事实的关键内容。

    mem0 会改写记忆（去主语、合并句子），故不能用前缀精确匹配，
    改为 2-gram 词块覆盖率。
    """
    grams = [fact[i:i + 2] for i in range(0, len(fact) - 1, 2)]
    return sum(1 for g in grams if g in text) >= max(2, len(grams) // 2)


def run(tag: str, llm_model: str) -> dict:
    print(f"\n{'=' * 70}\n【{tag}】{llm_model}\n{'=' * 70}")
    try:
        mem = build(llm_model, tag)
    except Exception as exc:
        print(f"  ❌ 构建失败: {type(exc).__name__}: {exc}")
        return {"tag": tag, "model": llm_model, "error": str(exc)[:120]}

    # 抽取耗时
    t0 = time.perf_counter()
    try:
        mem.add(messages=MSGS, user_id="homebrain")
    except Exception as exc:
        print(f"  ❌ 抽取失败: {type(exc).__name__}: {exc}")
        return {"tag": tag, "model": llm_model, "error": str(exc)[:120]}
    elapsed = time.perf_counter() - t0

    rows = mem.get_all(user_id="homebrain").get("results", [])
    stored = [str(r.get("memory", "")) for r in rows]
    print(f"  抽取耗时   : {elapsed:.1f}s（{len(MSGS)} 条消息）")
    print(f"  产出记忆   : {len(stored)} 条")
    for s in stored[:4]:
        print(f"     - {s[:46]}")

    # 检索质量
    hits = 0
    lat = []
    for fact, q in QUERIES:
        t = time.perf_counter()
        items = mem.search(query=q, user_id="homebrain", limit=3).get("results", [])
        lat.append((time.perf_counter() - t) * 1000)
        if any(covered(fact, str(it.get("memory", ""))) for it in items):
            hits += 1
    total = len(QUERIES)
    print(f"  检索命中   : {hits}/{total} = {hits/total*100:.0f}%")
    print(f"  检索延迟中位: {statistics.median(lat):.0f}ms")

    # 改写程度：原始 8 条 vs 存储 N 条，看是否丢信息
    rewritten = sum(1 for f in MSGS if not covered(f["content"], " ".join(stored)))
    print(f"  信息完整性 : 8 条原始事实中 {8 - rewritten} 条可在记忆里找到对应表述")

    return {"tag": tag, "model": llm_model, "extract_sec": round(elapsed, 1),
            "memories": len(stored), "hits": hits, "total": total,
            "accuracy": round(hits / total * 100, 1),
            "search_ms": round(statistics.median(lat)),
            "facts_covered": 8 - rewritten, "stored": stored}


def main() -> int:
    print("mem0 抽取模型对比：35B vs 8B")
    print(f"端点 {BASE_URL}  embedder {EMBED_MODEL}")
    results = []
    for tag, model in CANDIDATES:
        results.append(run(tag, model))

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}")
    print(f"{'模型':<16}{'抽取耗时':>10}{'记忆数':>8}{'检索命中':>10}{'检索延迟':>10}")
    for r in results:
        if r.get("error"):
            print(f"{r['tag']:<16}{'失败':>10}  {r['error'][:40]}")
            continue
        print(f"{r['tag']:<16}{r['extract_sec']:>8.1f}s{r['memories']:>8}"
              f"{r['hits']}/{r['total']:>6}{r['search_ms']:>8}ms")

    Path("/tmp/mem0_model_cmp.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细: /tmp/mem0_model_cmp.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())