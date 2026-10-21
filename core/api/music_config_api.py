import json
import logging
import sqlite3
from datetime import datetime
from aiohttp import web
import asyncio
import aiohttp
from urllib.parse import urlparse

routes = web.RouteTableDef()

# 数据库路径
DB_PATH = "data/devices.db"

# 默认HA配置
DEFAULT_HA_HOST = "192.168.0.252"
DEFAULT_HA_PORT = "8123"

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@routes.get('/api/music/config')
async def get_music_config(request):
    """获取当前音乐配置"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 获取系统配置
        cursor.execute("SELECT config_key, config_value FROM system_config WHERE config_key IN (?, ?, ?, ?, ?, ?)", 
                      ('ha_host', 'ha_port', 'ha_token', 'music_default_platform', 'music_default_volume', 'music_auto_scan_entities'))
        config_rows = cursor.fetchall()
        
        config = {}
        for row in config_rows:
            config[row['config_key']] = row['config_value']
        
        # 设置默认值
        if 'ha_host' not in config:
            config['ha_host'] = f"http://{DEFAULT_HA_HOST}:{DEFAULT_HA_PORT}"
        elif not config['ha_host'].startswith(('http://', 'https://')):
            config['ha_host'] = f"http://{config['ha_host']}:{config['ha_port']}"
        
        if 'music_default_platform' not in config:
            config['music_default_platform'] = 'spotify'
        if 'music_default_volume' not in config:
            config['music_default_volume'] = '80'
        if 'music_auto_scan_entities' not in config:
            config['music_auto_scan_entities'] = 'true'
        
        # 获取房间映射列表
        cursor.execute("SELECT * FROM music_room_mapping ORDER BY id")
        rooms = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return web.json_response({
            'success': True,
            'config': config,
            'rooms': rooms
        })
    except Exception as e:
        logging.error(f"Error getting music config: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.post('/api/music/config/save')
async def save_music_config(request):
    """保存音乐配置"""
    try:
        data = await request.json()
        # 支持两种格式：{config:{...}} 或直接 {...}
        config = data.get('config', data)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 更新配置
        for key, value in config.items():
            cursor.execute("""
                INSERT OR REPLACE INTO system_config (config_key, config_value, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (key, str(value)))
        
        conn.commit()
        conn.close()
        
        return web.json_response({'success': True})
    except Exception as e:
        logging.error(f"Error saving music config: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.post('/api/music/scan_entities')
async def scan_ha_entities(request):
    """扫描HA媒体播放器设备"""
    try:
        # 获取HA配置
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'ha_host'")
        host_row = cursor.fetchone()
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'ha_port'")
        port_row = cursor.fetchone()
        cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'ha_token'")
        token_row = cursor.fetchone()
        
        conn.close()
        
        ha_host = host_row['config_value'] if host_row else DEFAULT_HA_HOST
        ha_port = port_row['config_value'] if port_row else DEFAULT_HA_PORT
        ha_token = token_row['config_value'] if token_row else None
        
        if not ha_token:
            return web.json_response({
                'success': False, 
                'error': 'HA token not configured',
                'entities': []
            })
        
        # 如果host不包含协议，则添加
        if not ha_host.startswith(('http://', 'https://')):
            ha_url = f"http://{ha_host}:{ha_port}"
        else:
            parsed = urlparse(ha_host)
            if parsed.port is None:
                ha_url = f"{ha_host}:{ha_port}" if parsed.scheme == 'http' else f"{ha_host}:443"
            else:
                ha_url = ha_host
        
        headers = {
            'Authorization': f'Bearer {ha_token}',
            'Content-Type': 'application/json'
        }
        
        # 调用HA API获取实体状态
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{ha_url}/api/states", headers=headers) as resp:
                    if resp.status == 200:
                        entities = await resp.json()
                        
                        # 过滤出media_player实体
                        media_players = []
                        for entity in entities:
                            if entity['entity_id'].startswith('media_player.'):
                                media_players.append({
                                    'entity_id': entity['entity_id'],
                                    'friendly_name': entity['attributes'].get('friendly_name', ''),
                                    'state': entity['state'],
                                    'supported_features': entity['attributes'].get('supported_features', 0)
                                })
                        
                        return web.json_response({
                            'success': True,
                            'entities': media_players
                        })
                    else:
                        error_text = await resp.text()
                        return web.json_response({
                            'success': False,
                            'error': f'HA API returned status {resp.status}: {error_text}',
                            'entities': []
                        })
            except Exception as api_error:
                logging.error(f"Error calling HA API: {str(api_error)}")
                return web.json_response({
                    'success': False,
                    'error': f'Connection failed: {str(api_error)}',
                    'entities': []
                })
                
    except Exception as e:
        logging.error(f"Error scanning HA entities: {str(e)}")
        return web.json_response({
            'success': False, 
            'error': str(e),
            'entities': []
        })

@routes.post('/api/music/test_connection')
async def test_ha_connection(request):
    """测试HA连接"""
    try:
        data = await request.json()
        ha_host = data.get('ha_host', '')
        ha_token = data.get('ha_token', '')
        
        # 如果没有提供参数，则从数据库获取
        if not ha_host or not ha_token:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'ha_host'")
            host_row = cursor.fetchone()
            cursor.execute("SELECT config_value FROM system_config WHERE config_key = 'ha_token'")
            token_row = cursor.fetchone()
            
            conn.close()
            
            ha_host = host_row['config_value'] if host_row else f"http://{DEFAULT_HA_HOST}:{DEFAULT_HA_PORT}"
            ha_token = token_row['config_value'] if token_row else ''
        
        # 如果host不包含协议，则添加
        if not ha_host.startswith(('http://', 'https://')):
            ha_port = DEFAULT_HA_PORT
            ha_url = f"http://{ha_host}:{ha_port}"
        else:
            parsed = urlparse(ha_host)
            if parsed.port is None:
                ha_url = f"{ha_host}:{DEFAULT_HA_PORT}" if parsed.scheme == 'http' else f"{ha_host}:443"
            else:
                ha_url = ha_host
        
        headers = {
            'Authorization': f'Bearer {ha_token}',
            'Content-Type': 'application/json'
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{ha_url}/api/", headers=headers) as resp:
                    if resp.status == 200:
                        api_info = await resp.json()
                        return web.json_response({
                            'success': True,
                            'message': 'Connection successful',
                            'api_info': api_info
                        })
                    else:
                        error_text = await resp.text()
                        return web.json_response({
                            'success': False,
                            'message': f'Connection failed with status {resp.status}: {error_text}'
                        })
            except Exception as e:
                return web.json_response({
                    'success': False,
                    'message': f'Connection failed: {str(e)}'
                })
    except Exception as e:
        logging.error(f"Error testing HA connection: {str(e)}")
        return web.json_response({
            'success': False,
            'message': f'Connection test error: {str(e)}'
        })

@routes.get('/api/music/rooms')
async def get_music_rooms(request):
    """获取房间映射列表"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM music_room_mapping ORDER BY id")
        rooms = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return web.json_response({
            'success': True,
            'rooms': rooms
        })
    except Exception as e:
        logging.error(f"Error getting music rooms: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.post('/api/music/rooms')
async def add_music_room(request):
    """添加房间映射"""
    try:
        data = await request.json()
        room_name = data.get('room_name')
        entity_id = data.get('entity_id')
        device_name = data.get('device_name', '')
        is_default = data.get('is_default', 0)
        
        if not room_name or not entity_id:
            return web.json_response({
                'success': False,
                'error': 'Room name and entity ID are required'
            }, status=400)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 如果设为默认，则先清除其他默认设置
        if is_default:
            cursor.execute("UPDATE music_room_mapping SET is_default = 0 WHERE is_default = 1")
        
        # 插入新记录
        cursor.execute("""
            INSERT INTO music_room_mapping (room_name, entity_id, device_name, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (room_name, entity_id, device_name, is_default))
        
        room_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return web.json_response({
            'success': True,
            'room_id': room_id
        })
    except Exception as e:
        logging.error(f"Error adding music room: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.put('/api/music/rooms/{id}')
async def update_music_room(request):
    """更新房间映射"""
    try:
        room_id = int(request.match_info['id'])
        data = await request.json()
        room_name = data.get('room_name')
        entity_id = data.get('entity_id')
        device_name = data.get('device_name', '')
        is_default = data.get('is_default', 0)
        
        if not room_name or not entity_id:
            return web.json_response({
                'success': False,
                'error': 'Room name and entity ID are required'
            }, status=400)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 如果设为默认，则先清除其他默认设置
        if is_default:
            cursor.execute("UPDATE music_room_mapping SET is_default = 0 WHERE is_default = 1 AND id != ?", (room_id,))
        
        # 先检查房间是否存在
        cursor.execute("SELECT id FROM music_room_mapping WHERE id = ?", (room_id,))
        if not cursor.fetchone():
            conn.close()
            return web.json_response({'success': False, 'error': 'Room not found'}, status=404)
        
        # 更新记录
        cursor.execute("""
            UPDATE music_room_mapping 
            SET room_name = ?, entity_id = ?, device_name = ?, is_default = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (room_name, entity_id, device_name, is_default, room_id))
        
        conn.commit()
        conn.close()
        
        return web.json_response({
            'success': True
        })
    except ValueError:
        return web.json_response({
            'success': False,
            'error': 'Invalid room ID'
        }, status=400)
    except Exception as e:
        logging.error(f"Error updating music room: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.delete('/api/music/rooms/{id}')
async def delete_music_room(request):
    """删除房间映射"""
    try:
        room_id = int(request.match_info['id'])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM music_room_mapping WHERE id = ?", (room_id,))
        
        if cursor.rowcount == 0:
            conn.close()
            return web.json_response({
                'success': False,
                'error': 'Room not found'
            }, status=404)
        
        conn.commit()
        conn.close()
        
        return web.json_response({
            'success': True
        })
    except ValueError:
        return web.json_response({
            'success': False,
            'error': 'Invalid room ID'
        }, status=400)
    except Exception as e:
        logging.error(f"Error deleting music room: {str(e)}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

# 注册路由函数
def register_routes(app):
    """注册音乐配置API路由"""
    app.router.add_routes(routes)