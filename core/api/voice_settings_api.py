# -*- coding: utf-8 -*-
"""语音体验设置 API

GET  /api/voice-settings        读取当前设置
POST /api/voice-settings        保存设置（写 config.yaml + 触发热更新）

设置项：
- enable_wakeup_filler       唤醒等待填充音开关（true/false）
- enable_transition_phrase   工具调用过渡句开关（true/false）
- tool_calls_provider        复杂任务模型选择（auto/cloud/local），LLM routing-hybrid 块内
- routing_mode               路由模式（classify/delegate），LLM routing-hybrid 块内 mode 键
- cloud_tools                云端工具白名单（字符串数组），仅 delegate 模式生效
- tts_provider               默认 TTS 引擎选择（moss/edge），写 selected_module.TTS；
                             非默认引擎服务进程一并停（不允许两个 TTS 并存互抢）
"""
import json
import os
import re
import signal
import subprocess
import aiohttp
import yaml
from aiohttp import web

TAG = __name__

import logging

logger = logging.getLogger("voice_settings")

# 顶层键（文本级替换，保持 config.yaml 原格式/注释）
TOP_KEYS = ["enable_wakeup_filler", "enable_transition_phrase"]
# LLM 块内键
NESTED_KEYS = {
    "tool_calls_provider": {
        "section": "LLM",
        "sub": "routing-hybrid",
        "values": ["auto", "cloud", "local"],
    },
    "routing_mode": {
        "section": "LLM",
        "sub": "routing-hybrid",
        "yaml_key": "mode",       # YAML 中的实际键名
        "values": ["classify", "delegate"],
    },
}

# delegate 模式默认云端工具白名单
DEFAULT_CLOUD_TOOLS = [
    "web_search",
    "get_news_from_newsnow",
    "get_news_from_chinanews",
]

# 默认 TTS 引擎选项（互斥，只有默认可用；非默认服务一并停）
# key → {"label", "module"(config.yaml selected_module.TTS 值),
#        "keep": (服务脚本 basename, 专用 python), "kill": [需停的服务脚本]}
# ChatTTS(8082) 已淘汰；Edge-TTS 已按主人要求移除（2026-09-11），只保留 MOSS
TTS_OPTIONS = {
    "moss": {"label": "MOSS-TTS-Nano 本地",
             "module": "MossNano",
             "keep": ("moss_nano_server.py", ".venv/bin/python"),
             "kill": []},
}

# 服务脚本相对路径（相对到仓库根 projects/homebrain）
# script → (脚本所在子目录, 起用的 python 相对路径)
_TTS_SERVICE_DIRS = {
    "moss_nano_server.py": ("tts-moss-nano", "tts-moss-nano/.venv/bin/python"),
}


def _repo_root() -> str:
    """core/api/voice_settings_api.py → 上溯 5 层到 projects/homebrain
    文件在 homebrain/server/homebrain-server/core/api/，即：
    api → core → homebrain-server → server → homebrain(仓库根)"""
    p = os.path.abspath(__file__)
    for _ in range(5):
        p = os.path.dirname(p)
    return p


def _service_script_path(script: str) -> tuple:
    """把服务脚本 basename 解析到 (脚本绝对路径, python 绝对路径)；缺则 ("","")"""
    root = _repo_root()
    sub, pysub = _TTS_SERVICE_DIRS.get(script, ("", ""))
    sp = os.path.join(root, sub, script)
    py = os.path.join(root, pysub) if pysub else ""
    return (sp if os.path.exists(sp) else "", py if os.path.exists(py) else "")


