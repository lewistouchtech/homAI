import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.api.device_manager import DeviceManager
from core.api.plugins_api import list_plugins, toggle_plugin, test_mcp, reload_bundles, mcp_status
from core.api.voice_settings_api import get_voice_settings, save_voice_settings, get_available_tools, get_routing_models, save_routing_models
from core.api.family_api import list_family_members, add_family_member, update_family_member, delete_family_member, delete_member_voiceprint, bind_health_device, unbind_health_device, get_member_schedules, add_member_schedule, delete_member_schedule, get_member_health_records, update_member_health_status
from core.api.face_api import create_face_api_routes
from core.api.plate_api import create_plate_api_routes
from core.api.voiceprint_eval_api import create_voiceprint_eval_routes
from core.api.console_state_api import create_console_state_routes
from core.api.welcome_llm_api import create_welcome_llm_routes
from core.api.debug_api import get_debug_mode, set_debug_mode
from core.api.galaxy_api import handle_galaxy
from core.utils import secret_store
from core.api.todo_api import list_todos, add_todo, toggle_todo, delete_todo, get_dashboard_settings, save_dashboard_settings
from core.api.center_window_api import (
    get_avatar_preview,
    get_center_window,
    save_center_window,
    list_avatars,
    list_backgrounds,
    upload_background,
    get_background_image,
)
from core.api.llm_proxy_api import create_llm_proxy_routes
from core.api.express_api import get_express_config, update_express_config, get_express_by_member, get_all_express_with_members, add_express_package, update_member_phone, query_express_by_phone
from core.api.real_health_api import get_member_health_data, get_all_health_data, get_latest_vitals, get_latest_vitals_by_member
from core.api.health_data_api import get_health_records, add_health_data, get_latest_weight, get_health_summary, get_health_devices, get_ble_health_data
from core.api.config_api import (
    get_config, save_config, get_dashboard_config, save_dashboard_config, get_all_configs,
    config_save, config_load, config_load_all, config_save_batch, config_delete, config_load_all_categories,
    update_memory_config, get_memory_config, get_saved_models, save_saved_models,
    list_long_term_memories, delete_long_term_memory, clear_long_term_memories,
    trigger_memory_consolidation, memory_consolidation_status,
    get_memory_curator_config, save_memory_curator_config, check_memory_curator_config,
    get_model_providers, save_model_provider, delete_model_provider, activate_model_provider,
    get_server_effective_config
)
from core.api.config_sync_api import ConfigSyncAPI, create_config_sync_api_router
from core.api.onenet_health_api import handle_health_data_push, get_latest_health_data, get_health_history, sync_from_cloud
from core.api.medication_api import (
    handle_list_medications, handle_create_medication,
    handle_update_medication, handle_delete_medication)
from core.api.health_event_api import (
    handle_list_events, handle_create_event,
    handle_update_event, handle_delete_event)
from core.api.sync_api import handle_sync_members_to_console, handle_sync_members_from_console, handle_full_sync
from core.api.observation_api import (
    handle_observation_recent, handle_observation_feedback)
from core.api.dashboard_config_api import handle_save_dashboard_config, handle_get_dashboard_config, handle_save_dashboard_layout, handle_get_dashboard_layout, handle_get_all_dashboard_layouts, handle_dashboard_config_shortcut
from core.api.weather_api import setup_weather_api
from core.api.album_api import setup_album_api
from core.api.plugin_config_api import setup_plugin_config_api
from core.api.embed_api import setup_mac_embed_api_routes
from core.api.feishu_contacts_api import register_routes as register_feishu_contacts_routes
from core.api.search_api import register_routes as register_search_routes
from core.api.callpilot_api import register_routes as register_callpilot_routes
from core.api.notification_api import register_routes as register_notification_routes
from core.api.email_api import register_routes as register_email_routes
from core.api.emergency_api import register_routes as register_emergency_routes
# 长期记忆路由为商业版功能，社区版不提供（公开版已移除该模块）
try:  # pragma: no cover - 商业版才有该路由
    from core.api.ltm_api import register_LTM_routes
except Exception:  # noqa: BLE001
    register_LTM_routes = None
from core.api.weather_config_api import handle_get_weather_config, handle_save_weather_config
from core.api.weather_city_api import handle_city_lookup, handle_city_list
from core.api.weather_city_api import handle_city_lookup, handle_city_list, handle_lifestyle_indices
from core.api.house_api import HouseAPI
from core.api import place_api
from core.api.smart_home_api import (
    handle_smart_home_status,
    handle_smart_home_devices,
    handle_device_control
)
from core.utils.home_intent import handle_home_intent_logs
from core.api.camera_api import (
    handle_get_cameras,
    handle_add_camera,
    handle_update_camera,
    handle_delete_camera,
    handle_camera_stream,
    handle_camera_snapshot
)
from core.api.ai_audio_api import (
    get_ai_audio_devices,
    add_ai_audio_device,
    update_ai_audio_device,
    delete_ai_audio_device,
    test_ai_audio_device,
    get_ai_audio_test_records,
    get_ai_audio_rooms,
    get_channel_config,
    save_channel_config,
    batch_save_channel_config,
)
from core.api.console_api import (
    search_skills,
    install_skill,
    install_local_skill,
    get_room,
    update_room,
    get_stats,
    get_notifications,
    get_activity_logs,
    get_server_logs,
    generate_health_report,
    get_health_reports,
    delete_health_report,
    save_initial_measurement,
    get_initial_measurements,
    delete_initial_measurement,
    get_family_events,
    get_devices_status,
    asr_recognize,
    get_ha_config,
    save_ha_config,
    test_ha_connection,
    create_xiaozhi_routes,
    get_dialogue_history,
    get_conversation_summaries,
    delete_conversation_summaries,
    get_module_status_asr,
    get_module_status_tts,
    get_module_status_llm,
    get_ha_status,
    get_onenet_status,
    ragflow_search,
    ragflow_test_connection,
    ragflow_list_documents,
)
from core.api.music_config_api import (
    register_routes as register_music_config_routes
)
from core.api.skills_api import delete_skill
from core.api.news_api import handle_get_news, handle_refresh_news, handle_get_news_content
from .http_server_apis import get_health_data as get_health_status, clear_cache, restart_server
import json
import os
import re
import sqlite3

TAG = __name__


