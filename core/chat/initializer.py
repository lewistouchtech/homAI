"""
组件初始化模块 — 从 connection.py 拆分

职责：TTS/ASR/Memory/Intent/Voiceprint 初始化、配置加载
"""
import json
import time
import asyncio
import threading

from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from core.providers.tts.default import DefaultTTS
from core.providers.asr.dto.dto import InterfaceType
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from config.config_loader import get_private_config_from_api
from config.manage_api_client import DeviceNotFoundException, DeviceBindException
from core.utils.util import check_vad_update, check_asr_update, filter_sensitive_info
from config.logger import build_module_string, create_connection_logger
from core.utils.voiceprint_provider import VoiceprintProvider
from pathlib import Path

TAG = __name__


def initialize_components(conn):
    """初始化所有组件"""
    try:
        if conn.tts is None:
            conn.tts = _initialize_tts(conn)
        asyncio.run_coroutine_threadsafe(
            conn.tts.open_audio_channels(conn), conn.loop
        )
        if conn.need_bind:
            conn.bind_completed_event.set()
            return
        conn.selected_module_str = build_module_string(
            conn.config.get("selected_module", {})
        )
        conn.logger = create_connection_logger(conn.selected_module_str)

        if conn.config.get("prompt") is not None:
            user_prompt = conn.config["prompt"]
            prompt = conn.prompt_manager.get_quick_prompt(user_prompt)
            conn.change_system_prompt(prompt)
            conn.logger.bind(tag=TAG).info(
                f"快速初始化组件: prompt成功 {prompt[:50]}..."
            )

        if conn.vad is None:
            conn.vad = conn._vad
        if conn.asr is None:
            conn.asr = _initialize_asr(conn)

        if conn.asr is not None:
            _initialize_voiceprint(conn)
            asyncio.run_coroutine_threadsafe(
                conn.asr.open_audio_channels(conn), conn.loop
            )
        else:
            conn.logger.bind(tag=TAG).warning("ASR 未初始化，跳过语音识别功能")

        _initialize_memory(conn)
        _initialize_intent(conn)
        _initialize_speech_registry(conn)
        _init_report_threads(conn)
        _init_prompt_enhancement(conn)

        # 全部组件初始化完成，放行意图分析（intentHandler 等待此标志）
        conn.components_ready = True

    except Exception as e:
        import traceback as _tb
        conn.logger.bind(tag=TAG).error(f"实例化组件失败: {e}")
        conn.logger.bind(tag=TAG).error(f"实例化组件失败堆栈: {_tb.format_exc()}")


def _initialize_tts(conn):
    """初始化TTS"""
    tts = None
    if not conn.need_bind:
        tts = initialize_tts(conn.config)
    if tts is None:
        tts = DefaultTTS(conn.config, delete_audio_file=True)
    return tts


def _initialize_asr(conn):
    """初始化ASR"""
    # 【并发优化 2026-09-10】STREAM 类型也复用共享单例。
    # FunASR 流式实现内部按 device_key 隔离 SESSIONS，各连接缓冲互不污染，
    # 此前"每连接新建"会导致并发设备各加载一份模型，内存翻倍。
    if (
            conn._asr is not None
            and hasattr(conn._asr, "interface_type")
            and conn._asr.interface_type in (InterfaceType.LOCAL, InterfaceType.STREAM)
    ):
        asr = conn._asr
    else:
        asr = initialize_asr(conn.config)
    return asr


def _initialize_voiceprint(conn):
    """为当前连接初始化声纹识别"""
    try:
        voiceprint_config = conn.config.get("voiceprint", {})
        if voiceprint_config:
            voiceprint_provider = VoiceprintProvider(voiceprint_config)
            if voiceprint_provider is not None and voiceprint_provider.enabled:
                conn.voiceprint_provider = voiceprint_provider
                conn.logger.bind(tag=TAG).info("声纹识别功能已在连接时动态启用")
            else:
                conn.logger.bind(tag=TAG).warning("声纹识别功能启用但配置不完整")
        else:
            conn.logger.bind(tag=TAG).info("声纹识别功能未启用")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"声纹识别初始化失败: {str(e)}")


