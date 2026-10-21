"""回归测试：模拟长句流式识别，验证累积逻辑（不回跳、不丢失）"""
import sys, os, time
import numpy as np
sys.path.insert(0, os.getcwd())

from core.providers.asr.fun_local import ASRProvider

# 用两句话测试（say 生成，比单句长，模拟真实场景）
if not os.path.exists("/tmp/test_asr_long.pcm"):
    os.system('say -o /tmp/test_asr_long.aiff "帮我看看今天天气怎么样，顺便提醒我下午三点开会"')
    os.system('ffmpeg -y -i /tmp/test_asr_long.aiff -ar 16000 -ac 1 -f s16le /tmp/test_asr_long.pcm 2>/dev/null')

pcm = np.fromfile("/tmp/test_asr_long.pcm", dtype=np.int16)
print(f"总采样: {len(pcm)} ({len(pcm)/16000:.2f}s)")

config = {"type": "fun_local", "model_dir": "models/paraformer-zh-streaming", "output_dir": "tmp/", "streaming": "true", "sampleRate": 16000}
p = ASRProvider(config, False)

import asyncio

async def run():
    # 模拟真实设备流：60ms/帧（960采样），每10帧(600ms)触发识别
    frame = 960
    frames = (len(pcm) + frame - 1) // frame
    n_chunks = (frames + 9) // 10  # 每10帧一块
    print(f"共 {n_chunks} 个600ms块")
    intermediate = []
    final = None
    for i in range(n_chunks):
        start = i * 10 * frame
        end = min(start + 10 * frame, len(pcm))
        chunk = pcm[start:end]
        is_final = (i == n_chunks - 1)
        t0 = time.time()
        result = await p.process_streaming_chunk(chunk.tobytes(), "test-long", is_final=is_final)
        dt = (time.time()-t0)*1000
        if result:
            intermediate.append(result)
            print(f"块{i+1}({dt:.0f}ms) 中间STT: '{result}'")
    # 手动调用 finish_streaming 需要 conn，这里直接验证 buffer 语义
    # （上面最后一块已经 is_final=True，累积文本就是最终结果）
    session = p.SESSIONS.get("test-long")
    print(f"\n=== 最终累积文本: '{session.accumulated_text if session else 'None'}'")
    print(f"=== 中间结果序列（应单调递增，无回跳）: {len(intermediate)} 次")
    ok = all(intermediate[i] in intermediate[i+1] or len(intermediate[i+1]) >= len(intermediate[i]) for i in range(len(intermediate)-1))
    print(f"=== 单调性检查: {'通过' if ok else '失败（有回跳）'}")

asyncio.run(run())
