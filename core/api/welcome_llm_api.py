# -*- coding: utf-8 -*-
"""欢迎语 LLM 选择 API（后台"LLM 大模型设置"里的欢迎语下拉框）

设计原则（主人 2026-09-27 要求）：大模型的地址/key/模型名已经在"LLM 大模型设置"
里配好了，欢迎语**不需要重复配置**——只需要一个下拉框选用哪一个。

因此：
- GET  返回 LLM: 段里的所有 provider（供下拉列表）+ 当前 security.welcome_llm
- POST 接收 {"provider": "UserDefinedLLM"}，从 LLM: 段查出该 provider 的
  base_url / model_name / api_key，写入 security.welcome_llm
- security_listener 代码零改动（它继续读 security.welcome_llm 的字段）
"""
import os
import yaml
from pathlib import Path
from aiohttp import web
from config.logger import setup_logging
from config.config_loader import load_yaml_expanded

TAG = "welcome_llm_api"
logger = setup_logging()

# server/homebrain-server/
_SERVER_ROOT = Path(__file__).parent.parent.parent

def _llm_type_supported(t: str) -> bool:
    """该 type 是否有对应的 LLM 实现文件。没有就别列进下拉框
    （比如 routing-hybrid：它不是引擎，是个路由模式名）。"""
    import os
    if not t:
        return False
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    p1 = root / "core" / "providers" / "llm" / t / f"{t}.py"
    p2 = root / "core" / "providers" / "llm" / f"{t}.py"
    return p1.exists() or p2.exists()


def _yaml_paths():
    return (_SERVER_ROOT / "config.yaml", _SERVER_ROOT / "data" / ".config.yaml")

def _read_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
        return load_yaml_expanded(f) or {}

def _write_yaml(path, cfg):
    """整体写回 yaml。

    ⚠️ 2026-10-05 修同型缺陷：本模块 _read_yaml 用的是带插值的读取（拿到的是
    .env 里的真值），这里直接 safe_dump 写回 → 真值被写进 git 跟踪的 yaml。
    写回前统一归集，明文密钥换成 ${} 引用。

    同时把 sort_keys 去掉：它会把整个文件的键序打乱，主人手工整理过的
    配置顺序全部错位。保持原有顺序（sort_keys=False）代价为零。
    """
    from core.utils.secret_store import sweep_dict_secrets
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(sweep_dict_secrets(cfg), f, allow_unicode=True,
                       sort_keys=False, default_flow_style=False)

def _load_both():
    """返回 (合并后的配置, 原始默认配置, p1, p2)

    2026-10-05 修：原来把 config.yaml 和 data/.config.yaml **分别**返回，调用方
    只用了第一个（默认配置），于是运行时配置里新增的引擎根本不在下拉框里——
    主人在后台新建的「豆包 agent plan」就选不到。现在返回 merge 后的结果
    （与 load_config() 的启动行为一致），同时保留第二份原始返回以兼容旧调用。
    """
    p1, p2 = _yaml_paths()
    d1, d2 = _read_yaml(p1), _read_yaml(p2)
    try:
        from config.config_loader import merge_configs
        merged = merge_configs(d1, d2)
    except Exception:
        merged = d2 or d1
    return merged, d2, p1, p2

