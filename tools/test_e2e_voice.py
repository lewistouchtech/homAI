"""HomeBrain 端到端全链路测试客户端
流程：WS连接 → hello → listen start → PCM音频帧(60ms) → listen stop → 收集 stt/llm/tts/音频
验证：流式ASR增量、LLM触发、TTS流式下发
"""
import asyncio
import json
import sys
import time
import wave

import numpy as np
import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-e2e-001"
PCM_FILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test_asr_long.pcm"
FRAME_SAMPLES = 960  # 60ms @16k
TIMEOUT = 60

collected = {"stt": [], "llm": [], "tts": [], "audio_frames": 0, "other": []}


async def main():
    pcm = np.fromfile(PCM_FILE, dtype=np.int16)
    print(f"[E2E] 音频: {len(pcm)/16000:.2f}s, 连接 {WS_URL}")

    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}) as ws:
        # 1. hello
        hello = {
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }
        await ws.send(json.dumps(hello))
        t0 = time.time()

        # 2. 启动消息接收任务
        async def receiver():
            try:
                async for msg in ws:
                    if isinstance(msg, str):
                        try:
                            d = json.loads(msg)
                        except json.JSONDecodeError:
                            collected["other"].append(msg[:100])
                            continue
                        t = d.get("type", "?")
                        ts = time.time() - t0
                        if t == "stt":
                            collected["stt"].append((ts, d.get("text", "")))
                            print(f"  [{ts:6.2f}s] STT: '{d.get('text', '')}'")
                        elif t == "llm":
                            collected["llm"].append((ts, str(d)[:80]))
                            print(f"  [{ts:6.2f}s] LLM: {d}")
                        elif t == "tts":
                            state = d.get("state", "")
                            collected["tts"].append((ts, state))
                            print(f"  [{ts:6.2f}s] TTS: state={state}")
                        elif t in ("hello", "welcome"):
                            print(f"  [{ts:6.2f}s] 服务端: {str(d)[:120]}")
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        else:
                            collected["other"].append((ts, str(d)[:80]))
                    elif isinstance(msg, bytes):
                        collected["audio_frames"] += 1
                        if collected["audio_frames"] <= 2:
                            print(f"  [{time.time()-t0:6.2f}s] 收到音频帧 {len(msg)}B")
            except websockets.exceptions.ConnectionClosed:
                pass

        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(2)  # 等 welcome

        # 3. listen start
        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))

        # 4. 发送 PCM 帧（60ms 间隔）
        print("[E2E] 开始发送音频帧...")
        for i in range(0, len(pcm), FRAME_SAMPLES):
            frame = pcm[i:i+FRAME_SAMPLES].tobytes()
            await ws.send(b"PCM\x00" + frame)
            await asyncio.sleep(0.06)
        audio_done = time.time()
        print(f"[E2E] 音频发送完毕 ({audio_done-t0:.2f}s)")

        # 5. listen stop
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))
        print("[E2E] 已发送 listen stop")

        # 6. 等待 LLM/TTS
        deadline = time.time() + TIMEOUT
        last_activity = time.time()
        prev_llm = len(collected["llm"])
        prev_tts = len(collected["tts"])
        while time.time() < deadline:
            await asyncio.sleep(1)
            cur = len(collected["llm"]) + len(collected["tts"])
            if cur != prev_llm + prev_tts:
                prev_llm, prev_tts = len(collected["llm"]), len(collected["tts"])
                last_activity = time.time()
            if time.time() - last_activity > 12 and collected["audio_frames"] > 0:
                break

        print("\n========== 端到端结果 ==========")
        print(f"STT 中间+最终: {len(collected['stt'])} 条")
        print(f"LLM 消息: {len(collected['llm'])} 条")
        print(f"TTS 状态: {len(collected['tts'])} 条")
        print(f"收到音频帧: {collected['audio_frames']}")
        full_stt = collected["stt"][-1][1] if collected["stt"] else "(无)"
        print(f"最终STT文本: '{full_stt}'")
        for ts, s in collected["llm"][:5]:
            print(f"LLM[{ts:.1f}s]: {s[:100]}")

        recv_task.cancel()
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