def _kill_service_script(script: str) -> str:
    """pkill -f 停服务脚本。返回状态字"""
    try:
        r = subprocess.run(["pkill", "-f", script], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return "stopped"
        if r.returncode == 1:
            return "not-running"
        return f"pkill rc={r.returncode}"
    except Exception as e:
        return f"error: {e}"


def _start_service_script(script: str) -> str:
    """没在则 nohup 起服务脚本（用其 venv python）。返回状态字"""
    path, py = _service_script_path(script)
    if not path:
        return "script-missing"
    try:
        r = subprocess.run(["pgrep", "-f", script], capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            return "already-running"
        logdir = os.path.dirname(path)
        os.makedirs(os.path.join(logdir, "logs"), exist_ok=True)
        with open(os.path.join(logdir, "logs", script.replace(".py", ".log")), "ab") as logf:
            subprocess.Popen(
                [py or "python3", path],
                stdout=logf, stderr=logf, start_new_session=True, cwd=logdir,
            )
        return "started"
    except Exception as e:
        return f"error: {e}"


def _apply_tts_service_selection(key: str) -> dict:
    """把默认 TTS 选落实：停非默认，起/保持默认。返回 {script: status}"""
    opt = TTS_OPTIONS[key]
    result = {}
    for script in opt["kill"]:
        result[script] = _kill_service_script(script)
    if opt["keep"]:
        script, _py = opt["keep"]
        result[script] = _start_service_script(script)
    return result



def _config_path() -> str:
    # core/api/voice_settings_api.py -> 上溯 2 层到服务根
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "config.yaml")


def _read_config() -> dict:
    """读取 config.yaml（基础配置）"""
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"读取配置失败: {e}")
        return {}


def _read_merged_config() -> dict:
    """读取合并后的配置（config.yaml + data/.config.yaml，后者优先）。
    与 config_loader.load_config 行为一致。"""
    try:
        from config.config_loader import read_config, merge_configs
        base = read_config(_config_path())
        custom_path = os.path.join(os.path.dirname(_config_path()), "data", ".config.yaml")
        if os.path.exists(custom_path):
            custom = read_config(custom_path)
            if custom:
                return merge_configs(base, custom)
        return base
    except Exception as e:
        logger.warning(f"读取合并配置失败，退回基础配置: {e}")
        return _read_config()


def _current_settings() -> dict:
    cfg = _read_merged_config()
    llm_block = (cfg.get("LLM") or {}).get("routing-hybrid") or {}
    selected_tts = str((cfg.get("selected_module") or {}).get("TTS", "MossNano"))
    # 反查：config 的 selected_module.TTS → 选项 key（MossNano→moss, EdgeTTS→edge）
    tts_key = next((k for k, v in TTS_OPTIONS.items() if v["module"] == selected_tts), "moss")
    cloud_tools = llm_block.get("cloud_tools")
    if cloud_tools is None:
        cloud_tools = list(DEFAULT_CLOUD_TOOLS)
    return {
        "enable_wakeup_filler": bool(cfg.get("enable_wakeup_filler", True)),
        "enable_transition_phrase": bool(cfg.get("enable_transition_phrase", True)),
        "tool_calls_provider": str(llm_block.get("tool_calls_provider", "auto")),
        "routing_mode": str(llm_block.get("mode", "classify")),
        "cloud_tools": cloud_tools,
        "tts_provider": tts_key,
    }


def _set_top_key(lines: list, key: str, value: str) -> list:
    """替换顶层 key 行；不存在则追加到文件尾"""
    pat = re.compile(rf"^{key}:\s*.*$")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = f"{key}: {value}"
            return lines
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{key}: {value}")
    return lines


def _set_nested_key(lines: list, section: str, sub: str, key: str, value: str) -> list:
    """在 section.sub 块内替换 key 行（保持原格式，找不到则在 sub 块尾追加；
    section 或 sub 块不存在时自动创建）"""
    sec_pat = re.compile(rf"^{section}:\s*$")
    sub_pat = re.compile(rf"^\s+{sub}:\s*$")
    key_pat = re.compile(rf"^(\s+){key}:\s*.*$")
    in_sec = in_sub = False
    sec_found = sub_found = False
    sub_indent = 4
    insert_at = None
    for i, ln in enumerate(lines):
        if sec_pat.match(ln):
            in_sec = True
            sec_found = True
            continue
        if in_sec and re.match(r"^[A-Za-z_]", ln):
            in_sec = False
        if in_sec and sub_pat.match(ln):
            in_sub = True
            sub_found = True
            continue
        if in_sub and re.match(r"^\S", ln):
            in_sub = False
        if in_sub and key_pat.match(ln):
            indent = re.match(r"^(\s+)", ln).group(1)
            lines[i] = f"{indent}{key}: {value}"
            return lines
        if in_sub and re.match(r"^\s{4}\S", ln):
            sub_indent = len(re.match(r"^(\s+)", ln).group(1))
        if in_sub and ln.strip():
            insert_at = i + 1
    if insert_at is not None:
        lines.insert(insert_at, f"{' ' * sub_indent}{key}: {value}")
        return lines
    # sub 块存在但为空 → 直接在 sub 行后追加
    if sub_found:
        sub_idx = next(
            (i for i, ln in enumerate(lines) if sub_pat.match(ln)), None
        )
        if sub_idx is not None:
            lines.insert(sub_idx + 1, f"    {key}: {value}")
            return lines
    # sub 块不存在：section 存在则在其末尾创建 sub 块；section 也不存在则追加
    new_block_lines = [f"  {sub}:", f"    {key}: {value}"]
    if sec_found:
        # 在 section 末尾插入 sub 块
        sec_idx = next(
            (i for i, ln in enumerate(lines) if sec_pat.match(ln)), None
        )
        if sec_idx is not None:
            # 找 section 结束位置（下一个顶层键或文件末尾）
            j = sec_idx + 1
            while j < len(lines):
                if re.match(r"^[A-Za-z_]", lines[j]):
                    break
                j += 1
            lines[j:j] = new_block_lines
            return lines
    # 追加到文件末尾
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend([f"{section}:"] + new_block_lines)
    return lines


