"""
快递信息管理API
处理家庭成员与快递的关联及查询功能
"""
import json
import sqlite3
from aiohttp import web
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

async def get_express_config(request):
    """获取快递配置"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                SELECT config_value FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            row = cursor.fetchone()
        finally:
            conn.close()
        
        if row:
            config = json.loads(row[0])
            return web.json_response({
                'success': True,
                'config': config
            })
        else:
            return web.json_response({
                'success': True,
                'config': {'phones': [], 'packages': []}
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def update_express_config(request):
    """更新快递配置"""
    try:
        data = await request.json()
        config_json = json.dumps(data, ensure_ascii=False)
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 检查是否已存在配置
            cursor.execute("""
                SELECT id FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            existing = cursor.fetchone()
        
            if existing:
                # 更新现有配置
                cursor.execute("""
                    UPDATE dashboard_config 
                    SET config_value = ?, updated_at = ?
                    WHERE config_key = ?
                """, (config_json, datetime.now().isoformat(), 'express'))
            else:
                # 插入新配置
                cursor.execute("""
                    INSERT INTO dashboard_config (config_key, config_value, user_id)
                    VALUES (?, ?, 'default')
                """, ('express', config_json))
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': '快递配置已更新'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def get_express_by_member(request):
    """根据家庭成员获取快递信息"""
    try:
        member_id = int(request.match_info.get('member_id'))
        
        # 首先获取家庭成员的手机号
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                SELECT name, phone FROM family_members 
                WHERE id = ?
            """, (member_id,))
            member = cursor.fetchone()
        
            if not member:
                conn.close()
                return web.json_response({
                    'success': False,
                    'message': '家庭成员不存在'
                })
        
            member_name, member_phone = member
        
            # 获取快递配置
            cursor.execute("""
                SELECT config_value FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            row = cursor.fetchone()
        finally:
            conn.close()
        
        if row:
            config = json.loads(row[0])
            # 根据成员手机号筛选快递
            member_packages = []
            if 'packages' in config:
                for pkg in config['packages']:
                    # 检查快递是否与该成员关联（通过手机号）
                    if pkg.get('recipient_phone') == member_phone or member_phone in pkg.get('phones', []):
                        member_packages.append(pkg)
            
            return web.json_response({
                'success': True,
                'member_id': member_id,
                'member_name': member_name,
                'member_phone': member_phone,
                'packages': member_packages
            })
        else:
            return web.json_response({
                'success': True,
                'member_id': member_id,
                'member_name': member_name,
                'member_phone': member_phone,
                'packages': []
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def get_all_express_with_members(request):
    """获取所有快递信息，按家庭成员分组"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 获取所有家庭成员
            cursor.execute("""
                SELECT id, name, phone FROM family_members
            """)
            members = cursor.fetchall()
        
            # 获取快递配置
            cursor.execute("""
                SELECT config_value FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            row = cursor.fetchone()
        
            result = {}
        
            if row:
                config = json.loads(row[0])
                packages = config.get('packages', [])
            
                # 按成员分组快递
                for member_id, member_name, member_phone in members:
                    member_packages = []
                    for pkg in packages:
                        # 检查快递是否与该成员关联
                        if pkg.get('recipient_phone') == member_phone or member_phone in pkg.get('phones', []):
                            member_packages.append(pkg)
                
                    result[member_id] = {
                        'name': member_name,
                        'phone': member_phone,
                        'packages': member_packages
                    }
            else:
                # 如果没有配置，返回空的成员列表
                for member_id, member_name, member_phone in members:
                    result[member_id] = {
                        'name': member_name,
                        'phone': member_phone,
                        'packages': []
                    }
        
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'express_by_member': result
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def add_express_package(request):
    """添加快递包"""
    try:
        data = await request.json()
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 获取当前快递配置
            cursor.execute("""
                SELECT config_value FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            row = cursor.fetchone()
        
            if row:
                config = json.loads(row[0])
            else:
                config = {'phones': [], 'packages': []}
        
            # 添加新快递
            new_package = {
                'id': data.get('id', f"pkg_{len(config.get('packages', [])) + 1}"),
                'company': data.get('company', ''),
                'tracking_number': data.get('tracking_number', ''),
                'recipient_name': data.get('recipient_name', ''),
                'recipient_phone': data.get('recipient_phone', ''),
                'status': data.get('status', '运输中'),
                'statusText': data.get('statusText', '运输中'),
                'latestUpdate': data.get('latestUpdate', ''),
                'phones': data.get('phones', [])  # 用于关联多个电话
            }
        
            config.setdefault('packages', []).append(new_package)
        
            # 更新配置
            config_json = json.dumps(config, ensure_ascii=False)
        
            cursor.execute("""
                INSERT OR REPLACE INTO dashboard_config (config_key, config_value, user_id, created_at, updated_at)
                VALUES ('express', ?, 'default', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (config_json,))
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'package': new_package,
            'message': '快递信息已添加'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def update_member_phone(request):
    """更新家庭成员手机号"""
    try:
        member_id = int(request.match_info.get('member_id'))
        data = await request.json()
        phone = data.get('phone', '')
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 更新成员手机号
            cursor.execute("""
                UPDATE family_members 
                SET phone = ?, updated_at = ?
                WHERE id = ?
            """, (phone, datetime.now().isoformat(), member_id))
        
            if cursor.rowcount == 0:
                conn.close()
                return web.json_response({
                    'success': False,
                    'message': '家庭成员不存在'
                })
        
            conn.commit()
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'message': '手机号已更新'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def query_express_by_phone(request):
    """根据手机号查询快递"""
    try:
        phone = request.match_info.get('phone')
        
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 获取快递配置
            cursor.execute("""
                SELECT config_value FROM dashboard_config 
                WHERE config_key = 'express'
            """)
            row = cursor.fetchone()
        
            if row:
                config = json.loads(row[0])
                packages = config.get('packages', [])
            
                # 筛选匹配手机号的快递
                matching_packages = []
                for pkg in packages:
                    if pkg.get('recipient_phone') == phone or phone in pkg.get('phones', []):
                        matching_packages.append(pkg)
            else:
                matching_packages = []
        
        finally:
            conn.close()
        
        return web.json_response({
            'success': True,
            'phone': phone,
            'packages': matching_packages
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })