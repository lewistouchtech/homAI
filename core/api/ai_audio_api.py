"""
AI 空间麦 API
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from aiohttp import web
from config.logger import setup_logging
import aiohttp

TAG = __name__
logger = setup_logging()

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "devices.db")




def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_audio_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
                        channel INTEGER DEFAULT 1,
            room TEXT,
            status TEXT DEFAULT 'offline',
            ip_address TEXT,
            firmware TEXT,
            channel_type TEXT,
                        sample_rate INTEGER DEFAULT 48000,
            output_type TEXT DEFAULT 'local',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 迁移：添加新字段（如果不存在）
    try:
        cursor.execute("SELECT channel_type FROM ai_audio_devices LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE ai_audio_devices ADD COLUMN channel_type TEXT")

    try:
        cursor.execute("SELECT sample_rate FROM ai_audio_devices LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE ai_audio_devices ADD COLUMN sample_rate INTEGER DEFAULT 48000")
    try:
        cursor.execute("SELECT output_type FROM ai_audio_devices LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE ai_audio_devices ADD COLUMN output_type TEXT DEFAULT 'local'")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audio_test_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER,
            audio_file TEXT,
            quality_score REAL,
            noise_level REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (device_id) REFERENCES ai_audio_devices(id)
        )
    ''')
    conn.commit()
    conn.close()


# 初始化数据库
init_db()


async def get_ai_audio_devices(request):
    """获取 AI 空间麦设备列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ai_audio_devices ORDER BY created_at DESC')
        devices = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return web.json_response({"success": True, "data": devices})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取设备列表失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def add_ai_audio_device(request):
    """添加 AI 空间麦设备"""
    try:
        data = await request.json()
        name = data.get("name", "")
        channel = data.get("channel", 1)
        room = data.get("room", "")
        ip_address = data.get("ip_address", "")
        status = data.get("status", "online")
        channel_type = data.get("channel_type", "input")
        sample_rate = data.get("sample_rate", 48000)
        output_type = data.get("output_type", "local")

        if not name:
            return web.json_response({"success": False, "error": "设备名称不能为空"}, status=400)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO ai_audio_devices (name, channel, room, ip_address, status, channel_type, sample_rate, output_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (name, channel, room, ip_address, status, channel_type, sample_rate, output_type)
        )
        conn.commit()
        device_id = cursor.lastrowid
        conn.close()

        logger.bind(tag=TAG).info(f"添加设备: {name}, output_type={output_type}")
        return web.json_response({"success": True, "data": {"id": device_id}})
    except Exception as e:
        logger.bind(tag=TAG).error(f"添加设备失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def update_ai_audio_device(request):
    """更新设备信息（分配房间等）"""
    try:
        device_id = request.match_info.get("device_id")
        data = await request.json()

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ai_audio_devices WHERE id = ?', (device_id,))
        device = cursor.fetchone()
        if not device:
            conn.close()
            return web.json_response({"success": False, "error": "设备不存在"}, status=404)

        fields = []
        values = []
        for key in ["name", "channel", "room", "ip_address", "status", "channel_type", "sample_rate", "output_type"]:
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(device_id)

        cursor.execute(f'UPDATE ai_audio_devices SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

        return web.json_response({"success": True})
    except Exception as e:
        logger.bind(tag=TAG).error(f"更新设备失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_ai_audio_device(request):
    """删除设备"""
    try:
        device_id = request.match_info.get("device_id")
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM ai_audio_devices WHERE id = ?', (device_id,))
        conn.commit()
        conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        logger.bind(tag=TAG).error(f"删除设备失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def test_ai_audio_device(request):
    """测试设备拾音质量"""
    try:
        device_id = request.match_info.get("device_id")
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ai_audio_devices WHERE id = ?', (device_id,))
        device = cursor.fetchone()
        if not device:
            conn.close()
            return web.json_response({"success": False, "error": "设备不存在"}, status=404)

        # 模拟拾音测试
        test_id = str(uuid.uuid4())[:8]
        quality_score = 85.0  # 模拟评分
        noise_level = -42.0   # 模拟噪音水平

        cursor.execute(
            'INSERT INTO audio_test_records (device_id, quality_score, noise_level) VALUES (?, ?, ?)',
            (device_id, quality_score, noise_level)
        )
        conn.commit()
        conn.close()

        return web.json_response({
            "success": True,
            "data": {
                "test_id": test_id,
                "quality_score": quality_score,
                "noise_level": noise_level,
                "status": "pass" if quality_score > 70 else "fail"
            }
        })
    except Exception as e:
        logger.bind(tag=TAG).error(f"测试设备失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_ai_audio_test_records(request):
    """获取测试记录"""
    try:
        device_id = request.match_info.get("device_id")
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM audio_test_records WHERE device_id = ? ORDER BY created_at DESC LIMIT 10',
            (device_id,)
        )
        records = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return web.json_response({"success": True, "data": records})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取测试记录失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_ai_audio_rooms(request):
    """获取可用房间列表"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 从 ai_audio_devices 表获取已分配的房间
        cursor.execute('SELECT DISTINCT room FROM ai_audio_devices WHERE room IS NOT NULL AND room != ""')
        used_rooms = [row[0] for row in cursor.fetchall()]
        conn.close()

        # 预定义房间列表（可从配置或房屋管理模块获取）
        all_rooms = [
            {"id": "living_room", "name": "客厅"},
            {"id": "bedroom_master", "name": "主卧"},
            {"id": "bedroom_2", "name": "次卧"},
            {"id": "study", "name": "书房"},
            {"id": "kitchen", "name": "厨房"},
            {"id": "meeting_room", "name": "会议室"},
            {"id": "office", "name": "办公室"},
        ]

        # 标记已分配的房间
        for room in all_rooms:
            room["assigned"] = room["id"] in used_rooms

        return web.json_response({"success": True, "data": all_rooms})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取房间列表失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_channel_config(request):
    """获取通道配置"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, channel, channel_type, 
                   sample_rate, output_type, room, status 
            FROM ai_audio_devices 
            ORDER BY created_at DESC
        ''')
        devices = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return web.json_response({"success": True, "data": devices})
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取通道配置失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def save_channel_config(request):
    """保存通道配置 - 支持全局配置和设备配置"""
    try:
        data = await request.json()
        device_id = data.get("device_id")
        input_channel = data.get("input_channel")  # 输入通道
        output_channel = data.get("output_channel")  # 输出通道
        room = data.get("room")  # 关联房间
        sample_rate = data.get("sample_rate", 48000)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # 如果没有 device_id，返回错误
        if not device_id:
            return web.json_response({"success": False, "error": "缺少设备ID"}, status=400)
        
        # 更新设备配置
        cursor.execute('''
            UPDATE ai_audio_devices 
            SET channel_type = ?, sample_rate = ?, output_type = ?, room = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', ('input', sample_rate, output_channel or 'local', room or '', device_id))
        conn.commit()
        conn.close()
        
        logger.bind(tag=TAG).info(f"保存通道配置: 设备 {device_id}, 输入 {input_channel}, 输出 {output_channel}, 房间 {room}")
        return web.json_response({"success": True, "data": {"device_id": device_id}})
    except Exception as e:
        logger.bind(tag=TAG).error(f"保存通道配置失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def batch_save_channel_config(request):
    """批量保存通道配置"""
    try:
        data = await request.json()
        configs = data.get("configs", [])
        
        if not configs:
            return web.json_response({"success": False, "error": "缺少配置数据"}, status=400)
        
        conn = get_db()
        cursor = conn.cursor()
        
        for config in configs:
            device_id = config.get("device_id")
            channel_type = config.get("channel_type")
            sample_rate = config.get("sample_rate", 48000)
            output_type = config.get("output_type", "local")
            
            if device_id:
                cursor.execute('''
                    UPDATE ai_audio_devices 
                    SET channel_type = ?, sample_rate = ?, output_type = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (channel_type, sample_rate, output_type, device_id))
        
        conn.commit()
        conn.close()
        
        logger.bind(tag=TAG).info(f"批量保存通道配置: {len(configs)} 条")
        return web.json_response({"success": True, "count": len(configs)})
    except Exception as e:
        logger.bind(tag=TAG).error(f"批量保存通道配置失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)












