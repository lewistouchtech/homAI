"""HomeBrain 免唤醒守卫（idle_guard）服务端全链路测试（阶段1：Web 终端免唤醒）
模拟 Web 终端常开推流行为（帧格式 PCM\x00 + 4B 时间戳 + PCM，hello 声明 features.idle_guard）：
  1. 阶段1：发非唤醒词语音（今天天气怎么样）→ 静音收尾 → 验证被守卫忽略（无 TTS 回复）
  2. 阶段2：发唤醒词语音（你好伊娃）→ 静音收尾 → 验证会话激活（LLM 短回复 TTS start）
判定：阶段1 无 TTS start 且阶段2 有 TTS start = PASS
"""
import asyncio
import json
import struct
import time

import numpy as np
import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-idleguard-001"
FRAME_SAMPLES = 960  # 60ms @16k
NORMAL_PCM = "/tmp/e2e_weather.pcm"   # 非唤醒词：今天天气怎么样
WAKE_PCM = "/tmp/e2e_wake.pcm"        # 唤醒词：你好伊娃（ChatTTS seed 1332）
SILENCE_FRAMES = 40                   # 40 帧 × 60ms = 2.4s 静音，触发 VAD 收束
WAIT_IGNORE = 14                      # 阶段1 观察窗
WAIT_WAKE = 30                        # 阶段2 观察窗（LLM 短回复）

saw_tts_start_p1 = False
saw_tts_start_p2 = False
saw_stt_p1 = False


def ts_head():
    """帧头：PCM\x00 + 4B 大端时间戳（服务端 AEC 参考信号格式）"""
    return b"PCM\x00" + struct.pack(">I", int(time.time() * 1000) & 0xFFFFFFFF)


async def send_pcm(ws, pcm):
    for i in range(0, len(pcm), FRAME_SAMPLES):
        await ws.send(ts_head() + pcm[i:i + FRAME_SAMPLES].tobytes())
        await asyncio.sleep(0.06)
    zero = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    for _ in range(SILENCE_FRAMES):
        await ws.send(ts_head() + zero.tobytes())
        await asyncio.sleep(0.06)


async def main():
    normal = np.fromfile(NORMAL_PCM, dtype=np.int16)
    wake = np.fromfile(WAKE_PCM, dtype=np.int16)
    print(f"[IDLE-GUARD] 非唤醒词 {len(normal)/16000:.2f}s，唤醒词 {len(wake)/16000:.2f}s")

    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
            "features": {"idle_guard": True},
        }))
        t0 = time.time()

        async def receiver():
            global saw_tts_start_p1, saw_tts_start_p2, saw_stt_p1
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
                            txt = d.get("text", "")
                            print(f"  [{ts:6.2f}s] STT: {txt[:40]}")
                            if phase["p"] == 1:
                                saw_stt_p1 = True
                        elif t == "tts" and d.get("state") == "start":
                            print(f"  [{ts:6.2f}s] TTS start")
                            if phase["p"] == 1:
                                saw_tts_start_p1 = True
                            elif phase["p"] == 2:
                                saw_tts_start_p2 = True
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
            except websockets.exceptions.ConnectionClosed:
                pass

        phase = {"p": 1}
        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(2)

        # ---- 阶段1：非唤醒词语音，应被守卫忽略 ----
        print("[IDLE-GUARD] 阶段1：会话未激活，发非唤醒词语音（今天天气怎么样）...")
        await send_pcm(ws, normal)
        await asyncio.sleep(WAIT_IGNORE)
        phase["p"] = 2

        # ---- 阶段2：唤醒词语音，应激活会话并短回复 ----
        print("[IDLE-GUARD] 阶段2：发唤醒词语音（你好伊娃）...")
        await send_pcm(ws, wake)
        await asyncio.sleep(WAIT_WAKE)

        print("\n===== 结果 =====")
        print(f"阶段1 TTS start（应 False）: {saw_tts_start_p1}")
        print(f"阶段1 STT 回显出现: {saw_stt_p1}（Web 终端下允许，关键是无 TTS）")
        print(f"阶段2 TTS start（应 True）: {saw_tts_start_p2}")
        ok = (not saw_tts_start_p1) and saw_tts_start_p2
        print(f"\n[IDLE-GUARD] 判定: {'PASS' if ok else 'FAIL'}")
        recv_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
