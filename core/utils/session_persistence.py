"""
Ltm 会话持久化工具

管理 device_id -> session_id 的映射，支持 WebSocket 重连时恢复会话。

存储格式：data/.LTM_sessions.json
{
  "device_id_1": {
    "session_id": "xxx",
    "created_at": "2026-09-15T22:30:00Z",
    "last_active": "2026-09-15T23:00:00Z"
  },
  ...
}
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 会话映射文件路径
SESSIONS_FILE = Path("data/.LTM_sessions.json")


def _load_sessions() -> Dict:
    """加载会话映射"""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.bind(tag=TAG).error(f"加载会话映射失败: {e}")
        return {}


def _save_sessions(sessions: Dict) -> bool:
    """保存会话映射"""
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存会话映射失败: {e}")
        return False


def get_session_id(device_id: str) -> Optional[str]:
    """
    获取设备的 Ltm session_id

    Args:
        device_id: 设备 ID（如 ESP32 设备 ID）

    Returns:
        session_id 或 None
    """
    sessions = _load_sessions()
    device_sessions = sessions.get(device_id)
    if device_sessions:
        return device_sessions.get("session_id")
    return None


def save_session_id(device_id: str, session_id: str) -> bool:
    """
    保存设备的 Ltm session_id

    Args:
        device_id: 设备 ID
        session_id: Ltm 会话 ID

    Returns:
        是否保存成功
    """
    sessions = _load_sessions()
    sessions[device_id] = {
        "session_id": session_id,
        "created_at": sessions.get(device_id, {}).get("created_at", datetime.utcnow().isoformat() + "Z"),
        "last_active": datetime.utcnow().isoformat() + "Z",
    }
    result = _save_sessions(sessions)
    if result:
        logger.bind(tag=TAG).debug(f"会话已保存: {device_id} -> {session_id}")
    return result


def clear_session(device_id: str) -> bool:
    """
    清除设备的会话映射（用于重置会话）

    Args:
        device_id: 设备 ID

    Returns:
        是否清除成功
    """
    sessions = _load_sessions()
    if device_id in sessions:
        del sessions[device_id]
        return _save_sessions(sessions)
    return True


def list_sessions() -> Dict:
    """
    列出所有会话映射

    Returns:
        {device_id: {session_id, created_at, last_active}}
    """
    return _load_sessions()


def get_stats() -> Dict:
    """
    获取会话统计信息

    Returns:
        {total, active_today, by_device: {...}}
    """
    sessions = _load_sessions()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    stats = {
        "total": len(sessions),
        "active_today": 0,
        "by_device": {},
    }

    for device_id, session_info in sessions.items():
        last_active = session_info.get("last_active", "")
        if last_active.startswith(today):
            stats["active_today"] += 1
        stats["by_device"][device_id] = {
            "session_id": session_info.get("session_id"),
            "last_active": last_active,
        }

    return stats
