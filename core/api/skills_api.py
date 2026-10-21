"""
技能管理API
"""
import sqlite3
import json
from aiohttp import web
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

async def delete_skill(request):
    """
    删除技能记录
    DELETE /api/skills/{skill_id}
    """
    try:
        skill_id = request.match_info.get('skill_id')
        if not skill_id:
            return web.json_response({
                'success': False,
                'message': '缺少技能ID'
            }, status=400)

        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
        
            # 从skills_config表中删除技能记录
            cursor.execute("DELETE FROM skills_config WHERE skill_id = ?", (skill_id,))
            deleted_count = cursor.rowcount
        
            conn.commit()
        finally:
            conn.close()
        
        if deleted_count > 0:
            return web.json_response({
                'success': True,
                'message': f'技能 {skill_id} 已从数据库删除',
                'deleted_count': deleted_count
            })
        else:
            return web.json_response({
                'success': False,
                'message': f'未找到技能 {skill_id}',
                'deleted_count': 0
            })
            
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'删除技能失败: {str(e)}'
        }, status=500)