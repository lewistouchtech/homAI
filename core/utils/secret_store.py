"""密钥集中存储（2026-10-05 起全项目统一规范）

背景与规范
----------
本项目的 API Key 只允许存在两个地方：

1. ``<项目根>/.env``          —— 真值。不在 git 中（.gitignore 已排除）。
2. ``data/.config.yaml``    —— 只写 ``${变量名}`` 引用，不写真值。

这样做的原因：``.config.yaml`` 是被 git 跟踪的（且走 LFS），
明文 Key 一旦提交就会永久留在历史里，无法靠改 .gitignore 清除。

统一规范（全项目生效，不是一次性迁移）
--------------------------------------
* 新增/修改任何密钥 → 必须走 :func:`store` 拿到 ``${引用}`` 再写进 yaml
* 读取配置 → 走 ``config.config_loader.read_config``，它会递归展开 ``${}``
* 任何新的写入 yaml 的接口，都应调用 :func:`to_ref` 而不是直接赋值明文

命名规则
--------
``HB_<模块>_<提供者>_<字段>``，例如 ``HB_ASR_DOUBAO_STREAM_API_KEY``。
同一个真值只分配一个变量名，多处共用时自动复用（见 :func:`find_var`）。
"""

import os
import re
from collections.abc import Mapping
from typing import Dict, Optional, Tuple

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 判定「这个字段是密钥」的依据：键名里出现这些片段即视为密钥
SECRET_HINTS = ("key", "secret", "token", "appid", "app_id", "password", "passwd")

# 例外：含上述片段但**不是**密钥的字段，必须排除
NOT_SECRET_FIELDS = (
    "max_tokens", "max_token", "token_limit", "token_usage", "tokens",
    "token_budget", "chunk_size", "model_path", "model_dir", "key_type",
)

# 项目根目录（本文件在 <root>/core/utils/ 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(_ROOT, ".env")


# 示例/占位值：不是真密钥，写进 .env 毫无意义且会污染
PLACEHOLDER_VALUES = (
    "你的api_key", "你的密钥", "changeme", "your_api_key", "xxx",
    "1234", "null", "none", "~", "true", "false",
    # 本地/自建服务的占位 key（协议上要求有这个字段，但不是密钥）
    "ollaya-local", "local-no-key", "no-key", "no_api_key",
)


def is_placeholder(value: str) -> bool:
    """是否是占位/示例值而非真密钥。"""
    v = str(value or "").strip()
    if not v:
        return True
    low = v.lower()
    return v in PLACEHOLDER_VALUES or low in PLACEHOLDER_VALUES or "你的" in v


def is_secret_field(key: str) -> bool:
    """字段名是否属于密钥类。

    注意排除表优先：``max_tokens`` 含 "token" 但它不是密钥。
    排除表是必须的——迁移脚本曾因缺它把 max_tokens: 500 当成密钥搬进 .env。
    """
    k = str(key or "").lower()
    if k in NOT_SECRET_FIELDS:
        return False
    return any(h in k for h in SECRET_HINTS)


# ---------------- .env 读写（行级，保注释与顺序） ----------------

def read_env_lines() -> list:
    """读 .env 全部行；文件不存在返回空列表。"""
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def get_env_var(name: str) -> str:
    """按变量名读 .env 真值（不经 os.environ，便于单测与后台展示）。"""
    for line in read_env_lines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return ""


def find_var(value: str) -> str:
    """若该真值已在 .env 里，返回已有变量名；否则返回空串。用于多处共用去重。"""
    if not value:
        return ""
    for line in read_env_lines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if v.strip().strip('"').strip("'") == value:
            return k.strip()
    return ""


def set_env_var(name: str, value: str) -> bool:
    """写入/更新 .env 变量。已存在则原地更新（保留注释），返回是否改动。"""
    if not name or not value:
        return False
    lines = read_env_lines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k == name:
                if lines[i] == f"{name}={value}":
                    return False
                lines[i] = f"{name}={value}"
                break
    else:
        lines.append(f"{name}={value}")
    os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # .env 必须仅属主可读，避免其他本机用户读取密钥
    try:
        os.chmod(ENV_PATH, 0o600)
    except Exception as e:  # pragma: no cover
        logger.bind(tag=TAG).warning(f"设置 .env 权限失败: {e}")
    return True


