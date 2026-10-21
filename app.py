import sys
import uuid
import signal
import asyncio
import os
import json
import sqlite3
from core.utils.net_env import ensure_local_no_proxy

ensure_local_no_proxy()  # 必须早于任何 httpx/openai 客户端创建

from aioconsole import ainput
from config.settings import load_config
from config.logger import setup_logging
from core.utils.util import get_local_ip, validate_mcp_endpoint
from core.http_server import SimpleHttpServer
from core.websocket_server import WebSocketServer
from core.utils.util import check_ffmpeg_installed
from core.utils.gc_manager import get_gc_manager

TAG = __name__
logger = setup_logging()


def setup_ha_environment():
    """从数据库读取 HomeAssistant 配置并设置为环境变量"""
    try:
        db_path = os.path.join(os.path.dirname(__file__), 'data', 'devices.db')
        if not os.path.exists(db_path):
            logger.bind(tag=TAG).warning(f"数据库文件不存在: {db_path}")
            return

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # system_config 表列名统一为 config_key/config_value（与 console_api/music_config_api 一致）
        cursor.execute("SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'ha_%'")
        rows = cursor.fetchall()
        conn.close()

        config = {}
        for key, value in rows:
            config[key] = value

        host = config.get('ha_host', 'YOUR_HA_HOST')
        port = config.get('ha_port', '8123')
        token = config.get('ha_token', '')

        if host and token:
            # 构建正确的 base_url，处理 host 可能包含协议前缀的情况
            if host.startswith(('http://', 'https://')):
                # host 已经包含协议前缀，直接使用
                base_url = host.rstrip('/')
            else:
                # host 只是 IP 或域名，添加 http:// 前缀
                base_url = f"http://{host}"
            
            # 如果端口不是默认的 80/443，添加端口
            if port and port not in ('80', '443', 80, 443):
                base_url = f"{base_url}:{port}"
            
            # 设置环境变量供 mcporter 使用
            os.environ['HASS_BASE_URL'] = base_url
            os.environ['HASS_ACCESS_TOKEN'] = token

            logger.bind(tag=TAG).info(f"已设置 HomeAssistant 环境变量: HASS_BASE_URL={os.environ['HASS_BASE_URL']}")
        else:
            logger.bind(tag=TAG).warning("HomeAssistant 配置不完整，跳过环境变量设置")

    except Exception as e:
        logger.bind(tag=TAG).error(f"设置 HomeAssistant 环境变量失败: {e}")


# 在启动时设置 HA 环境变量
setup_ha_environment()


