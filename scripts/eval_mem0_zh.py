#!/usr/bin/env python3
"""mem0 中文能力实测（2026-10-06）

目的：LoCoMo / LongMemEval / BEAM 全是英文数据集，HomeBrain 是纯中文场景，
必须实测中文记忆的写入与检索能力。本脚本接本地 omlx（127.0.0.1:8001），
不依赖任何外部 API key。

注意：mem0 1.0.0 的配置类与官方文档不一致（文档写 EmbedderConfig，
实际是 BaseEmbedderConfig/OpenAIConfig），且 Memory.from_config 收的是
嵌套 dict 而非配置对象。本脚本按实际源码构造。

用法：
    cd server/homebrain-server && ./venv/bin/python scripts/eval_mem0_zh.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

BASE_URL = "http://localhost:8001/v1"
API_KEY = "omlx-local"
LLM_MODEL = "Qwen3.6-35B-A3B-Fable-5-Distill-4bit-DWQ-Vision-MLX"
EMBED_MODEL = "Qwen3-Embedding-0.6B-8bit"
HOME = Path.home() / ".mem0"
DB_PATH = str(HOME / "hombrain_zh_test.db")
FAISS_PATH = str(HOME / "hombrain_zh_faiss")

# 题库：模拟 HomeBrain 真实记忆场景，全部中文
CASES = [
    ("主人住在山谷", "主人住在哪里"),
    ("主人的摄像头 cam_1 装在三楼", "摄像头装在哪"),
    ("主人每天早上喝两杯手冲咖啡，用浅烘豆", "主人早上喝什么咖啡"),
    ("主人的车是白色的特斯拉 Model 3", "主人的车是什么型号"),
    ("主人讨厌香菜", "主人不喜欢什么蔬菜"),
    ("主人的生日是 3 月 15 号", "主人什么时候过生日"),
    ("主人养了一只叫豆豆的橘猫", "主人的猫叫什么"),
    ("主人在山谷的别墅装了地暖", "别墅有什么设施"),
]


def build_config() -> dict:
    """构造接本地 omlx 的 mem0 配置。

    mem0 1.0.0 的 LlmConfig / EmbedderConfig 格式为 {provider, config:{...}}，
    具体参数（如 model / api_key / openai_base_url）必须放在 config 子字典里，
    否则 openai client 收不到 api_key 会报 Missing credentials。
    """
    return {
        "vector_store": {
            "provider": "faiss",
            "config": {
                "path": FAISS_PATH,
                "embedding_model_dims": 1024,
                "distance_strategy": "cosine",
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": LLM_MODEL,
                "api_key": API_KEY,
                "openai_base_url": BASE_URL,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": EMBED_MODEL,
                "api_key": API_KEY,
                "openai_base_url": BASE_URL,
                "embedding_dims": 1024,
            },
        },
    }


def main() -> int:
    print("=" * 68)
    print("mem0 中文能力实测")
    print(f"  LLM     : {LLM_MODEL}")
    print(f"  Embedder: {EMBED_MODEL} (1024 维)")
    print(f"  端点    : {BASE_URL}")
    print(f"  向量库  : {FAISS_PATH}")
    print("=" * 68)

    HOME.mkdir(parents=True, exist_ok=True)
    p = Path(DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    if Path(FAISS_PATH).exists():
        shutil.rmtree(FAISS_PATH)

    print("\n[1/3] 构建 mem0 实例…")
    t0 = time.perf_counter()
    try:
        from mem0 import Memory
        mem = Memory.from_config(build_config())
    except Exception as exc:
        print(f"  ❌ 构建失败: {type(exc).__name__}: {exc}")
        return 1
    print(f"  ✅ 完成（{time.perf_counter() - t0:.1f}s）")

    print("\n[2/3] 写入 8 条中文记忆…")
    ok_write = 0
    for text, _q in CASES:
        t = time.perf_counter()
        try:
            mem.add(messages=[{"role": "user", "content": text}],
                    user_id="homebrain", agent_id="eva")
            ok_write += 1
            print(f"  ✅ {time.perf_counter() - t:5.1f}s  {text}")
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {str(exc)[:70]}  | {text}")
    print(f"\n  写入成功 {ok_write}/{len(CASES)}")

    print("\n[3/3] 中文语义检索…")
    hits = 0
    details = []
    for fact, query in CASES:
        t = time.perf_counter()
        try:
            res = mem.search(query=query, user_id="homebrain", limit=3)
            items = res.get("results", res) if isinstance(res, dict) else res
            # mem0 会改写记忆文本（去主语/合并句子），不能用前缀精确匹配。
            # 改为关键内容词覆盖率：取事实的 2-gram 词块，命中过半即算找到。
            grams = [fact[i:i + 2] for i in range(0, len(fact) - 1, 2)]
            found = any(
                sum(1 for g in grams if g in str(it.get("memory", ""))) >= max(2, len(grams) // 2)
                for it in items
            )
            hits += found
            ms = (time.perf_counter() - t) * 1000
            top = str(items[0].get("memory", ""))[:38] if items else "(空)"
            print(f"  {'✅' if found else '❌'} {ms:6.0f}ms  查「{query}」→ {len(items)} 条｜{top}")
            details.append({"query": query, "expected": fact, "found": found,
                            "count": len(items), "latency_ms": round(ms)})
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {str(exc)[:60]}  | 查「{query}」")
            details.append({"query": query, "expected": fact, "found": False,
                            "error": str(exc)[:80]})
    total = len(CASES)
    print(f"\n{'=' * 68}")
    print(f"结果：写入 {ok_write}/{total}   中文检索命中 {hits}/{total} = {hits/total*100:.0f}%")
    print("=" * 68)

    out = Path("/tmp/mem0_zh_result.json")
    out.write_text(json.dumps({
        "write_ok": ok_write, "total": total, "hits": hits,
        "accuracy": round(hits / total * 100, 1), "details": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())