# 实证 ⑪：说话后静默，120s 无语音是否触发服务端"告别+断连"
# 注意：Web 终端帧格式 = b"PCM\x00" + 4B大端时间戳 + PCM（AEC 参考信号格式）
import asyncio, json, struct, sys, time
import numpy as np
import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-wsrecon-003"
WAKE_PCM = "/tmp/e2e_wake.pcm"  # "你好伊娃"（ChatTTS seed 1332）
SILENCE_FRAMES = 40             # 2.4s 静音收尾，触发 VAD 收束

def ts_head():
    return b"PCM\x00" + struct.pack(">I", int(time.time() * 1000) & 0xFFFFFFFF)

async def send_frame(ws, pcm_chunk):
    await ws.send(ts_head() + pcm_chunk.tobytes())
    await asyncio.sleep(0.06)

async def main():
    events = []
    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
            "features": {"aec": True, "idle_guard": True},
        }))
        t0 = time.time()

        async def sender():
            # 先推唤醒词+静音收尾（激活会话+刷新活动时间），之后纯静音推流到 205s
            wake = np.fromfile(WAKE_PCM, dtype=np.int16)
            for i in range(0, len(wake), 960):
                await send_frame(ws, wake[i:i + 960])
            zero = np.zeros(960, dtype=np.int16)
            for _ in range(SILENCE_FRAMES):
                await send_frame(ws, zero)
            while time.time() - t0 < 205:
                await send_frame(ws, zero)

        async def pinger():
            # 心跳：15s 一次（修复后 enable_websocket_ping=true，应收到 pong 且连接不再被 180s 检查断开）
            got_pong = False
            while time.time() - t0 < 200:
                await asyncio.sleep(15)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return
            if not got_pong:
                events.append((round(time.time() - t0, 1), "NO-PONG", "200s 内未收到 pong"))

        sender_task = asyncio.ensure_future(sender())
        pinger_task = asyncio.ensure_future(pinger())
        try:
            while True:
                el = time.time() - t0
                if el > 200:
                    events.append((200.0, "STILL-OPEN", "200s（>180s 超时线）连接仍在，心跳保活生效"))
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=16)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    continue
                d = json.loads(msg)
                tag = d.get("type")
                if tag == "pong":
                    if not any(e[1] == "PONG" for e in events):
                        events.append((round(el, 1), "PONG", "心跳回复正常"))
                    continue
                if tag in ("tts", "llm", "server", "stt"):
                    events.append((round(el, 1), tag, str(d)[:90]))
                if tag == "server":
                    break
        except websockets.exceptions.ConnectionClosed as e:
            events.append((round(time.time() - t0, 1), "CLOSED", repr(e)[:120]))
        sender_task.cancel(); pinger_task.cancel()
    print("===== 心跳保活验证（>180s 超时线观察）=====")
    for e in events:
        print(e)

asyncio.get_event_loop().run_until_complete(main())
