"""
HomeAssistant 客户端简化版
用于获取智能家居设备状态

2026-09-17 改：设备边界改由 core.utils.ha_expose 的 HA 暴露清单决定。
此前是 `if ALLOWED_ENTITIES: 过滤` —— 同步未完成时 ALLOWED_ENTITIES 为空，
会 fail-open 把 HA 全量实体（含门锁）都返回出去。
"""
import aiohttp

from plugins_func.functions.ha_control import HASS_BASE_URL, HASS_ACCESS_TOKEN
from core.utils.ha_expose import fetch_exposed_entities, get_cached_entities

HEADERS = {
    "Authorization": f"Bearer {HASS_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


class HAClient:
    """HomeAssistant 客户端"""

    def __init__(self):
        self.base_url = HASS_BASE_URL

    async def get_all_devices(self):
        """获取所有设备状态（仅 HA 暴露给语音助手的设备）"""
        try:
            entities = await fetch_exposed_entities(HASS_BASE_URL, HASS_ACCESS_TOKEN)
            wanted = {e["entity_id"] for e in entities}
            if not wanted:
                # 同步失败 —— 宁可返回空，也不能 fail-open 泄露全量实体
                return []
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(
                    f"{self.base_url}/api/states",
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        states = await response.json()
                        states = [s for s in states if s.get("entity_id") in wanted]
                        return self._parse_devices(states)
                    return []
        except Exception as e:
            print(f"[HAClient] 获取设备失败: {e}")
            return []

    def get_all_devices_sync(self):
        """同步获取设备清单（仅 HA 暴露给语音助手的设备）。

        用上一次异步同步的缓存做边界，不做网络请求 ——
        同步函数里无法 await，硬跑 asyncio.run 在事件循环内会抛错。
        缓存为空时返回 []（不 fail-open）。
        """
        try:
            import requests

            wanted = {e["entity_id"] for e in get_cached_entities()}
            if not wanted:
                return []
            response = requests.get(
                f"{self.base_url}/api/states",
                headers=HEADERS,
                timeout=10,
                proxies={"http": None, "https": None},
            )
            if response.status_code == 200:
                states = response.json()
                states = [s for s in states if s.get("entity_id") in wanted]
                return self._parse_devices(states)
            return []
        except Exception as e:
            print(f"[HAClient] 获取设备失败: {e}")
            return []

    def _parse_devices(self, states):
        """解析设备状态"""
        devices = []
        for state in states:
            entity_id = state.get("entity_id", "")
            attributes = state.get("attributes", {})

            device = {
                "entity_id": entity_id,
                "name": attributes.get("friendly_name", entity_id),
                "state": state.get("state", "unknown"),
                "last_updated": state.get("last_updated"),
                "attributes": attributes
            }
            devices.append(device)
        return devices
