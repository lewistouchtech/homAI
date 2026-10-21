"""
OpenViking 记忆系统管理 API

提供：
- 配置管理（server_url, account, models —— 模型直接读写 ov.conf 并自动重启生效）
- 连接测试
- 会话管理（创建/恢复/列表）
- 记忆查询
- 健康检查
"""
import asyncio
import json
import subprocess
import yaml
from pathlib import Path
from aiohttp import web
from config.logger import setup_logging
from config.config_loader import load_yaml_expanded

TAG = __name__
logger = setup_logging()

OV_CONF_PATH = Path("data/openviking/ov.conf")


def _read_ov_conf() -> dict:
    """读取 ov.conf（OpenViking 服务端配置，JSON 格式）。不存在返回 {}。"""
    if not OV_CONF_PATH.exists():
        return {}
    try:
        return json.loads(OV_CONF_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取 ov.conf 失败: {e}")
        return {}


def _write_ov_conf(conf: dict) -> None:
    """写回 ov.conf（保留缩进，便于人工查看）。"""
    OV_CONF_PATH.write_text(
        json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _restart_openviking() -> bool:
    """重启 OpenViking 服务使配置生效。返回重启后是否恢复健康。

    步骤：杀 8765 端口进程 → start_openviking.sh（脚本自带就绪等待）→ /health 验证。
    """
    server_dir = Path(__file__).resolve().parent.parent.parent

    def _kill():
        try:
            # 只杀"监听"8765 的进程（-sTCP:LISTEN）；
            # 不加这个过滤会把所有连接 8765 的客户端（包括 HomeBrain 自己）也杀掉
            out = subprocess.run(
                ["lsof", "-ti", ":8765", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=10,
            )
            for pid in out.stdout.split():
                subprocess.run(["kill", pid], capture_output=True, timeout=5)
                logger.bind(tag=TAG).info(f"已停止 OpenViking 进程 PID={pid}")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"停止 OpenViking 进程出错: {e}")

    await asyncio.to_thread(_kill)
    await asyncio.sleep(2)

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "start_openviking.sh",
            cwd=str(server_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.bind(tag=TAG).warning("start_openviking.sh 等待超时（30s）")
    except Exception as e:
        logger.bind(tag=TAG).error(f"启动 OpenViking 失败: {e}")
        return False

    # /health 最终确认
    import urllib.request
    for _ in range(10):
        try:
            with urllib.request.urlopen("http://localhost:8765/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def get_openviking_config(request):
    """获取 OpenViking 配置"""
    try:
        config_path = Path("data/.config.yaml")
        if not config_path.exists():
            return web.json_response({"error": "配置文件不存在"}, status=404)
        
        with open(config_path, "r", encoding="utf-8") as f:
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            config = load_yaml_expanded(f)
        
        ov_config = config.get("Memory", {}).get("openviking", {})

        # 模型配置以 ov.conf（OpenViking 实际读取的文件）为准，yaml 仅作回退
        oc = _read_ov_conf()
        dense = oc.get("embedding", {}).get("dense", {})
        vlm = oc.get("vlm", {})

        return web.json_response({
            "server_url": ov_config.get("server_url", "http://localhost:8765"),
            "account": ov_config.get("account", "homebrain"),
            "retrieve_limit": ov_config.get("retrieve_limit", 5),
            "min_confidence": ov_config.get("min_confidence", 0.3),
            "auto_commit": ov_config.get("auto_commit", True),
            "embedding_model": dense.get("model") or ov_config.get("embedding_model", ""),
            "embedding_api_base": dense.get("api_base", ""),
            "embedding_dimension": dense.get("dimension", 1024),
            "embedding_api_key_set": bool(dense.get("api_key")),
            "vlm_model": vlm.get("model") or ov_config.get("vlm_model", ""),
            "vlm_api_base": vlm.get("api_base", ""),
            "vlm_api_key_set": bool(vlm.get("api_key")),
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取 OpenViking 配置失败: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def save_openviking_config(request):
    """保存 OpenViking 配置"""
    try:
        data = await request.json()
        config_path = Path("data/.config.yaml")
        
        if not config_path.exists():
            return web.json_response({"error": "配置文件不存在"}, status=404)
        
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        if "Memory" not in config:
            config["Memory"] = {}
        if "openviking" not in config["Memory"]:
            config["Memory"]["openviking"] = {"type": "openviking"}
        
        ov_config = config["Memory"]["openviking"]
        ov_config["server_url"] = data.get("server_url", ov_config.get("server_url"))
        ov_config["account"] = data.get("account", ov_config.get("account"))
        ov_config["retrieve_limit"] = data.get("retrieve_limit", ov_config.get("retrieve_limit", 5))
        ov_config["min_confidence"] = data.get("min_confidence", ov_config.get("min_confidence", 0.3))
        ov_config["auto_commit"] = data.get("auto_commit", ov_config.get("auto_commit", True))
        ov_config["embedding_model"] = data.get("embedding_model", "")
        ov_config["vlm_model"] = data.get("vlm_model", "")

        with open(config_path, "w", encoding="utf-8") as f:
            # 写前归集（防的是 search_api 同型事故：插值读 + 整体 dump = 密钥明文化）
            from core.utils.secret_store import sweep_dict_secrets
            yaml.dump(sweep_dict_secrets(config), f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        # ---- 模型配置写入 ov.conf（OpenViking 实际读取的文件）----
        restart_result = None  # None=无需重启, True=重启成功, False=重启失败
        embedding_changed = False
        if OV_CONF_PATH.exists():
            oc = _read_ov_conf()
            dense = oc.setdefault("embedding", {}).setdefault("dense", {})
            vlm = oc.setdefault("vlm", {})
            changed = False

            def _set(section, key, value, mark_embedding=False):
                nonlocal changed, embedding_changed
                if value is None or value == "":
                    return
                if section.get(key) != value:
                    section[key] = value
                    changed = True
                    if mark_embedding:
                        embedding_changed = True

            _set(dense, "model", data.get("embedding_model"), mark_embedding=True)
            _set(dense, "api_base", data.get("embedding_api_base"))
            if data.get("embedding_dimension"):
                try:
                    _set(dense, "dimension", int(data["embedding_dimension"]), mark_embedding=True)
                except (TypeError, ValueError):
                    pass
            # api_key 只在用户填写了新值时覆盖（不回传原 key，前端空值=保持不变）
            _set(dense, "api_key", data.get("embedding_api_key"))

            _set(vlm, "model", data.get("vlm_model"))
            _set(vlm, "api_base", data.get("vlm_api_base"))
            _set(vlm, "api_key", data.get("vlm_api_key"))

            if changed:
                _write_ov_conf(oc)
                logger.bind(tag=TAG).info("ov.conf 模型配置已更新，准备重启 OpenViking")
                restart_result = await _restart_openviking()
                if not restart_result:
                    logger.bind(tag=TAG).error("OpenViking 重启后未就绪")
        else:
            return web.json_response({
                "success": False,
                "error": f"未找到 {OV_CONF_PATH}，模型配置未保存。请确认 OpenViking 已部署。",
            }, status=400)

        message = "配置已保存"
        if restart_result is True:
            message += "，OpenViking 已重启生效"
            if embedding_changed:
                message += "。注意：Embedding 模型已更换，若维度变化旧记忆将无法检索（需重建记忆库）；同维度模型可直接继续使用"
        elif restart_result is False:
            message += "，但 OpenViking 重启后未就绪，请检查 tmp/openviking_server.log"

        logger.bind(tag=TAG).info(f"OpenViking 配置已更新")
        return web.json_response({
            "success": True,
            "message": message,
            "restarted": restart_result,
            "embedding_changed": embedding_changed,
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存 OpenViking 配置失败: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def test_openviking_connection(request):
    """测试 OpenViking 服务连接"""
    try:
        data = await request.json()
        server_url = data.get("server_url", "http://localhost:8765")
        
        from openviking_sdk.client import SyncHTTPClient
        from openviking_sdk import config as ov_config
        
        client_config = ov_config.resolve_client_config(
            url=server_url,
            account=data.get("account", "homebrain"),
        )
        
        client = SyncHTTPClient(
            url=client_config.url,
            api_key=client_config.api_key,
            account=client_config.account,
            user=client_config.user,
        )
        client.initialize()
        
        is_healthy = client.is_healthy()
        
        if is_healthy:
            status = client.get_status()
            client.close()
            return web.json_response({
                "success": True,
                "message": "连接成功",
                "status": status,
            })
        else:
            client.close()
            return web.json_response({
                "success": False,
                "message": "服务不可达",
            })
    except Exception as e:
        logger.bind(tag=TAG).error(f"OpenViking 连接测试失败: {e}")
        return web.json_response({
            "success": False,
            "message": f"连接失败: {str(e)}",
        })


# ==================== 记忆浏览 API ====================

# ==================== 会话管理 API ====================

def register_openviking_routes(app):
    """注册 OpenViking API 路由"""
    app.router.add_get("/api/openviking/config", get_openviking_config)
    app.router.add_post("/api/openviking/config", save_openviking_config)
    app.router.add_post("/api/openviking/test", test_openviking_connection)
    # 会话管理
    # 记忆浏览

    logger.bind(tag=TAG).info("OpenViking API 路由已注册")
