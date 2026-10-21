"""自定义场景存取（data/custom_scenes.json）

新格式（心情场景保存下来的）：{"name","description","commands":[{action,name,area,domain,value...}],"music"}
——存的是「说人话的指令」而不是 entity_id，设备改名/换位置后仍能重新寻址。
旧格式（预置场景、手写）：{"name","description","devices":[{entity_id,action,...}],"music"}。
"""
import json
import os
from typing import Any, Dict

CUSTOM_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "custom_scenes.json")


def load_custom() -> Dict[str, Any]:
    try:
        if os.path.exists(CUSTOM_PATH):
            with open(CUSTOM_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[AI场景] 加载自定义场景失败: {e}")
    return {}


def save_custom(name: str, cfg: Dict[str, Any]) -> bool:
    try:
        data = load_custom()
        data[name] = cfg
        os.makedirs(os.path.dirname(os.path.abspath(CUSTOM_PATH)), exist_ok=True)
        with open(CUSTOM_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[AI场景] 保存自定义场景失败: {e}")
        return False
