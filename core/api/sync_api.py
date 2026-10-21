"""
同步API - 处理控制台localStorage与服务器数据库的双向同步
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from aiohttp import web
import logging

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

def init_db():
    """初始化数据库表"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()
    
        # 创建同步状态表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT NOT NULL,
                last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'completed',
                details TEXT
            )
        """)
    
        conn.commit()
    finally:
        conn.close()

def sync_members_from_console(console_data: list) -> dict:
    """从控制台数据同步到服务器数据库"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 清空现有的家庭成员数据（保留示例数据标记）
            cursor.execute("DELETE FROM family_members WHERE id > 1000")  # 假设示例数据ID小于1000
        
            for member in console_data:
                # 检查是否已存在
                cursor.execute("SELECT id FROM family_members WHERE name = ?", (member.get('name', ''),))
                existing = cursor.fetchone()
            
                if existing:
                    # 更新现有记录
                    cursor.execute("""
                        UPDATE family_members 
                        SET relation = ?, avatar = ?, age = ?, gender = ?, birthday = ?, 
                            height = ?, weight = ?, occupation = ?, position = ?, 
                            notes = ?, health_enabled = ?, updated_at = ?
                        WHERE name = ?
                    """, (
                        member.get('relation', ''),
                        member.get('avatar', ''),
                        member.get('age', 0),
                        member.get('gender', ''),
                        member.get('birthday', ''),
                        member.get('height', 0.0),
                        member.get('weight', 0.0),
                        member.get('occupation', ''),
                        member.get('position', ''),
                        member.get('notes', ''),
                        member.get('health_enabled', False),
                        datetime.now().isoformat(),
                        member.get('name', '')
                    ))
                else:
                    # 插入新记录
                    cursor.execute("""
                        INSERT INTO family_members 
                        (name, relation, avatar, age, gender, birthday, height, weight, 
                         occupation, position, notes, health_enabled, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        member.get('name', ''),
                        member.get('relation', ''),
                        member.get('avatar', ''),
                        member.get('age', 0),
                        member.get('gender', ''),
                        member.get('birthday', ''),
                        member.get('height', 0.0),
                        member.get('weight', 0.0),
                        member.get('occupation', ''),
                        member.get('position', ''),
                        member.get('notes', ''),
                        member.get('health_enabled', False),
                        datetime.now().isoformat(),
                        datetime.now().isoformat()
                    ))
        
            conn.commit()
        
            # 记录同步状态
            cursor.execute("""
                INSERT INTO sync_status (sync_type, status, details) 
                VALUES (?, ?, ?)
            """, ("members_console_to_server", "completed", f"Synced {len(console_data)} members"))
        
            conn.commit()
        finally:
            conn.close()
        
        return {
            "success": True,
            "message": f"成功同步 {len(console_data)} 个家庭成员",
            "synced_count": len(console_data)
        }
        
    except Exception as e:
        logging.error(f"同步家庭成员数据失败: {e}")
        return {
            "success": False,
            "message": f"同步失败: {str(e)}"
        }

def get_console_format_members() -> list:
    """获取适合控制台格式的家庭成员数据"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='family_members'")
            if not cursor.fetchone():
                logging.warning("family_members表不存在")
                return []
        
            # 获取列信息
            cursor.execute("PRAGMA table_info(family_members)")
            columns = [col[1] for col in cursor.fetchall()]
            logging.info(f"family_members表列: {columns}")
        
            # 根据实际列名查询
            cursor.execute("SELECT * FROM family_members ORDER BY id")
            rows = cursor.fetchall()
            logging.info(f"查询到 {len(rows)} 条记录")
        
        finally:
            conn.close()
        
        members = []
        for row in rows:
            # 根据实际列数和数据构建成员对象
            member = {
                "_serverId": row[0] if len(row) > 0 else None,
                "name": row[1] if len(row) > 1 else "",
                "role": row[2] if len(row) > 2 else "",
                "relation": row[2] if len(row) > 2 else "",
                "avatar": row[3] if len(row) > 3 else "",
                "age": row[4] if len(row) > 4 else 0,
                "gender": row[5] if len(row) > 5 else "",
                "birthday": row[6] if len(row) > 6 else "",
                "height": row[7] if len(row) > 7 else 0.0,
                "weight": row[8] if len(row) > 8 else 0.0,
                "occupation": row[9] if len(row) > 9 else "",
                "position": row[10] if len(row) > 10 else "",
                "notes": row[11] if len(row) > 11 else "",
                "health_enabled": row[12] if len(row) > 12 else False,
                "devices": [],
                "_devices": [],
                "_healthEnabled": row[12] if len(row) > 12 else False
            }
            members.append(member)
        
        logging.info(f"返回 {len(members)} 个成员")
        return members
        
    except Exception as e:
        logging.error(f"获取家庭成员数据失败: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return []

async def handle_sync_members_to_console(request) -> web.Response:
    """处理同步服务器数据到控制台的请求"""
    try:
        members = get_console_format_members()
        
        # 模拟控制台localStorage格式
        console_data = {
            "members": members,
            "timestamp": datetime.now().isoformat(),
            "sync_type": "server_to_console"
        }
        
        return web.json_response({
            "success": True,
            "data": console_data,
            "message": "获取服务器数据成功，可用于更新控制台localStorage"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"获取数据失败: {str(e)}"
        })

async def handle_sync_members_from_console(request) -> web.Response:
    """处理从控制台同步数据到服务器的请求"""
    try:
        data = await request.json()
        console_members = data.get('members', [])
        
        if not console_members:
            return web.json_response({
                "success": False,
                "message": "没有提供成员数据"
            })
        
        result = sync_members_from_console(console_members)
        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"同步失败: {str(e)}"
        })

async def handle_full_sync(request) -> web.Response:
    """处理完整同步请求（双向同步）"""
    try:
        # 获取当前服务器数据
        server_members = get_console_format_members()
        
        # 如果请求中包含控制台数据，则同步到服务器
        try:
            data = await request.json()
            console_members = data.get('members', [])
            
            if console_members:
                sync_result = sync_members_from_console(console_members)
                if not sync_result['success']:
                    return web.json_response(sync_result)
        except:
            # 如果没有提供控制台数据，只是获取服务器数据
            pass
        
        # 返回服务器最新数据
        final_members = get_console_format_members()
        
        return web.json_response({
            "success": True,
            "server_members": final_members,
            "message": "双向同步完成"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"同步失败: {str(e)}"
        })

# 初始化数据库
init_db()