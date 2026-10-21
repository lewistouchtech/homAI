#!/usr/bin/env python3
"""后台配置界面自查脚本（2026-10-05）

用途：主人在后台点保存/应用/测试之后，如果没反应或「保存了却没生效」，
用这个脚本一次性确认每个配置接口的**真实**行为，而不是靠肉眼看提示。

它只做**只读探测** + 一个无副作用的写入探测（写入后立即还原），
不会破坏配置。每个探测项都打印 结论 + 证据。

用法：
    ./venv/bin/python tools/audit_config_ui.py            # 全部
    ./venv/bin/python tools/audit_config_ui.py ASR TTS    # 只跑指定模块

⚠️ 它会做「切换引擎再切回」的往返测试，并保证还原。
   2026-10-05 第一版没有 finally 还原，把 selected_module 留在了
   未配置的 provider 上，导致服务随后反复启动失败——工具自己成了故障源。

⚠️ 前置：必须确认服务是真进程（见下）。
    先看 lsof -nP -iTCP:8003 -sTCP:LISTEN 的 PID，
    再 ps -p <PID> -o lstart= 看启动时间 —— 确认是刚重启的。
    本项目曾因 launchd 留下孤儿进程占着端口，导致对着旧代码验证、
    结果「接口都正常」其实全是假的（2026-10-05 事故）。
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8003"


def call(path, data=None, timeout=30):
    """返回 (ok, payload)"""
    url = BASE + path
    try:
        if data is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode())
        except Exception:
            return False, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return False, {"error": str(e)[:160]}


def check(name, ok, evidence, verdict=None):
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}")
    print(f"      {verdict or evidence}")
    return ok


def audit_module(mod):
    print(f"\n{'='*66}\n【{mod}】\n{'='*66}")
    ok, d = call("/api/config/providers")
    if not ok or not d.get("success"):
        return check(f"{mod} 读引擎列表", False, f"接口异常：{d}")
    info = d["modules"][mod]
    sel = info.get("selected", "")
    providers = info.get("providers", {})
    print(f"  当前引擎：{sel}   可选 {len(providers)} 个")
    print(f"  分组分布：{ {g: sum(1 for v in providers.values() if v['group']==g) for g in sorted({v['group'] for v in providers.values()})} }")

    # 1) 每个 provider 都能读到自己的字段（这决定「⚙️ 配置引擎参数」有没有东西可填）
    for name in providers:
        ok, pf = call(f"/api/config/provider-fields?module={mod}&provider={urllib.parse.quote(name)}")
        if not ok or not pf.get("success"):
            check(f"{mod}.{name} 字段可读", False, f"{pf.get('error')}")
            continue
        keys = [f["key"] for f in pf.get("fields", [])]
        secrets = [f["key"] for f in pf.get("fields", []) if f.get("is_secret")]
        leak = [x["key"] for x in pf.get("fields", []) if x.get("is_secret") and x.get("value")]
        check(f"{mod}.{name} 字段可读",
              True,
              f"{len(keys)} 个字段（{'/'.join(keys[:5])}{'…' if len(keys)>5 else ''}）；"
              f"密钥字段 {secrets or '无'}，{'✅ 未回显' if not leak else '❌ 泄露了值'}")
        if leak:
            check(f"{mod}.{name} 密钥不回显", False, f"以下密钥字段回传了值：{leak}")

    # 2) 切换往返 —— 只挑「已配置」的 provider 来切，绝不碰未配置的
    #    （2026-10-05 教训：切到未配置的 CustomTTS 会让服务下次启动直接失败）
    ready = [n for n, v in providers.items() if v.get("configured")] + \
            [n for n, v in providers.items() if not v.get("configured")]
    others = [n for n in ready if n != sel and providers[n].get("configured")]
    if not others:
        check(f"{mod} 切换往返", True, f"除当前引擎外无「已配置」的可选引擎，跳过",
              verdict=f"ℹ️ 除 {sel} 外无已配置引擎，跳过（不拿未配置的做实验）")
        return True
    target = others[0]
    try:
        ok1, r1 = call("/api/config/switch-provider", {"module": mod, "provider": target})
        s1 = f"→{target}: success={r1.get('success')} msg={r1.get('message') or r1.get('error')}"
        passed1 = check(f"{mod} 切到 {target}", bool(r1.get("success")), s1)
        ok3, d3 = call("/api/config/providers")
        back_mid = d3["modules"][mod].get("selected")
        check(f"{mod} 切换已落盘", back_mid == target,
              f"回读 selected={back_mid}（期望 {target}）")
        return passed1
    finally:
        # 无论成败都必须还原——一个会改坏配置的检查脚本比没有更糟
        call("/api/config/switch-provider", {"module": mod, "provider": sel})
        ok4, d4 = call("/api/config/providers")
        restored = d4["modules"][mod].get("selected")
        check(f"{mod} 已还原为 {sel}", restored == sel, f"当前 selected={restored}")


def audit_test_buttons():
    print(f"\n{'='*66}\n【测试连接按钮】（这两个函数原本根本不存在，点一下抛 JS 错）\n{'='*66}")
    # ASR
    for kind, name in (("asr", "ASR"), ("tts", "TTS")):
        ok, d = call(f"/api/config/test-custom-{kind}", {"base_url": "", "model_name": ""})
        # 判定标准是「有没有给出可读的错误」，不是 HTTP 码
        # （缺参数本就该是 400，只要 body 里有中文提示就算通过）
        has_msg = bool(d.get("error") or d.get("message"))
        check(f"{name} 测试连接接口", has_msg,
              f"缺参数时回：{str(d.get('error'))[:70]}",
              verdict=("✅ 有明确回音：" + str(d.get("error"))[:60]) if has_msg else "❌ 无回音")
    # 真探测：无效域名，应回网络错误而不是静默
    ok, d = call("/api/config/test-custom-asr",
                 {"base_url": "https://__nonexistent.invalid/v1",
                  "model_name": "m", "api_key": "k"})
    check("ASR 测试连接真探测", ok and not d.get("success"),
          f"无效域名 → {str(d.get('error'))[:90]}",
          verdict=("✅ 如实报错：" + str(d.get("error"))[:70]) if d.get("error") else "❌ 没报错")


def audit_secret_health():
    print(f"\n{'='*66}\n【密钥健康】\n{'='*66}")
    ok, d = call("/api/config/search-providers")
    if ok and d.get("success"):
        t = [p for p in d.get("providers", []) if p["id"] == "tavily"]
        if t:
            p = t[0]
            check("网页搜索 tavily 密钥已识别", p["api_key_set"],
                  f"masked={p['api_key_masked']!r}",
                  verdict=("✅ " + p["api_key_masked"]) if p["api_key_set"] else "❌ 未识别到密钥")
    ok, d = call("/api/weather/config")
    if ok and d.get("config"):
        k = d["config"].get("api_key", "")
        check("天气密钥已插值", bool(k) and "${" not in k,
              f"api_key={str(k)[:12]}…",
              verdict=("✅ 真值已插入") if k and "${" not in k else "❌ 仍是 ${} 字面串")


if __name__ == "__main__":

    mods = sys.argv[1:] or ["ASR", "TTS", "LLM", "VLLM"]
    print(f"探测目标：{BASE}")
    results = []
    for m in mods:
        results.append(audit_module(m))
    audit_test_buttons()
    audit_secret_health()
    print(f"\n{'='*66}")
    print("⚠️ 提醒：本脚本只证明「接口行为正确」。若你刚改过代码，")
    print("   必须先确认服务是真进程（lsof 看 PID + ps 看启动时间），")
    print("   否则验的可能是旧代码 —— 2026-10-05 曾因此浪费整个下午。")