# ---------------- 变量名生成 ----------------

_SPLIT = re.compile(r"[^0-9a-zA-Z]+")

# 项目里 provider 键名用的是中文（如「豆包流式ASR」），但 POSIX 环境变量名只允许
# ASCII 字符，所以这里给出显式英文别名。未知的中文名会退化成 PROVIDER + 去重后缀。
_PROVIDER_ALIAS = {
    "豆包流式ASR": "DOUBAO_STREAM",
    "阿里百炼 ASR": "ALIYUN_BAILIAN",  # 2026-10-05 由 CustomASR 改名
    "豆包大模型": "DOUBAO_LLM",
    "豆包TTS": "DOUBAO_TTS",
    "火山双流TTS": "HUOSHAN_DOUBLE_STREAM",
    "豆包视觉": "DOUBAO_VLLM",
    "本地LLM": "LOCAL_LLM",
}


def detect_vendor(value: str) -> Optional[str]:
    """从密钥字面特征识别厂商，返回中性名（英文）。识别不出返回 None。

    必要性：同一把火山方舟 key 同时被 ASR / LLM / TTS / VLLM 四个模块使用，
    若按「首次出现的模块」命名，会生成 HB_ASR_DOUBAO_STREAM_API_KEY 这种
    带误导性前缀的名字。厂商名与模块无关，用它更准确。
    """
    v = str(value or "").strip()
    if not v:
        return None
    table = (
        ("ark-", "DOUBAO"),                 # 火山方舟
        ("sk-ws-", "ALIYUN_BAILIAN"),# 阿里百炼 WebSocket key
        ("sk-sp-", "ALIYUN_BAILIAN"),       # 阿里百炼 OpenAI 兼容 key
        ("sk-kimi-", "MOONSHOT_KIMI"),
        ("sk-", "OPENAI_COMPAT"),           # 其余 sk- 开头按兼容接口处理
        ("tvly-", "TAVILY"),
        ("ragflow-", "RAGFLOW"),
    )
    for prefix, vendor in table:
        if v.startswith(prefix):
            return vendor
    return None


def build_var_name(module: str, provider: str, field: str, taken: Optional[set] = None,
                   vendor: Optional[str] = None) -> str:
    """按 HB_<模块>_<提供者>_<字段> 生成变量名，全大写、非字母数字转下划线。

    vendor 非空时优先用它替代 provider 段（见 :func:`detect_vendor`）。
    """
    raw_provider = str(provider or "")

    def norm(s: str) -> str:
        return _SPLIT.sub("_", s).strip("_").upper() or "X"

    # 厂商名 > 中文别名 > ASCII 化 > 语义中性兜底
    p = vendor
    if p is None:
        if raw_provider.isascii() and raw_provider.strip():
            p = norm(raw_provider)
        else:
            p = _PROVIDER_ALIAS.get(raw_provider) or "PROVIDER"

    base = f"HB_{norm(module)}_{p}_{norm(field)}"
    if not taken or base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def existing_var_names() -> set:
    return {
        s.strip().partition("=")[0].strip()
        for s in read_env_lines()
        if s.strip() and not s.startswith("#") and "=" in s
    }


# ---------------- 对外主接口 ----------------

