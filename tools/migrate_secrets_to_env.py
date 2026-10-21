"""一次性迁移：把 data/.config.yaml 里的明文 API Key 搬进 .env，改写为 ${引用}。

用法（项目根目录下）：
    ./venv/bin/python tools/migrate_secrets_to_env.py [--dry-run]

设计要点：
- 只搬「疑似真密钥」的行：值非空、不以 ${ 开头、不含「你的」、不是 1234 等占位
- 同一个真值复用同一个变量名（豆包 ark-… 出现在 4 处，只写 1 个变量）
- 行级改写，保留原文件的注释、缩进与结构
- 支持 --dry-run 先看计划
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.utils.secret_store import (  # noqa: E402
    ENV_PATH,
    find_var,
    is_secret_field,
    set_env_var,
    build_var_name,
    detect_vendor,
    existing_var_names,
)

import re  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [
    os.path.join(ROOT, "data", ".config.yaml"),
    os.path.join(ROOT, "config.yaml"),
]

# 占位符判定统一由 secret_store 提供，避免两处规则漂移
# （历史上就漂移过一次：迁移脚本认识 local-no-key，to_ref 不认识）
from core.utils.secret_store import is_placeholder  # noqa: E402

PLACEHOLDERS = None  # 兼容旧引用；实际判定走 is_placeholder()

# yaml 顶层段 → 用于变量名的模块名
_SECTION_TO_MODULE = {
    "ASR": "ASR", "LLM": "LLM", "TTS": "TTS", "VLLM": "VLLM",
    "plugins": "PLUGINS", "security": "SECURITY", "Memory": "MEMORY",
    "Decision": "DECISION",
}

# 键名允许中文（如「豆包流式ASR:」），值可为空（provider 定义行就是这样）
_LINE_KEY = re.compile(r"^(\s*)([A-Za-z0-9_一-鿿\-]+)\s*:\s*(.*?)\s*$")


def _looks_like_placeholder(v: str) -> bool:
    """委托给 secret_store 的统一判定，避免与 to_ref 规则漂移。"""
    return is_placeholder(v)


def plan_file(path: str):
    """返回该文件里的迁移计划：list of dict(line_no, indent, key, value, module, provider, var)"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    plans = []
    section = ""
    provider = ""
    taken = existing_var_names()
    # 先建 key->变量名 的复用映射：同一个 value 只分配一次
    value_to_var = {}

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip())

        # 顶层段：indent 0
        if indent == 0 and ":" in stripped:
            section = stripped.split(":", 1)[0]
            provider = ""
            continue
        # provider 行：ASR/LLM/... 段下 indent 2 且以 : 结尾
        m_top = _LINE_KEY.match(raw)
        if m_top:
            _, key, val = m_top.group(1), m_top.group(2), m_top.group(3)
            # provider 定义行（值为空）
            if val in ("", "{}") and indent >= 2:
                if indent == 2:
                    provider = key
                continue
            if not is_secret_field(key):
                continue
            v = val.strip().strip('"').strip("'")
            if v.startswith("${") or _looks_like_placeholder(v):
                continue
            # 不处理 dict/list 形式（value 以 { 或 [ 开头）
            if v.startswith("{") or v.startswith("["):
                continue

            module = _SECTION_TO_MODULE.get(section, section.upper() or "PLUGINS")
            if v in value_to_var:
                var = value_to_var[v]
            else:
                # 同一把 key 跨模块共用时，用厂商名而非模块名，避免误导性前缀
                var = build_var_name(module, provider, key, taken,
                                     vendor=detect_vendor(v))
                taken.add(var)
                value_to_var[v] = var

            plans.append({
                "line_no": idx + 1,
                "indent": m_top.group(1),
                "key": key,
                "value": v,
                "module": module,
                "provider": provider,
                "var": var,
            })
    return plans


def apply_file(path: str, plans) -> int:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    by_line = {p["line_no"]: p for p in plans}
    changed = 0
    for p in plans:
        ln = p["line_no"]
        cur = lines[ln - 1]
        # 重新确认这一行仍是同一个 key（防御：文件被外部改动）
        if p["key"] not in cur:
            continue
        set_env_var(p["var"], p["value"])
        lines[ln - 1] = f"{p['indent']}{p['key']}: ${{{p['var']}}}"
        changed += 1

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = ap.parse_args()

    all_plans = {}
    for path in TARGETS:
        all_plans[path] = plan_file(path)

    total = sum(len(v) for v in all_plans.values())
    print(f"发现 {total} 处明文密钥待迁移：\n")
    for path, plans in all_plans.items():
        if not plans:
            continue
        rel = os.path.relpath(path, ROOT)
        print(f"--- {rel} ---")
        for p in plans:
            print(f"  L{p['line_no']:<4} {p['module']}.{p['provider'] or '(root)'}.{p['key']}"
                  f"  {p['value'][:8]}…  ->  ${{{p['var']}}}")
        print()

    if args.dry_run:
        print("（dry-run，未写入任何文件）")
        return

    if total == 0:
        print("无需迁移。")
        return

    grand = 0
    for path, plans in all_plans.items():
        if plans:
            grand += apply_file(path, plans)
            print(f"已改写 {os.path.relpath(path, ROOT)}：{len(plans)} 行")
    print(f"\n共改写 {grand} 行。真值已写入 {ENV_PATH}")


if __name__ == "__main__":
    main()