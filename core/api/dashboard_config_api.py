"""
家庭看板配置API - 处理看板设置的保存和加载
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from aiohttp import web
import logging

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

def init_dashboard_config_db():
    """初始化看板配置数据库表"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()

        # 创建看板配置表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default',
                config_key TEXT NOT NULL,
                config_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, config_key)
            )
        """)

        # 创建看板布局配置表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_layout (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default',
                layout_name TEXT NOT NULL,
                layout_config TEXT NOT NULL,
                is_default BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
    finally:
        conn.close()

def save_dashboard_config(user_id: str, config: dict) -> dict:
    """保存看板配置到数据库"""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # 保存配置项
        for key, value in config.items():
            cursor.execute("""
                INSERT OR REPLACE INTO dashboard_config 
                (user_id, config_key, config_value, updated_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, key, json.dumps(value), datetime.now().isoformat()))
        
        conn.commit()
        
        return {
            "success": True,
            "message": f"成功保存 {len(config)} 个配置项",
            "saved_keys": list(config.keys())
        }
        
    except Exception as e:
        logging.error(f"保存看板配置失败: {e}")
        return {
            "success": False,
            "message": f"保存失败: {str(e)}"
        }
    finally:
        if conn:
            conn.close()

def get_dashboard_config(user_id: str) -> dict:
    """从数据库获取看板配置"""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT config_key, config_value 
            FROM dashboard_config 
            WHERE user_id = ?
        """, (user_id,))
        
        rows = cursor.fetchall()
        
        config = {}
        for row in rows:
            try:
                value = json.loads(row[1])
            except:
                value = row[1]  # 如果不是JSON格式，直接使用字符串
            config[row[0]] = value
        
        return {
            "success": True,
            "config": config
        }
        
    except Exception as e:
        logging.error(f"获取看板配置失败: {e}")
        return {
            "success": False,
            "message": f"获取失败: {str(e)}",
            "config": {}
        }
    finally:
        if conn:
            conn.close()

def save_dashboard_layout(user_id: str, layout_name: str, layout_config: dict, is_default: bool = False) -> dict:
    """保存看板布局配置"""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # 如果设置为默认布局，先取消其他布局的默认状态
        if is_default:
            cursor.execute("""
                UPDATE dashboard_layout 
                SET is_default = FALSE 
                WHERE user_id = ? AND is_default = TRUE
            """, (user_id,))
        
        # 保存布局配置
        cursor.execute("""
            INSERT OR REPLACE INTO dashboard_layout 
            (user_id, layout_name, layout_config, is_default, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, layout_name, json.dumps(layout_config), is_default, datetime.now().isoformat()))
        
        conn.commit()
        
        return {
            "success": True,
            "message": f"成功保存布局配置: {layout_name}",
            "layout_name": layout_name
        }
        
    except Exception as e:
        logging.error(f"保存看板布局失败: {e}")
        return {
            "success": False,
            "message": f"保存布局失败: {str(e)}"
        }
    finally:
        if conn:
            conn.close()

def get_dashboard_layout(user_id: str, layout_name: str = None) -> dict:
    """获取看板布局配置"""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        if layout_name:
            # 获取指定布局
            cursor.execute("""
                SELECT layout_name, layout_config, is_default
                FROM dashboard_layout 
                WHERE user_id = ? AND layout_name = ?
            """, (user_id, layout_name))
        else:
            # 获取默认布局
            cursor.execute("""
                SELECT layout_name, layout_config, is_default
                FROM dashboard_layout 
                WHERE user_id = ? AND is_default = TRUE
                LIMIT 1
            """)
            result = cursor.fetchone()
            if not result:
                # 如果没有默认布局，获取第一个布局
                cursor.execute("""
                    SELECT layout_name, layout_config, is_default
                    FROM dashboard_layout 
                    WHERE user_id = ?
                    LIMIT 1
                """)
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            try:
                layout_config = json.loads(row[1])
            except:
                layout_config = row[1]
            
            return {
                "success": True,
                "layout": {
                    "name": row[0],
                    "config": layout_config,
                    "is_default": row[2]
                }
            }
        else:
            return {
                "success": True,
                "layout": None,
                "message": "未找到布局配置"
            }
        
    except Exception as e:
        logging.error(f"获取看板布局失败: {e}")
        return {
            "success": False,
            "message": f"获取布局失败: {str(e)}"
        }
    finally:
        if conn:
            conn.close()

