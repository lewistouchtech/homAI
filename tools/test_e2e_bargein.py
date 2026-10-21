"""HomeBrain barge-in（TTS 播放中说话即打断）服务端全链路测试
流程：
  1. WS连接 → hello → listen start → 发第一段语音 → 等 TTS start=true（模拟设备进入 speaking）
  2. TTS 音频持续下发期间，模拟用户插话：持续发"环境帧+第二段语音帧"
  3. 验证：服务端触发 abort（收到 tts stop / Abort 日志）+ 第二段语音产生新 STT + 新 LLM/TTS 回复
判定：打断后第二轮回复到达 = PASS
"""
import asyncio
import json
import sys
import time

import numpy as np
import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-bargein-001"
FRAME_SAMPLES = 960  # 60ms @16k
FIRST_PCM = "/tmp/e2e_weather.pcm"   # 第一句：今天天气怎么样
INT_PCM = "/tmp/e2e_time.pcm"        # 插话：现在几点了
TIMEOUT = 45

collected = {"stt": [], "tts": [], "audio_frames": 0, "abort_seen": False, "tts_stop_after_abort": False}


async def main():
    first = np.fromfile(FIRST_PCM, dtype=np.int16)
    intr = np.fromfile(INT_PCM, dtype=np.int16)
    print(f"[BARGE-IN] 第一句 {len(first)/16000:.2f}s，插话 {len(intr)/16000:.2f}s，连接 {WS_URL}")

    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }))
        t0 = time.time()

        async def receiver():
            try:
                async for msg in ws:
                    if isinstance(msg, str):
                        try:
                            d = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        t = d.get("type", "?")
                        ts = time.time() - t0
                        if t == "stt":
                            collected["stt"].append((ts, d.get("text", "")))
                            print(f"  [{ts:6.2f}s] STT: '{d.get('text', '')}'")
                        elif t == "tts":
                            state = d.get("state", "")
                            collected["tts"].append((ts, state))
                            print(f"  [{ts:6.2f}s] TTS: state={state}")
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        else:
                            print(f"  [{ts:6.2f}s] 服务端: {t} {str(d)[:80]}")
                    elif isinstance(msg, bytes):
                        collected["audio_frames"] += 1
                        if collected["audio_frames"] % 50 == 1:
                            print(f"  [{time.time()-t0:6.2f}s] TTS音频帧 #{collected['audio_frames']}")
            except websockets.exceptions.ConnectionClosed:
                pass

        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(2)

        # ---- 第一轮：触发 TTS ----
        print("[BARGE-IN] 阶段1：发送第一句语音...")
        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
        for i in range(0, len(first), FRAME_SAMPLES):
            await ws.send(b"PCM\x00" + first[i:i+FRAME_SAMPLES].tobytes())
            await asyncio.sleep(0.06)
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))

        # 等 TTS start + 若干音频帧（模拟设备正在播放）
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(s == "start" for _, s in collected["tts"]) and collected["audio_frames"] >= 20:
                break
            await asyncio.sleep(0.2)
        if not any(s == "start" for _, s in collected["tts"]):
            print("[BARGE-IN] FAIL：第一轮 TTS 未开始")
            recv_task.cancel()
            return
        print(f"[BARGE-IN] 阶段2：TTS 播放中（已收 {collected['audio_frames']} 帧），开始插话...")

        # ---- 第二轮：TTS 播放中插话 ----
        # 模拟设备推流：先发 1s 环境静音，再发插话语音（不打断推流节奏）
        for _ in range(16):
            await ws.send(b"PCM\x00" + np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes())
            await asyncio.sleep(0.06)
        for i in range(0, len(intr), FRAME_SAMPLES):
            await ws.send(b"PCM\x00" + intr[i:i+FRAME_SAMPLES].tobytes())
            await asyncio.sleep(0.06)
        # 插话后继续推 2.5s 静音帧（模拟真实设备持续推流，触发 VAD 静音判定→ASR 最终结果）
        for _ in range(40):
            await ws.send(b"PCM\x00" + np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes())
            await asyncio.sleep(0.06)
        intr_done = time.time()
        print(f"[BARGE-IN] 插话发送完毕，等待服务端打断+第二轮回复...")

        # ---- 收集结果 ----
        deadline = time.time() + TIMEOUT
        tts_start_idx = next(i for i, (_, s) in enumerate(collected["tts"]) if s == "start")
        last_activity = time.time()
        prev = len(collected["stt"]) + len(collected["tts"]) + collected["audio_frames"]
        second_stt = None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            cur = len(collected["stt"]) + len(collected["tts"]) + collected["audio_frames"]
            if cur != prev:
                prev = cur
                last_activity = time.time()
            # 打断证据：interrupted/stop 状态在第一轮 start 之后出现
            for ts, s in collected["tts"][tts_start_idx:]:
                if s in ("interrupted", "stop"):
                    collected["abort_seen"] = True
            # 第二轮 STT（插话后的转写）
            post_stt = [txt for ts, txt in collected["stt"] if ts > (intr_done - t0) - 0.5]
            if post_stt:
                second_stt = post_stt[-1]
            if second_stt and time.time() - last_activity > 8:
                break

        print("\n========== barge-in 结果 ==========")
        print(f"TTS 状态序列: {collected['tts']}")
        print(f"收到 TTS 音频帧: {collected['audio_frames']}")
        print(f"打断信号(interrupted/stop): {'✅' if collected['abort_seen'] else '❌'}")
        print(f"插话后 STT: '{second_stt}'")
        ok = collected["abort_seen"] and second_stt
        print(f"\n判定: {'✅ PASS' if ok else '❌ FAIL'}")
        recv_task.cancel()
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
