"""对比测试：int16 直接传入 vs float32 归一化，验证 FunASR 输入格式要求"""
import sys, os
import numpy as np
sys.path.insert(0, os.getcwd())
from core.providers.asr.fun_local import ASRProvider

pcm = np.fromfile("/tmp/test_asr_long.pcm", dtype=np.int16)
config = {"type": "fun_local", "model_dir": "models/paraformer-zh-streaming", "output_dir": "tmp/", "streaming": "true", "sampleRate": 16000}
p = ASRProvider(config, False)

print("=== 方式A：int16 直接传入（当前代码） ===")
r = p.model.generate(input=pcm, language="auto", use_itn=True)
print("结果:", r[0]["text"] if r else "空")

print("\n=== 方式B：float32 归一化（官方demo） ===")
r = p.model.generate(input=pcm.astype(np.float32) / 32768.0, language="auto", use_itn=True)
print("结果:", r[0]["text"] if r else "空")

print("\n=== 方式C：int16 切600ms块流式（非流式generate整段） ===")
r = p.model.generate(input=pcm.tobytes(), language="auto", use_itn=True)
print("结果:", r[0]["text"] if r else "空")