def _set_nested_list(lines: list, section: str, sub: str, key: str, values: list) -> list:
    """在 section.sub 块内写入 YAML 列表（key:\n  - item1\n  - item2）；
    section 或 sub 块不存在时自动创建"""
    sec_pat = re.compile(rf"^{section}:\s*$")
    sub_pat = re.compile(rf"^\s+{sub}:\s*$")
    key_pat = re.compile(rf"^(\s+){key}:\s*")
    in_sec = in_sub = False
    sec_found = sub_found = False
    sub_indent = 4
    insert_at = None
    key_start = None
    key_indent = None
    for i, ln in enumerate(lines):
        if sec_pat.match(ln):
            in_sec = True
            sec_found = True
            continue
        if in_sec and re.match(r"^[A-Za-z_]", ln):
            in_sec = False
        if in_sec and sub_pat.match(ln):
            in_sub = True
            sub_found = True
            continue
        if in_sub and re.match(r"^\S", ln):
            in_sub = False
        if in_sub and key_pat.match(ln):
            key_start = i
            key_indent = len(re.match(r"^(\s+)", ln).group(1))
            # 找到列表结束位置（下一个同缩进或更高层级的非列表项键）
            # 注意：YAML 中列表项 `- xxx` 可以和 key 同缩进（合法格式），
            # 所以必须排除以 `- ` 开头的列表项行
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if not stripped:
                    j += 1
                    continue
                indent = len(lines[j]) - len(lines[j].lstrip())
                if indent <= key_indent and stripped and not stripped.startswith("- "):
                    break
                j += 1
            # 替换为新的列表行
            list_indent = key_indent + 2
            new_lines = [f"{' ' * key_indent}{key}:"]
            for v in values:
                new_lines.append(f"{' ' * list_indent}- {v}")
            lines[key_start:j] = new_lines
            return lines
        if in_sub and re.match(r"^\s{4}\S", ln) and not ln.strip().startswith("- "):
            sub_indent = len(re.match(r"^(\s+)", ln).group(1))
            # 只在当前层级（routing-hybrid 的直接子键）更新插入点，
            # 避免插入到 cloud:/local: 等更深层的子块内部或列表项中间
            insert_at = i + 1
    # 构建列表行
    list_indent = sub_indent + 2
    new_lines = [f"{' ' * sub_indent}{key}:"]
    for v in values:
        new_lines.append(f"{' ' * list_indent}- {v}")
    # key 不存在，在 sub 块尾（当前层级的最后）追加
    if insert_at is not None:
        lines[insert_at:insert_at] = new_lines
        return lines
    # sub 块存在但为空（如只有 routing-hybrid: 一行）→ 直接在 sub 行后追加
    if sub_found:
        sub_idx = next(
            (i for i, ln in enumerate(lines) if sub_pat.match(ln)), None
        )
        if sub_idx is not None:
            lines[sub_idx + 1:sub_idx + 1] = [f"    {l.strip()}" for l in new_lines]
            return lines
    # sub 块不存在：section 存在则在其末尾创建 sub 块；section 也不存在则追加
    if sec_found:
        sec_idx = next(
            (i for i, ln in enumerate(lines) if sec_pat.match(ln)), None
        )
        if sec_idx is not None:
            j = sec_idx + 1
            while j < len(lines):
                if re.match(r"^[A-Za-z_]", lines[j]):
                    break
                j += 1
            lines[j:j] = [f"  {sub}:"] + [f"    {l.strip()}" for l in new_lines]
            return lines
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend([f"{section}:", f"  {sub}:"] + [f"    {l.strip()}" for l in new_lines])
    return lines


