"""
Plugins管理API

2026-09-11 增强（在原运行时注册表版本基础上）：
- list_plugins：运行时注册表 + 磁盘目录扫描合并，停用插件（functions_disabled/）也可见
- toggle_plugin：从 TODO 假实现改为真实启停（文件在 functions/ ↔ functions_disabled/ 间移动，重启生效）
- reload_bundles / test_mcp：保留原实现
"""
import ast
import json
import shutil
import sys
from aiohttp import web
from pathlib import Path

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

SERVER_ROOT = Path(__file__).parent.parent.parent
FUNCTIONS_DIR = SERVER_ROOT / "plugins_func" / "functions"
DISABLED_DIR = SERVER_ROOT / "plugins_func" / "functions_disabled"
_SKIP_FILES = {"__init__.py", "register.py", "loadplugins.py", "bundle_manager.py"}


def _extract_desc_literal(tree, var_names):
    """从 AST 中找指定变量名的 dict 字面量赋值，提取 description 文本"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in var_names:
                    try:
                        d = ast.literal_eval(node.value)
                        if isinstance(d, dict):
                            fn = d.get("function", {})
                            if isinstance(fn, dict) and fn.get("description"):
                                return fn["description"]
                            if d.get("description"):
                                return d["description"]
                    except (ValueError, SyntaxError):
                        continue
    return ""


def _parse_plugin_file(path: Path):
    """静态解析单个插件文件中的 @register_function 注册（不 import，避免副作用）"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.bind(tag=TAG).warning(f"插件解析失败 {path.name}: {e}")
        return []

    registrations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fname = getattr(dec.func, "id", None) or getattr(dec.func, "attr", None)
            if fname != "register_function":
                continue
            reg = {"name": None, "desc_var": None, "desc_inline": None}
            for i, arg in enumerate(dec.args):
                if i == 0 and isinstance(arg, ast.Constant):
                    reg["name"] = arg.value
                elif i == 1:
                    if isinstance(arg, ast.Name):
                        reg["desc_var"] = arg.id
                    elif isinstance(arg, ast.Constant):
                        reg["desc_inline"] = arg.value
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    reg["name"] = kw.value.value
                if kw.arg == "desc" and isinstance(kw.value, ast.Name):
                    reg["desc_var"] = kw.value.id
            if reg["name"]:
                registrations.append(reg)

    results = []
    for reg in registrations:
        desc = ""
        if reg["desc_var"]:
            desc = _extract_desc_literal(tree, {reg["desc_var"]})
        elif reg["desc_inline"]:
            desc = reg["desc_inline"]
        results.append({"name": reg["name"], "description": desc})
    return results


def _scan_plugin_dir(directory: Path, enabled: bool):
    """扫描目录：顶层 .py 文件 + 含 __init__.py 的子目录（bundle 包）"""
    plugins = []
    if not directory.exists():
        return plugins
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("__") or entry.name in _SKIP_FILES:
            continue
        if entry.is_file() and entry.suffix == ".py":
            funcs = _parse_plugin_file(entry)
            plugins.append({
                "id": entry.stem,
                "file": entry.name,
                "path": str(entry.relative_to(SERVER_ROOT)),
                "is_bundle_pkg": False,
                "enabled": enabled,
                "functions": funcs,
                "function_count": len(funcs),
            })
        elif entry.is_dir() and (entry / "__init__.py").exists():
            funcs = []
            for f in sorted(entry.glob("*.py")):
                funcs.extend(_parse_plugin_file(f))
            plugins.append({
                "id": entry.name,
                "file": entry.name + "/",
                "path": str(entry.relative_to(SERVER_ROOT)),
                "is_bundle_pkg": True,
                "enabled": enabled,
                "functions": funcs,
                "function_count": len(funcs),
            })
    return plugins


def list_all_plugins() -> list:
    """同步版：返回所有已扫描插件的列表（供其他模块无请求上下文时调用）。"""
    return _scan_plugin_dir(FUNCTIONS_DIR, True) + _scan_plugin_dir(DISABLED_DIR, False)


async def list_plugins(request):
    """列出所有插件（启用 = functions/ 目录；停用 = functions_disabled/ 目录）"""
    try:
        plugins = _scan_plugin_dir(FUNCTIONS_DIR, True) + _scan_plugin_dir(DISABLED_DIR, False)

        # 运行时注册表：标注哪些函数当前已加载（用于区分"目录启用但本次进程未加载"的状态）
        sys.path.insert(0, str(SERVER_ROOT))
        try:
            from plugins_func.register import all_function_registry
            loaded = set(all_function_registry.keys())
        except Exception:
            loaded = set()
        for p in plugins:
            for f in p["functions"]:
                f["loaded"] = f["name"] in loaded

        # J3：插件 Bundle 列表（保留原有能力）
        bundles = []
        try:
            from plugins_func.bundle_manager import get_bundle_manager
            bundles = get_bundle_manager().list_bundles()
        except Exception as bundle_err:
            logger.bind(tag=TAG).error(f"获取 Bundle 列表失败: {bundle_err}")

        return web.json_response({
            'success': True,
            'plugins': plugins,
            'total': len(plugins),
            'enabled_count': sum(1 for p in plugins if p["enabled"]),
            'disabled_count': sum(1 for p in plugins if not p["enabled"]),
            'bundles': bundles,
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})