def to_ref(module: str, provider: str, field: str, value: str) -> str:
    """把明文真值存进 .env，返回 ``${变量名}`` 引用。

    - 非密钥字段或空值 → 原样返回
    - 已是 ``${...}`` 形式 → 原样返回
    - 真值已在 .env 里 → 复用那个变量，不重复写
    """
    v = str(value or "").strip()
    if not v or not is_secret_field(field):
        return value
    if v.startswith("${"):
        return v
    # 占位符/示例值不是真密钥，搬进 .env 只会污染
    if is_placeholder(v):
        return value

    reused = find_var(v)
    if reused:
        return "${" + reused + "}"

    taken = existing_var_names()
    name = build_var_name(module, provider, field, taken, vendor=detect_vendor(v))
    if set_env_var(name, v):
        logger.bind(tag=TAG).info(f"密钥已归集到 .env：{name}（{module}.{provider}.{field}）")
    return "${" + name + "}"


def scan_plain_secrets(text: str) -> list:
    """在一段 yaml 文本里找出所有疑似明文密钥的行，供迁移工具/巡检使用。

    返回 [(行号(1基), 缩进, 键名, 值)]，值已脱敏。
    """
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if "api_key" not in line and not any(h in line.lower() for h in SECRET_HINTS):
            continue
        m = re.match(r"^(\s*)([A-Za-z0-9_]+)\s*:\s*(\S.*)$", line)
        if not m:
            continue
        indent, key, val = m.group(1), m.group(2), m.group(3).strip().strip('"').strip("'")
        if not is_secret_field(key):
            continue
        if not val or val.startswith("${") or "你的" in val:
            continue
        hits.append((i, indent, key, val[:6] + "…"))
    return hits


def secretize_fields(module: str, provider: str, fields: dict) -> dict:
    """把一批字段里的明文密钥换成 ${} 引用（非密钥字段原样保留）。

    这是「写配置进 yaml」的唯一入口应该走的路：调用它之后，
    返回的 dict 里已不含任何明文密钥，可以安全写进被 git 跟踪的文件。
    """
    out = {}
    for k, v in (fields or {}).items():
        try:
            out[k] = to_ref(module, provider, k, v)
        except Exception as e:  # 归集失败不能阻断保存，保守地原样写入
            logger.bind(tag=TAG).error(f"密钥归集失败 {module}.{provider}.{k}: {e}")
            out[k] = v
    return out


def snapshot() -> Dict[str, str]:
    """给后台/巡检用：返回 .env 现有变量名清单（不含值）。"""
    return {n: "***" for n in sorted(existing_var_names())}

# ---------------- 整棵配置字典的兜底归集 ----------------

def sweep_dict_secrets(node, _path: str = ""):
    """递归遍历配置字典，把明文密钥换成 ${} 引用（真值写入 .env）。

    这是「整体 dump 写回 yaml」类接口的最后一道防线。这类接口读配置时用了
    插值（拿到的就是真值），若直接 yaml.safe_dump 写回，密钥就重新落进了
    被 git 跟踪的文件——2026-10-05 在 ``core/api/search_api.py`` 真实存在过
    这个组合缺陷。

    与 :func:`sweep_plain_secrets`（扫文本）的区别：
      * 扫文本：用于行级改写，能保留注释和格式
      * 扫字典：用于整体 dump 前的内容净化，不管格式

    不修改入参，返回净化后的新对象（dict/list 会被复制）。
    """
    if isinstance(node, Mapping):
        return {
            k: sweep_dict_secrets(v, f"{_path}.{k}" if _path else str(k))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [sweep_dict_secrets(v, f"{_path}[{i}]") for i, v in enumerate(node)]
    if isinstance(node, str) and _path:
        field = _path.rsplit(".", 1)[-1].split("[", 1)[0]
        if is_secret_field(field):
            # 路径形如 plugins.web_search.api_key / ASR.豆包流式ASR.api_key
            # 取「模块段」= 第 1 段；「提供者」= 第 2 段（没有就用第 1 段）。
            # 不能把整条路径当 provider 传进 build_var_name，否则会生成
            # HB_PLUGINS_PLUGINS_GET_WEATHER_API_KEY_API_KEY 这种重复段的名字。
            segs = [x for x in _path.replace("[", ".").split(".") if x]
            section = segs[0] if segs else "CONFIG"
            provider = segs[1] if len(segs) > 1 else section
            return to_ref(section, provider, field, node)
    return node