def _silent_wav_100ms() -> bytes:
    """构造 100ms 静音的 16kHz 单声道 wav（44 字节头 + 3200 字节零样本）。

    用于测试 ASR 端点连通性：容器合法、内容为零，服务商能解析就说明链路与鉴权都通。
    """
    import io
    import struct
    sample_rate, channels, bits, seconds = 16000, 1, 16, 0.1
    n = int(sample_rate * seconds) * channels
    data = b"\x00" * (n * bits // 8)
    buf = io.BytesIO()
    buf.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                         sample_rate * channels * bits // 8, channels * bits // 8, bits))
    buf.write(b"data" + struct.pack("<I", len(data)) + data)
    return buf.getvalue()


async def _classify_probe(resp, label: str):
    """把探测响应归类成人话，给主人明确结论而不是裸 HTTP 码。"""
    if resp.status == 200:
        return web.json_response({"success": True, "message": f"{label}接口连通，API Key 可用"})
    text = ""
    try:
        text = (await resp.text())[:200]
    except Exception:
        pass
    mapping = {
        401: "API Key 无效或已过期（401）",
        403: "无访问权限（403）——Key 有效但没这个模型的权限",
        404: "端点不存在（404）——检查 base_url 是否多了/少了 /v1",
        429: "触发限流（429）——稍后重试",
        400: "请求被拒（400）——模型名不对或该端点不支持这种调用",
    }
    msg = mapping.get(resp.status, f"连接失败 HTTP {resp.status}")
    if text:
        msg += f"｜响应：{text}"
    return web.json_response({"success": False, "error": msg}, status=200)


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        self.device_manager = DeviceManager(config)
        self.weather_api = setup_weather_api(config)
        self.album_api = setup_album_api(config)
        self.plugin_config_api = setup_plugin_config_api(config)
        self.house_api = HouseAPI(config)
        self._config_sync_handler = ConfigSyncAPI()
        self.templates_dir = os.path.join(os.getcwd(), "templates")
        # [2026-10-14] 浏览器安全来源（https 端口 + 自签证书）状态，供 server-effective 页展示
        self.https_started = False
        self.https_port = None
        self.tls_status = None
        # 启动时从 DB model_providers 加载激活模型，覆盖内存 config（兼容 yaml 回退）
        self._load_model_providers_from_db()

    def _load_model_providers_from_db(self):
        """DB model_providers 表优先；没记录的 role 保持 yaml 原值。
        role='text'      → 覆盖 config['LLM'][provider_name] + selected_module.LLM
        role='vllm'      → 覆盖 config['VLLM'][provider_name] + selected_module.VLLM
        role='welcome'   → 覆盖 config['security']['welcome_llm']
        """
        import json as _json
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        db_path = _Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        if not db_path.exists():
            return
        try:
            conn = _sqlite3.connect(str(db_path))
            conn.row_factory = _sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM model_providers WHERE is_active=1 ORDER BY role").fetchall()
            finally:
                conn.close()
            for r in rows:
                role = r["role"]
                provider = r["provider_name"]
                base_url = r["base_url"] or ""
                api_key = r["api_key"] or ""
                model_name = r["model_name"] or ""
                try:
                    extra = _json.loads(r["extra_config"]) if r["extra_config"] else {}
                except Exception:
                    extra = {}
                if role == "text":
                    # merge 保留 yaml 已有参数（max_tokens/temperature 等），DB 连接字段优先
                    existing = self.config.setdefault("LLM", {}).get(provider) or {}
                    self.config["LLM"][provider] = {
                        **existing,
                        "type": existing.get("type") or "openai",
                        "api_key": api_key or existing.get("api_key", ""),
                        "base_url": base_url or existing.get("base_url", ""),
                        "url": base_url or existing.get("url", ""),
                        "model_name": model_name or existing.get("model_name", ""),
                        **extra,
                    }
                    self.config.setdefault("selected_module", {})["LLM"] = provider
                elif role == "vllm":
                    existing = self.config.setdefault("VLLM", {}).get(provider) or {}
                    self.config["VLLM"][provider] = {
                        **existing,
                        "type": existing.get("type") or "openai",
                        "api_key": api_key or existing.get("api_key", ""),
                        "url": base_url or existing.get("url", ""),
                        "base_url": base_url or existing.get("base_url", ""),
                        "model_name": model_name or existing.get("model_name", ""),
                    }
                    self.config.setdefault("selected_module", {})["VLLM"] = provider
                elif role == "welcome":
                    self.config.setdefault("security", {})["welcome_llm"] = {
                        "_provider": provider, "api_key": api_key,
                        "base_url": base_url, "model_name": model_name,
                    }
                self.logger.bind(tag="model_providers").info(
                    f"[模型提供商DB] role={role} provider={provider} model={model_name} active=1 已加载")
        except Exception as e:
            self.logger.bind(tag="model_providers").warning(f"[模型提供商DB] 加载失败，保持 yaml 默认: {e}")

    def _console_web_dir(self):
        """前端目录（唯一位置 console/V15_Current/web），相对项目根推导，兼容双机部署"""
        from pathlib import Path
        # http_server.py 位于 <项目根>/server/homebrain-server/core/ 下，向上 4 级到项目根
        project_root = Path(os.path.abspath(__file__)).parent.parent.parent.parent
        return project_root / "console" / "V15_Current" / "web"

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取 websocket 地址

        Args:
            local_ip: 本地 IP 地址
            port: 端口号

        Returns:
            str: websocket 地址
        """
        websocket_config = self.config.get("websocket", {})
        if websocket_config:
            # 从配置中获取 websocket 地址
            ws_ip = websocket_config.get("ip", local_ip)
            ws_port = websocket_config.get("port", port)
            return f"ws://{ws_ip}:{ws_port}/homebrain/v1/"
        else:
            return f"ws://{local_ip}:{port}/homebrain/v1/"

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                # 请求体上限 64MB：成员照片/声纹录音为 base64，默认 1MB 会导致
                # 添加成员被 413 拒绝且前端静默吞错，表现为"录入刷新后丢失"（主人 2026-09-09）
                app = web.Application(client_max_size=64 * 1024 * 1024)
                # J3：供 bundle reload API 使用
                app["_homebrain_server"] = getattr(self, "server", None)
                app["_homebrain_config"] = self.config

                if not read_config_from_api:
                    # 如果没有开启智控台，只是单模块运行，就需要再添加简单 OTA 接口，用于下发 websocket 接口
                    app.add_routes(
                        [
                            web.get("/homebrain/ota/", self.ota_handler.handle_get),
                            web.post("/homebrain/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/homebrain/ota/", self.ota_handler.handle_options
                            ),
                            # 下载接口，仅提供 data/bin/*.bin 下载
                            web.get(
                                "/homebrain/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/homebrain/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                # 添加路由
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                        # 设备管理前端页面
                        web.get("/admin/devices", self.handle_device_manager_page),
                        # HomeBrain 控制台页面
                        web.get("/mail", self.handle_mail_rules_page),
                        web.get("/mail/", self.handle_mail_rules_page),
                        web.get("/mail/index.html", self.handle_mail_rules_page),
                        web.get("/console", self.handle_homebrain_console_page),
                        web.get("/console/", self.handle_homebrain_console_page),
                        web.get("/admin/", self.handle_homebrain_console_page),
                        web.get("/admin/index.html", self.handle_homebrain_console_page),
                        # 小智设备管理控制台 (SPA)
                        web.get("/console/V15_Current/web/admin/xiaozhi-console/", self.handle_xiaozhi_console_page),
                        web.get("/console/V15_Current/web/admin/xiaozhi-console/index.html", self.handle_xiaozhi_console_page),
                        web.get("/console/V15_Current/web/admin/xiaozhi-console/{filename:.*}", self.handle_xiaozhi_console_static),
                        # 音乐配置测试页面
                        web.get("/admin/test_music_config.html", self.handle_music_config_test_page),
                        web.get("/admin/intent_log.html", self.handle_intent_log_page),
                        # HomeBrain 家庭看板页面
                        web.get("/dashboard", self.handle_homebrain_dashboard_page),
                        web.get("/dashboard/", self.handle_homebrain_dashboard_page),
                        web.get("/dashboard.html", self.handle_homebrain_dashboard_page),
                        web.get("/terminal", self.handle_terminal_page),
                        web.get("/terminal/", self.handle_terminal_page),
                        web.get("/terminal/vendor/{filename:.*}", self.handle_terminal_static),
                        web.get("/web/data/{filename:.*}", self.handle_web_data_static),
                        # 前端共享模块（pcm-player.js 等，被 /terminal 与 /led 同时加载）
                        web.get("/web/shared/{filename:.*}", self.handle_web_shared_static),
                        # 菜谱图片（本地菜谱库抓取的成品图/步骤图）
                        web.get("/recipe-images/{filename:.*}", self.handle_recipe_image),
                        # 前端版本探测：看板页用它检测"代码已更新"并自动刷新
                        web.get("/api/ui-version", self.handle_ui_version),
                        # LED 大屏专用页面（1920x960 独立界面，待机水族馆 + Home）
                        web.get("/led", self.handle_led_page),
                        web.get("/led/", self.handle_led_page),
                        web.get("/led/media/list", self.handle_led_media_list),
                        web.get("/led/media/{filename:.*}", self.handle_led_media),
                        web.get("/led/static/{filename:.*}", self.handle_led_static),
                        web.get("/led/{name}", self.handle_led_variant),
                        # 项目文档在线查看（markdown 渲染为 HTML）
                        web.get("/docs/{filename:.*}", self.handle_docs_static),
                        # 设备管理 API
                        web.get("/api/devices", self.device_manager.handle_list),
                        web.get("/api/devices/discover", self.device_manager.handle_discover),
                        web.get("/api/devices/{device_id}", self.device_manager.handle_get),
                        web.post("/api/devices/register", self.device_manager.handle_register),
                        web.put("/api/devices/{device_id}/config", self.device_manager.handle_update_config),
                        web.put("/api/devices/{device_id}/room", self.device_manager.handle_assign_room),
                        web.delete("/api/devices/{device_id}", self.device_manager.handle_delete),
                        web.get("/api/devices/{device_id}/history", self.device_manager.handle_config_history),
                        # 固件管理 API
                        web.get("/api/firmware", self.device_manager.handle_firmware_list),
                        web.post("/api/firmware/upload", self.device_manager.handle_firmware_upload),
                        web.delete("/api/firmware/{filename}", self.device_manager.handle_firmware_delete),
                        # 控制台 API
                        web.get("/admin/api/config", self.handle_get_config),
                        web.post("/admin/api/config", self.handle_save_config),
                        web.get("/admin/api/log", self.handle_get_log),
                        web.post("/admin/api/tts", self.handle_save_tts),
                        # 音色试听 API
                        web.post("/api/voice/tts/preview", self.handle_tts_preview),
                        web.get("/api/tts/voices", self.handle_get_tts_voices),
                        # 健康数据 API
                        # 系统维护 API
                        web.post("/api/system/clear-cache", self.handle_clear_cache),
                        web.post("/api/system/restart", self.handle_restart_server),
                        web.get("/api/server/info", self.handle_server_info),
                        # 健康检查（控制台“测试服务器连接”按钮 + 运维探活）
                        web.get("/api/health", self.handle_health_check),
                        # 数字人中间窗口配置（阶段1）
                        web.get("/api/center-window", get_center_window),
                        web.post("/api/center-window", save_center_window),
                        web.get("/api/center-window/avatars", list_avatars),
                        web.get("/api/center-window/avatars/{avatar_id}/preview", get_avatar_preview),
                        web.get("/api/center-window/backgrounds", list_backgrounds),
                        web.post("/api/center-window/backgrounds", upload_background),
                        web.get("/api/center-window/backgrounds/{bg_id}", get_background_image),
                        # Plugins管理 API
                        web.get("/api/plugins/list", list_plugins),
                        web.post("/api/plugins/toggle", toggle_plugin),
                        web.post("/api/plugins/reload", reload_bundles),
                        web.get("/api/plugins/test_mcp", test_mcp),
                        web.get("/api/plugins/mcp/status", mcp_status),
                        web.get("/api/voice-settings", get_voice_settings),
                        web.post("/api/voice-settings", save_voice_settings),
                        web.get("/api/available-tools", get_available_tools),
                        web.get("/api/routing-models", get_routing_models),
                        web.post("/api/routing-models", save_routing_models),
                        # 家庭成员 API
                        web.get("/api/family/members", list_family_members),
                        web.post("/api/family/members", add_family_member),
                        web.put("/api/family/members/{member_id}", update_family_member),
                        web.delete("/api/family/members/{member_id}", delete_family_member),
                        web.post("/api/family/members/{member_id}/bind-device", bind_health_device),
                        web.delete("/api/family/members/{member_id}/unbind-device", unbind_health_device),
                        web.get("/api/family/members/{member_id}/schedules", get_member_schedules),
                        web.post("/api/family/members/{member_id}/schedules", add_member_schedule),
                        web.delete("/api/family/members/{member_id}/schedules/{schedule_id}", delete_member_schedule),
                        web.get("/api/family/members/{member_id}/health-records", get_member_health_records),
                        web.put("/api/family/members/{member_id}/health-status", update_member_health_status),
                        # [2026-10-14] 真正删除声纹（原「删除声纹」只清前端字段，库里向量还在）
                        web.delete("/api/family/members/{member_id}/voiceprint", delete_member_voiceprint),
                        # 兼容旧API路径
                        web.get("/api/family-members", list_family_members),
                        web.post("/api/family-members", add_family_member),
                        # 声纹录制打分 API（后台「录制声纹」两遍一致性打分，2026-10-11）
                        *create_voiceprint_eval_routes(),
                        # 控制台页面状态 API（localStorage 的服务器版，2026-10-14）
                        *create_console_state_routes(),
                        # 人脸录入 API（家庭成员 → Frigate 原生人脸识别）
                        *create_face_api_routes(),
                        # 车牌绑定 API（车牌 ↔ 车主，支撑车牌三段式反馈）
                        *create_plate_api_routes(),
                        # 欢迎语 LLM 选择 API（后台下拉框用）
                        *create_welcome_llm_routes(),
                        # 数字人大脑代理（阶段2：OpenAI 兼容流式端点）
                        *create_llm_proxy_routes(),
                        # 调试模式 API（按模块勾选绕过时间/冷却/次数限制）
                        web.get("/api/debug/mode", get_debug_mode),
                        web.post("/api/debug/mode", set_debug_mode),
                        # 真实健康数据 API（已废弃，保留兼容性但返回重定向信息）
                        web.get("/api/health-data/{member_name}", get_member_health_data),      # 返回410 Gone
                        web.get("/api/health-data/all", get_all_health_data),                    # 返回410 Gone
                        web.get("/api/health-data/latest", get_latest_vitals_by_member),         # 重定向到新API
                        # 健康数据 API（推荐使用）
                        web.get("/api/health/data", get_health_records),
                        web.post("/api/health/data", add_health_data),
                        web.get("/api/health/weight", get_latest_weight),
                        web.get("/api/health/summary", get_health_summary),
                        web.get("/api/health/devices", get_health_devices),
                        # BLE 健康设备实时数据
                        web.get("/api/health/ble", get_ble_health_data),
                        web.post("/api/health/report", generate_health_report),
                        web.get("/api/health/reports", get_health_reports),
                        web.delete("/api/health/report", delete_health_report),
                        web.post("/api/health/initial-measurement", save_initial_measurement),
                        web.get("/api/medications", handle_list_medications),
                        web.post("/api/medications", handle_create_medication),
                        web.put("/api/medications/{id}", handle_update_medication),
                        web.delete("/api/medications/{id}", handle_delete_medication),
                        # 运行观察与反馈（2026-10-18 P9）：最近对讲列表 + 点赞/点踩/备注
                        web.get("/api/observation/recent", handle_observation_recent),
                        web.post("/api/observation/feedback", handle_observation_feedback),
                        web.get("/api/health-events", handle_list_events),
                        web.post("/api/health-events", handle_create_event),
                        web.put("/api/health-events/{id}", handle_update_event),
                        web.delete("/api/health-events/{id}", handle_delete_event),
                        web.get("/api/initial-measurements", get_initial_measurements),
                        web.delete("/api/initial-measurement", delete_initial_measurement),
                        # 配置持久化 API（旧版 - 向后兼容）
                        web.get("/api/config", get_config),
                        web.post("/api/config", save_config),
                        web.get("/api/config/dashboard", get_dashboard_config),
                        web.post("/api/config/dashboard", save_dashboard_config),
                        web.get("/api/config/all", get_all_configs),
                        # 统一配置 API（新版 - 商用级）
                        web.post("/api/config/save", config_save),
                        web.get("/api/config/load", config_load),
                        web.get("/api/config/load-all", config_load_all),
                        web.post("/api/config/save-batch", config_save_batch),
                        web.post("/api/config/delete", config_delete),
                        web.get("/api/config/load-all-categories", config_load_all_categories),
                        # 服务器配置只读镜像（控制台「服务器设置」页唯一数据源，2026-10-06）
                        web.get("/api/config/server-effective", get_server_effective_config),
                        # 已保存模型列表（后端持久化，替代浏览器 localStorage）
                        web.get("/api/saved-models", get_saved_models),
                        web.post("/api/saved-models", save_saved_models),
                        # 统一模型提供商管理（text/vllm/welcome/embedding 四类）
                        web.get("/api/model-providers", get_model_providers),
                        web.post("/api/model-providers", save_model_provider),
                        web.delete("/api/model-providers", delete_model_provider),
                        web.post("/api/model-providers/activate", activate_model_provider),
                        web.get("/api/config/memory", get_memory_config),
                        web.post("/api/config/update-memory", update_memory_config),
                        # 长期记忆（ltm 向量库）列表/删除/清空（2026-10-13 WP-6 / D6 权限）
                        web.get("/api/config/memory/entries", list_long_term_memories),
                        web.post("/api/config/memory/entries/delete", delete_long_term_memory),
                        web.post("/api/config/memory/entries/clear", clear_long_term_memories),
                        web.post("/api/config/memory/consolidate", trigger_memory_consolidation),
                        web.get("/api/config/memory/consolidate/status", memory_consolidation_status),
                        web.get("/api/config/memory/curator", get_memory_curator_config),
                        web.post("/api/config/memory/curator", save_memory_curator_config),
                        web.post("/api/config/memory/curator/test", check_memory_curator_config),
                        # OneNET 云平台健康数据 API
                        web.post("/api/onenet/push", handle_health_data_push),
                        web.get("/api/onenet/latest", get_latest_health_data),
                        web.get("/api/onenet/history", get_health_history),
                        web.get("/api/onenet/sync", sync_from_cloud),
                        # 数据同步 API
                        web.get("/api/sync/members/to-console", handle_sync_members_to_console),
                        web.post("/api/sync/members/from-console", handle_sync_members_from_console),
                        web.post("/api/sync/full", handle_full_sync),
                        # 看板配置 API
                        web.post("/api/dashboard/config", handle_save_dashboard_config),
                        web.get("/api/dashboard/config", handle_get_dashboard_config),
                        web.get("/api/dashboard-config", handle_dashboard_config_shortcut),
                        web.post("/api/dashboard-config", handle_dashboard_config_shortcut),
                        web.post("/api/dashboard/layout", handle_save_dashboard_layout),
                        web.get("/api/dashboard/layout", handle_get_dashboard_layout),
                        web.get("/api/dashboard/layouts", handle_get_all_dashboard_layouts),
                        # 天气 API
                        web.get("/api/weather/current", self.weather_api.get_current_weather),
                        # 相册 API
                        web.get("/api/album/photos", self.album_api.get_album_photos),
                        web.get("/api/album/photo", self.album_api.get_photo_by_path),
                        web.post("/api/album/upload", self.album_api.upload_photos),
                        web.post("/api/album/delete", self.album_api.delete_photo),
                        web.get("/api/weather/config", handle_get_weather_config),
                        web.post("/api/weather/config", handle_save_weather_config),
                        web.get("/api/weather/city/lookup", handle_city_lookup),
                        web.get("/api/weather/city/list", handle_city_list),
                        web.get("/api/weather/indices", handle_lifestyle_indices),
                        web.get("/api/album/photo", self.album_api.get_photo_by_path),
                        # Mac 应用嵌入 API
                        *setup_mac_embed_api_routes(),
                        # 房屋状态管理 API
                        web.get("/api/houses", self.house_api.handle_get_houses),
                        web.post("/api/houses", self.house_api.handle_create_house),
                        web.put("/api/houses/{house_id}", self.house_api.handle_update_house),
                        web.delete("/api/houses/{house_id}", self.house_api.handle_delete_house),
                        web.get("/api/houses/{house_id}/floors", self.house_api.handle_get_floors),
                        web.post("/api/houses/{house_id}/floors", self.house_api.handle_create_floor),
                        web.get("/api/floors/{floor_id}", self.house_api.handle_get_floor),
                        web.put("/api/floors/{floor_id}", self.house_api.handle_update_floor),
                        web.delete("/api/floors/{floor_id}", self.house_api.handle_delete_floor),
                        web.get("/api/floors/{floor_id}/rooms", self.house_api.handle_get_rooms),
                        web.post("/api/floors/{floor_id}/rooms", self.house_api.handle_create_room),
                        web.get("/api/rooms/{room_id}", self.house_api.handle_get_room),
                        web.put("/api/rooms/{room_id}", self.house_api.handle_update_room),
                        web.delete("/api/rooms/{room_id}", self.house_api.handle_delete_room),
                        web.get("/api/rooms/all", self.house_api.handle_get_all_rooms),
                        # 地点簿 API（2026-10-05）：家庭成员的常去地点
                        # 「家」由 houses 表迁移而来（is_primary=1），不可在这里重复添加/删除
                        web.get("/api/places", place_api.handle_get_places),
                        web.post("/api/places", place_api.handle_create_place),
                        web.put("/api/places/{place_id}", place_api.handle_update_place),
                        web.delete("/api/places/{place_id}", place_api.handle_delete_place),
                        web.post("/api/places/sync-home", place_api.handle_sync_home_place),
                        # 插件配置 API
                        web.get("/api/plugins/config", self.plugin_config_api.get_plugin_config),
                        web.post("/api/plugins/config", self.plugin_config_api.save_plugin_config),
                        # 智能家居 API - 看板和控制台同步用
                        web.get("/api/smart-home/status", handle_smart_home_status),
                        web.get("/api/smart-home/devices", handle_smart_home_devices),
                        web.post("/api/smart-home/control", handle_device_control),
                        # 直通判定日志（P3 后台日志页）
                        web.get("/api/home-intent/logs", handle_home_intent_logs),
                        # 摄像头 API - 服务器端持久化存储
                        web.get("/api/cameras", handle_get_cameras),
                        web.post("/api/cameras", handle_add_camera),
                        web.put("/api/cameras/{id}", handle_update_camera),
                        web.delete("/api/cameras/{id}", handle_delete_camera),
                        # 摄像头流代理 API
                        web.get("/api/camera/stream", handle_camera_stream),
                        web.get("/api/camera/snapshot", handle_camera_snapshot),
                        # AI 空间麦 API
                        web.get("/api/ai-audio/devices", get_ai_audio_devices),
                        web.post("/api/ai-audio/devices", add_ai_audio_device),
                        web.put("/api/ai-audio/devices/{device_id}", update_ai_audio_device),
                        web.delete("/api/ai-audio/devices/{device_id}", delete_ai_audio_device),
                        web.post("/api/ai-audio/devices/{device_id}/test", test_ai_audio_device),
                        web.get("/api/ai-audio/devices/{device_id}/tests", get_ai_audio_test_records),
                        web.get("/api/ai-audio/rooms", get_ai_audio_rooms),
                        # 通道配置 API
                        web.get("/api/ai-audio/channels", get_channel_config),
                        web.post("/api/ai-audio/channels", save_channel_config),
                        web.post("/api/ai-audio/channels/batch", batch_save_channel_config),
                        # 补充控制台 API - 前端需要的路由
                        web.get("/api/skills/search", search_skills),
                        web.post("/api/skills/install", install_skill),
                        web.post("/api/skills/install-local", install_local_skill),
                        web.delete("/api/skills/{skill_id}", delete_skill),
                        web.post("/api/asr/recognize", asr_recognize),
                        web.post("/api/chat", self.handle_chat),
                        web.post("/api/llm/test", self.handle_llm_test),
                        web.get("/api/rooms/{id}", get_room),
                        web.put("/api/rooms/{id}", update_room),
                        web.get("/api/stats", get_stats),
                        web.get("/api/devices/status", get_devices_status),
                        web.get("/api/notifications", get_notifications),
                        web.get("/api/activity-logs", get_activity_logs),
                        web.get("/api/server-logs", get_server_logs),
                        web.get("/api/family/events", get_family_events),
                        # 对话历史 API
                        web.get("/api/dialogue/history", get_dialogue_history),
                        web.get("/api/conversation/summaries", get_conversation_summaries),
                        web.post("/api/conversation/summaries/delete", delete_conversation_summaries),
                        # 模块状态 API
                        web.get("/api/config/asr", get_module_status_asr),
                        web.get("/api/config/tts", get_module_status_tts),
                        web.get("/api/config/llm", get_module_status_llm),
                        # 云端/本地引擎切换 API
                        web.get("/api/config/providers", self.handle_get_providers),
                        web.get("/api/config/provider-fields", self.handle_get_provider_fields),
                        web.post("/api/config/provider-fields", self.handle_save_provider_fields),
                        # 早安简报手动触发（测试用，2026-09-11）
                        web.post("/api/morning-digest/test", self.handle_morning_digest_test),
                        web.post("/api/config/switch-provider", self.handle_switch_provider),
                        # Qwen3-TTS 设置页（2026-10-07）：音色/语言/情绪指令 + 克隆音色 + 音色设计
                        web.get("/api/tts/qwen3/settings", self.handle_qwen3_settings),
                        web.post("/api/tts/qwen3/clone", self.handle_qwen3_clone),
                        web.post("/api/tts/qwen3/clone-delete", self.handle_qwen3_clone_delete),
                        web.post("/api/tts/qwen3/design", self.handle_qwen3_design),
                        web.post("/api/config/switch-mode", self.handle_switch_mode),
                        # 模型方案状态：各环节部署属性+联动配置（2026-09-28 双栈方案制）
                        web.get("/api/config/model-stack", self.handle_model_stack),
                        # 自定义视觉模型 API（2026-09-12）
                        web.post("/api/config/test-custom-vllm", self.handle_test_custom_vllm),
                        web.post("/api/config/save-custom-vllm", self.handle_save_custom_vllm),
                        web.get("/api/config/custom-vllm-models", self.handle_list_custom_vllm),
                        web.post("/api/config/delete-custom-vllm", self.handle_delete_custom_vllm),
                        # 自定义ASR模型 API
                        web.post("/api/config/save-custom-asr", self.handle_save_custom_asr),
                        web.post("/api/config/test-custom-asr", self.handle_test_custom_asr),
                        web.get("/api/config/custom-asr-models", self.handle_list_custom_asr),
                        web.post("/api/config/delete-custom-asr", self.handle_delete_custom_asr),
                        # 自定义TTS模型 API
                        web.post("/api/config/save-custom-tts", self.handle_save_custom_tts),
                        web.post("/api/config/test-custom-tts", self.handle_test_custom_tts),
                        web.get("/api/config/custom-tts-models", self.handle_list_custom_tts),
                        web.post("/api/config/delete-custom-tts", self.handle_delete_custom_tts),
                        web.get("/api/home-assistant/status", get_ha_status),
                        web.get("/api/onenet/status", get_onenet_status),
                        # 新闻 API
                        web.get("/api/news", handle_get_news),
                        web.post("/api/news/refresh", handle_refresh_news),
                        web.get("/api/news/content", handle_get_news_content),
                        # 快递 API
                        web.get("/api/express/config", get_express_config),
                        web.post("/api/express/config", update_express_config),
                        web.get("/api/express/members/{member_id}", get_express_by_member),
                        web.get("/api/express/by-member", get_all_express_with_members),
                        web.post("/api/express/packages", add_express_package),
                        web.put("/api/family/members/{member_id}/phone", update_member_phone),
                        web.get("/api/express/phone/{phone}", query_express_by_phone),
                        # Home Assistant 配置 API
                        web.get("/api/ha-config", self.handle_get_ha_config),
                        web.post("/api/ha-config", self.handle_save_ha_config),
                        # AI 管家配置保存（对话摘要等开关，写入 ai_butler_config 表）
                        web.post("/api/ai-butler-config", self.handle_save_ai_butler_config),
                        # 手动记忆条目（控制台"记忆系统"页的键值记忆库）
                        web.get("/api/memory", self.handle_memory_list),
                        web.post("/api/memory", self.handle_memory_save),
                        web.delete("/api/memory/{memory_id}", self.handle_memory_delete),
                        # 星空记忆星系（Web 终端，v1 只读聚合：家人+记事+日程+健康）
                        web.get("/api/memory/galaxy", handle_galaxy),
                        # 家庭待办 API（2026-09-10 新增）
                        web.get("/api/todos", list_todos),
                        web.post("/api/todos", add_todo),
                        web.put("/api/todos/{todo_id}/toggle", toggle_todo),
                        web.delete("/api/todos/{todo_id}", delete_todo),
                        web.get("/api/dashboard/settings", get_dashboard_settings),
                        web.post("/api/dashboard/settings", save_dashboard_settings),
                        # 知识库搜索（RAGFlow 代理：配置在 config.yaml plugins.search_from_ragflow）
                        web.get("/api/ragflow/search", self.handle_ragflow_search),
                        web.post("/api/ha-config/test", self.handle_test_ha_connection),
                        # 音乐配置 API
                        web.get("/api/music/config", self.handle_get_music_config),
                        web.post("/api/music/config/save", self.handle_save_music_config),
                        web.post("/api/music/scan_entities", self.handle_scan_ha_entities),
                        web.post("/api/music/test_connection", self.handle_test_ha_connection_music),
                        web.get("/api/music/rooms", self.handle_get_music_rooms),
                        web.post("/api/music/rooms", self.handle_add_music_room),
                        web.put("/api/music/rooms/{id}", self.handle_update_music_room),
                        web.delete("/api/music/rooms/{id}", self.handle_delete_music_room),

                    ]
                )

                # 飞书通讯录管理 API（2026-09-12）
                register_feishu_contacts_routes(app, getattr(self, "server", None))
                register_search_routes(app)
                register_callpilot_routes(app)
                register_notification_routes(app)
                register_email_routes(app)   # 邮箱管家（2026-10-21）
                register_emergency_routes(app)
                if register_LTM_routes is not None:  # 商业版功能
                    register_LTM_routes(app)

                # RAGflow 代理 API（搜索走 WebSocketServer.handle_ragflow_search，此处只注册测试和文档列表）
                app.router.add_get('/api/ragflow/test', ragflow_test_connection)
                app.router.add_get('/api/ragflow/datasets/{dataset_id}/documents', ragflow_list_documents)

                # 添加 /xiaozhi/* 路由（manager-web 兼容层）
                app.add_routes(create_xiaozhi_routes())
                
                # 添加配置同步API路由
                app.router.add_routes(create_config_sync_api_router(web))

                # [2026-10-14] 浏览器安全来源用：下载自签证书（手机/电脑装一次就不再警告，
                # 不装也能点「继续前往」——https 本身就够麦克风用）
                app.router.add_get('/api/tls/cert', self.handle_download_tls_cert)

                # 添加音乐配置API路由
                register_music_config_routes(app)

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                self.logger.bind(tag=TAG).info(f"HTTP 服务器启动在 http://{host}:{port}")
                self.logger.bind(tag=TAG).info(f"设备管理页面：http://{host}:{port}/admin/devices")

                # [2026-10-14] 再起一个 https 端口给浏览器（同一 app，只换 scheme）：
                # 浏览器只在安全来源（https / localhost）才给麦克风与摄像头，
                # 用户用家里 IP 或域名打开管理台时，声纹录入/拍照在 http 下根本不可用。
                await self._start_https_site(runner, host, port, server_config)

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP 服务器启动失败：{e}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈：{traceback.format_exc()}")
            # 端口被占用时直接退出进程，避免残留僵尸进程
            import sys
            sys.exit(1)

    async def _start_https_site(self, runner, host, http_port, server_config):
        """[2026-10-14] 给浏览器起一个 https 端口（自签证书）。

        为什么：浏览器只在「安全来源」（https，或 localhost/127.0.0.1）才提供
        navigator.mediaDevices。用户在自己的手机/电脑上用「家里 IP 或域名 + http」
        打开管理台时，声纹录入与摄像头拍照完全不可用（点了没反应）。
        设备侧的 http:8003 与 ws:9800 一个字不动 —— ESP32 上做 TLS 代价太大。

        证书缺失/损坏、端口被占用都只记日志，**绝不影响主服务**。
        """
        from core.utils import tls_certs

        try:
            if not server_config.get("https_enabled", True):
                self.logger.bind(tag=TAG).info("https 端口已按配置关闭（server.https_enabled=false）")
                return
            https_port = int(server_config.get("https_port", tls_certs.DEFAULT_HTTPS_PORT))
            if https_port <= 0 or https_port == http_port:
                return

            generated = tls_certs.ensure_self_signed()
            self.tls_status = generated
            if generated.get("status") == "failed":
                self.logger.bind(tag=TAG).error(f"自签证书生成失败，跳过 https：{generated.get('error')}")
                return

            ssl_context = tls_certs.ssl_context_for()
            if ssl_context is None:
                self.logger.bind(tag=TAG).error("证书读取失败，跳过 https 端口（http/设备链路不受影响）")
                return

            https_site = web.TCPSite(runner, host, https_port, ssl_context=ssl_context)
            await https_site.start()
            self.https_started = True
            self.https_port = https_port
            self.logger.bind(tag=TAG).info(
                f"HTTPS 服务器启动在 https://{host}:{https_port}（自签证书，证书状态：{generated.get('status')}）"
            )
        except Exception as e:
            self.https_started = False
            self.https_port = None
            self.logger.bind(tag=TAG).error(f"https 端口启动失败（不影响 http/设备链路）：{e}")

    async def handle_download_tls_cert(self, request):
        """GET /api/tls/cert —— 下载自签证书给用户在自己的手机/电脑上安装。

        装成受信任的根证书后浏览器不再红警告；不装、点一次「继续前往」也能用麦克风。
        """
        from core.utils import tls_certs

        cert_path, _key_path = tls_certs.default_paths()
        if not cert_path.exists():
            return web.json_response(
                {'success': False, 'message': '证书还不存在（https 端口未启用或生成失败）'}, status=404
            )
        return web.Response(
            body=cert_path.read_bytes(),
            content_type='application/x-x509-ca-cert',
            headers={'Content-Disposition': 'attachment; filename="homebrain.crt"'},
        )

    async def handle_device_manager_page(self, request):
        """处理设备管理前端页面"""
        try:
            template_path = os.path.join(self.templates_dir, "device_manager.html")
            if not os.path.exists(template_path):
                return web.Response(text="Template not found", status=404)
            
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            return web.Response(text=content, content_type="text/html")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载设备管理页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_mail_rules_page(self, request):
        """邮箱规则页（主人逐条定「留 / 清」规则）"""
        try:
            page_path = str(self._console_web_dir() / "mail" / "index.html")
            if not os.path.exists(page_path):
                return web.Response(text="Mail rules page not found", status=404)
            with open(page_path, "r", encoding="utf-8") as f:
                content = f.read()
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(text=content, content_type="text/html", headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载邮箱规则页失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_homebrain_console_page(self, request):
        """处理 HomeBrain 控制台页面"""
        try:
            # V15 控制台路径（相对项目根目录推导，兼容双机部署）
            console_path = str(self._console_web_dir() / "admin" / "index.html")
            if not os.path.exists(console_path):
                self.logger.bind(tag=TAG).error(f"控制台文件不存在：{console_path}")
                return web.Response(text="Console not found", status=404)

            with open(console_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 2026-09-13：禁用浏览器缓存，保证每次刷新都拿到最新 HTML
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(text=content, content_type="text/html", headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 HomeBrain 控制台页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_terminal_page(self, request):
        """HomeBrain Web 语音终端（MacBook / iPad 第二、三交互终端）"""
        try:
            terminal_path = str(self._console_web_dir() / "terminal" / "index.html")
            if not os.path.exists(terminal_path):
                self.logger.bind(tag=TAG).error(f"Web 终端文件不存在：{terminal_path}")
                return web.Response(text="Terminal not found", status=404)
            with open(terminal_path, "r", encoding="utf-8") as f:
                content = f.read()
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(text=content, content_type="text/html", headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 Web 终端页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_xiaozhi_console_page(self, request):
        """处理小智设备管理控制台首页 (SPA)"""
        try:
            console_path = str(self._console_web_dir() / "admin" / "xiaozhi-console" / "index.html")
            if not os.path.exists(console_path):
                self.logger.bind(tag=TAG).error(f"小智控制台文件不存在：{console_path}")
                return web.Response(text="Console not found", status=404)

            with open(console_path, "r", encoding="utf-8") as f:
                content = f.read()

            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(text=content, content_type="text/html", headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载小智控制台页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_terminal_static(self, request):
        """处理 Web 终端静态子资源（face-api 等 vendor 文件）"""
        try:
            filename = request.match_info.get('filename', '')
            base_dir = (self._console_web_dir() / "terminal" / "vendor").resolve()
            static_path = (base_dir / filename).resolve()
            # 防路径穿越：resolve 后必须仍在 vendor 目录内
            if not str(static_path).startswith(str(base_dir)) or not static_path.exists():
                return web.Response(text="File not found", status=404)

            content_types = {
                '.js': 'application/javascript',
                '.css': 'text/css',
                '.html': 'text/html',
                '.json': 'application/json',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.svg': 'image/svg+xml',
                '.ico': 'image/x-icon',
                '.bin': 'application/octet-stream',
            }
            ext = static_path.suffix.lower()
            content_type = content_types.get(ext, 'application/octet-stream')

            with open(static_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type=content_type)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载终端静态文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_led_page(self, request):
        """LED 大屏入口页 → 302 跳到最终版 design-v3

        [2026-10-04] led/index.html 等过程版已按主人要求删除，LED 唯一最终版是
        design-v3.html。这里保留 /led 与 /led/ 两个入口（旧链接、书签仍可用），
        直接重定向而不是返回 404。"""
        try:
            raise web.HTTPFound("/led/design-v3")
        except web.HTTPException as e:
            return e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LED 入口跳转失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_led_media(self, request):
        """LED 待机水族馆视频轮播静态服务（tmp/led_aquarium/，FileResponse 支持 Range 拖动）"""
        try:
            filename = request.match_info.get('filename', '')
            from pathlib import Path
            # http_server.py 在 <项目根>/server/homebrain-server/core/ 下，向上 4 级到项目根
            project_root = Path(os.path.abspath(__file__)).parent.parent.parent.parent
            base_dir = (project_root / "tmp" / "led_aquarium").resolve()
            media_path = (base_dir / filename).resolve()
            # 防路径穿越：resolve 后必须仍在素材目录内
            if not str(media_path).startswith(str(base_dir)) or not media_path.exists():
                return web.Response(text="File not found", status=404)
            if not media_path.is_file():
                return web.Response(text="File not found", status=404)
            return web.FileResponse(path=media_path)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 LED 媒体文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_led_media_list(self, request):
        """LED 待机素材清单（目录下全部视频文件名，供前端轮换播放）"""
        try:
            from pathlib import Path
            project_root = Path(os.path.abspath(__file__)).parent.parent.parent.parent
            base_dir = (project_root / "tmp" / "led_aquarium").resolve()
            if not base_dir.exists():
                return web.json_response({"success": True, "files": []})
            video_exts = {".mp4", ".webm", ".mov", ".m4v"}
            files = sorted(
                p.name for p in base_dir.iterdir()
                if p.is_file() and p.suffix.lower() in video_exts
            )
            return web.json_response({"success": True, "files": files})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取 LED 素材清单失败：{e}")
            return web.json_response({"success": False, "files": [], "error": str(e)})

    async def handle_led_variant(self, request):
        """LED 方向变体页面（/led/d1 … /led/d4，读 console/.../led/<name>.html）"""
        try:
            name = request.match_info.get('name', '')
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', name):
                return web.Response(text="Not found", status=404)
            variant_path = str(self._console_web_dir() / "led" / f"{name}.html")
            if not os.path.exists(variant_path):
                return web.Response(text="LED variant not found", status=404)
            with open(variant_path, "r", encoding="utf-8") as f:
                content = f.read()
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(text=content, content_type="text/html", headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 LED 变体页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_led_static(self, request):
        """LED 变体页共享静态文件（core.js 等，防路径穿越）

        [2026-10-03] 支持视频：数字人视频等媒体文件放到 web/led/media/ 下，
        走 /led/static/media/xxx.mp4 访问。视频用 FileResponse 走流式 + Range，
        否则 <video> 无法拖动/循环，且每次请求把整份读进内存。"""
        try:
            filename = request.match_info.get('filename', '')
            base_dir = (self._console_web_dir() / "led").resolve()
            static_path = (base_dir / filename).resolve()
            if not str(static_path).startswith(str(base_dir)) or not static_path.is_file():
                return web.Response(text="File not found", status=404)
            suffix = static_path.suffix.lower()
            # 视频等媒体走流式响应（支持 Range），其余小文件仍走内存直出
            if suffix in {'.mp4', '.webm', '.mov', '.m4v'}:
                return web.FileResponse(path=static_path, headers={"Cache-Control": "no-cache"})
            content_types = {
                '.js': 'application/javascript',
                '.css': 'text/css',
                '.html': 'text/html',
                '.json': 'application/json',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.svg': 'image/svg+xml',
                '.ico': 'image/x-icon',
            }
            content_type = content_types.get(suffix, 'application/octet-stream')
            with open(static_path, "rb") as f:
                body = f.read()

            # [2026-10-05] 脚本/样式一律 no-store，根治「改了前端但主人浏览器没生效」。
            #
            # 踩过的坑（主人实测）：19:01 修了 board-runtime.js（补 map_view 处理），
            # design-v3.html 也补了中央舞台容器，服务端返回的 HTML 确实是对的，
            # 但主人的浏览器加载的还是**缓存里的旧 JS** —— 因为 design-v3.html 里的
            # 版本号 ?v=20261005a 从那次提交之后没跟着 bump，
            # 而这里又只回 `Cache-Control: no-cache`（语义是「可以缓存，使用前需验证」），
            # 响应里既没有 ETag 也没有 Last-Modified，浏览器没法验证就直接用了缓存。
            # 结果：后端广播成功、语音说「已经打开地图了」，屏幕上什么都没有。
            #
            # 这些文件都很小（board-runtime.js 约 100KB），no-store 的性能开销可忽略，
            # 换来的是「改完刷新就生效」，不用每次教主人按 Cmd+Shift+R 强刷。
            # 视频仍然走上面的 FileResponse 分支，保持 Range 支持与缓存能力。
            headers = {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return web.Response(body=body, content_type=content_type, headers=headers)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 LED 静态文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_web_shared_static(self, request):
        """处理前端共享模块（pcm-player.js 等），供 /terminal 与 /led 同时加载。

        [2026-10-06] 新增。PCM 播放逻辑原本在 terminal/index.html 与
        led/board-runtime.js 里各有一份拷贝，改一份漏一份会造成
        「切换中央形象后杂音复发」，故收敛到 web/shared/ 下唯一一份。
        脚本一律 no-store，根治「改了前端但浏览器没生效」。"""
        try:
            filename = request.match_info.get('filename', '')
            base_dir = (self._console_web_dir() / "shared").resolve()
            static_path = (base_dir / filename).resolve()
            # 防路径穿越：resolve 后必须仍在 shared 目录内
            if not str(static_path).startswith(str(base_dir)) or not static_path.is_file():
                return web.Response(text="File not found", status=404)

            content_types = {
                '.js': 'application/javascript',
                '.css': 'text/css',
                '.json': 'application/json',
            }
            content_type = content_types.get(static_path.suffix.lower(),
                                            'application/octet-stream')
            with open(static_path, "rb") as f:
                body = f.read()
            return web.Response(body=body, content_type=content_type,
                                headers={"Cache-Control": "no-store"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"web/shared 静态文件读取失败: {e}")
            return web.Response(text="Internal error", status=500)

    async def handle_web_data_static(self, request):
        """处理前端共享数据文件（城市级联 JSON 等），修复 /web/data/ 无路由 404"""
        try:
            filename = request.match_info.get('filename', '')
            base_dir = (self._console_web_dir() / "data").resolve()
            static_path = (base_dir / filename).resolve()
            # 防路径穿越：resolve 后必须仍在 data 目录内
            if not str(static_path).startswith(str(base_dir)) or not static_path.exists():
                return web.Response(text="File not found", status=404)

            content_types = {
                '.json': 'application/json',
                '.gz': 'application/gzip',
                '.js': 'application/javascript',
                '.css': 'text/css',
            }
            ext = static_path.suffix.lower()
            content_type = content_types.get(ext, 'application/octet-stream')

            with open(static_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type=content_type)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载前端数据文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_recipe_image(self, request):
        """菜谱图片静态服务：本地菜谱库抓取并落盘的成品图 / 步骤图。

        路径形如 /recipe-images/<recipe_id>/cover.jpg，
        对应磁盘 data/recipe_images/<recipe_id>/cover.jpg。
        """
        try:
            filename = request.match_info.get("filename", "")
            server_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dir = os.path.abspath(os.path.join(server_root, "data", "recipe_images"))
            static_path = os.path.abspath(os.path.join(base_dir, filename))
            # 防路径穿越：解析后必须仍在 recipe_images 目录内
            if not static_path.startswith(base_dir + os.sep) or not os.path.isfile(static_path):
                return web.Response(text="File not found", status=404)

            content_types = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }
            ext = os.path.splitext(static_path)[1].lower()
            content_type = content_types.get(ext, "application/octet-stream")
            with open(static_path, "rb") as f:
                content = f.read()
            return web.Response(
                body=content,
                content_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"菜谱图片读取失败: {e}")
            return web.Response(text=str(e), status=500)

    async def handle_ui_version(self, request):
        """前端版本探测：返回 LED 看板关键文件的 mtime（取最大）。

        看板页（board-runtime.js）加载时记下首次读到的值，之后每 60 秒比对一次；
        值变了就说明前端代码更新过 → 自动刷新页面，根治「改了前端但看板还是旧代码」。
        """
        try:
            base = self._console_web_dir() / "led"
            ver = 0
            for name in ("board-runtime.js", "design-v3.html"):
                p = base / name
                if os.path.exists(p):
                    ver = max(ver, int(os.path.getmtime(p)))
            return web.json_response(
                {"ver": ver},
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取前端版本失败: {e}")
            return web.json_response({"ver": 0}, headers={"Cache-Control": "no-store"})

    async def handle_docs_static(self, request):
        """在线查看项目文档：markdown 渲染为 HTML 页面，其他文件直接返回"""
        try:
            from pathlib import Path
            filename = request.match_info.get('filename', '')
            project_root = Path(os.path.abspath(__file__)).parent.parent.parent.parent
            base_dir = (project_root / "docs").resolve()
            static_path = (base_dir / filename).resolve()
            # 防路径穿越：resolve 后必须仍在 docs 目录内
            if not str(static_path).startswith(str(base_dir)) or not static_path.exists():
                return web.Response(text="File not found", status=404)

            if static_path.suffix.lower() in ('.md', '.markdown'):
                with open(static_path, "r", encoding="utf-8") as f:
                    md_text = f.read()
                from markdown_it import MarkdownIt
                md_renderer = MarkdownIt('commonmark').enable('table')
                body = md_renderer.render(md_text)
                css = (
                    "body{max-width:900px;margin:0 auto;padding:40px 24px 80px;"
                    "font-family:-apple-system,'PingFang SC','Helvetica Neue',sans-serif;"
                    "line-height:1.8;color:#1f2329;background:#fff}"
                    "h1{font-size:28px;border-bottom:2px solid #e5e6eb;padding-bottom:12px}"
                    "h2{font-size:22px;margin-top:36px;border-bottom:1px solid #f0f0f0;padding-bottom:8px}"
                    "h3{font-size:18px;margin-top:28px}"
                    "table{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px}"
                    "th,td{border:1px solid #d9d9d9;padding:8px 12px;text-align:left;vertical-align:top}"
                    "th{background:#f7f8fa}"
                    "code{background:#f2f3f5;padding:2px 6px;border-radius:4px;font-size:.9em}"
                    "pre{background:#f7f8fa;padding:16px;border-radius:8px;overflow-x:auto}"
                    "pre code{background:none;padding:0}"
                    "blockquote{border-left:4px solid #165dff;margin:16px 0;padding:4px 16px;"
                    "background:#f7f8fa;color:#4e5969}"
                    "a{color:#165dff}"
                    "img{max-width:100%}"
                )
                html = (
                    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>{static_path.name} - homAi 文档</title>"
                    f"<style>{css}</style></head><body>{body}</body></html>"
                )
                return web.Response(
                    text=html,
                    content_type='text/html',
                    headers={'Cache-Control': 'no-cache'},
                )

            content_types = {
                '.txt': 'text/plain; charset=utf-8',
                '.json': 'application/json',
                '.html': 'text/html; charset=utf-8',
                '.css': 'text/css',
                '.js': 'application/javascript',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.svg': 'image/svg+xml',
            }
            ext = static_path.suffix.lower()
            content_type = content_types.get(ext, 'application/octet-stream')
            with open(static_path, "rb") as f:
                content = f.read()
            return web.Response(
                body=content,
                content_type=content_type,
                headers={'Cache-Control': 'no-cache'},
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载文档失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_xiaozhi_console_static(self, request):
        """处理小智控制台静态文件 (JS/CSS/图片等)"""
        try:
            filename = request.match_info.get('filename', '')
            static_path = str(self._console_web_dir() / "admin" / "xiaozhi-console" / filename)

            if not os.path.exists(static_path):
                return web.Response(text="File not found", status=404)

            # 根据扩展名设置 Content-Type
            content_types = {
                '.js': 'application/javascript',
                '.css': 'text/css',
                '.html': 'text/html',
                '.json': 'application/json',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.svg': 'image/svg+xml',
                '.ico': 'image/x-icon',
                '.woff': 'font/woff',
                '.woff2': 'font/woff2',
                '.ttf': 'font/ttf',
                '.gz': 'application/gzip',
            }
            ext = os.path.splitext(filename)[1].lower()
            content_type = content_types.get(ext, 'application/octet-stream')

            with open(static_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type=content_type)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载小智控制台静态文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_homebrain_dashboard_page(self, request):
        """处理 HomeBrain 家庭看板页面（已归档，仅供回溯）"""
        try:
            # 旧看板已归档到 console/archive/homebrain-dashboard/（相对项目根推导，兼容双机部署）
            dashboard_path = str(self._console_web_dir().parent.parent
                                 / "archive" / "homebrain-dashboard" / "dashboard.html")
            if not os.path.exists(dashboard_path):
                self.logger.bind(tag=TAG).error(f"家庭看板文件不存在：{dashboard_path}")
                return web.Response(text="Dashboard not found", status=404)
            
            with open(dashboard_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            return web.Response(text=content, content_type="text/html")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载 HomeBrain 家庭看板页面失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_intent_log_page(self, request):
        """P3 直通判定日志页（admin/intent_log.html）"""
        try:
            page_path = str(self._console_web_dir() / "admin" / "intent_log.html")
            if not os.path.exists(page_path):
                return web.Response(text="Intent Log Page not found", status=404)
            with open(page_path, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"直通日志页读取失败: {e}")
            return web.Response(text="Internal Error", status=500)

    async def handle_music_config_test_page(self, request):
        """处理音乐配置测试页面"""
        try:
            # 音乐配置测试页面路径
            test_page_path = str(self._console_web_dir() / "admin" / "test_music_config.html")
            if not os.path.exists(test_page_path):
                self.logger.bind(tag=TAG).error(f"音乐配置测试页面不存在：{test_page_path}")
                return web.Response(text="Music Config Test Page not found", status=404)
            
            with open(test_page_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            return web.Response(text=content, content_type="text/html")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载音乐配置测试页面失败：{e}")
            return web.Response(text=str(e), status=500)

    # ==================== 加固①（2026-09-17）：原始配置编辑器的写保护 ====================
    @staticmethod
    def _config_fingerprint(content: str) -> str:
        """计算配置内容指纹，用于检测「编辑器内容已过期」（陈旧快照）。"""
        import hashlib
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def _backup_config_file(self, config_path: str, old_text: str, keep: int = 20) -> None:
        """写盘前自动备份，只保留最近 keep 份。备份失败不影响保存。"""
        try:
            if not old_text:
                return
            from datetime import datetime as _dt
            backup_dir = os.path.join(os.getcwd(), "config_backups")
            os.makedirs(backup_dir, exist_ok=True)
            name = f".config.yaml.backup.{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}"
            with open(os.path.join(backup_dir, name), "w", encoding="utf-8") as f:
                f.write(old_text)
            backups = sorted(b for b in os.listdir(backup_dir)
                             if b.startswith(".config.yaml.backup."))
            for old in backups[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, old))
                except OSError:
                    pass
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"配置备份失败（不阻断保存）：{e}")

    async def handle_get_config(self, request):
        """获取配置文件"""
        try:
            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            resp = web.Response(text=content, content_type="text/yaml")
            # 加固①：回传当前指纹，前端保存时带回即可检测陈旧快照
            resp.headers["X-Config-Fingerprint"] = self._config_fingerprint(content)
            return resp
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取配置文件失败：{e}")
            return web.Response(text=str(e), status=500)

    # ==================== 云端/本地引擎切换（2026-09-10，2026-09-28 重写预设） ====================
    # 2026-09-28 主人裁决：双栈方案制——豆包为云端选项之一，本地/云端永远可选。
    # 预设只引用 config.yaml 中真实存在的 provider；LLM 仅云端预设切换
    # （本地 LLM 走"AI模型配置"页的路由模式：单模型/智能分类/委托）。
    # 云端联动：工具筛选自动旁路、本地断句判断自动停用（core/utils/model_stack.py）。
    _MODULE_MODE_PRESETS = {
        "local": {"ASR": "SenseVoiceASR", "TTS": "MossNano"},
        "cloud": {"ASR": "豆包流式ASR", "TTS": "火山双流TTS", "LLM": "豆包大模型"},
    }
    _KEY_FIELDS = ("api_key", "apiKey", "access_key_id", "secret_key", "token",
                   "personal_access_token", "app_id", "appid", "api_secret",
                   "model_path", "model_dir")

    def _fix_selected_after_delete(self, module: str, deleted_provider: str) -> str | None:
        """删除 provider 后校准 selected_module：若删的是当前选中的，
        自动切换到同模块剩余的第一个 provider；无剩余则移除选中键。
        返回切换信息（用于提示），不需要切换时返回 None。"""
        selected = (self.config.get("selected_module") or {}).get(module)
        if selected != deleted_provider:
            return None
        remaining = [n for n in (self.config.get(module) or {}) if isinstance(self.config[module][n], dict)]
        new_sel = remaining[0] if remaining else None
        self.config.setdefault("selected_module", {})[module] = new_sel
        # 持久化到 data/.config.yaml
        try:
            custom_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(custom_path, "r", encoding="utf-8") as f:
                text = f.read()
            if new_sel is not None:
                text, _ = self._replace_selected_module_line(text, module, new_sel)
            else:
                import re
                text = re.sub(rf"^  {module}:.*\n", "", text, count=1, flags=re.M)
            with open(custom_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"校准 selected_module.{module} 失败: {e}")
        if new_sel:
            return f"，已自动切换选中模型为 {new_sel}"
        return "，该模块暂无选中模型"

    @staticmethod
    def _provider_configured(provider_cfg: dict) -> bool:
        """判定 provider 是否配置完整：密钥字段为空/占位符/未展开的环境变量 = 未配置"""
        if not isinstance(provider_cfg, dict):
            return False
        for k, v in provider_cfg.items():
            if k in SimpleHttpServer._KEY_FIELDS and isinstance(v, str):
                if not v.strip() or "你的" in v or "${" in v:
                    return False
        return True

    def _get_cloud_key_from_db(self) -> str:
        """从数据库 llm_config 读取阿里百炼 API key（主人已配置的 sk-sp 开头密钥）"""
        try:
            db_path = os.path.join(os.getcwd(), "data", "devices.db")
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                cur = conn.cursor()
                cur.execute("SELECT config_value FROM llm_config WHERE config_key='api_key' LIMIT 1")
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
            finally:
                conn.close()
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"读取云端 key 失败: {e}")
            return ""

    @staticmethod
    def _replace_selected_module_line(text: str, module: str, provider: str):
        """在 selected_module 块内行级替换，保留注释与格式；块内没有该模块行则插入"""
        lines = text.split("\n")
        in_block = False
        replaced = False
        insert_pos = None  # selected_module: 行之后的位置（用于插入新行）
        for i, line in enumerate(lines):
            if re.match(r"^selected_module:\s*(#.*)?$", line):
                in_block = True
                insert_pos = i + 1
                continue
            if in_block:
                if line and not line[0].isspace():
                    in_block = False
                    continue
                # 2026-10-05 修 bug：原来是 (\S+)(.*)，而 provider 名可以含空格
                # （如「豆包TTS seed2.0」「阿里百炼 ASR」「MossNano seed2.0」）。
                # \S+ 只吃到第一个词、剩余部分被 (.*) 原样保留，于是拼回去等于没改，
                # 但 replaced 仍返回 True —— 切换「静默无效」，前端却提示「配置已写入」。
                # 主人实测切 TTS 就是这么卡住的。
                # 现在用非贪婪 (.+?) 吃到行尾或注释前，整段替换。
                m = re.match(r"^(\s*" + re.escape(module) + r":\s*)(.+?)(\s*(#.*)?)$", line)
                if m:
                    lines[i] = m.group(1) + provider + (m.group(3) or "")
                    replaced = True
        if not replaced and insert_pos is not None:
            lines.insert(insert_pos, f"  {module}: {provider}")
            replaced = True
        return "\n".join(lines), replaced

    @staticmethod
    def _inject_cloud_keys(text: str, cloud_key: str) -> str:
        """把百炼 key 写入云端 provider 的 api_key 占位处（Qwen3ASRFlash / AliBLTTS）"""
        if not cloud_key:
            return text
        lines = text.split("\n")
        current_provider = None
        targets = ("Qwen3ASRFlash", "AliBLTTS")
        for i, line in enumerate(lines):
            m = re.match(r"^  ([A-Za-z0-9_]+):\s*(#.*)?$", line)
            if m:
                current_provider = m.group(1)
                continue
            if current_provider in targets:
                km = re.match(r"^(\s*api_key:\s*).*$", line)
                if km:
                    old = line
                    new_line = km.group(1) + cloud_key
                    # 只替换占位符，避免覆盖已配置的真实值
                    if ("你的" in old or "${" in old) and cloud_key not in old:
                        lines[i] = new_line
        return "\n".join(lines)

    # ---------- provider 分类元数据（2026-09-11 新增） ----------
    # group: local_model=本地自动下载模型 | local_service=本地需启动服务 | cloud=云端付费 | cloud_free=云端有免费额度
    # 依据：core/providers/{asr,tts} 实现文件的连接目标与依赖逐一定性（见扫描记录）
    # 2026-09-11 清理：删除需自建服务/写死IP/被墙/中转/场景不符的 14 个（FunASRServer,VoskASR,OpenaiASR,GroqASR,
    #   GPT_SOVITS_V2/V3,FishSpeech,PaddleSpeechTTS,CustomTTS,IndexStreamTTS,GizwitsTTS,TTS302AI,ACGNTTS,OpenAITTS）
    _PROVIDER_META = {
        # ---- ASR ----
        # 只保留实际在用的 4 个（主人 2026-10-05 裁决）：本地 2 个 + 云端 2 个。
        # 引擎实现文件（aliyun/baidu/tencent/xunfei 等）都还在 core/providers/asr/ 下，
        # 将来要启用只需在 data/.config.yaml 的 ASR: 段加一个条目并在此补元信息，
        # 前端「⚙️ 配置」会按该条目实际字段自动生成表单（见 handle_get_provider_fields），
        # 不需要为每家写代码。删除的只是没人用的元信息，不是能力。
        "FunASR":             {"group": "local_model",   "note": "本地·Paraformer 真流式，无需 Key。⚠️ 2026-10-05 实测慢 43 倍（RTF 1.07 等于慢于实时）、占内存 3.1 倍，不建议使用"},
        "SenseVoiceASR":      {"group": "local_model",   "note": "本地·SenseVoice 整句识别，约 25 毫秒，无需 Key，断网可用（当前主力）"},
        "豆包流式ASR":          {"group": "cloud",         "note": "云端·火山引擎豆包流式识别，边说边出字、不用等静音判停，需 API Key"},
        "阿里百炼 ASR":         {"group": "cloud",         "note": "云端·阿里百炼 Qwen3-ASR-Flash，需百炼 API Key（有免费额度）。2026-10-05 由 CustomASR 改名，原名看不出是哪家"},
        # ---- TTS ----
        "Qwen3TTS-0.6B":      {"group": "local_service", "note": "本地·Qwen3-TTS 0.6B（MLX，内存约 2.6GB，首包约 150ms，无需 Key）"},
        "Qwen3TTS-1.7B":      {"group": "local_service", "note": "本地·Qwen3-TTS 1.7B（MLX，内存约 4.4GB，支持情绪/风格指令，无需 Key）"},
        "MossNano":           {"group": "local_service", "note": "本地·MOSS-TTS-Nano 服务（8086，音色 zh_11；100M 轻量，内存约 0.7GB）"},
        "火山双流TTS":          {"group": "cloud",         "note": "云端·火山引擎双向流式（低延迟），需 access_token+appid"},
        "豆包TTS":             {"group": "cloud",         "note": "云端·火山引擎豆包合成，需 access_token+appid"},
        "CustomTTS":          {"group": "cloud",         "note": "云端·自定义 OpenAI 兼容接口的语音合成，需 Base URL + API Key"},
        # ---- LLM ----
        # 注意：豆包大模型/UserDefinedLLM/kimi-* 的 type 都是 openai（走 OpenAI 兼容协议），
        # 部署属性要看 url 指向云端还是本机，不能只看 type。
        "豆包大模型":            {"group": "cloud",         "note": "云端·火山方舟豆包大模型（当前主力），需 API Key"},
        "UserDefinedLLM":     {"group": "cloud",         "note": "云端·自定义 OpenAI 兼容接口，需 Base URL + API Key"},
        "kimi-for-coding-highspeed": {"group": "cloud",   "note": "云端·Kimi 高速编程版，需 API Key"},
        "本地LLM":             {"group": "local_service", "note": "本地·自建 OpenAI 兼容服务（无需 Key）"},
        # 下面 2 个不是模型而是「记忆后端」，历史设计混在 LLM 列表里；均为本机/自建
        "mem_local_short":    {"group": "local_service", "note": "本地·短期记忆（不含长期记忆）"},
        "LTM":         {"group": "local_service", "note": "本地·Ltm 记忆后端"},
        # ---- VLLM（视觉模型）----
        "豆包视觉":             {"group": "cloud",         "note": "云端·火山方舟视觉理解模型，需 API Key"},
        "oMLX-Qwen3.6-35B-VL": {"group": "local_service", "note": "本地·MLX 视觉模型服务（127.0.0.1:8001），无需 Key"},
        "oMLX-qwen3.8":       {"group": "local_service", "note": "本地·MLX 视觉模型服务（127.0.0.1:8001），需 Key"},
    }

    def _provider_names_in_section(self, config_text: str, mod: str) -> dict:
        """列出该模块段内已存在的 provider 名 -> type，用于精确判重。

        为什么不用 `f"  {name}:" in config_text` 这种全文字符串包含判断（2026-10-05 换掉）：
        1. 跨段误判：LLM 段有个「本地LLM」，在 ASR 段新建同名条目会被拦下，
           报错还指向一个根本不相关的模块。
        2. 对缩进/行尾空白敏感：yaml 缩进一变就漏判，于是静默插入第二个同名条目，
           或把已有条目的字段整块覆盖。主人 2026-10-05 实测就撞上了：原「豆包流式ASR」
           是 doubao_stream（靠 ws_url + resource_id 工作），保存后被自定义模板覆盖成
           type=openai 且丢了 ws_url / resource_id / deployment，语音识别直接不可用。

        返回 {provider 名: type}；type 取不到时为空串。
        """
        lines = config_text.split("\n")
        names, current, in_sec = {}, None, False
        for ln in lines:
            if re.match(rf"^{re.escape(mod)}:\s*$", ln) or ln.strip() == f"{mod}: {{}}":
                in_sec = True
                continue
            if in_sec and re.match(r"^[A-Za-z_]", ln):
                break  # 下一个顶层段
            if not in_sec:
                continue
            m = re.match(r"^  ([^ #][^:]*):\s*$", ln)
            if m:
                current = m.group(1).strip()
                names[current] = ""
                continue
            if current:
                tm = re.match(r"^\s+type:\s*(\S+)", ln)
                if tm and not names[current]:
                    names[current] = tm.group(1).strip().strip("\"'")
        return names

    @staticmethod
    def _infer_provider_group(mod: str, name: str, cfg: dict) -> tuple:
        """给 _PROVIDER_META 里没有的自定义 provider 推断分组。

        背景：主人在后台「添加自定义 ASR/TTS/VLLM 模型」新建的条目不在元信息表里，
        group 为空会落进前端的「🔧 其他」，主人反馈「为什么豆包 TTS 2.0 在其他里？
        要合并到云端」。这里按「端点指向」判定，比按名字猜可靠：

        - base_url/url 指向 127.0.0.1 / localhost / 10.x -> local_service
        - type 指向本地引擎（fun_local/sherpa/vosk）-> local_model
        - 其余有端点的 -> cloud
        - 实在判不出 -> local_service（最保守，不会让主人点错分组）
        """
        url = str(cfg.get("base_url") or cfg.get("url") or cfg.get("ws_url") or "").lower()
        ptype = str(cfg.get("type") or "").lower()
        local_types = ("fun_local", "sherpa_onnx_local", "vosk", "funasr_nano", "fun_server")

        if url and ("127.0.0.1" in url or "localhost" in url or url.startswith("http://10.")):
            group, prefix = "local_service", "本地·自建服务"
        elif ptype in local_types or (mod == "ASR" and not url and ptype):
            group, prefix = "local_model", "本地·自动下载模型"
        elif url:
            group, prefix = "cloud", "云端·自定义接口"
        else:
            group, prefix = "local_service", "本地·自建服务"

        note = (f"{prefix}·{name}（后台自定义新增，未登记在 _PROVIDER_META；"
                f"type={cfg.get('type', '')}）")
        return group, note

    # 记忆后端被上游误放在 LLM: 段里（config.yaml 里就有，llm.py 里没有对应实现）。
    # 它们该在 Memory 段选，出现在 LLM 下拉框里会让主人切过去然后热更新失败
    # （ValueError: 不支持的LLM类型）。这里从 LLM 列表里滤掉，不动配置文件，
    # 避免影响可能存在的代码依赖。
    _MEMORY_ONLY_TYPES = ("mem_local_short", "LTM")

    async def handle_get_providers(self, request):
        """获取 ASR/TTS/LLM/VLLM 可用 provider 列表 + 当前选中（供后台下拉）"""
        try:
            result = {}
            for mod in ("ASR", "TTS", "LLM", "VLLM"):
                providers = {}
                for name, cfg in (self.config.get(mod) or {}).items():
                    if not isinstance(cfg, dict):
                        continue
                    if mod == "LLM" and str(cfg.get("type", "")) in self._MEMORY_ONLY_TYPES:
                        continue  # 记忆后端，不该作为 LLM 引擎出现在下拉框
                    meta = self._PROVIDER_META.get(name, {})
                    group = meta.get("group", "")
                    note = meta.get("note", "")
                    if not group:
                        # 2026-10-05：主人在后台新增的自定义 provider 不在 _PROVIDER_META 里，
                        # group 为空会被前端归到「🔧 其他」——主人原话是「为什么会有个其他
                        # 的选项？要合并到云端里」。这里按端点/类型推断归属。
                        group, note = self._infer_provider_group(mod, name, cfg)
                    providers[name] = {
                        "type": cfg.get("type", ""),
                        "configured": self._provider_configured(cfg),
                        "group": group,
                        "note": note,
                    }
                result[mod] = {
                    "selected": self.config.get("selected_module", {}).get(mod, ""),
                    "providers": providers,
                }
            return web.json_response({"success": True, "modules": result})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    # ---------- 自定义视觉模型配置：2026-09-12 新增 ----------

    async def handle_test_custom_vllm(self, request):
        """POST /api/config/test-custom-vllm - 测试自定义视觉模型连接"""
        try:
            data = await request.json()
            base_url = data.get("base_url", "").strip()
            model_name = data.get("model_name", "").strip()
            api_key = data.get("api_key", "").strip()

            if not base_url or not model_name:
                return web.json_response({"success": False, "error": "缺少 base_url 或 model_name"}, status=400)

            # 简单测试：尝试调用 models 列表接口
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                async with session.get(f"{base_url}/models", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return web.json_response({"success": True, "message": "连接成功"})
                    else:
                        return web.json_response({"success": False, "error": f"HTTP {resp.status}"}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _test_openai_style_endpoint(self, kind: str, data: dict):
        """探测自定义 ASR/TTS 的 OpenAI 兼容端点是否真的可用。

        2026-10-05 新增。此前 ASR/TTS 的「🔌 测试连接」按钮 onclick 指向
        testCustomAsrConnection / testCustomTtsConnection，而这两个函数在前端
        根本不存在 → 点一下 JS 报错、什么都不发生，主人无法判断通没通。

        实现要点：不能像 handle_llm_test 那样打 /chat/completions —— LLM 接口和
        ASR/TTS 接口不是一回事，ASR 服务商多数不支持 chat。这里按类型打真实端点：
          ASR → POST {base_url}/audio/transcriptions（发一段 100ms 静音 wav）
          TTS → POST {base_url}/audio/speech（合成 1 个字）
        200=通；401/403=key 无效；404=端点不对；其他带上前端截断的响应体。
        """
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        model_name = str(data.get("model_name") or "").strip()
        api_key = str(data.get("api_key") or "").strip()
        if not base_url or not model_name:
            return web.json_response({"success": False, "error": "缺少 base_url 或 model_name"}, status=400)
        if not api_key:
            return web.json_response({"success": False, "error": "缺少 API 密钥"}, status=400)

        import aiohttp
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if kind == "ASR":
                    url = f"{base_url}/audio/transcriptions"
                    # 100ms 静音 wav：合法容器、零内容，服务商能解析即说明链路通
                    wav = _silent_wav_100ms()
                    form = aiohttp.FormData()
                    form.add_field("file", wav, filename="test.wav", content_type="audio/wav")
                    form.add_field("model", model_name)
                    async with session.post(url, headers=headers, data=form) as resp:
                        return await _classify_probe(resp, "语音识别")
                else:
                    url = f"{base_url}/audio/speech"
                    payload = {"model": model_name, "input": "你好", "voice": "alloy"}
                    async with session.post(url, headers=headers, json=payload) as resp:
                        return await _classify_probe(resp, "语音合成")
        except aiohttp.ClientError as e:
            return web.json_response({"success": False, "error": f"网络错误：{e}"}, status=200)
        except Exception as e:
            return web.json_response({"success": False, "error": f"测试异常：{e}"}, status=200)

    async def handle_test_custom_asr(self, request):
        """POST /api/config/test-custom-asr - 测试自定义 ASR 连接（真实调用 /audio/transcriptions）"""
        try:
            return await self._test_openai_style_endpoint("ASR", await request.json())
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_test_custom_tts(self, request):
        """POST /api/config/test-custom-tts - 测试自定义 TTS 连接（真实调用 /audio/speech）"""
        try:
            return await self._test_openai_style_endpoint("TTS", await request.json())
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_save_custom_vllm(self, request):
        """POST /api/config/save-custom-vllm - 保存自定义视觉模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()
            base_url = data.get("base_url", "").strip()
            model_name = data.get("model_name", "").strip()
            api_key = data.get("api_key", "").strip()

            if not provider_name or not base_url or not model_name:
                return web.json_response({"success": False, "error": "缺少必填字段"}, status=400)

            # 读取现有配置
            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                config_text = f.read()

            # 检查是否已存在同名 provider
            _existing = self._provider_names_in_section(config_text, "VLLM")
            if provider_name in _existing:
                _old = _existing[provider_name] or "(未标type)"
                return web.json_response({"success": False, "error":
                    f"「{provider_name}」已存在（type={_old}）。自定义模型只能新建、不能覆盖已有引擎，"
                    f"否则会把原引擎的参数整块替换掉。要改这个引擎请用下拉框旁的「⚙️ 配置」；"
                    f"确实要新建请换个名字。"}, status=400)
            # 构建新的 provider 配置
            # 2026-10-05：密钥归集到 .env，这里只写 ${} 引用（data/.config.yaml 被 git 跟踪）
            _key_ref = secret_store.to_ref("VLLM", provider_name, "api_key", api_key) or "你的api_key"
            new_provider = f"""  {provider_name}:
    api_key: {_key_ref}
    model_name: {model_name}
    type: openai
    url: {base_url}
"""

            # 在 VLLM 段末尾添加
            lines = config_text.split("\n")
            insert_idx = -1
            in_vllm = False
            for i, line in enumerate(lines):
                if line.strip() == "VLLM:" or line.strip() == "VLLM: {}":
                    in_vllm = True
                    # 如果是 VLLM: {} 单行形式，需要替换这一行
                    if line.strip() == "VLLM: {}":
                        lines[i] = "VLLM:"
                        insert_idx = i + 1
                        # 找到后立即停止搜索，避免被后续行覆盖
                        break
                    continue
                elif in_vllm and line and not line.startswith(" ") and not line.startswith("#"):
                    insert_idx = i
                    break

            if insert_idx > 0:
                lines.insert(insert_idx, new_provider.rstrip())
            else:
                lines.append(new_provider.rstrip())

            # 写回文件
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            # 更新内存配置（让列表 API 能立即读到）
            # 必须走 read_config 而不是裸 yaml.safe_load：yaml 里密钥存的是 ${引用}，
            # 裸读会让 self.config 里留着 "${VAR}" 字面串，于是
            # ① _provider_configured 把它判成「未配置」
            # ② 引擎实例化时拿到字面串当 key，认证必然失败
            # （2026-10-05 主人在后台保存后实测复现）
            updated_config = self._reload_config_expanded()

            self.logger.bind(tag=TAG).info(f"已保存自定义视觉模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已保存 {provider_name}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存自定义视觉模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_list_custom_vllm(self, request):
        """GET /api/config/custom-vllm-models - 列出自定义视觉模型"""
        try:
            # 从配置中读取 VLLM 段
            vllm_config = self.config.get("VLLM", {})
            models = []

            # 预定义的 provider
            predefined = {"ChatGLMVLLM", "QwenVLVLLM", "XunfeiSparkLLM"}

            for name, cfg in vllm_config.items():
                if name in predefined:
                    continue
                if not isinstance(cfg, dict):
                    continue

                models.append({
                    "provider_name": name,
                    "model_name": cfg.get("model_name", ""),
                    "base_url": cfg.get("url", ""),
                    "configured": self._provider_configured(cfg)
                })

            return web.json_response({"success": True, "models": models})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_custom_vllm(self, request):
        """POST /api/config/delete-custom-vllm - 删除自定义视觉模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()

            if not provider_name:
                return web.json_response({"success": False, "error": "缺少 provider_name"}, status=400)

            # 读取配置文件
            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # 找到并删除对应的 provider 块
            new_lines = []
            skip = False
            for line in lines:
                if line.strip() == f"{provider_name}:":
                    skip = True
                    continue
                if skip:
                    # 遇到下一个同级 provider 或新段时停止跳过
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        skip = False
                    elif line.strip() and not line.startswith("  "):
                        skip = False
                if not skip:
                    new_lines.append(line)

            # 写回文件
            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            # 更新内存配置（走 read_config 展开 ${} 引用，理由见保存路径处的注释）
            updated_config = self._reload_config_expanded()
            self.config["VLLM"] = updated_config.get("VLLM", {})

            # 若删除的是当前选中模型，自动切换/清除选中
            switch_note = self._fix_selected_after_delete("VLLM", provider_name) or ""

            self.logger.bind(tag=TAG).info(f"已删除自定义视觉模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已删除 {provider_name}{switch_note}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"删除自定义视觉模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    # ==================== ASR 自定义模型配置 ====================

    async def handle_save_custom_asr(self, request):
        """POST /api/config/save-custom-asr - 保存自定义ASR模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()
            base_url = data.get("base_url", "").strip()
            model_name = data.get("model_name", "").strip()
            api_key = data.get("api_key", "").strip()

            if not provider_name or not base_url or not model_name:
                return web.json_response({"success": False, "error": "缺少必填字段"}, status=400)

            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                config_text = f.read()

            _existing = self._provider_names_in_section(config_text, "ASR")
            if provider_name in _existing:
                _old = _existing[provider_name] or "(未标type)"
                return web.json_response({"success": False, "error":
                    f"「{provider_name}」已存在（type={_old}）。自定义模型只能新建、不能覆盖已有引擎，"
                    f"否则会把原引擎的参数整块替换掉。要改这个引擎请用下拉框旁的「⚙️ 配置」；"
                    f"确实要新建请换个名字。"}, status=400)
            new_provider = f"""  {provider_name}:
    # 2026-10-05：密钥归集到 .env，yaml 里只留变量引用
    api_key: {secret_store.to_ref("ASR", provider_name, "api_key", api_key) or '你的api_key'}
    base_url: {base_url}
    model_name: {model_name}
    type: openai
"""

            lines = config_text.split("\n")
            insert_idx = -1
            in_asr = False
            for i, line in enumerate(lines):
                if line.strip() == "ASR:" or line.strip() == "ASR: {}":
                    in_asr = True
                    if line.strip() == "ASR: {}":
                        lines[i] = "ASR:"
                        insert_idx = i + 1
                        break
                    continue
                elif in_asr and line and not line.startswith(" ") and not line.startswith("#"):
                    insert_idx = i
                    break

            if insert_idx > 0:
                lines.insert(insert_idx, new_provider.rstrip())
            else:
                lines.append(new_provider.rstrip())

            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            # 更新内存配置（走 read_config 展开 ${} 引用，理由见 VLLM 处注释）
            updated_config = self._reload_config_expanded()
            self.config["ASR"] = updated_config.get("ASR", {})

            self.logger.bind(tag=TAG).info(f"已保存自定义ASR模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已保存 {provider_name}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存自定义ASR模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_list_custom_asr(self, request):
        """GET /api/config/custom-asr-models - 列出自定义ASR模型"""
        try:
            asr_config = self.config.get("ASR", {})
            models = []

            # 预定义的 provider
            predefined = {"FunASR", "SenseVoiceASR"}

            for name, cfg in asr_config.items():
                if name in predefined:
                    continue
                if not isinstance(cfg, dict):
                    continue

                models.append({
                    "provider_name": name,
                    "model_name": cfg.get("model_name", ""),
                    "base_url": cfg.get("base_url", cfg.get("url", "")),
                    "configured": self._provider_configured(cfg)
                })

            return web.json_response({"success": True, "models": models})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_custom_asr(self, request):
        """POST /api/config/delete-custom-asr - 删除自定义ASR模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()

            if not provider_name:
                return web.json_response({"success": False, "error": "缺少 provider_name"}, status=400)

            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            skip = False
            for line in lines:
                if line.strip() == f"{provider_name}:":
                    skip = True
                    continue
                if skip:
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        skip = False
                    elif line.strip() and not line.startswith("  "):
                        skip = False
                if not skip:
                    new_lines.append(line)

            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            # 更新内存配置（走 read_config 展开 ${} 引用，理由见 VLLM 处注释）
            updated_config = self._reload_config_expanded()
            self.config["ASR"] = updated_config.get("ASR", {})

            # 若删除的是当前选中模型，自动切换/清除选中
            switch_note = self._fix_selected_after_delete("ASR", provider_name) or ""

            self.logger.bind(tag=TAG).info(f"已删除自定义ASR模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已删除 {provider_name}{switch_note}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"删除自定义ASR模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    # ==================== TTS 自定义模型配置 ====================

    async def handle_save_custom_tts(self, request):
        """POST /api/config/save-custom-tts - 保存自定义TTS模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()
            base_url = data.get("base_url", "").strip()
            model_name = data.get("model_name", "").strip()
            api_key = data.get("api_key", "").strip()

            if not provider_name or not base_url or not model_name:
                return web.json_response({"success": False, "error": "缺少必填字段"}, status=400)

            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                config_text = f.read()

            _existing = self._provider_names_in_section(config_text, "TTS")
            if provider_name in _existing:
                _old = _existing[provider_name] or "(未标type)"
                return web.json_response({"success": False, "error":
                    f"「{provider_name}」已存在（type={_old}）。自定义模型只能新建、不能覆盖已有引擎，"
                    f"否则会把原引擎的参数整块替换掉。要改这个引擎请用下拉框旁的「⚙️ 配置」；"
                    f"确实要新建请换个名字。"}, status=400)
            new_provider = f"""  {provider_name}:
    # 2026-10-05：密钥归集到 .env，yaml 里只留变量引用
    api_key: {secret_store.to_ref("TTS", provider_name, "api_key", api_key) or '你的api_key'}
    base_url: {base_url}
    model_name: {model_name}
    type: openai
"""

            lines = config_text.split("\n")
            insert_idx = -1
            in_tts = False
            for i, line in enumerate(lines):
                if line.strip() == "TTS:" or line.strip() == "TTS: {}":
                    in_tts = True
                    if line.strip() == "TTS: {}":
                        lines[i] = "TTS:"
                        insert_idx = i + 1
                        break
                    continue
                elif in_tts and line and not line.startswith(" ") and not line.startswith("#"):
                    insert_idx = i
                    break

            if insert_idx > 0:
                lines.insert(insert_idx, new_provider.rstrip())
            else:
                lines.append(new_provider.rstrip())

            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            # 更新内存配置（走 read_config 展开 ${} 引用，理由见 VLLM 处注释）
            updated_config = self._reload_config_expanded()
            self.config["TTS"] = updated_config.get("TTS", {})

            self.logger.bind(tag=TAG).info(f"已保存自定义TTS模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已保存 {provider_name}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存自定义TTS模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_list_custom_tts(self, request):
        """GET /api/config/custom-tts-models - 列出自定义TTS模型"""
        try:
            tts_config = self.config.get("TTS", {})
            models = []

            # 预定义的 provider
            predefined = {"MossNano"}

            for name, cfg in tts_config.items():
                if name in predefined:
                    continue
                if not isinstance(cfg, dict):
                    continue

                models.append({
                    "provider_name": name,
                    "model_name": cfg.get("model_name", cfg.get("voice", "")),
                    "base_url": cfg.get("base_url", cfg.get("api_url", "")),
                    "configured": self._provider_configured(cfg)
                })

            return web.json_response({"success": True, "models": models})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_delete_custom_tts(self, request):
        """POST /api/config/delete-custom-tts - 删除自定义TTS模型"""
        try:
            data = await request.json()
            provider_name = data.get("provider_name", "").strip()

            if not provider_name:
                return web.json_response({"success": False, "error": "缺少 provider_name"}, status=400)

            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            skip = False
            for line in lines:
                if line.strip() == f"{provider_name}:":
                    skip = True
                    continue
                if skip:
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        skip = False
                    elif line.strip() and not line.startswith("  "):
                        skip = False
                if not skip:
                    new_lines.append(line)

            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            # 更新内存配置（走 read_config 展开 ${} 引用，理由见 VLLM 处注释）
            updated_config = self._reload_config_expanded()
            self.config["TTS"] = updated_config.get("TTS", {})

            # 若删除的是当前选中模型，自动切换/清除选中
            switch_note = self._fix_selected_after_delete("TTS", provider_name) or ""

            self.logger.bind(tag=TAG).info(f"已删除自定义TTS模型: {provider_name}")
            return web.json_response({"success": True, "message": f"已删除 {provider_name}{switch_note}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"删除自定义TTS模型失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    # ---------- provider 字段（密钥）配置：2026-09-11 新增 ----------
    # 「选中云端 provider → 后台填 key」闭环。密钥值永不回传前端，只回传"是否已配置"。

    _SECRET_HINTS = ("key", "secret", "token", "appid", "app_id", "model_path", "model_dir")

    async def handle_get_provider_fields(self, request):
        """GET /api/config/provider-fields?module=TTS&provider=AliyunTTS
        返回该 provider 的可编辑字段清单；密钥类字段只回 configured 状态，不回传值。"""
        try:
            mod = request.query.get("module", "")
            provider = request.query.get("provider", "")
            if mod not in ("ASR", "TTS", "LLM", "VLLM"):
                return web.json_response({"success": False, "error": "module 须为 ASR/TTS/LLM/VLLM"}, status=400)
            cfg = (self.config.get(mod) or {}).get(provider)
            if not isinstance(cfg, dict):
                return web.json_response({"success": False, "error": f"未找到 {mod}.{provider}"}, status=404)

            fields = []
            for k, v in cfg.items():
                if k == "type":
                    continue  # type 决定实现文件，不允许改
                if isinstance(v, (dict, list)):
                    continue  # 嵌套结构（如 routing 的 cloud/local）不在本表单编辑
                is_secret = any(h in k.lower() for h in self._SECRET_HINTS)
                configured = bool(str(v or "").strip()) and "你的" not in str(v) and "${" not in str(v)
                fields.append({
                    "key": k,
                    "is_secret": is_secret,
                    "configured": configured,
                    # 密钥不回传值；非密钥回传当前值便于编辑
                    "value": None if is_secret else ("" if v is None else v),
                })
            return web.json_response({"success": True, "module": mod, "provider": provider,
                                      "type": cfg.get("type", ""), "fields": fields})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    def _reload_config_expanded(self) -> dict:
        """重新读 data/.config.yaml 并展开 ${} 引用，供各保存/删除接口热更新内存。

        为什么不能直接 yaml.safe_load：yaml 里密钥一律存成 ${变量名}（真值在 .env，
        见 core/utils/secret_store.py）。裸读会让内存配置里留着 "${VAR}" 字面串，
        造成两个后果：
        1. _provider_configured 判定命中 `"${" in v` → 后台显示「未配置」
        2. 引擎实例化时把字面串当 API key → 云端调用 100% 认证失败

        2026-10-05 主人在后台保存自定义 ASR 后实测复现了这两个后果。
        """
        from config.config_loader import read_config
        path = "data/.config.yaml"
        # 兜底：任何写入路径漏掉的明文密钥，这里统一搬进 .env。
        # 必须在读之前做——否则读回来的内存配置里还带着明文。
        try:
            self.sweep_plain_secrets(path)
        except Exception as e:  # 兜底不该阻断主流程
            self.logger.bind(tag=TAG).warning(f"密钥兜底扫描失败（不影响本次保存）: {e}")
        return read_config(path) or {}

    @staticmethod
    def sweep_plain_secrets(path: str) -> int:
        """扫描整个 yaml，把残留的明文密钥换成 ${} 引用。返回归集处数。

        为什么需要这层兜底（2026-10-05）
        --------------------------------
        密钥归集规范只在「已知写入点」上强制（provider-fields 保存、三处自定义模型
        新增、update_llm_config_in_yaml）。但主人实测发现 yaml 里仍出现过明文
        LLM key，说明还有我们没定位到的写入路径（可能是别的会话、手工编辑，或
        经由 yaml.dump 整体重写的旧代码）。

        与其继续追路径，不如加一道扫全文件的兜底：任何路径写进来的明文，
        下一次保存时都会被搬进 .env。这样「配置里永不出现明文密钥」成为不变量，
        不依赖「所有写入点都记得归集」。

        代价极小：一次线性扫描 + 行级替换，只在写完yaml 后调用。
        """
        import re as _re
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        section, provider, changed = None, None, 0
        for i, ln in enumerate(lines):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            m0 = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*$", ln)
            if m0:
                section, provider = m0.group(1), None
                continue
            m1 = _re.match(r"^  ([^ #][^:]*):\s*$", ln)
            if m1:
                provider = m1.group(1).strip()
                continue
            m2 = _re.match(r"^(\s+)([A-Za-z0-9_]+)\s*:\s*(\S.*?)\s*$", ln)
            if not m2:
                continue
            indent, key, val = m2.group(1), m2.group(2), m2.group(3).strip().strip('"').strip("'")
            if not secret_store.is_secret_field(key):
                continue
            ref = secret_store.to_ref(section or "CONFIG", provider or "root", key, val)
            if ref != val:
                lines[i] = f"{indent}{key}: {ref}"
                changed += 1
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.logger.bind(tag=TAG).info(
                f"密钥兜底归集：{os.path.basename(path)} 中 {changed} 处明文已搬入 .env")
        return changed

    @staticmethod
    def _write_provider_fields_to_yaml(path: str, mod: str, provider: str, fields: dict) -> int:
        """文本行级写回：定位 `  provider:` 块，替换或追加 `    field: value` 行。
        保持原文件注释与结构。返回写入字段数。

        2026-10-05：写入前把「明文密钥」归集到 .env，本文件只留 ${} 引用。
        这是全项目规范（见 core/utils/secret_store.py）的强制点——任何经本接口
        保存的密钥都不再进入 git（data/.config.yaml 是被跟踪的）。
        """
        fields = secret_store.secretize_fields(mod, provider, fields)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")

        sec_pat = re.compile(rf"^{mod}:\s*$")
        prov_pat = re.compile(rf"^  {re.escape(provider)}:\s*$")
        in_sec = in_prov = False
        prov_start = prov_end = -1
        for i, ln in enumerate(lines):
            if sec_pat.match(ln):
                in_sec = True
                continue
            if in_sec and re.match(r"^[A-Za-z_]", ln) and not ln.startswith(" "):
                in_sec = False  # 下一个顶层段
            if in_sec and prov_pat.match(ln):
                in_prov = True
                prov_start = i
                continue
            if in_prov and re.match(r"^  \S", ln):  # 下一个同级 provider
                prov_end = i
                break
        if prov_start < 0:
            raise ValueError(f"{os.path.basename(path)} 中找不到 {mod}.{provider} 块")
        if prov_end < 0:
            prov_end = len(lines)

        written = 0
        remaining = dict(fields)
        for i in range(prov_start + 1, prov_end):
            m = re.match(r"^(\s+)([\w]+):\s*(.*)$", lines[i])
            if m and m.group(2) in remaining:
                key = m.group(2)
                lines[i] = f"{m.group(1)}{key}: {fields[key]}"
                del remaining[key]
                written += 1
        # 块内不存在的字段：追加到块尾
        for key, val in remaining.items():
            lines.insert(prov_end, f"    {key}: {val}")
            prov_end += 1
            written += 1

        new_content = "\n".join(lines)
        import yaml as _yaml
        _yaml.safe_load(new_content)  # 写前校验 YAML 合法
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return written

    @staticmethod
    def _append_tts_engine_block(content: str, engine: str, voice: str) -> str:
        """在 TTS 段末尾追加一个新引擎块（加固②：目标引擎在该文件里尚无独立块时用）。"""
        lines = content.split("\n")
        block = [f"  {engine}:", f"    voice: {voice}", "    format: mp3"]
        tts_at = -1
        for i, ln in enumerate(lines):
            if re.match(r"^TTS:\s*$", ln):
                tts_at = i
                break
        if tts_at < 0:                       # 没有 TTS 段 → 新建
            if lines and lines[-1] == "":
                lines.pop()
            lines += ["TTS:"] + block + [""]
        else:                                # 插到 TTS 段末尾（下一个顶层键之前）
            insert_at = len(lines)
            for j in range(tts_at + 1, len(lines)):
                if lines[j].strip() and re.match(r"^[A-Za-z_]", lines[j]):
                    insert_at = j
                    break
            lines[insert_at:insert_at] = block
        return "\n".join(lines)

    async def handle_save_provider_fields(self, request):
        """POST /api/config/provider-fields
        Body: {module, provider, fields: {api_key: "..."}} 写回两份配置并热更新。"""
        try:
            data = await request.json()
            mod = data.get("module", "")
            provider = data.get("provider", "")
            fields = data.get("fields") or {}
            if mod not in ("ASR", "TTS", "LLM"):
                return web.json_response({"success": False, "error": "module 须为 ASR/TTS/LLM"}, status=400)
            cfg = (self.config.get(mod) or {}).get(provider)
            if not isinstance(cfg, dict):
                return web.json_response({"success": False, "error": f"未找到 {mod}.{provider}"}, status=404)
            # 只允许写该 provider 已有字段（防注入任意键）；不允许改 type
            allowed = {k for k in cfg.keys() if k != "type"}
            clean = {}
            for k, v in fields.items():
                if k not in allowed:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue  # 空值不覆盖（前端密钥留空=不改）
                clean[k] = str(v).strip() if isinstance(v, str) else v
            if not clean:
                return web.json_response({"success": False, "error": "没有可写入的字段"}, status=400)

            results = {}
            for path in (os.path.join(os.getcwd(), "config.yaml"),
                         os.path.join(os.getcwd(), "data", ".config.yaml")):
                if not os.path.exists(path):
                    results[os.path.basename(path)] = "skip(不存在)"
                    continue
                try:
                    n = self._write_provider_fields_to_yaml(path, mod, provider, clean)
                    results[os.path.basename(path)] = f"ok({n})"
                except Exception as e:
                    results[os.path.basename(path)] = f"fail: {e}"

            # 内存同步 + 热更新组件
            for k, v in clean.items():
                cfg[k] = v
            reloaded = False
            ws_server = getattr(self, "server", None)
            if ws_server is not None and hasattr(ws_server, "update_config"):
                try:
                    reloaded = bool(await ws_server.update_config())
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"字段保存后热更新失败（重启生效）: {e}")

            ok = all(str(v).startswith("ok") for v in results.values())
            return web.json_response({
                "success": ok, "files": results, "hot_reload": reloaded,
                "configured_now": self._provider_configured(cfg),
                "message": "已保存" + ("并热更新" if reloaded else "，重启后生效"),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_morning_digest_test(self, request):
        """POST /api/morning-digest/test — 立即执行一次早安简报（测试/调试）"""
        try:
            from core.utils.morning_digest import run_digest
            ws_server = getattr(self, "server", None)
            script = await run_digest(ws_server, self.config)
            return web.json_response({"success": True, "script": script})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _sync_asr_config_table(self, provider: str) -> dict:
        """ASR 切换后同步 asr_config 表。

        背景（2026-10-05）：core/utils/modules_initialize.py 决定实际用哪个 ASR 引擎时
        **数据库优先级高于配置文件**：
            asr_type = db_asr_config.get("type") or config["ASR"][module]["type"]
        而 _switch_modules 只改 data/.config.yaml 的 selected_module，不碰数据库，
        于是后台点「应用」提示成功，实际引擎仍是数据库里那个 —— 表现为
        「切换后重启没变」。这里把所选 provider 的配置原样写进表，让两处一致。

        写入后返回该 provider 在数据库优先规则下的实际生效 type，供前端如实显示。
        """
        import sqlite3

        cfg = (self.config.get("ASR") or {}).get(provider) or {}
        asr_type = cfg.get("type")
        if not asr_type:
            return {
                "ok": False,
                "actual_type": None,
                "message": f"provider {provider} 的配置里没有 type 字段，无法确定引擎",
            }
        # 记录到哪些键（与旧 saveASRConfig 写入的字段保持一致）
        fields = {
            "type": asr_type,
            "provider": provider,
            "model": cfg.get("model", ""),
            "model_dir": cfg.get("model_dir", cfg.get("model", "")),
            "sampleRate": str(cfg.get("sampleRate", "16000")),
            "output_dir": cfg.get("output_dir", "tmp/"),
        }
        # 只写该引擎真正用得到的键，避免留下空值/过期值：
        # - model 与 model_dir 必须一致。data/.config.yaml 里 SenseVoiceASR 只写了
        #   model_dir（没有 model），旧写法会把 model 写成空串，依赖
        #   modules_initialize 的「不一致就以 model_dir 为准」兜底才没出事。
        # - model_type/streaming 是本地引擎专属；切到云端时若不覆盖，上一个本地引擎的
        #   model_type（如 sense_voice）会残留给云端引擎。
        model_dir = cfg.get("model_dir") or cfg.get("model") or ""
        fields["model"] = model_dir
        fields["model_dir"] = model_dir
        if "model_type" in cfg:
            fields["model_type"] = cfg["model_type"]
        else:
            fields["model_type"] = ""
        if "streaming" in cfg:
            fields["streaming"] = str(cfg["streaming"]).lower()
        else:
            fields["streaming"] = ""
        try:
            # 复用 config_api 的标准 DB_PATH（绝对路径），不写相对路径 ——
            # 后台接口可能被以任意 cwd 调用，相对路径会写到别处去
            from core.api.config_api import DB_PATH

            conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
            try:
                cur = conn.cursor()
                for k, v in fields.items():
                    cur.execute(
                        "INSERT INTO asr_config(config_key, config_value) VALUES(?,?) "
                        "ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value",
                        (k, str(v)),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            return {
                "ok": False,
                "actual_type": None,
                "message": f"已改配置文件，但同步 asr_config 表失败（{exc}）。"
                f"服务器实际仍会用数据库里的旧引擎，重启后切换不生效",
            }
        return {"ok": True, "actual_type": asr_type, "message": "配置与数据库已同步"}

    async def _switch_modules(self, module_provider_map: dict):
        """切换 selected_module：写内存 + 持久化到 data/.config.yaml + 热更新组件

        ASR 额外同步 asr_config 表（见 _sync_asr_config_table 的说明），
        否则后台切换会被数据库里的 type 架空。
        """
        custom_path = os.path.join(os.getcwd(), "data/.config.yaml")
        with open(custom_path, "r", encoding="utf-8") as f:
            text = f.read()
        asr_sync_note = ""
        if "ASR" in module_provider_map:
            sync = await self._sync_asr_config_table(module_provider_map["ASR"])
            if not sync["ok"]:
                # 同步失败必须让调用方知道：否则前端提示「切换成功」而实际没生效
                return False, f"切换失败：{sync['message']}"
            asr_sync_note = f"，实际生效引擎 {sync['actual_type']}"
        for module, provider in module_provider_map.items():
            # 2026-10-05 加校验：selected_module 一旦指向不存在的 provider，
            # initialize_tts/initialize_asr 会抛 KeyError，表现为
            # 「配置已写入，但组件热更新失败」——配置生效不了却看不出原因。
            # 历史故障：selected_module.TTS 卡在「MossNano seed2.0」，
            # 而该 provider 早已不在 TTS 段里，导致每次切任何 TTS 都热更新失败。
            avail = self.config.get(module) or {}
            if provider not in avail:
                return False, (
                    f"配置中 {module} 段不存在 provider「{provider}」，已取消切换。"
                    f"当前可选：{'、'.join(avail.keys()) or '(空)'}"
                )
            # 2026-10-05：也要拦住「存在但没配好密钥」的云端 provider。
            # 主人实测：TTS 切到未配置的 CustomTTS 后接口提示保存成功，
            # 但服务下次启动直接失败（反复 spawn 失败、last exit code=1），
            # 整个后台不可用。现在当场拒绝并说清要先去「⚙️ 配置」填密钥。
            if module in ("ASR", "TTS", "LLM", "VLLM"):
                cfg = avail.get(provider) or {}
                if isinstance(cfg, dict) and not self._provider_configured(cfg):
                    return False, (
                        f"「{provider}」还没配置好密钥，切换后服务会启动失败，已取消。"
                        f"请先在下拉框旁点「⚙️ 配置引擎参数」填好密钥（密钥存.env，"
                        f"填完再切）。"
                    )
            text, replaced = self._replace_selected_module_line(text, module, provider)
            if not replaced:
                return False, f"配置文件中找不到 selected_module.{module}"
        with open(custom_path, "w", encoding="utf-8") as f:
            f.write(text)
        # 【双文件同步 2026-10-07】只写 data/.config.yaml 会让根 config.yaml 的
        # selected_module 永远停在旧引擎上（实测根=TTS 火山双流、data=Qwen3TTS-0.6B）。
        # 运行时虽然以 data 为准，但管理台有一部分接口只读根文件 —— 两边不一致
        # 会导致「后台显示的当前引擎」与「实际在跑的引擎」不同，排查时极易误判。
        # 与「配置引擎参数」写两份 YAML 的做法保持一致：行级替换，保留注释格式。
        try:
            root_path = os.path.join(os.getcwd(), "config.yaml")
            if os.path.abspath(root_path) != os.path.abspath(custom_path) and os.path.exists(root_path):
                with open(root_path, "r", encoding="utf-8") as f:
                    root_text = f.read()
                root_changed = False
                for module, provider in module_provider_map.items():
                    root_text, replaced = self._replace_selected_module_line(
                        root_text, module, provider)
                    root_changed = root_changed or replaced
                if root_changed:
                    with open(root_path, "w", encoding="utf-8") as f:
                        f.write(root_text)
                    self.logger.bind(tag=TAG).info("selected_module 已同步写入根 config.yaml")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"同步根 config.yaml 失败（不影响本次切换）: {e}")
        # 兜底：切换也是 yaml 写入路径，同样不能留下明文密钥
        try:
            self.sweep_plain_secrets(custom_path)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"密钥兜底扫描失败（不影响本次切换）: {e}")
        for module, provider in module_provider_map.items():
            self.config.setdefault("selected_module", {})[module] = provider
        self.logger.bind(tag=TAG).info(f"引擎切换: {module_provider_map}，开始热更新组件...")
        # app.py 启动时注入 self.server = WebSocketServer，热更新走它的 update_config
        ws_server = getattr(self, "server", None)
        detail = ""
        if ws_server is not None and hasattr(ws_server, "update_config"):
            ws_server.last_update_error = ""      # 先清空，避免读到上次的旧错误
            ok = await ws_server.update_config()
            if not ok:
                # 2026-10-05：把真实失败原因透出。以前只说「建议重启服务」，
                # 而重启往往不解决（provider 缺必填参数、依赖服务没起等），
                # 主人只能对着这一句话干瞪眼。
                detail = getattr(ws_server, "last_update_error", "") or "原因未记录（服务端日志里有）"
        else:
            ok = False
            detail = "WebSocketServer 实例未注入，服务未以正常方式启动"
        if ok:
            return True, f"切换成功{asr_sync_note}"
        # 注意用词：热更新失败时「当前正在用的」还是旧引擎，配置文件里的新选择
        # 要等重启/下次生效。以前写「配置文件已生效」会让人以为已经换好了。
        return False, (
            f"配置已写入{asr_sync_note}，但组件热更新失败，当前仍在用旧引擎。"
            f"原因：{detail}。"
            f"选中的引擎会在下次重启服务后生效；在此之前系统行为不变。"
        )

    async def handle_switch_provider(self, request):
        """切换单个模块的 provider：POST /api/config/switch-provider {module, provider}"""
        try:
            data = await request.json()
            module = data.get("module", "")
            provider = data.get("provider", "")
            if module not in ("ASR", "TTS", "LLM", "VLLM"):
                return web.json_response({"success": False, "error": f"不支持的模块: {module}"}, status=400)
            if provider not in (self.config.get(module) or {}):
                return web.json_response({"success": False, "error": f"{module} 下不存在 provider: {provider}"}, status=400)
            ok, msg = await self._switch_modules({module: provider})
            # 如实回报「实际生效的引擎」：ASR 的真实 type 由数据库优先规则决定，
            # 前端不能只看 success 就以为换好了（2026-10-05 修复盲信成功）
            actual_type = None
            if module == "ASR":
                actual_type = ((self.config.get(module) or {}).get(provider) or {}).get("type")
            if ok:
                return web.json_response(
                    {"success": True, "message": msg, "actual_type": actual_type}
                )
            return web.json_response(
                {"success": False, "message": msg, "error": msg, "actual_type": actual_type}
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"切换 provider 失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_switch_mode(self, request):
        """一键切换云端/本地全家桶：POST /api/config/switch-mode {mode: local|cloud}"""
        try:
            data = await request.json()
            mode = data.get("mode", "")
            if mode not in self._MODULE_MODE_PRESETS:
                return web.json_response({"success": False, "error": f"不支持的模式: {mode}（仅 local/cloud）"}, status=400)
            mapping = dict(self._MODULE_MODE_PRESETS[mode])
            if mode == "cloud":
                # 2026-09-28：云端预设已换豆包（key 内嵌 config.yaml），百炼 key 注入
                # 仅对仍引用百炼占位 provider 的旧配置有意义；无百炼 key 不再阻断切换。
                cloud_key = self._get_cloud_key_from_db()
                if not cloud_key:
                    self.logger.bind(tag=TAG).info("云端切换：无百炼 key，跳过占位注入（豆包 key 已内嵌）")
                else:
                    custom_path = os.path.join(os.getcwd(), "data/.config.yaml")
                    with open(custom_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    text = self._inject_cloud_keys(text, cloud_key)
                    with open(custom_path, "w", encoding="utf-8") as f:
                        f.write(text)
            ok, msg = await self._switch_modules(mapping)
            return web.json_response({"success": ok, "message": f"{mode}: {msg}", "mapping": mapping})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"切换模式失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_model_stack(self, request):
        """模型方案状态：GET /api/config/model-stack
        返回当前方案预设、各环节所选引擎+部署属性、联动配置状态。
        后台"语音引擎"卡片与"AI模型配置"页共用此数据源（2026-09-28）。"""
        try:
            from core.utils.model_stack import get_stack_state
            return web.json_response({"success": True, **get_stack_state(self.config)})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取模型方案状态失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_save_config(self, request):
        """保存配置文件（加固①：YAML 校验 + 陈旧快照防护 + 写前备份）"""
        try:
            config_content = await request.text()
            config_path = os.path.join(os.getcwd(), "data/.config.yaml")

            # 校验 1：必须是能解析为映射的合法 YAML（防止坏文件把服务写崩）
            import yaml as _yaml
            try:
                new_cfg = _yaml.safe_load(config_content)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"拒绝保存配置：YAML 语法错误 {e}")
                return web.Response(text=f"YAML 语法错误，已拒绝保存：{e}", status=400)
            if not isinstance(new_cfg, dict):
                self.logger.bind(tag=TAG).warning("拒绝保存配置：内容不是 YAML 映射")
                return web.Response(text="配置内容不是合法的 YAML 映射，已拒绝保存", status=400)

            old_text = ""
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    old_text = f.read()

            force = request.rel_url.query.get("force") == "1"
            expected_fp = (request.headers.get("X-Config-Fingerprint")
                           or request.rel_url.query.get("fingerprint"))

            # 校验 2：陈旧快照防护——编辑器是「读全文 → 改 → 整份写回」，
            # 若读之后、写之前文件被别处改过，直接写回会静默丢掉中间改动。
            if expected_fp and not force and expected_fp != self._config_fingerprint(old_text):
                self.logger.bind(tag=TAG).warning("拒绝保存配置：编辑器内容已过期（指纹不一致）")
                return web.Response(
                    text=("配置文件在你编辑期间已被其它操作修改，为避免覆盖丢失，已拒绝保存。"
                          "请刷新页面后重新编辑；若确认要用当前内容覆盖，请在保存时加参数 ?force=1"),
                    status=409,
                )

            # 校验 3：顶层键删除防护（给没带指纹的旧前端兜底）。
            # 陈旧快照最常见的表现就是把后来新增的整段顶层配置（如 system_aec）抹掉。
            if not force and not expected_fp:
                try:
                    old_cfg = _yaml.safe_load(old_text) or {}
                except Exception:
                    old_cfg = {}
                if isinstance(old_cfg, dict):
                    lost = [k for k in old_cfg if k not in new_cfg]
                    if lost:
                        shown = "、".join(lost[:5]) + (" 等" if len(lost) > 5 else "")
                        self.logger.bind(tag=TAG).warning(f"拒绝保存配置：将删除顶层配置 {lost}")
                        return web.Response(
                            text=(f"检测到本次保存会删除以下顶层配置：{shown}。"
                                  "这通常是编辑器内容过期（陈旧快照）导致，已拒绝保存。"
                                  "请刷新页面后重试；若确认要删除，请在保存时加参数 ?force=1"),
                            status=409,
                        )

            self._backup_config_file(config_path, old_text)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(config_content)
            self.logger.bind(tag=TAG).info("配置文件已保存")
            return web.Response(text="OK", status=200)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存配置文件失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_get_log(self, request):
        """获取系统日志"""
        try:
            log_path = "/tmp/server.log"
            if not os.path.exists(log_path):
                return web.Response(text="日志文件不存在", status=404)
            
            with open(log_path, "r", encoding="utf-8") as f:
                # 只返回最后 100 行
                lines = f.readlines()[-100:]
                content = ''.join(lines)
            return web.Response(text=content, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取日志失败：{e}")
            return web.Response(text=str(e), status=500)

    async def handle_save_tts(self, request):
        """保存 TTS 配置"""
        try:
            data = await request.json()
            engine = data.get("engine", "Edge")
            voice = data.get("voice", "zh-CN-XiaoxiaoNeural")
            
            # 读取当前配置
            config_path = os.path.join(os.getcwd(), "data/.config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            # 更新 TTS 配置
            # 加固②（2026-09-17）：原实现 re.sub(r'voice: .*', ...) 会把全文件每一行 voice: 都替换掉，
            # 导致其它 TTS 引擎（如 豆包TTS）的音色被连带覆盖。改为只写目标引擎块内的 voice。
            try:
                self._write_provider_fields_to_yaml(config_path, "TTS", engine, {"voice": voice})
                content = None          # 已由上面直接写盘
            except ValueError:
                # 该引擎在本文件里还没有独立块 → 追加一个后整体写回
                content = self._append_tts_engine_block(content, engine, voice)
            
            if content is not None:
                import yaml as _yaml
                _yaml.safe_load(content)          # 写前校验 YAML 合法
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(content)
            
            self.logger.bind(tag=TAG).info(f"TTS 配置已保存：{engine} - {voice}")
            return web.Response(text="OK", status=200)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存 TTS 配置失败：{e}")
            return web.Response(text=str(e), status=500)
    
    # ---------- TTS 音色列表：2026-09-13 新增（TTS 设置页音色卡片数据源） ----------
    # 静态音色表：{provider type: [(id, 名称, 描述), ...]}
    _TTS_VOICE_TABLES = {
        "alibl_stream": [
            ("longxiaochun_v2", "龙小淳", "阿里 CosyVoice 官方女声，温柔亲切"),
            ("longwan_v2", "龙婉", "阿里 CosyVoice 官方女声，成熟稳重"),
            ("longcheng_v2", "龙诚", "阿里 CosyVoice 官方男声，沉稳可靠"),
            ("longhua_v2", "龙桦", "阿里 CosyVoice 官方男声，清亮年轻"),
            ("longjielidou_v2", "龙杰力豆", "阿里 CosyVoice 官方童声"),
            ("longshuo_v2", "龙硕", "阿里 CosyVoice 官方男声，磁性解说"),
            ("longyue_v2", "龙悦", "阿里 CosyVoice 官方女声，活泼开朗"),
            ("longfei_v2", "龙飞", "阿里 CosyVoice 官方男声，新闻播报"),
            ("longluo_v2", "龙洛", "阿里 CosyVoice 官方女声，温婉柔和"),
            ("longxiang_v2", "龙翔", "阿里 CosyVoice 官方男声，潇洒随性"),
            ("longmiao_v2", "龙妙", "阿里 CosyVoice 官方女声，甜美可爱"),
            ("longyixiaoshu_v2", "龙易小树", "阿里 CosyVoice 官方童声，天真烂漫"),
        ],
        "edge": [
            ("zh-CN-XiaoxiaoNeural", "晓晓", "微软 Edge 女声，温暖自然"),
            ("zh-CN-XiaoyiNeural", "晓伊", "微软 Edge 女声，活泼轻快"),
            ("zh-CN-YunjianNeural", "云健", "微软 Edge 男声，运动解说风"),
            ("zh-CN-YunxiNeural", "云希", "微软 Edge 男声，年轻阳光"),
            ("zh-CN-YunyangNeural", "云扬", "微软 Edge 男声，新闻播报"),
            ("zh-CN-liaoning-XiaobeiNeural", "晓北", "微软 Edge 女声，东北口音"),
            ("zh-CN-shaanxi-XiaoniNeural", "晓妮", "微软 Edge 女声，陕西口音"),
            ("zh-HK-HiuMaanNeural", "晓曼", "微软 Edge 女声，粤语"),
            ("zh-TW-HsiaoChenNeural", "晓臻", "微软 Edge 女声，台湾口音"),
        ],
        "doubao": [
            ("BV001_streaming", "通用女声", "火山引擎官方通用女声"),
            ("BV002_streaming", "通用男声", "火山引擎官方通用男声"),
            ("BV700_streaming", "爽快思思", "火山引擎官方女声，爽快利落"),
            ("BV701_streaming", "温暖阿阳", "火山引擎官方男声，温暖磁性"),
        ],
        # 火山豆包语音合成 2.0（双向流式，resource_id=seed-tts-2.0）。
        # 2026-10-07：Mac Mini 全云端部署后，云端音色需要能在管理台挑选+试听。
        # 来源：官方 2.0 音色列表（koodo-reader 维护的中文名/性别对照）。
        "huoshan_double_stream": [
            ("zh_female_vv_uranus_bigtts", "Vivi 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_xiaohe_uranus_bigtts", "小何 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_m191_uranus_bigtts", "云舟 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_taocheng_uranus_bigtts", "小天 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_liufei_uranus_bigtts", "刘飞 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_sophie_uranus_bigtts", "魅力苏菲 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_qingxinnvsheng_uranus_bigtts", "清新女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_cancan_uranus_bigtts", "知性灿灿 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_sajiaoxuemei_uranus_bigtts", "撒娇学妹 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_tianmeixiaoyuan_uranus_bigtts", "甜美小源 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_tianmeitaozi_uranus_bigtts", "甜美桃子 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_shuangkuaisisi_uranus_bigtts", "爽快思思 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_peiqi_uranus_bigtts", "佩奇猪 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_linjianvhai_uranus_bigtts", "邻家女孩 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_shaonianzixin_uranus_bigtts", "少年梓辛/Brayan 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_sunwukong_uranus_bigtts", "猴哥 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_yingyujiaoxue_uranus_bigtts", "Tina老师 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_kefunvsheng_uranus_bigtts", "暖阳女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_xiaoxue_uranus_bigtts", "儿童绘本 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_dayi_uranus_bigtts", "大壹 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_mizai_uranus_bigtts", "黑猫侦探社咪仔 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_jitangnv_uranus_bigtts", "鸡汤女 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_meilinvyou_uranus_bigtts", "魅力女友 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_liuchangnv_uranus_bigtts", "流畅女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_ruyayichen_uranus_bigtts", "儒雅逸辰 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("en_male_tim_uranus_bigtts", "Tim", "火山豆包语音合成 2.0 · 男声"),
            ("en_female_dacey_uranus_bigtts", "Dacey", "火山豆包语音合成 2.0 · 女声"),
            ("en_female_stokie_uranus_bigtts", "Stokie", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_wenroumama_uranus_bigtts", "温柔妈妈 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_jieshuoxiaoming_uranus_bigtts", "解说小明 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_tvbnv_uranus_bigtts", "TVB女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_yizhipiannan_uranus_bigtts", "译制片男 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_qiaopinv_uranus_bigtts", "俏皮女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_zhishuaiyingzi_uranus_bigtts", "直率英子 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_linjiananhai_uranus_bigtts", "邻家男孩 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_silang_uranus_bigtts", "四郎 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_ruyaqingnian_uranus_bigtts", "儒雅青年 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_qingcang_uranus_bigtts", "擎苍 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_xionger_uranus_bigtts", "熊二 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_yingtaowanzi_uranus_bigtts", "樱桃丸子 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_wennuanahu_uranus_bigtts", "温暖阿虎/Alvin 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_naiqimengwa_uranus_bigtts", "奶气萌娃 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_popo_uranus_bigtts", "婆婆 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_gaolengyujie_uranus_bigtts", "高冷御姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_aojiaobazong_uranus_bigtts", "傲娇霸总 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_lanyinmianbao_uranus_bigtts", "懒音绵宝 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_wenroushunv_uranus_bigtts", "温柔淑女 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_gufengshaoyu_uranus_bigtts", "古风少御 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_huolixiaoge_uranus_bigtts", "活力小哥 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_baqiqingshu_uranus_bigtts", "霸气青叔 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_xuanyijieshuo_uranus_bigtts", "悬疑解说 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_mengyatou_uranus_bigtts", "萌丫头/Cutey 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_tiexinnvsheng_uranus_bigtts", "贴心女声/Candy 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_jitangmei_uranus_bigtts", "鸡汤妹妹", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_cixingjieshuonan_uranus_bigtts", "磁性解说男声/Morgan 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_liangsangmengzai_uranus_bigtts", "亮嗓萌仔 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_kailangjiejie_uranus_bigtts", "开朗姐姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_gaolengchenwen_uranus_bigtts", "高冷沉稳 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_shenyeboke_uranus_bigtts", "深夜播客 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_lubanqihao_uranus_bigtts", "鲁班七号 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_jiaochuannv_uranus_bigtts", "娇喘女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_linxiao_uranus_bigtts", "邻萧 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_lingling_uranus_bigtts", "玲玲姐姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_chunribu_uranus_bigtts", "春日部姐姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_tangseng_uranus_bigtts", "唐僧 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_zhuangzhou_uranus_bigtts", "庄周 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_kailangdidi_uranus_bigtts", "开朗弟弟 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_zhubajie_uranus_bigtts", "猪八戒 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_ganmaodianyin_uranus_bigtts", "感冒电音姐姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_chanmeinv_uranus_bigtts", "谄媚女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_nvleishen_uranus_bigtts", "女雷神 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_qinqienv_uranus_bigtts", "亲切女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_kuailexiaodong_uranus_bigtts", "快乐小东 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_kailangxuezhang_uranus_bigtts", "开朗学长 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_youyoujunzi_uranus_bigtts", "悠悠君子 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_wenjingmaomao_uranus_bigtts", "文静毛毛 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_zhixingnv_uranus_bigtts", "知性女声 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_qingshuangnanda_uranus_bigtts", "清爽男大 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_yuanboxiaoshu_uranus_bigtts", "渊博小叔 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_male_yangguangqingnian_uranus_bigtts", "阳光青年 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_qingchezizi_uranus_bigtts", "清澈梓梓 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_tianmeiyueyue_uranus_bigtts", "甜美悦悦 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_xinlingjitang_uranus_bigtts", "心灵鸡汤 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_wenrouxiaoge_uranus_bigtts", "温柔小哥 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_roumeinvyou_uranus_bigtts", "柔美女友 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_dongfanghaoran_uranus_bigtts", "东方浩然 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_wenrouxiaoya_uranus_bigtts", "温柔小雅 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_tiancaitongsheng_uranus_bigtts", "天才童声 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_wuzetian_uranus_bigtts", "武则天 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_female_gujie_uranus_bigtts", "顾姐 2.0", "火山豆包语音合成 2.0 · 女声"),
            ("zh_male_guanggaojieshuo_uranus_bigtts", "广告解说 2.0", "火山豆包语音合成 2.0 · 男声"),
            ("zh_female_shaoergushi_uranus_bigtts", "少儿故事 2.0", "火山豆包语音合成 2.0 · 女声"),
        ],
    }
    # MossNano 音色显示名（id → (名称, 描述)），完整列表动态取自 8086 /health
    # ⚠️ 只列8086 VOICE_PROMPTS 里真实存在的 id。历史上这里曾有 "eva" /
    # "中文女" 两条，但 8086 根本没有这些音色名——传进去会被静默兜底成
    # DEFAULT_VOICE（zh_1 男声），导致「以为在用女声、实际播出男声」。
    # 助理身份/唤醒词一律读后台「AI 身份设定」，不在这张表里。
    _MOSS_VOICE_NAMES = {
        "zh_1": ("俊浩（男声）", "OpenMOSS 原生中文男声"),
        "zh_3": ("志明（男声）", "OpenMOSS 原生中文男声"),
        "zh_10": ("卫国（男声）", "OpenMOSS 原生中文男声"),
        "zh_4": ("月文（女声）", "OpenMOSS 原生中文女声"),
        "zh_6": ("灵语（女声）", "OpenMOSS 原生中文女声"),
        "zh_11": ("小玉（女声）", "OpenMOSS 原生中文女声"),
    }

    async def handle_get_tts_voices(self, request):
        """GET /api/tts/voices?provider=<引擎名> - 返回该引擎可用音色列表

        响应里带 `field`：该引擎「音色」写在配置的哪个键上。
        前端「设为默认」必须按它写，否则写错键会被 provider-fields 拒绝
        （2026-10-07 主人实测：Qwen3 引擎写 voice，实际配置键是 speaker，
        报「没有可写入的字段」，看着就是「不能选默认值」）。
        """
        try:
            provider = request.query.get("provider", "")
            cfg = (self.config.get("TTS") or {}).get(provider) or {}
            ptype = cfg.get("type", "")
            voices = []
            field = "voice"
            if ptype == "qwen3_tts":
                field = "speaker"
                # Qwen3-TTS：中文预置音色 + 后台克隆出来的命名音色（音色文件见
                # tts-qwen3/cloned_voices.json，由「Qwen3-TTS 设置页」维护）
                presets = [
                    ("Serena", "Serena", "中文·女声（默认）"),
                    ("Vivian", "Vivian", "中文·女声"),
                    ("Uncle_Fu", "Uncle_Fu", "中文·男声"),
                    ("Dylan", "Dylan", "中文·北京话"),
                    ("Eric", "Eric", "中文·四川话"),
                ]
                for vid, name, desc in presets:
                    voices.append({"id": vid, "name": name, "desc": desc})
                try:
                    import json as _json

                    from core.utils.qwen3_assets import clone_file as _qclone

                    _f = _qclone()
                    if _f.exists():
                        for item in _json.loads(_f.read_text(encoding="utf-8")):
                            voices.append({"id": item.get("id", ""),
                                           "name": item.get("name", ""),
                                           "desc": "克隆音色"})
                except Exception as _e:
                    logger.warning(f"读取克隆音色列表失败: {_e}")
            elif ptype == "moss_nano":
                import aiohttp
                api_url = cfg.get("api_url", "http://127.0.0.1:8086/api/tts")
                from urllib.parse import urlsplit
                _u = urlsplit(api_url)
                health_url = f"{_u.scheme}://{_u.netloc}/health"
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            data = await r.json()
                    for vid in data.get("voices", []):
                        name, desc = self._MOSS_VOICE_NAMES.get(vid, (vid, "MOSS 参考音色"))
                        voices.append({"id": vid, "name": name, "desc": desc})
                except Exception as e:
                    return web.json_response(
                        {"success": True, "voices": [], "field": field,
                         "note": f"MossNano 服务未响应（{e}），请先在本地启动 8086 服务"}, status=200)
            elif ptype in self._TTS_VOICE_TABLES:
                if ptype == "huoshan_double_stream":
                    field = "speaker"
                voices = [{"id": v[0], "name": v[1], "desc": v[2]}
                          for v in self._TTS_VOICE_TABLES[ptype]]
            else:
                return web.json_response(
                    {"success": True, "voices": [], "field": field,
                     "note": "该引擎暂不支持音色浏览，请在下方配置字段中填写 voice"}, status=200)
            return web.json_response({"success": True, "voices": voices, "field": field})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    def _default_tts_preview_text(self):
        """试听/合成兜底文本：读后台助理名，长度足够听出音色特点（主人要求
        2026-10-06：不能只问候一句就完事）。读不到配置用中性措辞。"""
        try:
            from core.utils.assistant_identity import get_name
            name = (get_name() or "").strip()
        except Exception:
            name = ""
        if not name:
            return "你好，我是你的家庭助手，有什么需要我帮忙的吗？"
        return f"{name}，你好呀，我是你的家庭助手，有什么需要我帮忙的吗？"

    def _backend_moss_voice(self):
        """读后台主音色（devices.db/tts_config，provider=MossNano 时才认）。

        与 core/providers/tts/moss_nano.py:_get_current_voice 同一数据源，
        保证试听听到的和设备播报完全一致。读不到返回 None（交8086 用默认）。
        """
        try:
            import sqlite3
            from pathlib import Path as _P
            db = _P(__file__).resolve().parent.parent / "data" / "devices.db"
            conn = sqlite3.connect(str(db), timeout=2)
            try:
                rows = conn.execute(
                    "SELECT config_key, config_value FROM tts_config "
                    "WHERE config_key IN ('voice', 'provider')"
                ).fetchall()
            finally:
                conn.close()
            cfg = {str(k): ("" if v is None else str(v)) for k, v in rows}
            if cfg.get("provider", "").strip() == "MossNano":
                return cfg.get("voice", "").strip() or None
            return None
        except Exception:
            return None

    # ── Qwen3-TTS 设置页（2026-10-07）──────────────────────────────
    # 主人要求：引擎列表里 1.7B 后面能进一个独立页面，放音色/语言/情绪指令/
    # 音色设计/克隆。配置项本身复用「⚙️ 配置引擎参数」（provider-fields 已能改），
    # 这里补的是**克隆音色**与**音色设计**这两个需要落文件的能力。
    def _qwen3_paths(self):
        from core.utils.qwen3_assets import clone_dir, clone_file, find_tts_dir

        base = find_tts_dir()
        return base, clone_file(), clone_dir()

    def _load_qwen3_clones(self):
        import json as _json

        _base, _f, _ = self._qwen3_paths()
        try:
            if _f.exists():
                items = _json.loads(_f.read_text(encoding="utf-8"))
                return items if isinstance(items, list) else []
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"读取克隆音色表失败: {e}")
        return []

    def _save_qwen3_clones(self, items):
        import json as _json

        _base, _f, _ = self._qwen3_paths()
        _f.parent.mkdir(parents=True, exist_ok=True)
        _f.write_text(_json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def _qwen3_provider_cfg(self, provider: str) -> dict:
        cfg = dict((self.config.get("TTS") or {}).get(provider) or {})
        if str(cfg.get("type") or "") != "qwen3_tts":
            # provider 名传错/为空时，退一步找任意 qwen3_tts 引擎
            for _n, _c in (self.config.get("TTS") or {}).items():
                if isinstance(_c, dict) and _c.get("type") == "qwen3_tts":
                    return dict(_c)
        return cfg

    async def _qwen3_synth(self, cfg: dict, payload: dict):
        """调本地 8087 合成，返回 wav bytes；失败抛异常（带人话说明）。"""
        import aiohttp

        url = str(cfg.get("api_url") or "http://127.0.0.1:8087/api/tts")
        body = {
            "text": payload.get("text", ""),
            "model": payload.get("model") or cfg.get("model", "0.6B"),
            "mode": payload.get("mode") or cfg.get("mode", "custom"),
            "speaker": payload.get("speaker") or cfg.get("speaker", "Serena"),
            "language": payload.get("language") or cfg.get("language", "Chinese"),
            "instruct": payload.get("instruct", cfg.get("instruct", "")),
            "speed": cfg.get("speed", 1.0),
            "temperature": cfg.get("temperature", 0.9),
            "top_k": cfg.get("top_k", 50),
            "top_p": cfg.get("top_p", 1.0),
            "seed": cfg.get("seed", 1234),
        }
        for k in ("ref_audio", "ref_text"):
            if payload.get(k):
                body[k] = payload[k]
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    raise RuntimeError(f"8087 合成失败 HTTP {r.status}: {(await r.text())[:150]}")
                return await r.read()

    async def handle_qwen3_settings(self, request):
        """GET /api/tts/qwen3/settings?provider=Qwen3TTS-0.6B

        返回当前参数 + 预置音色 + 克隆音色 + 模型能力（caps）。
        caps 从 8087 /health 实时取，取不到用本地兜底表。
        """
        try:
            provider = request.query.get("provider", "")
            cfg = self._qwen3_provider_cfg(provider)
            if not cfg:
                return web.json_response({"success": False, "error": "未找到 Qwen3-TTS 引擎配置"}, status=400)
            presets = [
                ("Serena", "Serena", "中文·女声（默认）"),
                ("Vivian", "Vivian", "中文·女声"),
                ("Uncle_Fu", "Uncle_Fu", "中文·男声"),
                ("Dylan", "Dylan", "中文·北京话"),
                ("Eric", "Eric", "中文·四川话"),
            ]
            caps = {}
            try:
                import aiohttp

                _u = str(cfg.get("api_url") or "http://127.0.0.1:8087/api/tts")
                from urllib.parse import urlsplit

                _p = urlsplit(_u)
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"{_p.scheme}://{_p.netloc}/health",
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        caps = (await r.json()).get("caps", {})
            except Exception:
                caps = {}
            if not caps:
                caps = {
                    "0.6B": {"clone": False, "instruct": False},
                    "1.7B": {"clone": False, "instruct": True},
                    "0.6B-Base": {"clone": True, "instruct": False},
                }
            keys = ("model", "speaker", "language", "instruct", "temperature",
                    "top_k", "top_p", "seed", "speed", "mode")
            return web.json_response({
                "success": True,
                "settings": {k: cfg.get(k) for k in keys},
                "presets": [{"id": a, "name": b, "desc": c} for a, b, c in presets],
                "cloned": self._load_qwen3_clones(),
                "caps": caps,
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    def _qwen3_save_ref_audio(self, audio_b64: str, filename: str, voice_id: str) -> str:
        """把上传的参考音频转成 24kHz 单声道 16bit wav 落盘，返回绝对路径。

        ⚠️ 必须真转码：Qwen3 的参考音频要求 24kHz 单声道 PCM，
        直接存原始 mp3/m4a 会合成出乱跑的长音频（2026-10-07 实测）。
        """
        import base64 as _b64
        import io as _io

        _base, _f, _dir = self._qwen3_paths()
        _dir.mkdir(parents=True, exist_ok=True)
        out = _dir / f"{voice_id}.wav"
        raw = _b64.b64decode(audio_b64)
        from pydub import AudioSegment

        seg = AudioSegment.from_file(_io.BytesIO(raw), format=(filename or "").rsplit(".", 1)[-1] or None)
        seg = seg.set_frame_rate(24000).set_channels(1).set_sample_width(2)
        seg.export(str(out), format="wav")
        return str(out)

    async def handle_qwen3_clone(self, request):
        """POST /api/tts/qwen3/clone

        {provider, name, ref_text, audio_b64, filename, verify_text}
        流程：转码落盘 → 用 mode=clone 真合成一段验证（失败即回滚删文件）
        → 写入 cloned_voices.json → 返回试听音频 base64。
        ref_text 必填：没有对应文字时 15 字能合成出 23 秒乱跑音频（实测）。
        """
        import base64 as _b64

        saved = None
        try:
            import time

            data = await request.json()
            provider = data.get("provider", "")
            cfg = self._qwen3_provider_cfg(provider)
            name = str(data.get("name") or "").strip()
            ref_text = str(data.get("ref_text") or "").strip()
            audio_b64 = str(data.get("audio_b64") or "").strip()
            if not name:
                return web.json_response({"success": False, "error": "请填写音色名称"}, status=400)
            if not ref_text:
                return web.json_response(
                    {"success": False,
                     "error": "请填写这段录音对应的文字（必填）——没有它合成会乱跑"}, status=400)
            if not audio_b64:
                return web.json_response({"success": False, "error": "请先选择一段录音"}, status=400)
            import uuid as _uuid

            voice_id = "clone_" + _uuid.uuid4().hex[:8]
            try:
                saved = self._qwen3_save_ref_audio(audio_b64, data.get("filename", ""), voice_id)
            except Exception as e:
                return web.json_response(
                    {"success": False, "error": f"音频转码失败（需要 ffmpeg/pydub 可读的格式）: {e}"},
                    status=400)
            verify_text = str(data.get("verify_text") or "").strip() or \
                f"你好，我是{name}，很高兴认识你。"
            try:
                wav = await self._qwen3_synth(cfg, {
                    "text": verify_text, "mode": "clone", "model": "0.6B-Base",
                    "ref_audio": saved, "ref_text": ref_text,
                })
            except Exception as e:
                try:
                    os.remove(saved)
                except Exception:
                    pass
                return web.json_response(
                    {"success": False, "error": f"克隆验证合成失败：{e}"}, status=502)
            items = self._load_qwen3_clones()
            items = [i for i in items if i.get("id") != voice_id]
            items.append({"id": voice_id, "name": name, "ref_audio": saved,
                          "ref_text": ref_text, "verify_text": verify_text,
                          "created": time.strftime("%Y-%m-%d %H:%M:%S")})
            self._save_qwen3_clones(items)
            self.logger.bind(tag=TAG).info(f"Qwen3 克隆音色已保存: {name} ({voice_id})")
            return web.json_response({
                "success": True, "id": voice_id, "name": name,
                "preview_b64": _b64.b64encode(wav).decode(),
                "message": f"音色「{name}」已创建",
            })
        except Exception as e:
            if saved:
                try:
                    os.remove(saved)
                except Exception:
                    pass
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_qwen3_clone_delete(self, request):
        """POST /api/tts/qwen3/clone-delete  {id} —— 同时删参考音频文件。"""
        try:
            data = await request.json()
            vid = str(data.get("id") or "").strip()
            items = self._load_qwen3_clones()
            keep, removed = [], None
            for i in items:
                if i.get("id") == vid:
                    removed = i
                else:
                    keep.append(i)
            if removed is None:
                return web.json_response({"success": False, "error": "音色不存在"}, status=404)
            ref = str(removed.get("ref_audio") or "")
            _base, _f, _dir = self._qwen3_paths()
            try:
                if ref and os.path.abspath(ref).startswith(str(_dir)):
                    os.remove(ref)
            except Exception:
                pass
            self._save_qwen3_clones(keep)
            return web.json_response({"success": True, "message": "已删除"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_qwen3_design(self, request):
        """POST /api/tts/qwen3/design

        {provider, description, text} —— 用一句话描述「想要的音色」，
        走 mode=design 生成一段试听。返回 wav（前端直接播放）。
        """
        try:
            data = await request.json()
            cfg = self._qwen3_provider_cfg(data.get("provider", ""))
            desc = str(data.get("description") or "").strip()
            if not desc:
                return web.json_response({"success": False, "error": "请先描述你想要的音色"}, status=400)
            text = str(data.get("text") or "").strip() or \
                "你好，我是你的家庭助手，今天天气不错，有什么可以帮你的吗？"
            wav = await self._qwen3_synth(cfg, {
                "text": text, "mode": "design", "instruct": desc,
                # 音色设计必须用 VoiceDesign 模型（0.6B/1.7B CustomVoice 不支持）
                "model": data.get("model") or "1.7B-Design",
            })
            return web.Response(body=wav, content_type="audio/wav")
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_tts_preview(self, request):
        """TTS 音色试听【2026-09-13 扩展】按引擎分派：moss_nano / edge / alibl_stream"""
        try:
            import io as _io
            data = await request.json()
            engine = (data.get("engine") or "MossNano").lower()
            # 允许直接传 provider 名（如「火山双流TTS」）——解析成它的 type 再分派。
            # 前端音色网格手上只有 provider 名，这样它不必再记一份 type 映射（2026-10-07）。
            _prov_cfg = (self.config.get("TTS") or {}).get(data.get("engine") or "")
            if isinstance(_prov_cfg, dict) and _prov_cfg.get("type"):
                engine = str(_prov_cfg["type"]).lower()
            text = data.get("text") or self._default_tts_preview_text()
            voice = data.get("voice")
            try:
                speed = float(data.get("speed") or 1.0)
                volume = float(data.get("volume") or 5.0)
            except (TypeError, ValueError):
                speed, volume = 1.0, 5.0

            audio_data = None
            if engine in ("moss", "mossnano", "moss_nano"):
                import aiohttp
                # 不传 voice 时读后台主音色（与实际播报同源）。绝不能写死一个
                # 8086 不认识的音色名——会被静默兜底成默认男声。
                if not voice:
                    voice = self._backend_moss_voice()
                payload = {"text": text, "speed": speed, "volume": volume}
                if voice:
                    payload["voice"] = voice
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                            "http://127.0.0.1:8086/api/tts",
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status != 200:
                            return web.json_response(
                                {"error": f"MOSS 8086 合成失败 HTTP {r.status}"}, status=502)
                        audio_data = await r.read()
            elif engine in ("edge", "edgetts", "edge-tts", "edgettshttp"):
                import edge_tts
                from pydub import AudioSegment
                mp3_buf = bytearray()
                communicate = edge_tts.Communicate(text, voice or "zh-CN-XiaoxiaoNeural")
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        mp3_buf.extend(chunk["data"])
                if not mp3_buf:
                    return web.json_response({"error": "Edge 合成无音频返回"}, status=502)
                seg = AudioSegment.from_file(_io.BytesIO(bytes(mp3_buf)), format="mp3")
                seg = seg.set_channels(1).set_frame_rate(16000).set_sample_width(2)
                wav_io = _io.BytesIO()
                seg.export(wav_io, format="wav")
                audio_data = wav_io.getvalue()
            elif engine in ("alibl", "alibl_stream", "customtts", "alibltts"):
                # 一次性合成：独立实例 + 最小 stub conn，不碰运行中的连接
                from core.providers.tts.alibl_stream import TTSProvider as AliBlProvider
                from core.utils import opus_encoder_utils
                from core.utils.util import opus_datas_to_wav_bytes
                tts_cfg = None
                for _name, _c in (self.config.get("TTS") or {}).items():
                    if isinstance(_c, dict) and _c.get("type") == "alibl_stream":
                        tts_cfg = dict(_c)
                        break
                if not tts_cfg:
                    return web.json_response({"error": "未找到 alibl_stream 引擎配置"}, status=400)
                if voice:
                    tts_cfg["voice"] = voice
                try:
                    prov = AliBlProvider(tts_cfg, delete_audio_file=True)
                except Exception as e:
                    return web.json_response({"error": f"引擎初始化失败: {e}"}, status=400)

                class _StubConn:
                    sample_rate = 16000
                    client_abort = False
                    stop_event = None

                prov.conn = _StubConn()
                prov.opus_encoder = opus_encoder_utils.OpusEncoderUtils(16000, 1, 60)
                opus_list = await asyncio.get_running_loop().run_in_executor(None, prov.to_tts, text)
                if not opus_list:
                    return web.json_response(
                        {"error": "AliBLTTS 合成失败（请检查该引擎的 ws_url/api_key 是否已配置）"}, status=502)
                audio_data = opus_datas_to_wav_bytes(opus_list, sample_rate=16000)
            elif engine in ("huoshan", "huoshan_double_stream", "huoshantts"):
                # 火山豆包 2.0 双向流式：复用流式路径（api_key 鉴权 + StartConnection 握手），
                # 用最小 stub conn 承接音频，不碰运行中的连接。
                # ⚠️ 不能用本类的 to_tts()：那是旧 appid/token 鉴权且缺 StartConnection，
                # 对 seed-tts-2.0 plan 端点不可用（2026-10-07 实测确认）。
                import io as _io2
                import threading as _th
                import time as _tm
                import uuid as _uuid
                import wave as _wave
                from core.providers.tts.huoshan_double_stream import TTSProvider as HuoshanProvider
                tts_cfg = None
                for _name, _c in (self.config.get("TTS") or {}).items():
                    if isinstance(_c, dict) and _c.get("type") == "huoshan_double_stream":
                        tts_cfg = dict(_c)
                        break
                if not tts_cfg:
                    return web.json_response({"error": "未找到 huoshan_double_stream 引擎配置"}, status=400)
                if voice:
                    tts_cfg["speaker"] = voice
                try:
                    prov = HuoshanProvider(tts_cfg, delete_audio_file=True)
                except Exception as e:
                    return web.json_response({"error": f"引擎初始化失败: {e}"}, status=400)

                sid = "preview" + _uuid.uuid4().hex
                captured = bytearray()
                finished = _th.Event()

                class _StubConn:
                    audio_format = "pcm"
                    sample_rate = 16000
                    client_abort = False
                    active = True
                    tts_MessageText = None

                    def __init__(self):
                        self.stop_event = _th.Event()
                        self.sentence_id = sid

                    def __getattr__(self, _n):
                        return None

                conn = _StubConn()

                def _capture(pcm_data, end_of_stream=False):
                    if pcm_data:
                        captured.extend(pcm_data)
                    if end_of_stream:
                        finished.set()

                prov.handle_pcm = _capture
                try:
                    await prov.open_audio_channels(conn)
                    await prov.start_session(sid)
                    await prov.send_text(prov.voice, text, sid)
                    await asyncio.sleep(0.4)
                    await prov.finish_session(sid)
                    _deadline = _tm.time() + 20
                    while _tm.time() < _deadline and not finished.is_set():
                        await asyncio.sleep(0.1)
                finally:
                    try:
                        conn.stop_event.set()
                    except Exception:
                        pass
                    try:
                        await prov.close()
                    except Exception:
                        pass
                if not captured:
                    return web.json_response(
                        {"error": "火山双流TTS 合成失败（请检查该引擎的 api_key / ws_url / resource_id）"},
                        status=502)
                wav_io = _io2.BytesIO()
                with _wave.open(wav_io, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(bytes(captured))
                audio_data = wav_io.getvalue()
            elif engine in ("qwen3", "qwen3_tts", "qwen3tts"):
                # Qwen3-TTS（本地 8087）：直接走 provider 同款 HTTP 接口，
                # 试听听到的就是设备实际播报的音色（含 instruct/采样参数）。
                import aiohttp
                tts_cfg = None
                for _name, _c in (self.config.get("TTS") or {}).items():
                    if isinstance(_c, dict) and _c.get("type") == "qwen3_tts":
                        tts_cfg = dict(_c)
                        break
                if not tts_cfg:
                    return web.json_response({"error": "未找到 qwen3_tts 引擎配置"}, status=400)
                if voice:
                    tts_cfg["speaker"] = voice
                api_url = tts_cfg.get("api_url", "http://127.0.0.1:8087/api/tts")
                # 克隆音色要走 mode=clone + 0.6B-Base + 该音色的参考音频/文字，
                # 否则服务端把 clone_xxx 当预置音色名 → 报错（与 provider 同逻辑）
                _mode = tts_cfg.get("mode", "custom")
                _model = tts_cfg.get("model", "0.6B")
                _ref_audio = ""
                _ref_text = ""
                try:
                    from core.utils.qwen3_assets import clone_file as _qclone
                    import json as _json

                    if _qclone().exists():
                        for _it in _json.loads(_qclone().read_text(encoding="utf-8")):
                            if _it.get("id") == voice:
                                _mode = "clone"
                                _model = "0.6B-Base"
                                _ref_audio = str(_it.get("ref_audio") or "")
                                _ref_text = str(_it.get("ref_text") or "")
                                break
                except Exception:
                    pass
                payload = {
                    "text": text,
                    "model": _model,
                    "mode": _mode,
                    "speaker": tts_cfg.get("speaker", "Serena"),
                    "language": tts_cfg.get("language", "Chinese"),
                    "instruct": tts_cfg.get("instruct", ""),
                    "speed": tts_cfg.get("speed", 1.0),
                    "temperature": tts_cfg.get("temperature", 0.9),
                    "top_k": tts_cfg.get("top_k", 50),
                    "top_p": tts_cfg.get("top_p", 1.0),
                    "seed": tts_cfg.get("seed", 1234),
                    "ref_audio": _ref_audio,
                    "ref_text": _ref_text,
                }
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.post(api_url, json=payload,
                                          timeout=aiohttp.ClientTimeout(total=60)) as r:
                            if r.status != 200:
                                body = (await r.text())[:150]
                                return web.json_response(
                                    {"error": f"Qwen3-TTS 8087 合成失败 HTTP {r.status}: {body}"},
                                    status=502)
                            audio_data = await r.read()
                except Exception as e:
                    return web.json_response(
                        {"error": f"Qwen3-TTS 服务未响应（请确认 8087 服务已启动）：{e}"},
                        status=502)
            else:
                return web.json_response(
                    {"error": f"引擎 {engine} 暂不支持在线试听"}, status=400)

            if not audio_data:
                return web.json_response({"error": "音频文件生成失败"}, status=500)
            self.logger.bind(tag=TAG).info(f"TTS 试听生成成功：{engine}，{len(audio_data)}B")
            return web.Response(body=audio_data, content_type="audio/wav")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"TTS 试听生成失败：{e}")
            import traceback
            traceback.print_exc()
            return web.json_response({"error": str(e)}, status=500)

    async def handle_get_health_data(self, request):
        """获取健康数据"""
        try:
            date = request.query.get("date")
            data = get_health_data(date)
            return web.json_response({"success": True, "data": data})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_clear_cache(self, request):
        """清理缓存"""
        try:
            result = clear_cache()
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_restart_server(self, request):
        """重启服务"""
        try:
            result = restart_server()
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_health_check(self, request):
        """健康检查：返回服务存活状态与核心组件就绪情况"""
        import time as _time
        # WebSocketServer 实例由 app.py 注入到 http_server 的 self.server
        # （见 app.py: ota_server.server = ws_server）。
        # 原代码取的是 self.websocket_server —— 该属性根本不存在，
        # getattr 恒返回 None，所以 components 永远是 null（2026-10-06 修）。
        # 组件属性名带下划线：_asr / _llm / _vad / _tts / _memory
        # （见 core/websocket_server.py:68-72）
        srv = getattr(self, "server", None)
        components = {
            "asr": bool(getattr(srv, "_asr", None)) if srv else None,
            "llm": bool(getattr(srv, "_llm", None)) if srv else None,
            "vad": bool(getattr(srv, "_vad", None)) if srv else None,
            "memory": bool(getattr(srv, "_memory", None)) if srv else None,
            # 注意：不列 tts——WebSocketServer 构造函数里根本没设 _tts
            # （只有 update_config 热更新路径才设），列了会永远误报 false
        }
        return web.json_response({
            "success": True,
            "status": "ok",
            "timestamp": int(_time.time()),
            "components": components,
        })

    async def handle_server_info(self, request):
        """获取服务器信息"""
        import os
        import time

        # 本机网卡 IP：UDP 探测真实出口地址。
        # 与「对外 IP」（config server.local_ip，设备连的那个）是两个概念，
        # 这里只回答「这台机器的网卡地址是什么」（2026-10-06 主人纠正过：
        # 在开发机上把配置里的 .214 当成本机 IP 显示是错的，本机应为 .209）
        try:
            import socket as _socket
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            real_ip = _s.getsockname()[0]
            _s.close()
        except Exception:
            real_ip = "127.0.0.1"

        # 获取进程 PID
        pid = os.getpid()

        # 获取运行时间（从进程启动时间计算）
        # 2026-10-06 修：原来用 `'psutil' in globals()` 判断，但本模块从未 import psutil，
        # 三元永远走 time.time()-3600 分支 → 面板「运行时长」恒定显示 1h 0m
        uptime_seconds = None
        try:
            import psutil
            create_time = psutil.Process(pid).create_time()
            uptime_seconds = int(time.time() - create_time)
        except Exception:
            pass
        if uptime_seconds is None:
            uptime_str = "--"
        else:
            days = uptime_seconds // 86400
            hours = (uptime_seconds % 86400) // 3600
            minutes = (uptime_seconds % 3600) // 60
            parts = []
            if days:
                parts.append(f"{days}天")
            if days or hours:
                parts.append(f"{hours}小时")
            parts.append(f"{minutes}分钟")
            uptime_str = " ".join(parts)
        
        server_config = self.config.get("server", {}) or {}
        return web.json_response({
            # 只返回本机网卡实测 IP。原先还返回 config 里的 server.local_ip，
            # 那是「别的机器的地址」（开发机上是从 Mac Mini 同步来的 .214），
            # 显示出来会让人误以为是本机状态（主人 2026-10-06纠正）
            "realIP": real_ip,
            "port": server_config.get("http_port", 8003),
            "pid": pid,
            "uptime": uptime_str
        })

    async def handle_chat(self, request):
        """处理聊天请求 - 用于前端测试 LLM 回复"""
        try:
            data = await request.json()
            message = data.get("message", "")
            device_id = data.get("device_id")
            
            if not message:
                return web.json_response({"success": False, "error": "消息不能为空"}, status=400)
            
            self.logger.bind(tag=TAG).info(f"收到聊天请求: {message}")
            
            # 获取 LLM 配置
            # selected_module.LLM 是提供者名称（如 "GLM47FlashLLM"）
            selected_module = self.config.get("selected_module", {})
            llm_provider_name = selected_module.get("LLM", "GLM47FlashLLM")
            llm_config = self.config.get("LLM", {}).get(llm_provider_name, {})
            llm_type = llm_config.get("type", "openai")
            
            self.logger.bind(tag=TAG).info(f"使用LLM: {llm_provider_name}, type: {llm_type}")
            
            # 动态导入 LLM 提供者
            try:
                # OpenAI 类型的提供者都使用 openai.py
                module_path = "core.providers.llm.openai.openai"
                if llm_type == "AliBL":
                    module_path = "core.providers.llm.AliBL.AliBL"
                elif llm_type == "ollama":
                    module_path = "core.providers.llm.ollama.ollama"
                elif llm_type == "dify":
                    module_path = "core.providers.llm.dify.dify"
                elif llm_type == "gemini":
                    module_path = "core.providers.llm.gemini.gemini"
                
                import importlib
                module = importlib.import_module(module_path)
                llm_provider = module.LLMProvider(llm_config)
                
                # 使用与 ESP32 相同的时间处理逻辑
                from core.utils.current_time import get_current_time_info
                current_time, today_date, today_weekday, lunar_date = get_current_time_info()
                
                # 构建带上下文的基础提示（与 intentHandler.py 相同）
                context_prompt = f"""当前时间：{current_time}
今天日期：{today_date} ({today_weekday})
今天农历：{lunar_date}

请根据以上信息回答用户的问题：{message}"""
                
                # 调用 LLM
                response_text = llm_provider.response_no_stream(
                    system_prompt="你是 homAi 智能助手，负责帮助用户控制智能家居设备。",
                    user_prompt=context_prompt
                )
                
                self.logger.bind(tag=TAG).info(f"LLM回复: {response_text}")
                return web.json_response({"success": True, "reply": response_text})
                
            except Exception as llm_error:
                self.logger.bind(tag=TAG).error(f"LLM调用失败: {llm_error}")
                import traceback
                self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
                return web.json_response({"success": False, "error": f"LLM调用失败: {str(llm_error)}"}, status=500)
                
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理聊天请求失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_llm_test(self, request):
        """测试 LLM API 连接 - 从服务器端测试，避免 CORS 问题"""
        try:
            data = await request.json()
            base_url = data.get("base_url", "")
            api_key = data.get("api_key", "")
            model = data.get("model", "")
            
            if not base_url or not api_key or not model:
                return web.json_response({"success": False, "error": "缺少必要参数"}, status=400)
            
            self.logger.bind(tag=TAG).info(f"测试 LLM 连接: {base_url}, model: {model}")
            
            import aiohttp
            test_url = base_url.rstrip('/') + '/chat/completions'
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1
            }
            
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(test_url, headers=headers, json=payload) as resp:
                    status = resp.status
                    if status == 200 or status == 400:
                        return web.json_response({"success": True, "message": "连接成功，API 可用"})
                    elif status == 401:
                        return web.json_response({"success": False, "error": "API Key 无效 (401)"})
                    else:
                        text = await resp.text()
                        return web.json_response({"success": False, "error": f"连接失败: {status}, {text[:200]}"})
                        
        except aiohttp.ClientError as e:
            self.logger.bind(tag=TAG).error(f"LLM 连接测试失败: {e}")
            return web.json_response({"success": False, "error": f"网络错误: {str(e)}"})
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 测试异常: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_get_ha_config(self, request):
        """获取 Home Assistant 配置"""
        return await get_ha_config(request)

    @staticmethod
    def _memory_db():
        """手动记忆条目表（devices.db），首次调用自动建表"""
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        db_path = _Path(__file__).parent.parent / "data" / "devices.db"
        conn = _sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = _sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            title TEXT,
            content TEXT,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        return conn

    async def handle_ragflow_search(self, request):
        """知识库搜索代理：转发到 RAGFlow retrieval API，返回 {success, results:[{title,content,source}]}"""
        import asyncio as _asyncio
        import aiohttp as _aiohttp
        try:
            keyword = request.query.get("q", "").strip()
            if not keyword:
                return web.json_response({"success": False, "message": "缺少搜索词 q"}, status=400)
            cfg = self.config.get("plugins", {}).get("search_from_ragflow", {})
            base_url = (cfg.get("base_url") or "").rstrip("/")
            api_key = cfg.get("api_key", "")
            dataset_ids = cfg.get("dataset_ids") or []
            if not base_url or "你的" in api_key or api_key.endswith("-xxx") or not dataset_ids:
                return web.json_response({
                    "success": False,
                    "message": "RAGFlow 未配置（config.yaml plugins.search_from_ragflow 需填真实 base_url/api_key/dataset_ids）",
                }, status=400)
            payload = {
                "question": keyword,
                "dataset_ids": dataset_ids,
                "top_k": 5,
                "similarity_threshold": 0.2,
            }
            async def _do():
                async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=8)) as s:
                    async with s.post(f"{base_url}/api/v1/retrieval", json=payload,
                                      headers={"Authorization": f"Bearer {api_key}"}) as resp:
                        return await resp.json(content_type=None)
            raw = await _do()
            chunks = (raw or {}).get("data", {}).get("chunks", [])
            results = [{
                "title": (c.get("document_keyword") or c.get("docnm_kwd") or "知识片段"),
                "content": c.get("content") or c.get("content_with_weight") or "",
                "source": c.get("document_id") or "",
                "similarity": c.get("similarity"),
            } for c in chunks]
            return web.json_response({"success": True, "results": results})
        except Exception as e:
            return web.json_response({"success": False, "message": f"知识库搜索失败: {e}"}, status=500)

    async def handle_memory_list(self, request):
        try:
            conn = self._memory_db()
            rows = conn.execute("SELECT * FROM memory_notes WHERE active=1 ORDER BY updated_at DESC").fetchall()
            conn.close()
            memories = [{
                "id": r["id"], "key": r["key"], "title": r["title"] or r["key"],
                "content": r["content"] or "", "value": r["content"] or "",
                "timestamp": r["updated_at"], "created_at": r["created_at"],
                "active": bool(r["active"]),
            } for r in rows]
            return web.json_response({"success": True, "memories": memories})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_memory_save(self, request):
        try:
            body = await request.json()
            key = (body.get("key") or "").strip()
            value = (body.get("value") or body.get("content") or "").strip()
            if not key or not value:
                return web.json_response({"success": False, "message": "key 和 value 不能为空"}, status=400)
            conn = self._memory_db()
            conn.execute(
                "INSERT INTO memory_notes (key, title, content) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING", (key, key, value))
            # 同 key 覆盖更新（按 key 幂等）
            conn.execute(
                "UPDATE memory_notes SET content=?, title=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
                (value, key, key))
            conn.commit()
            conn.close()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_memory_delete(self, request):
        try:
            memory_id = request.match_info["memory_id"]
            conn = self._memory_db()
            if memory_id.isdigit():
                conn.execute("DELETE FROM memory_notes WHERE id=?", (int(memory_id),))
            else:
                conn.execute("DELETE FROM memory_notes WHERE key=?", (memory_id,))
            conn.commit()
            conn.close()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_save_ai_butler_config(self, request):
        """保存 AI 管家配置（前端传 {configs: {key: value}}），写入 devices.db 的 ai_butler_config"""
        import json as _json
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        try:
            body = await request.json()
            configs = body.get("configs") or {}
            if not isinstance(configs, dict) or not configs:
                return web.json_response({"success": False, "message": "configs 不能为空"}, status=400)
            db_path = _Path(__file__).parent.parent / "data" / "devices.db"
            conn = _sqlite3.connect(str(db_path), timeout=30)
            try:
                cur = conn.cursor()
                cur.execute("""CREATE TABLE IF NOT EXISTS ai_butler_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT NOT NULL UNIQUE,
                    config_value TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                for k, v in configs.items():
                    val = "true" if v is True else ("false" if v is False else str(v))
                    cur.execute(
                        "INSERT INTO ai_butler_config (config_key, config_value) VALUES (?, ?) "
                        "ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, "
                        "updated_at=CURRENT_TIMESTAMP", (k, val))
                conn.commit()
            finally:
                conn.close()

            # 后台人设是唯一权威源：保存后让身份缓存失效，并异步同步到 Ltm，
            # 保证 Ltm 的 identity.md / soul.md 与后台设置一致
            if any(k in configs for k in ("name", "nickname", "soul", "identity")):
                try:
                    from core.utils.assistant_identity import invalidate_cache
                    from core.utils.ltm_sync import sync_assistant_identity_to_LTM

                    invalidate_cache()
                    import asyncio as _asyncio
                    _asyncio.get_running_loop().run_in_executor(
                        None, sync_assistant_identity_to_LTM
                    )
                except Exception as _sync_err:
                    self.logger.bind(tag="ai_butler_config").warning(
                        f"同步人设到 Ltm 失败（不影响保存）: {_sync_err}"
                    )

            # 唤醒词热更新（2026-10-13 修复）：后台改 wakeWord/name 等键后不必重启服务。
            # 历史坑：10-10 曾把同样的热更新加进 config_api.py:save_ai_butler_configs，
            # 但那份是死代码（register_routes 从未被调用，路由真身在本文件 L660），
            # 且变量名写错（request_data）NameError 被静默吞 —— 热更新从未生效过。
            # 主人 10-13 实测：后台把唤醒词从「汤圆」改成「伊娃」后说新词永远不醒。
            try:
                if set(configs) & {'wakeWord', 'enable_wakeWord', 'wakeword_session_timeout',
                                   'enable_auto_end_dialogue', 'name', 'nickname'}:
                    from core.utils.wakeword_provider import reload_wakeword_detector
                    reload_wakeword_detector()
                    self.logger.bind(tag="ai_butler_config").info("已热更新唤醒词检测器")
            except Exception as _wake_err:
                self.logger.bind(tag="ai_butler_config").warning(
                    f"唤醒词热更新失败（不影响保存，重启后生效）: {_wake_err}"
                )

            return web.json_response({"success": True, "saved": len(configs)})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    async def handle_save_ha_config(self, request):
        """保存 Home Assistant 配置"""
        return await save_ha_config(request)

    async def handle_test_ha_connection(self, request):
        """测试 Home Assistant 连接"""
        return await test_ha_connection(request)

    async def handle_get_music_config(self, request):
        """获取音乐配置"""
        from core.api.music_config_api import get_music_config
        return await get_music_config(request)

    async def handle_save_music_config(self, request):
        """保存音乐配置"""
        from core.api.music_config_api import save_music_config
        return await save_music_config(request)

    async def handle_scan_ha_entities(self, request):
        """扫描HA实体"""
        from core.api.music_config_api import scan_ha_entities
        return await scan_ha_entities(request)

    async def handle_test_ha_connection_music(self, request):
        """测试HA连接（音乐配置专用）"""
        from core.api.music_config_api import test_ha_connection
        return await test_ha_connection(request)

    async def handle_get_music_rooms(self, request):
        """获取音乐房间列表"""
        from core.api.music_config_api import get_music_rooms
        return await get_music_rooms(request)

    async def handle_add_music_room(self, request):
        """添加音乐房间"""
        from core.api.music_config_api import add_music_room
        return await add_music_room(request)

    async def handle_update_music_room(self, request):
        """更新音乐房间"""
        from core.api.music_config_api import update_music_room
        return await update_music_room(request)

    async def handle_delete_music_room(self, request):
        """删除音乐房间"""
        from core.api.music_config_api import delete_music_room
        return await delete_music_room(request)
    