def _set_routing_sub_config(lines: list, sub_key: str, config: dict) -> list:
    """替换 routing-hybrid 块内的 cloud:/local: 子配置块为新的模型配置。

    sub_key: "cloud" 或 "local"
    config:  {"type": "openai", "base_url": "...", "api_key": "...", "model_name": "..."}
    """
    rh_pat = re.compile(r"^\s+routing-hybrid:\s*$")
    sub_pat = re.compile(rf"^(\s+){sub_key}:\s*$")
    new_block = [f"    {sub_key}:"]
    for k in ("type", "base_url", "api_key", "model_name"):
        if k in config and config[k] is not None:
            new_block.append(f"      {k}: {config[k]}")

    in_rh = False
    sub_start = None
    sub_indent = 4
    for i, ln in enumerate(lines):
        if rh_pat.match(ln):
            in_rh = True
            continue
        if in_rh and re.match(r"^\S", ln):
            break  # routing-hybrid 块结束
        if in_rh and sub_pat.match(ln):
            sub_start = i
            sub_indent = len(sub_pat.match(ln).group(1))
            # 找子块结束（下一个缩进 ≤ sub_indent 的非空行）
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if not stripped:
                    j += 1
                    continue
                indent = len(lines[j]) - len(lines[j].lstrip())
                if indent <= sub_indent:
                    break
                j += 1
            lines[sub_start:j] = new_block
            return lines

    # cloud:/local: 子块不存在，在 routing-hybrid 块末尾追加
    if in_rh:
        # 找 routing-hybrid 块末尾
        rh_idx = next((i for i, ln in enumerate(lines) if rh_pat.match(ln)), None)
        if rh_idx is not None:
            j = rh_idx + 1
            while j < len(lines):
                if re.match(r"^\S", lines[j]):
                    break
                j += 1
            lines[j:j] = new_block
            return lines
    return lines


