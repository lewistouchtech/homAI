# J3：插件 Bundle 模式
# Bundle = plugins_func/bundles/<name>/ 目录：
#   manifest.py  — MANIFEST dict（name/version/description）+ 可选 setup/stop/cleanup 同步钩子
#   plugin.py    — @register_function 注册函数（完全兼容现有机制）
# BundleManager：扫描/加载/卸载/重载，支持热加载与活跃连接工具刷新
from __future__ import annotations

import importlib
import pkgutil
import sys

from config.logger import setup_logging
from plugins_func.register import all_function_registry

TAG = __name__
logger = setup_logging()

BUNDLES_PACKAGE = "plugins_func.bundles"

HOOKS = ("setup", "stop", "cleanup")


class BundleManager:
    def __init__(self):
        self._loaded = {}  # name -> {"module_names": [...], "functions": [...]}

    @staticmethod
    def is_enabled(name, config=None) -> bool:
        """config["plugins"]["bundles"][name]["enabled"]，默认启用"""
        if not config:
            return True
        bundle_cfg = (config.get("plugins") or {}).get("bundles") or {}
        return bool((bundle_cfg.get(name) or {}).get("enabled", True))

    def scan_bundles(self) -> list[str]:
        """扫描 bundles 包下所有子包名"""
        try:
            package = importlib.import_module(BUNDLES_PACKAGE)
        except ImportError as e:
            logger.bind(tag=TAG).warning(f"bundles 包不存在: {e}")
            return []
        return [m.name for m in pkgutil.iter_modules(package.__path__)]

    def _call_hook(self, manifest_mod, hook_name, action):
        hook = getattr(manifest_mod, hook_name, None)
        if hook is None:
            return
        try:
            hook()
        except Exception as e:
            logger.bind(tag=TAG).error(f"Bundle 钩子 {hook_name} 执行失败({action}): {e}")

    def load_bundle(self, name, config=None) -> dict:
        """加载 bundle：import manifest + plugin，diff 记录函数，调用 setup 钩子"""
        if name in self._loaded:
            return {"success": False, "message": f"bundle {name} 已加载"}
        if not self.is_enabled(name, config):
            logger.bind(tag=TAG).info(f"bundle {name} 已禁用，跳过加载")
            return {"success": False, "message": f"bundle {name} 已禁用"}

        before = set(all_function_registry.keys())
        manifest_mod = None
        try:
            # 强制重新执行注册：unload 后模块缓存会让装饰器不再执行、函数无法恢复
            for suffix in ("manifest", "plugin"):
                sys.modules.pop(f"{BUNDLES_PACKAGE}.{name}.{suffix}", None)
            manifest_mod = importlib.import_module(f"{BUNDLES_PACKAGE}.{name}.manifest")
            importlib.import_module(f"{BUNDLES_PACKAGE}.{name}.plugin")
        except Exception as e:
            logger.bind(tag=TAG).error(f"bundle {name} 加载失败: {e}")
            return {"success": False, "message": str(e)}

        new_functions = sorted(set(all_function_registry.keys()) - before)
        self._call_hook(manifest_mod, "setup", "load")
        self._loaded[name] = {
            "module_names": [f"{BUNDLES_PACKAGE}.{name}.manifest", f"{BUNDLES_PACKAGE}.{name}.plugin"],
            "functions": new_functions,
            "manifest": manifest_mod,
        }
        logger.bind(tag=TAG).info(
            f"bundle {name} 加载成功，注册函数: {new_functions or '(无新函数)'}"
        )
        return {"success": True, "functions": new_functions}

    def unload_bundle(self, name) -> dict:
        """卸载 bundle：从全局注册表移除函数、调用 stop/cleanup 钩子"""
        info = self._loaded.pop(name, None)
        if not info:
            return {"success": False, "message": f"bundle {name} 未加载"}
        removed = []
        for fn in info["functions"]:
            if all_function_registry.pop(fn, None) is not None:
                removed.append(fn)
        manifest_mod = info.get("manifest")
        if manifest_mod:
            self._call_hook(manifest_mod, "stop", "unload")
            self._call_hook(manifest_mod, "cleanup", "unload")
        logger.bind(tag=TAG).info(f"bundle {name} 卸载成功，移除函数: {removed}")
        return {"success": True, "removed": removed}

    def reload_bundle(self, name, config=None) -> dict:
        """热重载 bundle：卸载 → 清模块缓存 → 重新加载"""
        self.unload_bundle(name)
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(f"{BUNDLES_PACKAGE}.{name}"):
                del sys.modules[mod_name]
        return self.load_bundle(name, config)

    def load_all(self, config=None) -> dict:
        """启动时加载所有未加载的 bundle（幂等，可被每连接调用）"""
        results = {}
        for name in self.scan_bundles():
            if name in self._loaded:
                continue
            results[name] = self.load_bundle(name, config)
        return results

    def list_bundles(self) -> list[dict]:
        """bundle 元信息（含已加载状态）；未加载时懒读 manifest 元信息（不执行 plugin）"""
        out = []
        for name in self.scan_bundles():
            info = self._loaded.get(name)
            version, desc = None, None
            if info and info.get("manifest"):
                mf = getattr(info["manifest"], "MANIFEST", None) or {}
                version, desc = mf.get("version"), mf.get("description")
            else:
                try:
                    mf_mod = importlib.import_module(f"{BUNDLES_PACKAGE}.{name}.manifest")
                    mf = getattr(mf_mod, "MANIFEST", None) or {}
                    version, desc = mf.get("version"), mf.get("description")
                except Exception:
                    pass
            out.append(
                {
                    "name": name,
                    "version": version,
                    "description": desc,
                    "functions": info["functions"] if info else [],
                    "loaded": name in self._loaded,
                }
            )
        return out

    def refresh_connections(self, server):
        """刷新所有活跃连接的工具描述缓存（热加载后调用）"""
        if server is None:
            return 0
        refreshed = 0
        for ws in list(getattr(server, "active_connections", set())):
            handler = getattr(ws, "_homebrain_handler", None)
            if handler is None:
                continue
            try:
                handler.tool_handler.upload_functions_desc()
                refreshed += 1
            except Exception as e:
                logger.bind(tag=TAG).warning(f"刷新连接工具失败: {e}")
        logger.bind(tag=TAG).info(f"已刷新 {refreshed} 个活跃连接的工具描述")
        return refreshed


_manager = None


def get_bundle_manager() -> BundleManager:
    global _manager
    if _manager is None:
        _manager = BundleManager()
    return _manager