def _initialize_memory(conn):
    """初始化记忆模块。

    ⚠️ 2026-10-13 WP-1：这里传的 `role_id=conn.device_id` **不再是记忆身份**。
    Mem0Provider 是 websocket_server 的单例（所有连接共用一个实例），
    原来 provider 内部执行 `self.user_id = role_id`，于是「最后一个连上的设备」
    决定全家的记忆归属 —— 真跑复现：管理台 WS 一连（device_id=admin-console），
    所有终端此后的查询与抽取都写到 admin-console 名下（库里 27 条）。
    现在记忆归属由**说话人**决定（`core/utils/memory_scope.py`：
    声纹识别出成员 → `m:<成员id>`，未识别 → `family`），device_id 只作
    metadata 与幂等键的一部分。本函数签名与调用方式保持不变。
    """
    if conn.memory is None:
        return
    conn.memory.init_memory(
        role_id=conn.device_id,
        llm=conn.llm,
        summary_memory=conn.config.get("summaryMemory", None),
        save_to_file=not conn.read_config_from_api,
    )

    memory_config = conn.config["Memory"]
    memory_type = conn.config["Memory"][conn.config["selected_module"]["Memory"]]["type"]

    # ── 2026-10-13 WP-5：短期上下文续接（L1）──
    # 新连接的 dialogue 是空的（刷新页面 / 120 秒待机唤醒都会新建连接），
    # 而 dialogue_history 表里已经有上万行按 device_id 记录的真实对话，
    # 却一直只写不读。这里把最近几轮回灌进 <recent_context>（system prompt 段）。
    # ⚠️ 对所有记忆后端都生效：它补的是「短期上下文」，与长期记忆后端选型无关
    # （任何记忆后端都应该记得住两分钟前说过什么）。
    try:
        from core.utils.memory_short_term import build_recent_context

        _carried = build_recent_context(
            conn.device_id, exclude_session_id=getattr(conn, "session_id", None)
        )
        if _carried:
            conn._recent_context_db = _carried
            conn.logger.bind(tag=TAG).info(
                f"[短期续接] 新连接已带上最近对话（{len(_carried)} 字）"
            )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[短期续接] 初始化失败（不影响对话）: {e}")

    if memory_type == "openviking":
        # OpenViking：恢复会话上下文（M1 会话恢复）
        if hasattr(conn.memory, 'get_recent_messages'):
            try:
                recent_msgs = conn.memory.get_recent_messages(limit=10)
                if recent_msgs:
                    # 将会话上下文注入到对话历史
                    if not hasattr(conn, 'dialogue'):
                        conn.dialogue = []
                    conn.dialogue.extend(recent_msgs)
                    conn.logger.bind(tag=TAG).info(
                        f"已恢复 {len(recent_msgs)} 条会话消息"
                    )
            except Exception as e:
                conn.logger.bind(tag=TAG).warning(f"恢复会话上下文失败: {e}")

        # 使用主 LLM
        conn.memory.set_llm(conn.llm)

    elif memory_type == "mem_local_short":
        memory_llm_name = memory_config[conn.config["selected_module"]["Memory"]]["llm"]
        if memory_llm_name and memory_llm_name in conn.config["LLM"]:
            from core.utils import llm as llm_utils
            memory_llm_config = conn.config["LLM"][memory_llm_name]
            memory_llm_type = memory_llm_config.get("type", memory_llm_name)
            memory_llm = llm_utils.create_instance(memory_llm_type, memory_llm_config)
            conn.logger.bind(tag=TAG).info(
                f"为记忆总结创建了专用LLM: {memory_llm_name}, 类型: {memory_llm_type}"
            )
            conn.memory.set_llm(memory_llm)
        else:
            conn.memory.set_llm(conn.llm)



def _initialize_intent(conn):
    """初始化意图识别模块"""
    if conn.intent is None:
        return
    conn.intent_type = conn.config["Intent"][conn.config["selected_module"]["Intent"]]["type"]
    if conn.intent_type == "function_call" or conn.intent_type == "intent_llm":
        conn.load_function_plugin = True

    intent_config = conn.config["Intent"]
    intent_type = conn.config["Intent"][conn.config["selected_module"]["Intent"]]["type"]

    if intent_type == "nointent":
        return
    elif intent_type == "intent_llm":
        intent_llm_name = intent_config[conn.config["selected_module"]["Intent"]]["llm"]
        if intent_llm_name and intent_llm_name in conn.config["LLM"]:
            from core.utils import llm as llm_utils
            intent_llm_config = conn.config["LLM"][intent_llm_name]
            intent_llm_type = intent_llm_config.get("type", intent_llm_name)
            intent_llm = llm_utils.create_instance(intent_llm_type, intent_llm_config)
            conn.logger.bind(tag=TAG).info(
                f"为意图识别创建了专用LLM: {intent_llm_name}, 类型: {intent_llm_type}"
            )
            conn.intent.set_llm(intent_llm)
        else:
            conn.intent.set_llm(conn.llm)
            conn.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

    conn.func_handler = UnifiedToolHandler(conn)
    if hasattr(conn, "loop") and conn.loop:
        asyncio.run_coroutine_threadsafe(conn.func_handler._initialize(), conn.loop)