async def handle_save_dashboard_config(request) -> web.Response:
    """处理保存看板配置请求"""
    try:
        data = await request.json()
        user_id = data.get('user_id', 'default')
        config = data.get('config', {})
        
        if not config:
            return web.json_response({
                "success": False,
                "message": "没有提供配置数据"
            })
        
        result = save_dashboard_config(user_id, config)
        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

async def handle_get_dashboard_config(request) -> web.Response:
    """处理获取看板配置请求"""
    try:
        user_id = request.query.get('user_id', 'default')
        result = get_dashboard_config(user_id)
        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

async def handle_save_dashboard_layout(request) -> web.Response:
    """处理保存看板布局请求"""
    try:
        data = await request.json()
        user_id = data.get('user_id', 'default')
        layout_name = data.get('layout_name', 'default')
        layout_config = data.get('layout_config', {})
        is_default = data.get('is_default', False)
        
        if not layout_config:
            return web.json_response({
                "success": False,
                "message": "没有提供布局配置数据"
            })
        
        result = save_dashboard_layout(user_id, layout_name, layout_config, is_default)
        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

async def handle_get_dashboard_layout(request) -> web.Response:
    """处理获取看板布局请求"""
    try:
        user_id = request.query.get('user_id', 'default')
        layout_name = request.query.get('layout_name', None)
        
        result = get_dashboard_layout(user_id, layout_name)
        return web.json_response(result)
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

async def handle_get_all_dashboard_layouts(request) -> web.Response:
    """处理获取所有看板布局请求"""
    try:
        user_id = request.query.get('user_id', 'default')
        
        conn = None
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT layout_name, layout_config, is_default, created_at, updated_at
                FROM dashboard_layout 
                WHERE user_id = ?
                ORDER BY updated_at DESC
            """, (user_id,))
            
            rows = cursor.fetchall()
        finally:
            if conn:
                conn.close()
        
        layouts = []
        for row in rows:
            try:
                layout_config = json.loads(row[1])
            except:
                layout_config = row[1]
            
            layouts.append({
                "name": row[0],
                "config": layout_config,
                "is_default": row[2],
                "created_at": row[3],
                "updated_at": row[4]
            })
        
        return web.json_response({
            "success": True,
            "layouts": layouts
        })
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })

# 初始化数据库
init_dashboard_config_db()
async def handle_dashboard_config_shortcut(request) -> web.Response:
    """处理 /api/dashboard-config 快捷路由（兼容控制台前端）"""
    conn = None
    try:
        if request.method == 'POST':
            # 保存配置
            data = await request.json()
            user_id = 'default'
            config = data
            
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            # 保存每个配置项
            saved_count = 0
            for key, value in config.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO dashboard_config 
                    (user_id, config_key, config_value, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (user_id, key, json.dumps(value), datetime.now().isoformat()))
                saved_count += 1
            
            # 同步写入 config_storage（天气API从此表读取）
            cursor.execute("""
                INSERT OR REPLACE INTO config_storage (key, value, updated_at)
                VALUES ('dashboard', ?, ?)
            """, (json.dumps(config), datetime.now().isoformat()))
            
            conn.commit()
            
            return web.json_response({
                "success": True,
                "message": f"成功保存 {saved_count} 个配置项"
            })
        
        elif request.method == 'GET':
            # 获取配置
            user_id = 'default'
            
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT config_key, config_value 
                FROM dashboard_config 
                WHERE user_id = ?
            """, (user_id,))
            
            rows = cursor.fetchall()
            
            config = {}
            for row in rows:
                try:
                    value = json.loads(row[1])
                except:
                    value = row[1]
                config[row[0]] = value
            
            return web.json_response({
                "success": True,
                "config": config
            })
    
    except Exception as e:
        return web.json_response({
            "success": False,
            "message": f"请求处理失败: {str(e)}"
        })
    finally:
        if conn:
            conn.close()
