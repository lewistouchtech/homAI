#!/usr/bin/env python3
"""HomeBrain LLM 一键切换脚本（开发阶段用 MacBook 本机模型）

用法:
    python3 scripts/switch_llm.py dev-local      # 本机 oMLX qwen3.8-flash（免费，日常开发）
    python3 scripts/switch_llm.py dev-local-4b   # LM Studio qwen3-4b-2507（非思考小模型，J2 路由）
    python3 scripts/switch_llm.py dev-cloud      # 云端 qwen3.7-plus（质量对比）
    python3 scripts/switch_llm.py prod-cloud     # 云端 qwen3.6-plus（原生产配置）
    python3 scripts/switch_llm.py status         # 查看当前配置

修改 data/.config.yaml（运行时优先级最高的配置），改后需重启服务生效。
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
RUNTIME_CFG = BASE / "data" / ".config.yaml"

# 预设: (base_url, model_name, api_key, max_tokens)
PRESETS = {
    "dev-local": (
        "http://127.0.0.1:8000/v1",
        "Qwen3.8-Flash-Next-REAP-288-MLX-4bit",
        "1234",
        "500",
    ),
    # LM Studio 本地 4B（非思考 Instruct，无 reasoning 泄漏，tools 正常）——J2 路由小模型
    "dev-local-4b": (
        "http://127.0.0.1:1234/v1",
        "qwen3-4b-instruct-2507",
        "lm-studio",
        "500",
    ),
    "dev-cloud": (
        "https://coding.dashscope.aliyuncs.com/v1",
        "qwen3.7-plus",
        "sk-sp-7db179faa1a84197a283b8b3ad073ef0",
        "500",
    ),
    "prod-cloud": (
        "https://coding.dashscope.aliyuncs.com/v1",
        "qwen3.6-plus",
        "sk-sp-7db179faa1a84197a283b8b3ad073ef0",
        "500",
    ),
}


def read_block(text: str) -> str:
    """提取 UserDefinedLLM 块（用于 status）"""
    m = re.search(r"(  UserDefinedLLM:\n(?:    .*\n)+)", text)
    return m.group(1) if m else "(未找到)"


def switch(preset_name: str) -> bool:
    if preset_name not in PRESETS:
        print(f"未知预设: {preset_name}，可选: {', '.join(PRESETS)}")
        return False
    base_url, model, api_key, max_tokens = PRESETS[preset_name]

    if not RUNTIME_CFG.exists():
        print(f"运行时配置不存在: {RUNTIME_CFG}")
        return False

    text = RUNTIME_CFG.read_text(encoding="utf-8")
    new_block = (
        f"  UserDefinedLLM:\n"
        f"    api_key: {api_key}\n"
        f"    base_url: {base_url}\n"
        f"    max_tokens: {max_tokens}\n"
        f"    model_name: {model}\n"
        f"    temperature: 0.7\n"
        f"    type: openai\n"
        f"    url: {base_url}\n"
    )
    m = re.search(r"  UserDefinedLLM:\n(?:    .*\n)+", text)
    if not m:
        print("未找到 UserDefinedLLM 配置块")
        return False
    text = text[:m.start()] + new_block + text[m.end():]
    RUNTIME_CFG.write_text(text, encoding="utf-8")
    print(f"✓ 已切换到 {preset_name}: model={model}, base_url={base_url}")
    print("  需重启服务生效: bash restart.sh")
    return True


def status():
    text = RUNTIME_CFG.read_text(encoding="utf-8") if RUNTIME_CFG.exists() else ""
    block = read_block(text)
    print("当前 UserDefinedLLM 配置（data/.config.yaml）:")
    print(block)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    else:
        sys.exit(0 if switch(cmd) else 1)
