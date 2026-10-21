#!/usr/bin/env python3
"""TTS 链路对照实验：对比「ChatTTS 原始合成」与「看板流式链路还原」的音频。
产出到 tmp/tts_experiment/：
  direct.wav   — 直接 POST chat_tts_server 合成的原始 wav（24000Hz）
  stream.wav   — 模拟看板 WS 收到的裸 PCM（按 16000Hz 解释）包装的 wav
  分析结论打印到 stdout：时长比、频谱质心比（>1.1 或 <0.9 视为变调/变速实锤）
"""
import asyncio, json, struct, wave, sys, os, time
import aiohttp
import numpy as np

WS_URL = "ws://localhost:9800"
TTS_URL = "http://localhost:8082/api/tts"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "tmp", "tts_experiment")
TEXT = "主人您好，我是伊娃，很高兴为您服务。"


def save_wav(path, pcm_bytes, sample_rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


def analyze(path, rate):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16)
    dur = n / rate
    # 频谱质心（能量加权平均频率，变调会明显偏移）
    spec = np.abs(np.fft.rfft(data.astype(np.float32)))
    freqs = np.fft.rfftfreq(len(data), 1 / rate)
    centroid = float((spec * freqs).sum() / (spec.sum() + 1e-9))
    rms = float(np.sqrt((data.astype(np.float32) ** 2).mean()))
    return dur, centroid, rms


async def direct_synthesize():
    async with aiohttp.ClientSession() as s:
        async with s.post(TTS_URL, json={"text": TEXT, "speaker_seed": 1332},
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            raw = await r.read()
    path = os.path.join(OUT_DIR, "direct.wav")
    with open(path, "wb") as f:
        f.write(raw)
    return path


async def stream_capture():
    """模拟看板：hello 声明 pcm/16000 → 发文本 → 收二进制 PCM 帧"""
    import aiohttp
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(WS_URL, headers={"device-id": "tts-exp-analyzer"})
    await ws.send_json({
        "type": "hello",
        "version": 1,
        "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1,
                         "frame_duration": 60},
    })
    await asyncio.sleep(1)
    await ws.send_json({"type": "listen", "state": "detect", "text": TEXT})
    pcm = bytearray()
    gaps = []  # 帧间到达间隔（毫秒），>300ms 即播放时停顿实锤
    last_bin = None
    idle, last = 0, time.time()
    stopped = False
    while time.time() - last < 30 and not stopped:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=35)
        except asyncio.TimeoutError:
            break
        if msg.type == aiohttp.WSMsgType.BINARY:
            now = time.time()
            if last_bin is not None:
                gap_ms = (now - last_bin) * 1000
                gaps.append(gap_ms)
                if gap_ms > 300:
                    print(f"[停顿实锤] 帧间隔 {gap_ms:.0f}ms（累计 {len(pcm)}B）")
            gaps_t0 = None
            last_bin = now
            pcm.extend(msg.data)
            last = time.time()
        elif msg.type == aiohttp.WSMsgType.TEXT:
            d = json.loads(msg.data)
            print(f"[WS消息] {json.dumps(d, ensure_ascii=False)[:120]}")
            if d.get("type") == "tts" and d.get("state") == "stop":
                stopped = True
    await ws.close()
    await session.close()
    path = os.path.join(OUT_DIR, "stream.wav")
    save_wav(path, bytes(pcm), 16000)
    # 段间停顿分析：60ms 帧应连续到达，>300ms 的间隔在播放侧表现为明显停顿
    big = [g for g in gaps if g > 300]
    print(f"[段间接缝] 二进制帧 {len(gaps) + 1} 个，>300ms 停顿 {len(big)} 处"
          + (f"，最长 {max(gaps):.0f}ms" if gaps else ""))
    return path, len(pcm)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    direct = asyncio.run(direct_synthesize())
    stream, nbytes = asyncio.run(stream_capture())
    print(f"看板收流字节: {nbytes}")
    d0, c0, r0 = analyze(direct, 24000)
    d1, c1, r1 = analyze(stream, 16000)
    print(f"[原始合成 24k] 时长 {d0:.2f}s 质心 {c0:.0f}Hz RMS {r0:.0f}")
    print(f"[链路还原 16k] 时长 {d1:.2f}s 质心 {c1:.0f}Hz RMS {r1:.0f}")
    print(f"时长比 {d1/d0:.3f}（≈1 无变速；>1.15 慢放实锤；<0.87 加速实锤）")
    print(f"质心比 {c1/c0:.3f}（≈1 无变调；明显偏离 = 链路变调）")
    if 0.9 <= d1 / d0 <= 1.1 and 0.85 <= c1 / c0 <= 1.18:
        print("结论: 链路基本无损，「难听」不在采样率链路（音色/内容/播放设备问题）")
    else:
        print("结论: 链路存在变速/变调实锤，需修服务端重采样")


if __name__ == "__main__":
    main()