def _initialize_speech_registry(conn):
    """加载 speech_registry.yaml，构建 {tool_name: SpeechSpec} 内存表（v3.0 第一波 P1）"""
    try:
        import yaml
        from pathlib import Path

        registry_path = Path(__file__).parent.parent.parent / "config" / "speech_registry.yaml"
        if not registry_path.exists():
            conn.logger.bind(tag=TAG).warning(f"speech_registry.yaml 不存在: {registry_path}")
            conn.speech_specs = {}
            return

        with open(registry_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        tools = data.get("tools", {})
        defaults = data.get("defaults", {})

        # 构建 SpeechSpec 字典
        conn.speech_specs = {}
        for tool_name, spec in tools.items():
            conn.speech_specs[tool_name] = {
                "tier": spec.get("tier", "B"),
                "type": spec.get("type", "query"),
                "lead_in": spec.get("lead_in"),
                "pre_announce": spec.get("pre_announce"),
                "duration_phrase": spec.get("duration_phrase"),
                "progress_mode": spec.get("progress_mode", "none"),
                "heartbeat_interval": spec.get("heartbeat_interval", 25),
                "result_template": spec.get("result_template"),
                "confirm_required": spec.get("confirm_required", False),
                "confirm_template": spec.get("confirm_template"),
                "anchors": spec.get("anchors", []),
                "polish": spec.get("polish", False),
            }

        # 存默认配置
        conn.speech_defaults = defaults

        conn.logger.bind(tag=TAG).info(
            f"speech_registry 已加载: {len(conn.speech_specs)} 个工具话术登记"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"speech_registry 加载失败: {e}")
        conn.speech_specs = {}
        conn.speech_defaults = {}


def _init_prompt_enhancement(conn):
    """增强系统提示词"""
    conn.prompt_manager.update_context_info(conn, conn.client_ip)
    enhanced_prompt = conn.prompt_manager.build_enhanced_prompt(
        conn.config["prompt"], conn.device_id, conn.client_ip
    )
    if enhanced_prompt:
        conn.change_system_prompt(enhanced_prompt)
        conn.logger.bind(tag=TAG).debug("系统提示词已增强更新")


def _init_report_threads(conn):
    """初始化ASR和TTS上报线程"""
    from core.handle.reportHandle import report

    if not conn.read_config_from_api or conn.need_bind:
        return
    if conn.chat_history_conf == 0:
        return
    if conn.report_thread is None or not conn.report_thread.is_alive():
        conn.report_thread = threading.Thread(
            target=_report_worker, args=(conn,), daemon=True
        )
        conn.report_thread.start()
        conn.logger.bind(tag=TAG).info("TTS上报线程已启动")


def _report_worker(conn):
    """聊天记录上报工作线程"""
    import queue
    while not conn.stop_event.is_set():
        try:
            item = conn.report_queue.get(timeout=1)
            if item is None:
                break
            try:
                if conn.executor is None:
                    continue
                conn.executor.submit(_process_report, conn, *item)
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"聊天记录上报线程异常: {e}")
        except queue.Empty:
            continue
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"聊天记录上报工作线程异常: {e}")
    conn.logger.bind(tag=TAG).info("聊天记录上报线程已退出")


def _process_report(conn, type, text, audio_data, report_time):
    """处理上报任务"""
    import asyncio as _asyncio
    from core.handle.reportHandle import report
    try:
        _asyncio.run(report(conn, type, text, audio_data, report_time))
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"上报处理异常: {e}")
    finally:
        conn.report_queue.task_done()


async def background_initialize(conn):
    """在后台初始化配置和组件"""
    try:
        await _initialize_private_config_async(conn)
        conn.executor.submit(initialize_components, conn)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"后台初始化失败: {e}")


