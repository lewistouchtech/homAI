from typing import Dict, Any
from config.logger import setup_logging
from core.utils import tts, llm, intent, memory, vad, asr

TAG = __name__
logger = setup_logging()


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """
    初始化所有模块组件

    Args:
        config: 配置字典

    Returns:
        Dict[str, Any]: 包含所有初始化后的模块的字典
    """
    modules = {}

    # 初始化TTS模块
    if init_tts:
        select_tts_module = config["selected_module"]["TTS"]
        modules["tts"] = initialize_tts(config)
        logger.bind(tag=TAG).info(f"初始化组件: tts成功 {select_tts_module}")

    # 初始化LLM模块
    if init_llm:
        select_llm_module = config["selected_module"]["LLM"]
        llm_type = (
            select_llm_module
            if "type" not in config["LLM"][select_llm_module]
            else config["LLM"][select_llm_module]["type"]
        )
        modules["llm"] = llm.create_instance(
            llm_type,
            config["LLM"][select_llm_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: llm成功 {select_llm_module}")

    # 初始化Intent模块
    if init_intent:
        select_intent_module = config["selected_module"]["Intent"]
        intent_type = (
            select_intent_module
            if "type" not in config["Intent"][select_intent_module]
            else config["Intent"][select_intent_module]["type"]
        )
        modules["intent"] = intent.create_instance(
            intent_type,
            config["Intent"][select_intent_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: intent成功 {select_intent_module}")

    # 初始化Memory模块
    if init_memory:
        select_memory_module = config["selected_module"]["Memory"]
        memory_type = (
            select_memory_module
            if "type" not in config["Memory"][select_memory_module]
            else config["Memory"][select_memory_module]["type"]
        )
        modules["memory"] = memory.create_instance(
            memory_type,
            config["Memory"][select_memory_module],
            config.get("summaryMemory", None),
        )
        logger.bind(tag=TAG).info(f"初始化组件: memory成功 {select_memory_module}")

    # 初始化VAD模块
    if init_vad:
        select_vad_module = config["selected_module"]["VAD"]
        vad_type = (
            select_vad_module
            if "type" not in config["VAD"][select_vad_module]
            else config["VAD"][select_vad_module]["type"]
        )
        modules["vad"] = vad.create_instance(
            vad_type,
            config["VAD"][select_vad_module],
        )
        logger.bind(tag=TAG).info(f"初始化组件: vad成功 {select_vad_module}")

    # 初始化ASR模块
    if init_asr:
        select_asr_module = config["selected_module"]["ASR"]
        modules["asr"] = initialize_asr(config)
        logger.bind(tag=TAG).info(f"初始化组件: asr成功 {select_asr_module}")
    return modules


def initialize_tts(config):
    select_tts_module = config["selected_module"]["TTS"]
    
    # 从数据库读取 TTS 配置并合并
    tts_config = config["TTS"][select_tts_module].copy()
    # provider 需要知道自己属于哪个后台模块，避免读取其他 TTS 保存的音色。
    tts_config["_module_name"] = select_tts_module
    db_tts_config = {}
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent / "data" / "devices.db"
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                # 从 tts_config 表读取配置
                cursor.execute("SELECT config_key, config_value FROM tts_config")
                rows = cursor.fetchall()
            finally:
                conn.close()
            if rows:
                db_tts_config = {row[0]: row[1] for row in rows}
                # 【修复 2026-09-06】防止跨 TTS 类型配置污染：
                # 数据库 tts_config 是控制台保存的配置，可能来自上一个 TTS 实现
                # （如 ChatTTS 的 voice=1332 是 seed 数字），直接合并会破坏新 TTS
                # （如 EdgeTTS 的 voice 需要 'zh-CN-XiaoxiaoNeural' 格式）。
                # 规则：纯数字 voice 只对 chattts 系列有意义，其他类型不合并。
                _tts_type_in_cfg = config["TTS"][select_tts_module].get("type", "")
                # 【修复 2026-09-12】tts_config 是一张全局键值表，voice/volume/speed 这些
                # 引擎专属参数没有"归属引擎"概念：控制台为 MossNano 存的 voice=eva、
                # volume=5（0-10 刻度）会被合并进云端引擎，导致 CosyVoice 拿到不存在的
                # 音色报 411、音量刻度也不对。provider 行记录这批值是当前哪个引擎存的，
                # 与本次要初始化的引擎不一致时，跳过引擎专属键、改用 config.yaml 自带值。
                _ENGINE_SCOPED_KEYS = ("voice", "volume", "speed", "temperature", "topP", "topK")
                db_owner = str(db_tts_config.get("provider") or "").strip()
                filtered_db = {}
                for k, v in db_tts_config.items():
                    if k == "provider":
                        continue
                    if k == "voice" and str(v).strip().isdigit() and "chattts" not in _tts_type_in_cfg.lower():
                        logger.bind(tag=TAG).info(
                            f"跳过数据库 voice='{v}'（ChatTTS seed 数字，不适用于 {_tts_type_in_cfg}）"
                        )
                        continue
                    if db_owner and db_owner != select_tts_module and k in _ENGINE_SCOPED_KEYS:
                        logger.bind(tag=TAG).info(
                            f"跳过数据库 {k}='{v}'（属 {db_owner} 的参数，当前引擎 {select_tts_module} 用 config.yaml 值）"
                        )
                        continue
                    filtered_db[k] = v
                # 合并数据库配置到 TTS 配置
                tts_config.update(filtered_db)
                logger.bind(tag=TAG).info(f"从数据库合并 TTS 配置: {filtered_db}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取数据库 TTS 配置失败: {e}")
    
    # 在数据库合并后确定 tts_type（优先使用数据库的 type 字段）
    tts_type = (
        db_tts_config.get("type")
        or (select_tts_module
            if "type" not in config["TTS"][select_tts_module]
            else config["TTS"][select_tts_module]["type"])
    )
    logger.bind(tag=TAG).info(f"TTS type 确定: {tts_type} (数据库={db_tts_config.get('type')}, 配置文件={config['TTS'][select_tts_module].get('type')})")
    try:
        new_tts = tts.create_instance(
            tts_type,
            tts_config,
            str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
        )
        return new_tts
    except Exception as e:
        logger.bind(tag=TAG).error(f"TTS 实例化失败 ({tts_type}): {e}")
        import traceback as _tb
        logger.bind(tag=TAG).error(f"TTS 实例化堆栈: {_tb.format_exc()}")
        return None


def initialize_asr(config):
    select_asr_module = config["selected_module"]["ASR"]
    
    # 从数据库读取 ASR 配置并合并
    asr_config = config["ASR"][select_asr_module].copy()
    db_asr_config = {}
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent / "data" / "devices.db"
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                # 从 asr_config 表读取配置
                cursor.execute("SELECT config_key, config_value FROM asr_config")
                rows = cursor.fetchall()
            finally:
                conn.close()
            if rows:
                db_asr_config = {row[0]: row[1] for row in rows}
                # 合并数据库配置到 ASR 配置
                asr_config.update(db_asr_config)
                # 关键修复：确保 model 和 model_dir 一致（fun_local.py 使用 model_dir）
                if "model" in db_asr_config and "model_dir" in db_asr_config:
                    if db_asr_config["model"] != db_asr_config["model_dir"]:
                        logger.bind(tag=TAG).warning(f"model 和 model_dir 不一致: model={db_asr_config['model']}, model_dir={db_asr_config['model_dir']}，优先使用 model_dir")
                        asr_config["model_dir"] = db_asr_config["model_dir"]
                        asr_config["model"] = db_asr_config["model_dir"]
                    else:
                        asr_config["model_dir"] = db_asr_config["model"]
                elif "model" in db_asr_config:
                    asr_config["model_dir"] = db_asr_config["model"]
                    asr_config["model"] = db_asr_config["model"]
                elif "model_dir" in db_asr_config:
                    asr_config["model"] = db_asr_config["model_dir"]
                logger.bind(tag=TAG).info(f"从数据库合并 ASR 配置: {db_asr_config}")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取数据库 ASR 配置失败: {e}")
    
    # 在数据库合并后确定 asr_type（优先使用数据库的 type 字段）
    asr_type = (
        db_asr_config.get("type")
        or (select_asr_module
            if "type" not in config["ASR"][select_asr_module]
            else config["ASR"][select_asr_module]["type"])
    )
    logger.bind(tag=TAG).info(f"ASR type 确定: {asr_type} (数据库={db_asr_config.get('type')}, 配置文件={config['ASR'][select_asr_module].get('type')})")
    
    try:
        new_asr = asr.create_instance(
            asr_type,
            asr_config,
            str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
        )
        logger.bind(tag=TAG).info("ASR模块初始化完成")
        return new_asr
    except Exception as e:
        logger.bind(tag=TAG).error(f"ASR 实例化失败 ({asr_type}): {e}")
        import traceback as _tb
        logger.bind(tag=TAG).error(f"ASR 实例化堆栈: {_tb.format_exc()}")
        return None


def initialize_voiceprint(asr_instance, config):
    """初始化声纹识别功能"""
    voiceprint_config = config.get("voiceprint")
    if not voiceprint_config:
        return False  

    # 应用配置
    if not voiceprint_config.get("url") or not voiceprint_config.get("speakers"):
        logger.bind(tag=TAG).warning("声纹识别配置不完整")
        return False

        
    try:
        asr_instance.init_voiceprint(voiceprint_config)
        logger.bind(tag=TAG).info("ASR模块声纹识别功能已动态启用")
        logger.bind(tag=TAG).info(f"配置说话人数量: {len(voiceprint_config['speakers'])}")
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"动态初始化声纹识别功能失败: {str(e)}")
        return False
