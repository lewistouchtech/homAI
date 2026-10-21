"""HomeBrain 告别意图（handle_exit_intent）服务端全链路测试
流程：
  1. WS连接 → hello
  2. 发送文本 detect 消息（明确告别语）→ startToChat → LLM function_call
  3. LLM 应调用 handle_exit_intent → say_goodbye 告别语 TTS → close_after_chat=True
  4. TTS LAST 后服务端主动关闭连接
判定：收到告别语 TTS start 且随后 WS 被服务端关闭 = PASS
"""
import asyncio
import json
import time

import websockets

WS_URL = "ws://localhost:9800/homebrain/v1/"
DEVICE_ID = "test-exit-001"
GOODBYE_TEXT = "我这就出门了，再见"
TIMEOUT = 60

events = []


def log(ts, kind, detail=""):
    events.append((ts, kind, detail))
    print(f"  [{ts:6.2f}s] {kind}: {str(detail)[:120]}")


async def main():
    print(f"[EXIT-INTENT] 连接 {WS_URL}，发送告别文本：{GOODBYE_TEXT}")

    async with websockets.connect(WS_URL, additional_headers={"device-id": DEVICE_ID}) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "device_id": DEVICE_ID,
            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }))
        t0 = time.time()
        closed_by_server = {"flag": False, "ts": None}

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
                        elif t == "llm":
                            log(ts, "LLM", str(d.get("content", ""))[:80])
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        else:
                            log(ts, t, str(d)[:80])
            except websockets.exceptions.ConnectionClosed as e:
                closed_by_server["flag"] = True
                closed_by_server["ts"] = time.time() - t0
                print(f"  [{time.time()-t0:6.2f}s] WS_CLOSED(code={e.code if e.code else 'n/a'})")
            finally:
                # async for 正常结束也代表连接终止（服务端发 close 帧后迭代正常退出）
                if not closed_by_server["flag"]:
                    closed_by_server["flag"] = True
                    closed_by_server["ts"] = time.time() - t0
                    print(f"  [{time.time()-t0:6.2f}s] WS_CLOSED（async for 正常结束，服务端 close 帧）")

        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(2)

        # 发送告别文本（detect 路径：非唤醒词 → startToChat）
        await ws.send(json.dumps({
            "type": "listen", "state": "detect", "text": GOODBYE_TEXT,
        }))
        sent_ts = time.time() - t0
        print(f"[EXIT-INTENT] 告别文本已发送（{sent_ts:.2f}s），等待 LLM 调用 handle_exit_intent...")

        # 等待：TTS start（告别语）+ 连接被服务端关闭
        deadline = time.time() + TIMEOUT
        goodbye_start = None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            starts = [ts for ts, kind, detail in events
                      if kind == "TTS" and detail == "start" and ts > sent_ts]
            if starts:
                goodbye_start = starts[0]
            if goodbye_start and closed_by_server["flag"]:
                break
        # 给关闭留一点时间
        await asyncio.sleep(3)

        print("\n========== 告别意图结果 ==========")
        print(f"全部事件序列: {[(round(ts,1), k, str(d)[:24]) for ts, k, d in events]}")
        print(f"告别语 TTS start: {'%.2fs' % goodbye_start if goodbye_start else '无'}")
        print(f"服务端主动关闭: {'是 @%.2fs' % closed_by_server['ts'] if closed_by_server['flag'] else '否'}")
        ok = bool(goodbye_start) and closed_by_server["flag"]
        print(f"\n判定: {'✅ PASS' if ok else '❌ FAIL'}")
        recv_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