async def _initialize_private_config_async(conn):
    """从接口异步获取差异化配置"""
    if not conn.read_config_from_api:
        conn.need_bind = False
        conn.bind_completed_event.set()
        return
    try:
        begin_time = time.time()
        private_config = await get_private_config_from_api(
            conn.config,
            conn.headers.get("device-id"),
            conn.headers.get("client-id", conn.headers.get("device-id")),
        )
        private_config["delete_audio"] = bool(conn.config.get("delete_audio", True))
        conn.logger.bind(tag=TAG).info(
            f"{time.time() - begin_time} 秒，异步获取差异化配置成功: {json.dumps(filter_sensitive_info(private_config), ensure_ascii=False)}"
        )
        conn.need_bind = False
        conn.bind_completed_event.set()
    except DeviceNotFoundException as e:
        conn.need_bind = True
        conn.bind_completed_event.set()
        private_config = {}
    except DeviceBindException as e:
        conn.need_bind = True
        conn.bind_code = e.bind_code
        conn.bind_completed_event.set()
        private_config = {}
    except Exception as e:
        conn.need_bind = True
        conn.logger.bind(tag=TAG).error(f"异步获取差异化配置失败: {e}")
        conn.bind_completed_event.set()
        private_config = {}

    init_llm, init_tts, init_memory, init_intent = False, False, False, False
    init_vad = check_vad_update(conn.common_config, private_config)
    init_asr = check_asr_update(conn.common_config, private_config)

    if init_vad:
        conn.config["VAD"] = private_config["VAD"]
        conn.config["selected_module"]["VAD"] = private_config["selected_module"]["VAD"]
    if init_asr:
        conn.config["ASR"] = private_config["ASR"]
        conn.config["selected_module"]["ASR"] = private_config["selected_module"]["ASR"]
    if private_config.get("TTS", None) is not None:
        init_tts = True
        conn.config["TTS"] = private_config["TTS"]
        conn.config["selected_module"]["TTS"] = private_config["selected_module"]["TTS"]
    if private_config.get("LLM", None) is not None:
        init_llm = True
        conn.config["LLM"] = private_config["LLM"]
        conn.config["selected_module"]["LLM"] = private_config["selected_module"]["LLM"]
    if private_config.get("VLLM", None) is not None:
        conn.config["VLLM"] = private_config["VLLM"]
        conn.config["selected_module"]["VLLM"] = private_config["selected_module"]["VLLM"]
    if private_config.get("Memory", None) is not None:
        init_memory = True
        conn.config["Memory"] = private_config["Memory"]
        conn.config["selected_module"]["Memory"] = private_config["selected_module"]["Memory"]
    if private_config.get("Intent", None) is not None:
        init_intent = True
        conn.config["Intent"] = private_config["Intent"]
        model_intent = private_config.get("selected_module", {}).get("Intent", {})
        conn.config["selected_module"]["Intent"] = model_intent
        if model_intent != "Intent_nointent":
            plugin_from_server = private_config.get("plugins", {})
            for plugin, config_str in plugin_from_server.items():
                plugin_from_server[plugin] = json.loads(config_str)
            conn.config["plugins"] = plugin_from_server
            conn.config["Intent"][conn.config["selected_module"]["Intent"]]["functions"] = plugin_from_server.keys()
    if private_config.get("prompt", None) is not None:
        conn.config["prompt"] = private_config["prompt"]
    if private_config.get("voiceprint", None) is not None:
        conn.config["voiceprint"] = private_config["voiceprint"]
    if private_config.get("summaryMemory", None) is not None:
        conn.config["summaryMemory"] = private_config["summaryMemory"]
    if private_config.get("device_max_output_size", None) is not None:
        conn.max_output_size = int(private_config["device_max_output_size"])
    if private_config.get("chat_history_conf", None) is not None:
        conn.chat_history_conf = int(private_config["chat_history_conf"])
    if private_config.get("mcp_endpoint", None) is not None:
        conn.config["mcp_endpoint"] = private_config["mcp_endpoint"]
    if private_config.get("context_providers", None) is not None:
        conn.config["context_providers"] = private_config["context_providers"]

    try:
        modules = await conn.loop.run_in_executor(
            None,
            initialize_modules,
            conn.logger,
            private_config,
            init_vad,
            init_asr,
            init_llm,
            init_tts,
            init_memory,
            init_intent,
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"初始化组件失败: {e}")
        modules = {}
    if modules.get("tts", None) is not None:
        conn.tts = modules["tts"]
    if modules.get("vad", None) is not None:
        conn.vad = modules["vad"]
    if modules.get("asr", None) is not None:
        conn.asr = modules["asr"]
    if modules.get("llm", None) is not None:
        conn.llm = modules["llm"]
    if modules.get("intent", None) is not None:
        conn.intent = modules["intent"]
    if modules.get("memory", None) is not None:
        conn.memory = modules["memory"]
