#!/usr/bin/env python3
"""重生填充音/过渡句，统一音色（MOSS-Nano zh_3 参考音频，与正式回复一致）

背景：填充音与过渡句为预录 wav。【2026-09-11 已改】不再用 ChatTTS seed
（旧版读 tts_config.voice 当 int seed，已不成立：voice 现为 'zh_3' 音色名，
且 8082 ChatTTS 服务与 provider 均已删/归档，见 archive/chattts_removed_20260911）。
运行：先起 8086（cd ../../tts-moss-nano && .venv/bin/python moss_nano_server.py），
再 python3 tools/regenerate_filler_voice.py。
"""
import os

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server/homebrain-server
ASSETS = os.path.join(BASE, "config", "assets")
FILLERS_DIR = os.path.join(ASSETS, "fillers")
TRANSITION_DIR = os.path.join(ASSETS, "transition")
TTS_URL = "http://127.0.0.1:8086/api/tts"   # MOSS-Nano 唯一本地 TTS

FILLER_TEXTS = ["哦哦", "嗯", "嗯呢", "好嘞", "来啦"]
TRANSITION_TEXTS = ["我这就去看看", "稍等我查一查", "让我查一下"]


def _current_voice():
    """读取当前生效音色，避免硬编码与运行时漂移（2026-09-17 修：
    脚本原硬编码 zh_3，而运行时已切到 zh_11，导致填充音与正式回复音色不一致）。"""
    for p in (os.path.join(BASE, "data", ".config.yaml"),
              os.path.join(BASE, "config.yaml")):
        try:
            import yaml
            cfg = yaml.safe_load(open(p, encoding="utf-8")) or {}
            v = (cfg.get("TTS", {}) or {}).get("MossNano", {}).get("voice")
            if v:
                return str(v)
        except Exception:
            continue
    return "zh_3"


def synth(text, out_path, voice):
    """走 8086 请求体 {text, voice, speed, volume}"""
    r = requests.post(TTS_URL, json={
        "text": text,
        "voice": voice,
        "speed": 1.0,
        "volume": 5.0,
    }, timeout=90)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    print(f"  ✓ {os.path.basename(out_path)}  {len(r.content)}B")


def main():
    voice = _current_voice()
    print(f"[音色统一] MOSS-Nano {voice} → 重生成填充音+过渡句")

    # 备份旧文件
    ts = __import__("time").strftime("%Y%m%d_%H%M%S")
    for d in (FILLERS_DIR, TRANSITION_DIR):
        if os.path.isdir(d):
            bak = d.rstrip("/") + f".bak_{ts}"
            __import__("shutil").copytree(d, bak)
            print(f"  已备份 {d} → {bak}")

    print("[填充音]")
    for t in FILLER_TEXTS:
        synth(t, os.path.join(FILLERS_DIR, f"filler_{t}.wav"), voice)
    # filler_mm.wav（默认单文件回退，内容为"嗯"）
    synth("嗯", os.path.join(ASSETS, "filler_mm.wav"), voice)

    print("[过渡句]")
    for t in TRANSITION_TEXTS:
        synth(t, os.path.join(TRANSITION_DIR, f"transition_{t}.wav"), voice)

    print(f"完成：全部预录音频已统一为 MOSS {voice} 音色（与回复一致）")


if __name__ == "__main__":
    main()