async def wait_for_exit() -> None:
    """
    阻塞直到收到 Ctrl‑C / SIGTERM。
    - Unix: 使用 add_signal_handler
    - Windows: 依赖 KeyboardInterrupt
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":  # Unix / macOS
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        # Windows：await一个永远pending的fut，
        # 让 KeyboardInterrupt 冒泡到 asyncio.run，以此消除遗留普通线程导致进程退出阻塞的问题
        try:
            await asyncio.Future()
        except KeyboardInterrupt:  # Ctrl‑C
            pass


async def monitor_stdin():
    """监控标准输入，消费回车键"""
    while True:
        await ainput()  # 异步等待输入，消费回车


async def main():
    check_ffmpeg_installed()
    config = load_config()

    # auth_key优先级：配置文件server.auth_key > manager-api.secret > 自动生成
    # auth_key用于jwt认证，比如视觉分析接口的jwt认证、ota接口的token生成与websocket认证
    # 获取配置文件中的auth_key
    auth_key = config["server"].get("auth_key", "")
    
    # 验证auth_key，无效则尝试使用manager-api.secret
    if not auth_key or len(auth_key) == 0 or "你" in auth_key:
        auth_key = config.get("manager-api", {}).get("secret", "")
        # 验证secret，无效则生成随机密钥
        if not auth_key or len(auth_key) == 0 or "你" in auth_key:
            auth_key = str(uuid.uuid4().hex)
    
    config["server"]["auth_key"] = auth_key

    # 启动前检查端口是否被占用（从配置读取，避免硬编码）
    import socket
    _srv_cfg = config.get("server", {}) if isinstance(config.get("server"), dict) else {}
    for check_port in {int(_srv_cfg.get("port", 8000)), int(_srv_cfg.get("http_port", 8003))}:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', check_port))
        sock.close()
        if result == 0:
            logger.bind(tag=TAG).error(f"端口 {check_port} 已被占用，可能有残留进程，请先杀掉旧进程")
            import sys
            sys.exit(1)

    # 添加 stdin 监控任务
    stdin_task = asyncio.create_task(monitor_stdin())

    # 启动全局GC管理器（5分钟清理一次）
    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    # ========================================================================
    # 禁止修改：健康数据定时同步任务
    # 功能：每小时从 OneNET 平台同步健康数据到本地数据库
    # 负责人：bot-eva
    # 修改需获得 bot-eva 同意
    # 后续计划：部署到网站后，采用 OneNET 平台主动推送逻辑替代定时拉取
    # ========================================================================
    async def sync_health_data_periodically():
        """每小时同步一次健康数据"""
        import aiohttp
        while True:
            try:
                await asyncio.sleep(3600)  # 1小时 = 3600秒
                async with aiohttp.ClientSession() as session:
                    async with session.get('http://localhost:8003/api/onenet/sync') as resp:
                        if resp.status == 200:
                            logger.bind(tag=TAG).info("健康数据定时同步成功")
                        else:
                            logger.bind(tag=TAG).warning(f"健康数据定时同步失败: HTTP {resp.status}")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"健康数据定时同步异常: {e}")

    # 启动健康数据定时同步任务
    health_sync_task = asyncio.create_task(sync_health_data_periodically())
    logger.bind(tag=TAG).info("健康数据定时同步任务已启动（每小时一次）")

    # ── 数据采集：旁路同步对话历史（统计用，2026-09-26 主人确认）────────────
    # 只为开发端分析用；不改对话链路，只把已有的 dialogue_history 搬一份到
    # interaction_log。任何失败都只记日志，不影响主流程。
    async def sync_interaction_log_periodically():
        """每 10 分钟把 dialogue_history 旁路同步到 interaction_log（幂等）"""
        from core.utils import data_collector
        while True:
            try:
                await asyncio.sleep(600)
                added = await asyncio.to_thread(
                    data_collector.sync_from_dialogue_history)
                if added:
                    logger.bind(tag=TAG).info(f"[数据采集] 同步 {added} 条对话到 interaction_log")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"[数据采集] 周期同步异常（忽略）: {e}")

    interaction_sync_task = asyncio.create_task(sync_interaction_log_periodically())
    logger.bind(tag=TAG).info("数据采集旁路同步任务已启动（每 10 分钟一次）")
    # ========================================================================
    # 禁止修改区域结束
    # ========================================================================

    # 启动 WebSocket 服务器
    ws_server = WebSocketServer(config)
    # 启动 Frigate 安防事件监听（MQTT → 前端安防提示卡）
    from core.utils.security_listener import start_frigate_security_listener
    start_frigate_security_listener(ws_server, asyncio.get_running_loop(), config)
    # 内部调度器（2026-09-11，替代已弃用的 OpenClaw cron）：提醒
    from core.utils.scheduler import scheduler
    from plugins_func.functions.reminder import setup_reminder_handler
    setup_reminder_handler(scheduler, ws_server)
    scheduler.start()

    # ── 飞书长连接事件监听（2026-09-12）───────────────────────────
    # 复用 data/.mcp_server_settings.json 的 lark 凭证，独立 Python SDK 长连接收事件
    feishu_task = None
    feishu_client = None
    try:
        from core.utils.feishu_listener import FeishuListener, set_main_loop
        from core.utils.feishu_client import FeishuClient
        feishu_cfg = config.get("feishu", {}) or {}
        if feishu_cfg.get("enabled", False):
            # 读凭证：data/.mcp_server_settings.json 的 lark 块
            mcp_settings_path = os.path.join(os.path.dirname(__file__) or ".", "data", ".mcp_server_settings.json")
            app_id = app_secret = None
            if os.path.exists(mcp_settings_path):
                try:
                    with open(mcp_settings_path) as f:
                        ms = json.load(f)
                    args = (ms.get("mcpServers", {}) or {}).get("lark", {}).get("args") or []
                    # args: ["-y", "--registry=...", "@larksuiteoapi/lark-mcp", "mcp", "-a", ID, "-s", SECRET, "-t", TOOLS]
                    if len(args) >= 8 and args[4] == "-a" and args[6] == "-s":
                        app_id, app_secret = args[5], args[7]
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"读 mcp_server_settings.json 失败: {e}")
            if not app_id or not app_secret:
                logger.bind(tag=TAG).warning("飞书启用但 data/.mcp_server_settings.json 里没有 lark 凭证，跳过")
            else:
                # 读白名单：data/.lark_contacts.json 的 owner.open_id
                wl_path = os.path.join(os.path.dirname(__file__) or ".", feishu_cfg.get("whitelist_file", "data/.lark_contacts.json"))
                wl_ids: set = set()
                if os.path.exists(wl_path):
                    try:
                        with open(wl_path) as f:
                            wl = json.load(f)
                        # 读 owner
                        owner_id = (wl.get("owner") or {}).get("open_id")
                        if owner_id:
                            wl_ids.add(owner_id)
                        # 读 family 数组（支持多家庭成员）
                        for member in (wl.get("family") or []):
                            mid = member.get("open_id")
                            if mid:
                                wl_ids.add(mid)
                    except Exception as e:
                        logger.bind(tag=TAG).warning(f"读白名单失败: {e}")
                if not wl_ids:
                    logger.bind(tag=TAG).warning("飞书启用但白名单为空，跳过")
                else:
                    set_main_loop(asyncio.get_running_loop())
                    feishu_client = FeishuClient(app_id, app_secret)
                    feishu_listener = FeishuListener(config, feishu_client, ws_server, wl_ids)
                    feishu_task = asyncio.create_task(feishu_listener.start())
                    def _feishu_done(t):
                        try:
                            t.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            logger.bind(tag=TAG).error(f"飞书 listener 任务异常退出: {type(e).__name__}: {e}")
                    feishu_task.add_done_callback(_feishu_done)
                    logger.bind(tag=TAG).info(f"飞书长连接已启动（白名单 {len(wl_ids)} 人，app_id={app_id}）")
    except Exception as e:
        logger.bind(tag=TAG).error(f"飞书监听器初始化失败: {e}")

    # 将WebSocket服务器实例传递给配置API
    from core.api.config_api import set_websocket_server_instance
    set_websocket_server_instance(ws_server)
    ws_task = asyncio.create_task(ws_server.start())
    # 启动 Simple http 服务器
    ota_server = SimpleHttpServer(config)
    # J3：把 ws_server 引用给 HTTP 侧，供 bundle 热重载刷新活跃连接
    ota_server.server = ws_server
    ota_task = asyncio.create_task(ota_server.start())

    async def _resume_memory_backlog():
        """服务启动后恢复记忆抽取积压（等模块初始化完再动）。"""
        await asyncio.sleep(20)
        try:
            mem = getattr(ws_server, "_memory", None)
            if mem is not None and hasattr(mem, "resume_pending"):
                mem.resume_pending()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"记忆积压恢复失败: {e}")

    asyncio.create_task(_resume_memory_backlog())

    # 夜间短期记忆梳理：每天 03:30 把当天对话补抽进 mem0（去重由 mem0 写入链路负责）
    from core.utils.memory_consolidation import consolidation_loop
    asyncio.create_task(consolidation_loop(ws_server, config))

    # ── 吃药提醒调度器（2026-10-11）：按 medications 表的服药时间到点播报 ──
    try:
        from core.utils.medication_reminder import medication_reminder_loop
        asyncio.create_task(medication_reminder_loop(ws_server, config))
        logger.bind(tag=TAG).info("吃药提醒调度器已启动")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"吃药提醒调度器启动失败（不影响主服务）: {e}")

    # ── 健康问询巡检（2026-10-11）：每 30 分钟查指标突变 → 飞书主动问 ──
    async def health_watch_periodically():
        from core.utils import health_inbox
        while True:
            try:
                await asyncio.sleep(1800)
                await health_inbox.proactive_check(config)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"健康问询巡检异常（不影响主服务）: {e}")

    try:
        asyncio.create_task(health_watch_periodically())
        logger.bind(tag=TAG).info("健康问询巡检已启动（每 30 分钟）")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"健康问询巡检启动失败（不影响主服务）: {e}")

    read_config_from_api = config.get("read_config_from_api", False)
    port = int(config["server"].get("http_port", 8003))
    if not read_config_from_api:
        logger.bind(tag=TAG).info(
            "OTA接口是\t\thttp://{}:{}/homebrain/ota/",
            get_local_ip(),
            port,
        )
    logger.bind(tag=TAG).info(
        "视觉分析接口是\thttp://{}:{}/mcp/vision/explain",
        get_local_ip(),
        port,
    )
    mcp_endpoint = config.get("mcp_endpoint", None)
    if not mcp_endpoint:
        # [20261020 修复] 空串=未配置 MCP 接入点（多数连接走设备 WS，不走 MCP），
        # 旧守卫只挡 None 与占位文本，空串穿透到校验器误报 ERROR「不符合规范」
        logger.bind(tag=TAG).info("mcp接入点未配置，跳过 MCP 连接")
    elif "你" not in mcp_endpoint:
        # 校验MCP接入点格式
        if validate_mcp_endpoint(mcp_endpoint):
            # 脱敏：URL 带 token 参数时日志隐藏凭据
            safe_endpoint = mcp_endpoint.split("?")[0] + ("?token=[已隐藏]" if "?token=" in mcp_endpoint else "")
            logger.bind(tag=TAG).info("mcp接入点是\t{}", safe_endpoint)
            # 保留原始 MCP 端点地址（HA MCP 使用 /api/mcp 路径）
            config["mcp_endpoint"] = mcp_endpoint
        else:
            logger.bind(tag=TAG).error("mcp接入点不符合规范")
            config["mcp_endpoint"] = "你的接入点 websocket地址"

    # 获取WebSocket配置，使用安全的默认值
    websocket_port = 8000
    server_config = config.get("server", {})
    if isinstance(server_config, dict):
        websocket_port = int(server_config.get("port", 8000))

    logger.bind(tag=TAG).info(
        "Websocket地址是\tws://{}:{}/homebrain/v1/",
        get_local_ip(),
        websocket_port,
    )

    logger.bind(tag=TAG).info(
        "=======上面的地址是websocket协议地址，请勿用浏览器访问======="
    )
    logger.bind(tag=TAG).info(
        "如想测试websocket请用谷歌浏览器打开test目录下的test_page.html"
    )
    logger.bind(tag=TAG).info(
        "=============================================================\n"
    )

    try:
        await wait_for_exit()  # 阻塞直到收到退出信号
    except asyncio.CancelledError:
        print("任务被取消，清理资源中...")
    finally:
        # 停止全局GC管理器
        await gc_manager.stop()

        # 停止系统音频 AEC 的 capture 子进程（否则会遗留孤儿进程持续采集）
        try:
            from core.utils.system_aec import shutdown as _system_aec_shutdown
            _system_aec_shutdown()
        except Exception as e:
            print(f"停止系统AEC失败: {e}")

        # 取消所有任务（关键修复点）
        stdin_task.cancel()
        ws_task.cancel()
        if ota_task:
            ota_task.cancel()
        if feishu_task:
            feishu_task.cancel()
        if feishu_client:
            try:
                await feishu_client.close()
            except Exception:
                pass

        # 等待任务终止（必须加超时）
        wait_tasks = [stdin_task, ws_task]
        if ota_task:
            wait_tasks.append(ota_task)
        if feishu_task:
            wait_tasks.append(feishu_task)
        await asyncio.wait(wait_tasks, timeout=3.0, return_when=asyncio.ALL_COMPLETED)
        print("服务器已关闭，程序退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("手动中断，程序终止。")