async def toggle_plugin(request):
    """启用/禁用插件（真实实现：移动文件，重启服务后生效）

    Body: {"id": "插件文件ID", "enabled": true}
    兼容旧调用：{"name": "函数名", "enabled": true} —— 按函数名定位所属插件文件
    """
    try:
        data = await request.json()
        plugin_id = (data.get("id") or "").strip()
        enabled = bool(data.get("enabled"))

        # 兼容旧的前端调用（按函数名）
        if not plugin_id and data.get("name"):
            fname = data["name"]
            for p in _scan_plugin_dir(FUNCTIONS_DIR, True) + _scan_plugin_dir(DISABLED_DIR, False):
                if any(f["name"] == fname for f in p["functions"]):
                    plugin_id = p["id"]
                    break

        if not plugin_id or plugin_id in (".", "..") or "/" in plugin_id or "\\" in plugin_id:
            return web.json_response({'success': False, 'message': '非法插件 ID'}, status=400)

        src = dst = None
        search_dirs = (DISABLED_DIR, FUNCTIONS_DIR) if enabled else (FUNCTIONS_DIR, DISABLED_DIR)
        for cand in (plugin_id + ".py", plugin_id):
            p = search_dirs[0] / cand
            if p.exists():
                src, dst = p, search_dirs[1] / cand
                break
        if src is None:
            return web.json_response({
                'success': False,
                'message': f'找不到插件 {plugin_id}（当前状态可能与请求一致）',
            }, status=404)

        DISABLED_DIR.mkdir(exist_ok=True)
        shutil.move(str(src), str(dst))
        logger.bind(tag=TAG).info(f"插件 {plugin_id} 已{'启用' if enabled else '停用'}: {src.name} -> {dst.parent.name}/")
        return web.json_response({
            'success': True,
            'id': plugin_id,
            'enabled': enabled,
            'need_restart': True,
            'message': f'插件已{"启用" if enabled else "停用"}，重启服务后生效',
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})


async def reload_bundles(request):
    """热重载插件 Bundle（body: {name?: 指定 bundle 名，缺省重载全部}）"""
    try:
        from plugins_func.bundle_manager import get_bundle_manager

        data = await request.json() if request.can_read_body else {}
        bundle_name = data.get("name")
        mgr = get_bundle_manager()
        server = request.app.get("_homebrain_server")

        reloaded = []
        if bundle_name:
            result = mgr.reload_bundle(bundle_name, request.app.get("_homebrain_config") or {})
            reloaded.append(bundle_name)
            success = result
            message = f"Bundle {bundle_name} 重载{'成功' if result else '失败'}"
        else:
            for name in mgr.scan_bundles():
                ok = mgr.reload_bundle(name, request.app.get("_homebrain_config") or {})
                reloaded.append(f"{name}{'✓' if ok else '✗'}")
            success = True
            message = f"全部 Bundle 重载完成: {reloaded}"

        refreshed = mgr.refresh_connections(server)

        return web.json_response({
            'success': success,
            'message': message,
            'reloaded': reloaded,
            'connections_refreshed': refreshed
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})


async def test_mcp(request):
    """测试MCP连接（注：OpenClaw 已弃用，此接口仅保留兼容，2026-09-11）"""
    try:
        import asyncio

        result = await asyncio.create_subprocess_exec(
            'curl', '-s', '-m', '3', 'http://127.0.0.1:18789/health',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode == 0:
            health = json.loads(stdout.decode())
            if health.get('ok'):
                return web.json_response({
                    'success': True,
                    'message': f'OpenClaw Gateway运行正常 (PID: {health.get("pid", "未知")})'
                })

        return web.json_response({
            'success': False,
            'message': 'OpenClaw Gateway未响应（OpenClaw 已弃用，属预期）'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'MCP测试失败: {str(e)}'
        })


async def mcp_status(request):
    """读取 MCP 服务器配置列表（仅名称，不暴露命令参数中的敏感值）"""
    mcp_settings_path = SERVER_ROOT / "data" / ".mcp_server_settings.json"
    servers = []
    try:
        if mcp_settings_path.exists():
            data = json.loads(mcp_settings_path.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or {}).items():
                kind = "stdio" if cfg.get("command") else "url"
                servers.append({"name": name, "kind": kind})
    except Exception as e:
        logger.bind(tag=TAG).warning(f"MCP 配置读取失败: {e}")
    return web.json_response({
        "success": True,
        "configured": len(servers) > 0,
        "servers": servers,
        "settings_path": str(mcp_settings_path.relative_to(SERVER_ROOT)),
    })
