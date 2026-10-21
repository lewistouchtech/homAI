# -*- coding: utf-8 -*-
"""模型方案（云端/本地双栈）机制。

设计（2026-09-28 主人裁决）：
- 方案只是预设：一键把 ASR/LLM/TTS 切到预设引擎，每个环节仍可单独覆盖（自定义混合）。
- 联动配置按当前所选引擎的部署属性自动推导，不允许出现不一致组合：
  · 工具筛选：仅本地 LLM 需要（给 16K 小模型省上下文），云端 LLM 自动旁路
  · 断句判断（Decision endpoint）：仅本地 ASR 需要，云端 ASR（豆包 seedasr 自带
    VAD/断句/标点）自动停用
- 后台"模型方案"开关由技术人员操作；本模块是所有判定逻辑的唯一入口。
"""
from typing import Any, Dict, Optional

# ===== 方案预设：一键切换的引擎组合 =====
# 仅引用 config.yaml 中真实存在的 provider；新增 provider 时同步这里
MODEL_STACK_PRESETS: Dict[str, Dict[str, str]] = {
    "local": {
        "label": "本地模型版（隐私优先，数据不出户）",
        "ASR": "SenseVoiceASR",
        "TTS": "MossNano",
        "VLLM": "oMLX-Qwen3.6-35B-VL",
        # LLM 不在预设内：本地 LLM 由"AI模型配置"页的路由模式（单模型/智能分类/委托）决定
    },
    "cloud": {
        "label": "云端豆包版（demo/低延迟，数据经火山引擎）",
        "ASR": "豆包流式ASR",
        "LLM": "豆包大模型",
        "TTS": "火山双流TTS",
        "VLLM": "豆包视觉",
    },
}

# ===== 部署属性判定 =====
# 显式 deployment 字段 > 内置表 > base_url 启发式 > 默认 cloud
_LOCAL_NAMES = {
    "ASR": {"FunASR", "SenseVoiceASR", "SherpaASR", "sherpa_onnx_local", "fun_local"},
    "TTS": {"MossNano"},
    "LLM": set(),
}
_LOCAL_URL_HINTS = ("127.0.0.1", "localhost", "0.0.0.0", "192.168.", "10.0.", "172.16.")


def get_module_deployment(config: Dict[str, Any], module: str) -> str:
    """判定某环节当前所选引擎的部署属性，返回 'cloud' | 'local'。"""
    selected = (config.get("selected_module") or {}).get(module) or ""
    block = ((config.get(module) or {}).get(selected)) or {}
    explicit = str(block.get("deployment") or "").strip().lower()
    if explicit in ("cloud", "local"):
        return explicit
    if selected in _LOCAL_NAMES.get(module, set()):
        return "local"
    # LLM 等无内置表的模块：按 base_url 推断
    url = str(block.get("base_url") or block.get("url") or block.get("ws_url") or "")
    if url:
        return "local" if any(h in url for h in _LOCAL_URL_HINTS) else "cloud"
    return "cloud"


def tool_filter_active(config: Dict[str, Any]) -> bool:
    """工具筛选是否启用。mode: auto(默认,跟随LLM部署) | on | off。"""
    mode = str((config.get("tool_filter") or {}).get("mode") or "auto").lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return get_module_deployment(config, "LLM") == "local"


def tool_prefilter_active(config: Dict[str, Any]) -> bool:
    """云端 LLM 的工具预筛选是否启用（2026-10-12）。

    auto(默认)：云端 LLM 启用（本地 LLM 仍走老的严格筛选）；prefilter：强制启用；
    off：全部旁路（回到「60 个工具全带」）。on 沿用老筛选语义，不开预筛选。
    """
    mode = str((config.get("tool_filter") or {}).get("mode") or "auto").lower()
    if mode == "prefilter":
        return True
    if mode == "auto":
        return get_module_deployment(config, "LLM") != "local"
    return False


def tool_filter_reason(config: Dict[str, Any]) -> str:
    mode = str((config.get("tool_filter") or {}).get("mode") or "auto").lower()
    if mode == "on":
        return "手动强制开启"
    if mode == "off":
        return "手动强制关闭"
    dep = get_module_deployment(config, "LLM")
    return "LLM 为本地模型，筛选生效" if dep == "local" else "LLM 为云端模型，已自动旁路"


def endpoint_judge_active(config: Dict[str, Any]) -> bool:
    """本地断句判断（Decision endpoint）是否启用。云端 ASR 自动停用。"""
    if config.get("decision_endpoint_follow_asr") is False:
        # 显式配置为不跟随（保留手动控制权）
        return True
    return get_module_deployment(config, "ASR") == "local"


def endpoint_judge_reason(config: Dict[str, Any]) -> str:
    dep = get_module_deployment(config, "ASR")
    if config.get("decision_endpoint_follow_asr") is False:
        return "已配置为不跟随 ASR（手动控制）"
    return "ASR 为本地，断句判断生效" if dep == "local" \
        else "ASR 为云端（自带VAD/断句），已自动停用"


def get_stack_state(config: Dict[str, Any]) -> Dict[str, Any]:
    """当前方案状态：各环节所选引擎+部署属性+联动配置（供后台页展示）。"""
    links: Dict[str, Any] = {}
    for module in ("ASR", "LLM", "TTS", "VLLM"):
        selected = (config.get("selected_module") or {}).get(module) or ""
        links[module] = {
            "selected": selected,
            "deployment": get_module_deployment(config, module),
        }
    # 当前命中哪个预设（全匹配才算；否则 custom）
    stack = "custom"
    for key, preset in MODEL_STACK_PRESETS.items():
        if all(links[m]["selected"] == p for m, p in preset.items()
               if m != "label"):
            stack = key
            break
    return {
        "stack": stack,
        "presets": {k: {"label": v["label"], **{m: v[m] for m in ("ASR", "LLM", "TTS", "VLLM") if m in v}}
                    for k, v in MODEL_STACK_PRESETS.items()},
        "links": links,
        "tool_filter": {"active": tool_filter_active(config), "reason": tool_filter_reason(config)},
        "endpoint_judge": {"active": endpoint_judge_active(config), "reason": endpoint_judge_reason(config)},
    }
