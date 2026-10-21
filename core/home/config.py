"""家居引擎的 HA 连接配置（唯一来源）

优先级：数据库 system_config(ha_host/ha_port/ha_token) → 环境变量 HASS_BASE_URL / HASS_TOKEN。
每次调用实时读取（带 30 秒缓存），后台改了令牌无需重启。
源码里不再写任何默认令牌：取不到就明确报错，由调用方翻译成人话。
"""
import os
import sqlite3
import time
from typing import Tuple

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "devices.db")
_TTL = 30.0
_cache = {"ts": 0.0, "val": None}


class HomeConfigError(RuntimeError):
    """HA 地址或令牌没配置"""


def _read_db() -> dict:
    conn = None
    try:
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute(
            "SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'ha_%'"
        ).fetchall()
        return {k: v for k, v in rows}
    except Exception:
        return {}
    finally:
        if conn:
            conn.close()


def _build() -> Tuple[str, str]:
    cfg = _read_db()
    host = (cfg.get("ha_host") or "").strip()
    port = str(cfg.get("ha_port") or "").strip()
    token = (cfg.get("ha_token") or "").strip() or os.environ.get("HASS_TOKEN", "").strip()
    if host:
        base = host.rstrip("/") if host.startswith(("http://", "https://")) else f"http://{host}"
        if port and port not in ("80", "443"):
            base = f"{base}:{port}"
    else:
        base = os.environ.get("HASS_BASE_URL", "").strip().rstrip("/")
    return base, token


def get_ha_config(force: bool = False) -> Tuple[str, str]:
    """返回 (base_url, token)；两者缺一抛 HomeConfigError。"""
    now = time.time()
    if force or _cache["val"] is None or now - _cache["ts"] > _TTL:
        _cache["val"] = _build()
        _cache["ts"] = now
    base, token = _cache["val"]
    if not base:
        raise HomeConfigError("没有配置 Home Assistant 地址")
    if not token:
        raise HomeConfigError("没有配置 Home Assistant 访问令牌")
    return base, token


def get_ha_base_url() -> str:
    return get_ha_config()[0]


def ha_headers() -> dict:
    return {"Authorization": f"Bearer {get_ha_config()[1]}", "Content-Type": "application/json"}


def reset_cache() -> None:
    _cache["val"] = None
    _cache["ts"] = 0.0
