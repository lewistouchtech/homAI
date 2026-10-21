"""
真实健康数据API - DEPRECATED（已废弃）

⚠️ 重要：此模块已废弃，请使用 health_data_api.py 替代
此模块的功能已被 health_data_api.py 完全覆盖，且效率更高

废弃原因：
1. 数据未与家庭成员关联
2. 读取原始OneNET数据，没有进行数据清洗
3. 已有更完善的 health_data_api.py 提供相同功能
4. 数据重复问题严重（需要去重处理）

请使用以下替代API：
- /api/health/data - 获取家庭成员关联的健康数据
- /api/health/devices - 获取健康设备列表
- /api/health-data/latest - 获取最新生命体征（已重定向到新API）

注意：此文件保留仅用于备份和历史参考，新开发请勿使用
"""
import sqlite3
from aiohttp import web
from pathlib import Path
from datetime import datetime

# 数据库路径
_DATA_DB = Path(__file__).parent.parent.parent / "data" / "health_data.db"
_ROOT_DB = Path(__file__).parent.parent.parent / "health_data.db"

def _get_conn():
    """获取数据库连接，优先使用 data/health_data.db"""
    path = _DATA_DB if _DATA_DB.exists() else _ROOT_DB
    return sqlite3.connect(str(path)), path

def _query_one(cursor, sql, params=None):
    """执行查询并返回一行"""
    if params:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)
    return cursor.fetchone()

def _query_all(cursor, sql, params=None):
    """执行查询并返回所有行"""
    if params:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)
    return cursor.fetchall()

def _table_exists(cursor, table_name):
    """检查表是否存在"""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cursor.fetchone() is not None

async def get_member_health_data(request):
    """DEPRECATED: 获取指定成员的健康数据 - 请使用 health_data_api.py"""
    try:
        # 记录废弃警告
        print("⚠️ DEPRECATED: /api/health-data/{member_name} 已废弃，请使用 /api/health/data?member_id=xxx")
        
        # 实际返回错误，建议使用新API
        return web.json_response({
            'success': False,
            'deprecated': True,
            'message': '此API已废弃，请使用 /api/health/data?member_id=xxx',
            'alternative': '/api/health/data',
            'usage_example': '/api/health/data?member_id=11&days=7'
        }, status=410)  # 410 Gone
        
    except Exception as e:
        return web.json_response({
            'success': False,
            'deprecated': True,
            'message': f'API已废弃: {str(e)}'
        }, status=410)

async def get_all_health_data(request):
    """DEPRECATED: 获取所有健康数据 - 请使用 health_data_api.py"""
    try:
        # 记录废弃警告
        print("⚠️ DEPRECATED: /api/health-data/all 已废弃，请使用 /api/health/data")
        
        return web.json_response({
            'success': False,
            'deprecated': True,
            'message': '此API已废弃，请使用 /api/health/data',
            'alternative': '/api/health/data',
            'usage_example': '/api/health/data?days=30&limit=100'
        }, status=410)  # 410 Gone
        
    except Exception as e:
        return web.json_response({
            'success': False,
            'deprecated': True,
            'message': f'API已废弃: {str(e)}'
        }, status=410)

async def get_latest_vitals(request):
    """DEPRECATED: 获取最新生命体征 - 已重定向到新逻辑"""
    try:
        # 注意：这个API已被修改，现在重定向到新逻辑
        # 保持兼容性，但输出警告
        print("⚠️ DEPRECATED API USED: /api/health-data/latest (但已更新为新逻辑)")
        
        # 实际逻辑已转移到新的实现
        # 这里保持原样，但建议前端更新
        from core.api.health_data_api import get_health_summary
        return await get_health_summary(request)
        
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })


async def get_latest_vitals_by_member(request):
    """根据成员名获取最新生命体征（2026-09-07 修复：原来只返回 410 重定向建议，前端 4 处调用全部拿不到数据）"""
    try:
        member = request.query.get('member', '')
        if not member:
            return web.json_response({'success': False, 'message': '缺少 member 参数'}, status=400)

        # 按名字查成员 ID
        conn = sqlite3.connect(str(Path(__file__).parent.parent.parent / "data" / "devices.db"))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM family_members WHERE name = ?", (member,))
            member_row = cursor.fetchone()
        finally:
            conn.close()

        if not member_row:
            return web.json_response({'success': False, 'message': f'成员不存在: {member}'}, status=404)

        # 直接查最新健康数据（每类取最新一条），与 health_data_api.get_health_records 同源
        conn = sqlite3.connect(str(Path(__file__).parent.parent.parent / "data" / "devices.db"))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT hd.id, hd.member_id, hd.data_type, hd.value_main, hd.unit, hd.timestamp "
                "FROM health_data hd WHERE hd.member_id = ? "
                "ORDER BY hd.timestamp DESC LIMIT 60", (member_row[0],))
            rows = cursor.fetchall()
        finally:
            conn.close()
        data = [{
            'id': r[0], 'member_id': r[1], 'data_type': r[2],
            'value': r[3], 'unit': r[4], 'timestamp': r[5],  # value=health_data.value_main
        } for r in rows]
        pass
        # 每种 data_type 只保留最新一条
        latest = {}
        for rec in data:
            t = rec.get('data_type')
            if t and t not in latest:
                latest[t] = rec
        return web.json_response({
            'success': True,
            'member': member,
            'member_id': member_row[0],
            'data': list(latest.values()),
            'count': len(latest),
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)