async def handle_get_welcome_llm(request):
    """GET /api/security/welcome-llm → 可用 LLM provider 列表 + 当前选择"""
    try:
        # DB model_providers 表优先；无记录回退 yaml（兼容老部署）
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        db_rows = []
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                try:
                    db_rows = conn.execute(
                        "SELECT * FROM model_providers WHERE role IN ('text','welcome') ORDER BY role, id").fetchall()
                finally:
                    conn.close()
            except Exception:
                db_rows = []

        cfg, _, _, _ = _load_both()
        llm_section = cfg.get("LLM") or {}
        # 2026-10-05：与 /api/config/providers 用同一口径过滤，否则这里会把
        # 记忆后端（mem_local_short/openviking）也列进下拉框——
        # 主人反馈「下拉菜单乱，什么都有，还有什么 power 都有」。它们是上游
        # 误放在 LLM: 段的记忆后端，llm.py 里没有实现，选了必然失败。
        _MEMORY_ONLY = ("mem_local_short", "openviking")
        # 记忆后端之外，还要排掉没有实现文件的（routing-hybrid 之类）
        providers = []
        for name, v in llm_section.items():
            if not isinstance(v, dict):
                continue
            if str(v.get("type", "")) in _MEMORY_ONLY:
                continue
            if not _llm_type_supported(str(v.get("type", ""))):
                continue
            providers.append({
                "name": name,
                "type": v.get("type", ""),
                "base_url": v.get("base_url") or v.get("url", ""),
                "model_name": v.get("model_name") or v.get("model", ""),
            })

        current = None
        if db_rows:
            # DB 里有 welcome active 记录，优先返回
            active = [r for r in db_rows if r["role"] == "welcome" and r["is_active"]]
            if active:
                a = active[0]
                current = {
                    "_provider": a["provider_name"],
                    "base_url": a["base_url"] or "",
                    "model_name": a["model_name"] or "",
                    "api_key": a["api_key"] or "",
                }
        if current is None:
            sec = cfg.get("security") or {}
            current = sec.get("welcome_llm") or {}
        return web.json_response({
            "success": True,
            "providers": providers,
            "current": current,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"[欢迎语] 读取失败: {e}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


async def handle_set_welcome_llm(request):
    """POST /api/security/welcome-llm
    Body: {"provider": "UserDefinedLLM"}
    从 LLM: 段查出该 provider 的连接信息，写入 security.welcome_llm（两份 YAML 同步）。
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "请求体不是合法 JSON"}, status=400)

    provider_name = str(data.get("provider") or "").strip()
    if not provider_name:
        return web.json_response({"success": False, "message": "provider 不能为空"}, status=400)

    cfg, _, p1, p2 = _load_both()
    llm_cfg = (cfg.get("LLM") or {}).get(provider_name)
    if not llm_cfg or not isinstance(llm_cfg, dict):
        return web.json_response(
            {"success": False, "message": f"LLM 段里没有 '{provider_name}' 这个 provider"},
            status=404)

    # 从选中的 provider 提取连接信息
    base_url = llm_cfg.get("base_url") or llm_cfg.get("url") or ""
    model_name = llm_cfg.get("model_name") or llm_cfg.get("model") or ""
    api_key = llm_cfg.get("api_key") or ""

    welcome = {
        "base_url": base_url.rstrip("/"),
        "model_name": model_name,
        "api_key": api_key,
        "_provider": provider_name,   # 标注来源，方便回显
    }

    # 写入 DB model_providers 表（role=welcome），同时同步 yaml 兼容
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                # 同 role 取消激活，再 upsert 该 provider 为激活
                conn.execute("UPDATE model_providers SET is_active=0 WHERE role='welcome'")
                conn.execute(
                    "INSERT INTO model_providers(role, provider_name, display_name, base_url, api_key, model_name, is_active) "
                    "VALUES('welcome', ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(role, model_name, base_url) DO UPDATE SET "
                    "  provider_name=excluded.provider_name, api_key=excluded.api_key, "
                    "  is_active=1, updated_at=CURRENT_TIMESTAMP",
                    (provider_name, f"欢迎语-{model_name}", base_url.rstrip("/"), api_key, model_name),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[欢迎语] 写 DB 失败（yaml 仍会更新）: {e}")

    # yaml 双写（兼容层：yaml 继续作为回退源）
    for path in (p1, p2):
        c = _read_yaml(path)
        sec = c.setdefault("security", {})
        sec["welcome_llm"] = dict(welcome)
        _write_yaml(path, c)

    logger.bind(tag=TAG).info(
        f"[欢迎语] 已切换到 {provider_name}（{model_name} @ {base_url}）")
    return web.json_response({
        "success": True,
        "provider": provider_name,
        "welcome_llm": welcome,
    })


def create_welcome_llm_routes():
    return [
        web.get("/api/security/welcome-llm", handle_get_welcome_llm),
        web.post("/api/security/welcome-llm", handle_set_welcome_llm),
    ]
