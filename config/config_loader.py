import os
import re
import yaml
from collections.abc import Mapping
from loguru import logger
from config.manage_api_client import init_service, get_server_config, get_agent_models


def _load_env_file():
    """加载项目根目录 .env（密钥隔离：config.yaml 用 ${VAR} 引用，真实值不入 git）

    ⚠️ 2026-10-05 改为 override=True。
    原来用的是默认 override=False + os.environ.setdefault，两者都「不覆盖已存在的
    同名变量」。而本项目的 .env 是密钥的唯一真值来源，后台保存密钥时正是往这里
    写新值——若进程环境里已残留同名变量（launchd/继承的 shell 环境、或上一次
    运行留下的空串），重启后读到的仍是旧值甚至空串，表现为：
        配置里明明有 ${HB_XXX}，插值出来却是空 → 401 /「需要 API Key」
    .env 应当覆盖外部环境，而不是相反。
    """
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)
        except ImportError:
            # 无 python-dotenv 时手动解析（同样覆盖）
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        os.environ[k] = v


_load_env_file()


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def find_unresolved_env_refs(value, _path="") -> list:
    """找出配置里**没被插值成功**的 ${VAR} 引用

    [2026-10-06] 为什么需要这个函数：
        `os.path.expandvars()` 遇到 .env 里不存在的变量时，
        **原样保留 `${VAR}` 字符串**，既不报错也不置空 ——
        于是配置里的 `api_key` 变成字面量 "${XXX}"，
        而它是非空字符串，管理台的「是否已配置」判断会显示 **已配置**，
        真正拿去连云端时才 401。排查时极具迷惑性。

        实测踩到过：`config.yaml` 里豆包流式ASR 写的是
        `${HB_ASR_DOUBAO_API_KEY_2}`，而 .env 里只有 `HB_ASR_DOUBAO_API_KEY`
        → 管理台显示豆包ASR「密钥已配置」，实际发出去的是 26 个字符的占位符。

        另外 `.env` 里写成空串（`KEY=`）也会插值成空，同样是静默失败，
        所以这里把「缺失」和「空串」都算作未解析。

    返回: [(配置路径, 变量名, 原因), ...]
    """
    bad = []
    if isinstance(value, str):
        for var in _ENV_REF_PATTERN.findall(value):
            # 完全等于占位符 = 没插值成功；嵌在文本里的另行人工判断
            if value.strip() == "${" + var + "}":
                raw = os.environ.get(var)
                if raw is None:
                    bad.append((_path, var, "变量不存在（.env 里没有这一项）"))
                elif raw == "":
                    bad.append((_path, var, "变量为空串（.env 里写了 KEY= 但没给值）"))
    elif isinstance(value, Mapping):
        for k, v in value.items():
            bad += find_unresolved_env_refs(v, f"{_path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            bad += find_unresolved_env_refs(v, f"{_path}[{i}]")
    return bad


def _expand_env_vars(value):
    """递归展开配置值中的 ${VAR} 环境变量引用"""
    if isinstance(value, str):
        if "${" in value:
            return os.path.expandvars(value)
        return value
    if isinstance(value, Mapping):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def get_project_dir():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def load_yaml_expanded(src):
    """读 YAML 并展开 ${} 引用。src 可以是路径 / Path / 已打开的文件对象 / 文本。

    为什么需要这个函数（2026-10-05）
    ---------------------------------
    ``data/.config.yaml`` 与 ``config.yaml`` 里的 API Key 一律存成 ``${变量名}``，
    真值在 ``.env``（见 ``core/utils/secret_store.py``）。全仓有几十处直接调
    ``yaml.safe_load`` 读这两个文件，其中：

    * **消费型**（读出来是为了拿密钥去调云端 / 显示）必须走本函数，否则拿到的是
      ``${VAR}`` 字面串——天气查询会拿它当 key 拼 URL、搜索引擎会带它去鉴权，
      结果必然 401。
    * **写回型**（读出来改完再dump 回同一个文件）**必须继续用 ``yaml.safe_load``**，
      否则插值后写回会把引用还原成明文密钥，等于把密钥又塞回被 git 跟踪的文件。

    判定方法见 ``docs/测试记录/2026-10-05_密钥归集与ASR入口合并验证.md``。

    字符串入参的歧义（踩过的坑）
    ---------------------------
    早期实现只按 ``str`` → 路径处理，而 ``search_api._load_config`` 传的是
    ``CONFIG_PATH.read_text()``（整份 YAML 文本）—— 于是把 YAML 当成文件名去 open，
    抛 ``OSError: File name too long``，网页搜索直接坏掉。现按下面顺序判别：

    1. Path / os.PathLike → 当路径
    2. 有 ``read`` 属性 → 当文件对象
    3. 字符串且该路径真实存在 → 当路径
    4. 字符串且含换行或冒号 → 当 YAML 文本
    5. 其余 → 抛错（路径拼错必须暴露，不能静默解析出一堆字符串让人摸不着头脑）
    """
    if isinstance(src, os.PathLike):
        with open(src, "r", encoding="utf-8") as f:
            return _expand_env_vars(yaml.safe_load(f)) or {}
    if hasattr(src, "read"):
        return _expand_env_vars(yaml.safe_load(src)) or {}
    if isinstance(src, str):
        if os.path.exists(src):
            with open(src, "r", encoding="utf-8") as f:
                return _expand_env_vars(yaml.safe_load(f)) or {}
        if "\n" in src or ":" in src:
            return _expand_env_vars(yaml.safe_load(src)) or {}
    raise TypeError(
        f"load_yaml_expanded 只接受 路径 / Path / 文件对象 / YAML 文本，"
        f"收到 {type(src).__name__}：{str(src)[:80]!r}"
    )


def read_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return _expand_env_vars(config)


def load_config():
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_config_path = get_project_dir() + "data/.config.yaml"

    # 加载默认配置
    default_config = read_config(default_config_path)
    custom_config = read_config(custom_config_path)
    
    # 确保custom_config不为None
    if custom_config is None:
        custom_config = {}

    if custom_config.get("manager-api", {}).get("url"):
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # 如果已经在事件循环中，使用异步版本
            config = asyncio.run_coroutine_threadsafe(
                get_config_from_api_async(custom_config), loop
            ).result()
        except RuntimeError:
            # 如果不在事件循环中（启动时），创建新的事件循环
            config = asyncio.run(get_config_from_api_async(custom_config))
    else:
        # 合并配置
        config = merge_configs(default_config, custom_config)
    # 初始化目录
    ensure_directories(config)

    # [2026-10-06] 配置加载完立刻体检一遍「没插值成功的 ${VAR}」。
    #   这类问题最阴的地方在于：它是非空字符串，管理台的「是否已配置」会显示
    #   「已配置」，主人看不出异常，真连云端时才 401。
    #   在这里集中报出来，日志里能直接看到是哪个配置项、缺哪个变量。
    unresolved = find_unresolved_env_refs(config)
    if unresolved:
        detail = "；".join(f"{p} → {var}（{reason}）" for p, var, reason in unresolved)
        logger.bind(tag="config").warning(
            f"配置里有 {len(unresolved)} 处 ${{VAR}} 没能插值成功，"
            f"这些字段会被当成字面量字符串使用（管理台可能误显示为「已配置」）：{detail}"
        )

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


async def get_config_from_api_async(config):
    """从Java API获取配置（异步版本）"""
    # 初始化API客户端
    init_service(config)

    # 获取服务器配置
    config_data = await get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    auth_enabled = config_data.get("server", {}).get("auth", {}).get("enabled", False)
    # server的配置以本地为准
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    config_data["server"]["auth"] = {"enabled": auth_enabled}
    # 如果服务器没有prompt_template，则从本地配置读取
    if not config_data.get("prompt_template"):
        config_data["prompt_template"] = config.get("prompt_template")
    return config_data


async def get_private_config_from_api(config, device_id, client_id):
    """从Java API获取私有配置"""
    return await get_agent_models(device_id, client_id, config["selected_module"])


def ensure_directories(config):
    """确保所有配置路径存在"""
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # 根据selected_module创建模型目录
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module) is None:
            continue
        if config.get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    # 2026-10-05 护栏：selected_module 指向的 provider 必须真实存在。
    # 历史故障：selected_module.TTS 写的是「MossNano seed2.0」，而该 provider
    # 早已从 TTS 段删除 —— 于是 initialize_tts 里 config["TTS"][...] 抛 KeyError，
    # 表现为主人看到的「配置已写入，但组件热更新失败（建议重启服务）」，
    # 重启也没用（每次热更新都会再失败）。这里在配置合并阶段就纠正，
    # 回落到该段第一个可用 provider，避免整机语音不可用。
    selected = merged.get("selected_module")
    if isinstance(selected, Mapping):
        for mod in ("ASR", "TTS", "LLM", "VLLM", "VAD", "Memory", "Intent"):
            cur = selected.get(mod)
            avail = merged.get(mod)
            if not isinstance(avail, Mapping) or not avail:
                continue
            if cur not in avail:
                # 优先回落���「已配置好密钥」的 provider，否则主人会落到一个
                # 什么都没填的条目上（比如 CustomTTS），表现为「切换成功但用不了」
                _KEY_FIELDS = ("api_key", "access_token", "appid", "secret_key",
                               "access_key_id", "access_key_secret", "api_secret")
                _LOCAL_TYPES = ("fun_local", "sherpa_onnx_local", "vosk", "funasr_nano",
                                "fun_server", "moss_nano", "mem_local_short",
                                "openviking")
                # 记忆后端（mem_local_short/openviking）被上游
                # 误放在 LLM: 段里，但 llm.py 没有它们的实现，选中会让服务启动失败。
                # 2026-10-05 主人/我自己连续踩到两次，这里额外兜一层。
                if mod == "LLM" and cur in ("mem_local_short", "openviking"):
                    real_llm = [n for n, c in avail.items()
                                if isinstance(c, dict)
                                and str(c.get("type", "")) not in
                                ("mem_local_short", "openviking")]
                    if real_llm:
                        selected[mod] = real_llm[0]
                        print(f"[config] ⚠️ selected_module.LLM 指向记忆后端 {cur!r}，"
                              f"已改为真正的 LLM 引擎 {real_llm[0]!r}。"
                              f"（记忆后端请在 selected_module.Memory 里选）")
                        continue

                def _ready(cfg: dict) -> bool:
                    if not isinstance(cfg, dict):
                        return False
                    for k, v in cfg.items():
                        if k in _KEY_FIELDS and isinstance(v, str):
                            if not v.strip() or "你的" in v or "${" in v:
                                return False
                    return True
                # 回落时只挑「本地/已配好密钥」的条目。绝不能回落到未配置的云端
                # provider——那会让服务下次启动直接失败（主人实测：TTS 切到未配置的
                # CustomTTS 后，服务反复启动不了，last exit code=1）。
                def _usable(n: str, c) -> bool:
                    return isinstance(c, dict) and str(c.get("type", "")) in _LOCAL_TYPES

                configured = [n for n, c in avail.items() if _ready(c) and _usable(n, c)]
                locals_ = [n for n, c in avail.items() if _usable(n, c)]
                if configured:
                    fallback, why = configured[0], "已配置密钥的本地/记忆类provider"
                elif locals_:
                    fallback, why = locals_[0], "本地/记忆类 provider（无需密钥）"
                else:
                    fallback, why = next(iter(avail.keys())), "该段首个条目"
                selected[mod] = fallback
                print(
                    f"[config] ⚠️ selected_module.{mod} 指向不存在的 provider "
                    f"{cur!r}，已自动回落到 {fallback!r}（{why}；"
                    f"可用的：{'、'.join(avail.keys())}）"
                )

    return merged
