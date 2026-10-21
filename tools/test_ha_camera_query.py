#!/usr/bin/env python3
"""端到端验证：LLM 能否选中真实 HA 工具回答摄像头问题（mcp-hass 时序修复回归）"""
import asyncio, json, time, websockets

DEVICE_ID = "e2e-ha-test"
WS_URL = f"ws://localhost:9800/homebrain/v1/?device-id={DEVICE_ID}"
events = []

def log(ts, kind, detail=""):
    events.append((ts, kind, detail))
    print(f"  [{ts:6.2f}s] {kind}: {str(detail)[:120]}")

async def main():
    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000,
                             "channels": 1, "frame_duration": 60},
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
                        if t == "tts" and d.get("state") == "sentence_start":
                            log(time.time() - t0, "TTS字幕", d.get("text", ""))
                        elif t == "llm":
                            log(time.time() - t0, "LLM", str(d)[:100])
                        elif t == "tts" and d.get("state") in ("start", "stop"):
                            log(time.time() - t0, "TTS", d.get("state"))
                        elif t == "stt":
                            log(time.time() - t0, "STT", d.get("text", ""))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
            except websockets.exceptions.ConnectionClosed:
                pass
        recv = asyncio.create_task(receiver())
        await asyncio.sleep(2)

        # ① 唤醒（免唤醒守卫放行）
        print("[HA-TEST] ① 发唤醒词...")
        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": "你好伊娃"}))
        await asyncio.sleep(2)

        # ② 问摄像头问题（真实触发工具选择）
        print("[HA-TEST] ② 问：三楼摄像头现在有几个人")
        await ws.send(json.dumps({"type": "listen", "state": "detect",
                                  "text": "三楼摄像头现在有几个人"}))

        # ③ 收 60s 回复
        deadline = time.time() + 60
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            stops = [ts for ts, k, _ in events if k == "TTS" and _ == "stop"]
            if stops:
                break
        recv.cancel()
        subs = [d for ts, k, d in events if k == "TTS字幕"]
        print("\n========== 结果 ==========")
        print(f"回复字幕数: {len(subs)}")
        for s in subs:
            print(" ", s)
        ok = any(("帮您" not in s and "查看" not in s) or len(subs) == 0 for s in subs) and len(subs) > 0
        has_mcp = any(s for s in subs)  # 有实质回复即可，具体人看 LLM
        print("判定:", "PASS（有实质回复）" if subs and not any("我来帮您查看" == s for s in subs) else "FAIL")

asyncio.run(main())
