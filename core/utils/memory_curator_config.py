"""记忆整理模型的配置（2026-10-13，主人要求后台可配 + 同步到服务器信息）。

「记忆整理」= 主人说「把这段记下来」时，用一个**强模型**把前面几轮对话整理成一条
完整的记忆。它跟 mem0 的逐句抽取（本地 8B，次数多、要快）是两件事：次数少、要求高，
所以单独一个配置项。两个版本共用同一套代码，只改配置：
  - 本地版：mode=local，指向本机常驻的强模型（默认 oMLX 上的 Qwen3.6-35B）
  - 云端版：mode=main，沿用主对话大模型（云端）
  - 兜底  ：mode=extractor，用 mem0 的抽取模型（本地 8B）；任何模式失败也都退回它

配置存 data/.memory_curator.json（机器私有，不入库；热生效，无需重启）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

MODES = ("local", "main", "extractor")
MODE_LABELS = {
    "local": "本地强模型",
    "main": "沿用主对话大模型",
    "extractor": "mem0 抽取模型（本地 8B）",
}
DEFAULT_LOCAL_MODEL = "Qwen3.6-35B-A3B-Fable-5-Distill-4bit-DWQ-Vision-MLX"


def _path() -> Path:
    return _DATA_DIR / ".memory_curator.json"


def load_raw(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        d = json.loads(Path(path or _path()).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_raw(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path or _path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _mem0_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return ((config.get("Memory") or {}).get("mem0") or {}) if config else {}


def _expand(v: Any) -> str:
    """${VAR} 引用展开；不存在的变量按空串处理（不把字面量 ${VAR} 当密钥）。"""
    s = str(v or "")
    if "${" in s:
        s = os.path.expandvars(s)
        if "${" in s:
            return ""
    return s.strip()


def extractor_endpoint(config: Dict[str, Any]) -> Dict[str, str]:
    m = _mem0_cfg(config)
    return {
        "base_url": _expand(m.get("llm_base_url") or m.get("base_url")),
        "api_key": _expand(m.get("llm_api_key") or m.get("api_key")),
        "model": _expand(m.get("llm_model")),
    }


def main_llm_endpoint(config: Dict[str, Any]) -> Dict[str, str]:
    sel = ((config.get("selected_module") or {}).get("LLM")) if config else None
    e = ((config.get("LLM") or {}).get(sel) or {}) if sel else {}
    return {
        "base_url": _expand(e.get("base_url") or e.get("url")),
        "api_key": _expand(e.get("api_key")),
        "model": _expand(e.get("model_name") or e.get("model")),
        "name": str(sel or ""),
    }


def resolve(config: Dict[str, Any], raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """解析出实际要用的端点。返回 {mode, base_url, api_key, model, label, fallback{...}}。"""
    raw = load_raw() if raw is None else raw
    mode = raw.get("mode") if raw.get("mode") in MODES else "local"
    fallback = extractor_endpoint(config)
    if mode == "main":
        ep = main_llm_endpoint(config)
        label = f"{ep.get('model') or '未配置'}（沿用主对话：{ep.get('name') or '未选择'}）"
    elif mode == "extractor":
        ep = dict(fallback)
        label = f"{ep.get('model') or '未配置'}（mem0 抽取模型）"
    else:
        m = _mem0_cfg(config)
        ep = {
            "base_url": _expand(raw.get("base_url")) or _expand(m.get("embed_base_url") or m.get("base_url")),
            "api_key": _expand(raw.get("api_key")) or _expand(m.get("api_key")),
            "model": _expand(raw.get("model")) or DEFAULT_LOCAL_MODEL,
        }
        label = f"{ep['model']}（本地）"
    return {
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "base_url": ep.get("base_url", ""),
        "api_key": ep.get("api_key", ""),
        "model": ep.get("model", ""),
        "label": label,
        "fallback": fallback,
    }


def public_view(config: Dict[str, Any]) -> Dict[str, Any]:
    """给前端/服务器信息页用：不含密钥。"""
    raw = load_raw()
    r = resolve(config, raw)
    return {
        "mode": r["mode"],
        "mode_label": r["mode_label"],
        "model": r["model"],
        "base_url": r["base_url"],
        "api_key_set": bool(r["api_key"]),
        "label": r["label"],
        "fallback_model": r["fallback"].get("model", ""),
        "modes": [{"value": k, "label": v} for k, v in MODE_LABELS.items()],
    }
