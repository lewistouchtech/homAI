"""实测 FunASR paraformer-zh-streaming 流式语义：中间帧返回增量还是全量"""
import sys, os, time
import numpy as np
sys.path.insert(0, os.getcwd())

from core.providers.asr.fun_local import ASRProvider

pcm = np.fromfile("/tmp/test_asr.pcm", dtype=np.int16)
print(f"总采样: {len(pcm)} ({len(pcm)/16000:.2f}s)")

config = {
    "type": "fun_local",
    "model_dir": "models/paraformer-zh-streaming",
    "output_dir": "tmp/",
    "streaming": "true",
    "sampleRate": 16000,
}
p = ASRProvider(config, False)

# 模拟：600ms 一块，异步调用 process_streaming_chunk
import asyncio

async def run():
    chunk_samples = 9600  # 600ms
    n_chunks = (len(pcm) + chunk_samples - 1) // chunk_samples
    print(f"切成 {n_chunks} 块（600ms/块）\n--- 中间帧输出 ---")
    for i in range(n_chunks):
        chunk = pcm[i*chunk_samples:(i+1)*chunk_samples]
        is_final = (i == n_chunks - 1)
        t0 = time.time()
        result = await p.process_streaming_chunk(
            chunk.tobytes(), "test-session", is_final=is_final
        )
        dt = (time.time()-t0)*1000
        # 直接看模型原始返回
        print(f"块{i+1}({dt:.0f}ms) is_final={is_final} process返回: {result!r}")
    # 单独看原始 generate 返回，验证语义
    print("\n--- 手动验证原始 generate 输出（模拟第二块场景）---")
    cache = {}
    r1 = p.model.generate(input=pcm[:9600], cache=cache, is_final=False, chunk_size=[0,10,5])
    print(f"块1原始: {r1}")
    r2 = p.model.generate(input=pcm[9600:19200], cache=cache, is_final=False, chunk_size=[0,10,5])
    print(f"块2原始: {r2}")
    r3 = p.model.generate(input=pcm[19200:], cache=cache, is_final=True, chunk_size=[0,10,5])
    print(f"块3(final)原始: {r3}")

asyncio.run(run())
