"""HomeBrain 多轮免唤醒（TTS 播完保持聆听 10 秒回 idle）服务端全链路测试
模拟设备 AutoStop 模式真实行为：
  1. WS连接 → hello → listen start(auto) → 第一轮语音 → 服务端 TTS 回复
  2. 等 TTS 播完（收到 tts stop，模拟设备回 Listening 但不发新 listen start）
  3. 持续推流节奏中直接说第二轮话（免唤醒）→ 静音触发服务端 VAD 收束
  4. 验证：第二轮 STT + 第二轮 TTS start
判定：第一轮 stop 后出现第二轮 start = PASS（免唤醒多轮链路通）
"""
import asyncio
import json
import time

import numpy as np
import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-multiturn-001"
FRAME_SAMPLES = 960  # 60ms @16k
FIRST_PCM = "/tmp/e2e_weather.pcm"   # 第一轮：今天天气怎么样
SECOND_PCM = "/tmp/e2e_time.pcm"     # 第二轮（免唤醒）：现在几点了
TIMEOUT = 60

events = []  # (ts, kind, detail)


def log(ts, kind, detail=""):
    events.append((ts, kind, detail))
    print(f"  [{ts:6.2f}s] {kind}: {str(detail)[:100]}")


async def main():
    first = np.fromfile(FIRST_PCM, dtype=np.int16)
    second = np.fromfile(SECOND_PCM, dtype=np.int16)
    print(f"[MULTI-TURN] 第一轮 {len(first)/16000:.2f}s，第二轮 {len(second)/16000:.2f}s，连接 {WS_URL}")

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
                            log(ts, "STT", d.get("text", ""))
                        elif t == "tts":
                            log(ts, "TTS", d.get("state", ""))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        else:
                            log(ts, t, str(d)[:80])
                    elif isinstance(msg, bytes):
                        pass  # TTS 音频帧不逐帧打印
            except websockets.exceptions.ConnectionClosed:
                pass

        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(2)

        # ---- 第一轮 ----
        print("[MULTI-TURN] 阶段1：第一轮语音（今天天气怎么样）...")
        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
        for i in range(0, len(first), FRAME_SAMPLES):
            await ws.send(b"PCM\x00" + first[i:i+FRAME_SAMPLES].tobytes())
            await asyncio.sleep(0.06)
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "listen", "state": "stop"}))

        # ---- 等 TTS 播完（stop 状态）----
        deadline = time.time() + 45
        first_stop = None
        while time.time() < deadline:
            stops = [ts for ts, kind, detail in events
                     if kind == "TTS" and detail == "stop"]
            if stops:
                first_stop = stops[0]
                break
            await asyncio.sleep(0.3)
        if first_stop is None:
            print("[MULTI-TURN] FAIL：第一轮 TTS 未完成（无 stop）")
            recv_task.cancel()
            return
        print(f"[MULTI-TURN] 阶段2：第一轮 TTS 已播完（{first_stop:.2f}s），模拟设备回 Listening，保持聆听...")

        # ---- 第二轮：免唤醒直接说话（不 listen start，与真实 AutoStop 设备一致）----
        for _ in range(16):  # 1s 环境静音
            await ws.send(b"PCM\x00" + np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes())
            await asyncio.sleep(0.06)
        for i in range(0, len(second), FRAME_SAMPLES):
            await ws.send(b"PCM\x00" + second[i:i+FRAME_SAMPLES].tobytes())
            await asyncio.sleep(0.06)
        for _ in range(40):  # 2.5s 静音触发服务端 VAD 收束
            await ws.send(b"PCM\x00" + np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes())
            await asyncio.sleep(0.06)
        second_sent_at = time.time() - t0
        print(f"[MULTI-TURN] 第二轮发送完毕（{second_sent_at:.2f}s），等待免唤醒第二轮回复...")

        # ---- 收集第二轮结果 ----
        deadline = time.time() + TIMEOUT
        second_stt = None
        second_tts_start = None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            post_stt = [detail for ts, kind, detail in events
                        if kind == "STT" and ts > first_stop + 0.5]
            if post_stt:
                second_stt = post_stt[-1]
            post_start = [ts for ts, kind, detail in events
                          if kind == "TTS" and detail == "start" and ts > first_stop + 0.5]
            if post_start:
                second_tts_start = post_start[0]
            if second_stt and second_tts_start:
                break
        # 再等一会让第二轮回复充分
        await asyncio.sleep(5)

        print("\n========== 多轮免唤醒结果 ==========")
        print(f"全部事件序列: {[(round(ts,1), k, str(d)[:20]) for ts, k, d in events]}")
        print(f"第二轮 STT: '{second_stt}'")
        print(f"第二轮 TTS start: {'%.2fs' % second_tts_start if second_tts_start else '无'}")
        ok = bool(second_stt) and second_tts_start is not None
        print(f"\n判定: {'✅ PASS' if ok else '❌ FAIL'}")
        recv_task.cancel()
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