def _set_selected_module_llm(lines: list, provider_name: str) -> list:
    """设置 selected_module.LLM 的值"""
    pat = re.compile(r"^(\s+)LLM:\s*.*$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m and len(m.group(1)) == 2:  # selected_module 下的 LLM（2空格缩进）
            lines[i] = f"  LLM: {provider_name}"
            return lines
    return lines


async def get_voice_settings(request):
    """GET /api/voice-settings"""
    try:
        return web.json_response({"success": True, "settings": _current_settings()})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def get_available_tools(request):
    """GET /api/available-tools —— 返回 Intent function_call 配置中所有可用工具名列表"""
    try:
        cfg = _read_config()
        intent_cfg = (cfg.get("Intent") or {}).get("function_call") or {}
        functions = intent_cfg.get("functions") or []
        return web.json_response({"success": True, "tools": functions})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def get_routing_models(request):
    """GET /api/routing-models —— 返回 routing-hybrid 的 local/cloud 模型配置 + 当前 LLM 选择"""
    try:
        cfg = _read_merged_config()
        rh = (cfg.get("LLM") or {}).get("routing-hybrid") or {}
        selected_llm = str((cfg.get("selected_module") or {}).get("LLM", ""))
        return web.json_response({
            "success": True,
            "local": rh.get("local") or {},
            "cloud": rh.get("cloud") or {},
            "selected_module_llm": selected_llm,
        })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def save_routing_models(request):
    """POST /api/routing-models —— 保存 routing-hybrid 的 local/cloud 模型配置。

    Body: {"local": {type, base_url, api_key, model_name},
           "cloud": {type, base_url, api_key, model_name},
           "activate": true/false}   # activate=true 时同时切换 selected_module.LLM=routing-hybrid
    """
    try:
        data = await request.json()
        local_cfg = data.get("local") or {}
        cloud_cfg = data.get("cloud") or {}
        activate = bool(data.get("activate", False))

        if not local_cfg.get("model_name") and not cloud_cfg.get("model_name"):
            return web.json_response({"success": False, "message": "至少需要配置 local 或 cloud 模型"}, status=400)

        path = _config_path()
        paths = [path, os.path.join(os.path.dirname(path), "data", ".config.yaml")]

        # 备份
        backup_path = path + ".bak_routing_models"
        with open(path, "r", encoding="utf8") as f:
            content = f.read()
        with open(backup_path, "w", encoding="utf8") as f:
            f.write(content)

        results = {}
        for p in paths:
            try:
                lines = open(p, encoding="utf8").read().split("\n")
                if cloud_cfg.get("model_name"):
                    lines = _set_routing_sub_config(lines, "cloud", cloud_cfg)
                if local_cfg.get("model_name"):
                    lines = _set_routing_sub_config(lines, "local", local_cfg)
                if activate:
                    lines = _set_selected_module_llm(lines, "routing-hybrid")
                new_content = "\n".join(lines)
                yaml.safe_load(new_content)
                with open(p, "w", encoding="utf8") as f:
                    f.write(new_content)
                results[os.path.basename(p)] = "ok"
            except Exception as e:
                results[os.path.basename(p)] = f"fail: {e}"

        # 触发热更新
        reloaded = False
        try:
            from core.api.config_api import _ws_server_instance
            if _ws_server_instance is not None:
                import asyncio
                asyncio.create_task(_ws_server_instance.update_config())
                reloaded = True
        except Exception as e:
            logger.warning(f"触发热更新失败（配置已写入，重启后生效）: {e}")

        return web.json_response({
            "success": all(str(v) == "ok" for v in results.values()),
            "files": results,
            "hot_reload": reloaded,
            "activated": activate,
            "message": "模型配置已保存" + ("，已切换到 routing-hybrid 并热更新" if activate and reloaded else "，重启后生效"),
        })
    except Exception as e:
        logger.error(f"保存 routing 模型配置失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def save_voice_settings(request):
    """POST /api/voice-settings —— 写 config.yaml + 触发服务热更新"""
    try:
        data = await request.json()
        updates = data.get("settings") or {}
        current = _current_settings()

        new_top = {}
        new_nested = {}
        if "enable_wakeup_filler" in updates:
            new_top["enable_wakeup_filler"] = "true" if bool(updates["enable_wakeup_filler"]) else "false"
        if "enable_transition_phrase" in updates:
            new_top["enable_transition_phrase"] = "true" if bool(updates["enable_transition_phrase"]) else "false"
        if "tool_calls_provider" in updates:
            v = str(updates["tool_calls_provider"]).lower()
            if v not in NESTED_KEYS["tool_calls_provider"]["values"]:
                return web.json_response({"success": False, "message": f"tool_calls_provider 只支持 {NESTED_KEYS['tool_calls_provider']['values']}"}, status=400)
            new_nested["tool_calls_provider"] = v
        if "routing_mode" in updates:
            v = str(updates["routing_mode"]).lower()
            if v not in NESTED_KEYS["routing_mode"]["values"]:
                return web.json_response({"success": False, "message": f"routing_mode 只支持 {NESTED_KEYS['routing_mode']['values']}"}, status=400)
            new_nested["routing_mode"] = v
        cloud_tools = None
        if "cloud_tools" in updates:
            ct = updates["cloud_tools"]
            if not isinstance(ct, list):
                return web.json_response({"success": False, "message": "cloud_tools 必须是字符串数组"}, status=400)
            cloud_tools = [str(t).strip() for t in ct if str(t).strip()]
        tts_key = None
        if "tts_provider" in updates:
            v = str(updates["tts_provider"]).lower()
            if v not in TTS_OPTIONS:
                return web.json_response({"success": False, "message": f"tts_provider 只支持 {list(TTS_OPTIONS)}"}, status=400)
            tts_key = v
        if not new_top and not new_nested and cloud_tools is None and not tts_key:
            return web.json_response({"success": False, "message": "没有可更新的设置项"}, status=400)

        path = _config_path()
        # 运行时配置真用两份：config.yaml + data/.config.yaml（合并，后者优先）。
        # 必须把 selected_module.TTS 写进两份，否则 data/.config.yaml 的 TTS 盖住。
        paths = [path, os.path.join(os.path.dirname(path), "data", ".config.yaml")]
        # 切换默认 TTS 时一并清 ChatTTS 残留 DB 键（voice=zh_3/seed 等会盖住新引擎）
        tts_db_cleared = False
        if tts_key:
            try:
                import sqlite3
                dbp = os.path.join(os.path.dirname(path), "data", "devices.db")
                conn = sqlite3.connect(dbp, timeout=30)
                try:
                    conn.execute("PRAGMA busy_timeout=30000")
                    n = conn.execute(
                        "DELETE FROM tts_config WHERE config_key LIKE 'voice_%_temperature' "
                        "OR config_key LIKE 'voice_%_topP' OR config_key LIKE 'voice_%_topK' "
                        "OR config_key LIKE 'voice_%_speed' OR config_key = 'is_default' "
                        "OR config_key = 'temperature' OR config_key = 'topP' "
                        "OR config_key = 'topK'").rowcount
                    # 同步 provider 属主：voice/volume/speed 这批键从此归新引擎所有。
                    # 不修的话旧引擎名会一直留在 provider 字段上，新引擎读 DB 音色时
                    # 属主校验失败、控制台音色切换静默失效（2026-09-28 实测实锤）。
                    module = TTS_OPTIONS[tts_key]['module']
                    conn.execute(
                        "INSERT INTO tts_config (config_key, config_value, updated_at) "
                        "VALUES ('provider', ?, datetime('now')) "
                        "ON CONFLICT(config_key) DO UPDATE SET "
                        "config_value = excluded.config_value, "
                        "updated_at = excluded.updated_at",
                        (module,))
                    conn.commit()
                finally:
                    conn.close()
                tts_db_cleared = True
                logger.info(f"清 tts_config 残留键 {n} 行")
            except Exception as e:
                logger.warning(f"清 tts_config 失败: {e}")

        # 备份
        backup_path = path + ".bak_voice_settings"
        with open(path, "r", encoding="utf8") as f:
            content = f.read()
        with open(backup_path, "w", encoding="utf8") as f:
            f.write(content)

        results = {}
        for path2 in paths:
            try:
                lines = open(path2, encoding="utf8").read().split("\n")
                for k, v in new_top.items():
                    lines = _set_top_key(lines, k, v)
                for k, v in new_nested.items():
                    spec = NESTED_KEYS[k]
                    yaml_key = spec.get("yaml_key", k)
                    lines = _set_nested_key(lines, spec["section"], spec["sub"], yaml_key, v)
                if cloud_tools is not None:
                    lines = _set_nested_list(lines, "LLM", "routing-hybrid", "cloud_tools", cloud_tools)
                if tts_key:
                    for i, ln in enumerate(lines):
                        if re.match(r"^  TTS:\s*.*$", ln):
                            indent = ln[: len(ln) - len(ln.lstrip())]
                            lines[i] = f"{indent}TTS: {TTS_OPTIONS[tts_key]['module']}"
                            break
                new_content = "\n".join(lines)
                yaml.safe_load(new_content)  # 写前校验
                with open(path2, "w", encoding="utf8") as f:
                    f.write(new_content)
                results[os.path.basename(path2)] = "ok"
            except Exception as e:
                results[os.path.basename(path2)] = f"fail: {e}"

        # 落实 TTS 服务互斥：停非默认，起/保持默认
        svc_status = _apply_tts_service_selection(tts_key) if tts_key else {}

        # 触发服务热更新（websocket_server.update_config 重读 config.yaml）
        reloaded = False
        try:
            from core.api.config_api import _ws_server_instance
            if _ws_server_instance is not None:
                import asyncio
                asyncio.create_task(_ws_server_instance.update_config())
                reloaded = True
        except Exception as e:
            logger.warning(f"触发热更新失败（配置已写入，重启后生效）: {e}")

        return web.json_response({
            "success": all(str(v) == "ok" for v in results.values()),
            "saved": {**{k: v == "true" for k, v in new_top.items()}, **new_nested,
                      **({"cloud_tools": cloud_tools} if cloud_tools is not None else {})},
            "files": results,
            "tts_db_cleared": tts_db_cleared,
            "hot_reload": reloaded,
            "service_actions": svc_status,
            "message": "设置已保存" + ("并已热更新" if reloaded else "，重启后生效"),
        })
    except Exception as e:
        logger.error(f"保存语音设置失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)
