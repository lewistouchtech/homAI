"""助手身份统一访问器（Single Source of Truth）

背景
----
助手的名字、昵称、灵魂（soul）、身份（identity）在后台「🎭 AI身份设定」里配置，
历史上被硬编码散落在多个模块（唤醒词、错误回复、路由正则、OpenViking 过滤等），
导致后台改了名字/人设后，其他地方仍然用旧值，出现"后台说是男生、别处还是女生"的冲突。

权威源
------
1. ``data/devices.db`` 的 ``ai_butler_config`` 表（key-value）
   —— 后台保存时由 ``/api/ai-butler-config`` 写入，字段：name / nickname / soul / identity / traits / wakeWord
2. ``data/.config.yaml`` 的 ``prompt`` 字段
   —— 后台保存时由 ``/api/config/sync`` 拼接写入，是**实际注入 LLM** 的 system prompt

使用方式
--------
所有需要助手名字/昵称/人设的地方都调用本模块，不要硬编码：

    from core.utils.assistant_identity import get_name
    text = f"我是{get_name()}，你的AI管家~"

后台保存配置后调用 ``invalidate_cache()`` 使缓存立即失效。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 默认值：数据库缺失/读取失败时兜底，与后台「AI身份设定」默认值保持一致。
#
# [2026-10-06] 助手名不应在代码里写死 —— 每个部署的助理名、唤醒词都不同，
# 必须以后台「AI身份设定」为准。主链路（get_name/get_nickname）早就读数据库了，
# 但散落各处的**异常兜底**仍写着具体的旧人名，换个人部署、数据库恰好为空时
# 就会说出别人的名字。统一收敛到 safe_name()/safe_nickname()，兜底文案也
# 不再带具体人名。
DEFAULT_NAME = "家庭助手"
DEFAULT_NICKNAME = "家庭助手"
DEFAULT_SOUL = "我是家庭智能管家，致力于让家庭生活更温暖、更智能。"
DEFAULT_IDENTITY = "我是家庭智能管家，负责协调智能设备、管理家庭日程、提供生活协助。"

_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
_DB_PATH = _PROJECT_DIR / "data" / "devices.db"
_CONFIG_PATH = _PROJECT_DIR / "data" / ".config.yaml"

# 缓存：避免每轮对话都读 sqlite / yaml
_CACHE_TTL_SECONDS = 30
_cache: Dict = {"data": None, "ts": 0.0}


def invalidate_cache() -> None:
    """清空缓存。后台保存 AI 身份设定后必须调用。"""
    _cache["data"] = None
    _cache["ts"] = 0.0


def _load_from_db() -> Dict[str, str]:
    """从 devices.db 的 ai_butler_config 表读取身份配置。"""
    rows: Dict[str, str] = {}
    try:
        if not _DB_PATH.exists():
            return rows
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT config_key, config_value FROM ai_butler_config")
            rows = {k: v for k, v in cursor.fetchall() if k}
        finally:
            conn.close()
    except Exception as e:  # 数据库缺失/表不存在时使用默认值
        logger.bind(tag=TAG).warning(f"读取 ai_butler_config 失败，使用默认值: {e}")
    return rows


def _load_prompt_from_config() -> str:
    """从 data/.config.yaml 读取实际注入 LLM 的 prompt 字段。"""
    try:
        if not _CONFIG_PATH.exists():
            return ""
        import yaml

        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("prompt") or ""
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取生效 prompt 失败: {e}")
        return ""


def _load_location_from_config() -> str:
    """从 data/.config.yaml 读取后台「天气预报设置」里的默认城市。

    这是家庭成员所在地的权威源（后台天气设置页保存时写入该字段），
    与 prompt_manager 注入 <context> 的城市、get_weather 的默认城市保持一致。
    """
    try:
        if not _CONFIG_PATH.exists():
            return ""
        import yaml

        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        location = (
            (cfg.get("plugins") or {}).get("get_weather", {}).get("default_location")
        )
        return (location or "").strip()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取后台天气默认城市失败: {e}")
        return ""


def _get_data() -> Dict:
    """获取（带缓存的）身份配置。"""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]

    rows = _load_from_db()
    data = {
        "name": (rows.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME,
        "nickname": (rows.get("nickname") or DEFAULT_NICKNAME).strip() or DEFAULT_NICKNAME,
        "soul": (rows.get("soul") or DEFAULT_SOUL).strip() or DEFAULT_SOUL,
        "identity": (rows.get("identity") or DEFAULT_IDENTITY).strip() or DEFAULT_IDENTITY,
        "traits": rows.get("traits", ""),
        "agents": rows.get("agents", ""),
        "tools": rows.get("tools", ""),
        "wake_word": rows.get("wakeWord", ""),
        "location": _load_location_from_config(),
    }
    _cache["data"] = data
    _cache["ts"] = now
    return data


def get_name() -> str:
    """助手名字（后台「AI管家」Tab 的 name）。"""
    return _get_data()["name"]


def get_nickname() -> str:
    """助手昵称。未单独配置时回退为名字。"""
    return _get_data()["nickname"]


def safe_name(default: str = DEFAULT_NAME) -> str:
    """取助手名，**任何异常都不会抛**，最差回退到中性默认值。

    [2026-10-06] 供各调用方的 except 分支使用。过去那些分支直接写
    `ai_name = "伊娃"`，导致换部署/数据库异常时会念出别人的名字。
    这里统一兜底，且默认值本身也不再含具体人名。
    """
    try:
        return (get_name() or default).strip() or default
    except Exception:
        return default


def safe_nickname(default: str = DEFAULT_NICKNAME) -> str:
    """safe_name 的昵称版本。"""
    try:
        return (get_nickname() or default).strip() or default
    except Exception:
        return default


def get_soul() -> str:
    """灵魂本质（🧬 SOUL）：价值观、核心信念。"""
    return _get_data()["soul"]


def get_identity() -> str:
    """身份角色（🎭 IDENTITY）：职责、定位。"""
    return _get_data()["identity"]


def get_traits() -> List[str]:
    """核心性格特质列表（后台 12 项复选框）。"""
    raw = _get_data()["traits"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        # 兼容逗号分隔的旧格式
        return [t.strip() for t in str(raw).split(",") if t.strip()]


def get_agents() -> List[str]:
    """职责清单（后台「职责」字段，按行分隔）。"""
    return [line.strip() for line in (_get_data()["agents"] or "").splitlines() if line.strip()]


def get_config() -> Dict:
    """一次性返回全部身份配置（副本）。"""
    return dict(_get_data())


def get_full_prompt() -> str:
    """实际注入 LLM 的完整 system prompt（来自 data/.config.yaml 的 prompt 字段）。

    灵魂里出现的 ``{name}`` 占位符统一替换为后台 AI 名称，
    后台改名后下一次缓存失效即同步给 LLM。
    """
    raw = _load_prompt_from_config()
    try:
        return raw.replace("{name}", get_name())
    except Exception:
        return raw


def get_soul_first_line() -> str:
    """灵魂本质的第一行，用于拼接简短自我介绍。"""
    for line in get_soul().splitlines():
        line = line.strip()
        if line:
            return line
    return DEFAULT_SOUL


def get_self_intro() -> str:
    """简短自我介绍，名字取自后台配置，例如：我是小玉，你的AI管家~"""
    return f"我是{safe_name()}，你的AI管家~"


def get_location() -> str:
    """家庭所在地，取自后台「天气预报设置」的默认城市（default_location）。

    这是地点的权威源（主人 2026-09-17 指示）：后台天气设置里设什么，
    记忆档案（OpenViking profile.md）里的地点就跟着是什么。未配置时返回空串。
    """
    return _get_data()["location"]
