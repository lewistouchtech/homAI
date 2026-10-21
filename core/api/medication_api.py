"""家庭成员用药管理 API（2026-10-11）

健康管理页「家庭成员用药管理」区域的后端。
设计要点：
- 数据存 devices.db 的 medications 表（与 family_members 同库，member_id 关联）
- 用药记录是健康报告「干预感知分析」的权威数据源：
  报告以 start_date 为分界做服药前/后分段统计，建议随状态切换
- times 为 JSON 数组 ["08:00"]，meal_relation ∈ 饭前/饭后/随餐/空腹/任意
- remind=1 时由 medication_reminder 调度器到点播报
"""
import json
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')

_MEAL_RELATIONS = ('饭前', '饭后', '随餐', '空腹', '任意')


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_medications_table():
    """建表（幂等）。启动时调用一次。"""
    conn = _db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS medications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                dose TEXT DEFAULT '',
                indication TEXT DEFAULT '',
                times_per_day INTEGER DEFAULT 1,
                times TEXT DEFAULT '[]',
                meal_relation TEXT DEFAULT '任意',
                meal_minutes INTEGER DEFAULT 0,
                start_date TEXT DEFAULT '',
                end_date TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                remind INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()


def list_medications(member_id=None, include_inactive=True):
    """读取用药记录，按成员过滤可选。首次调用自动建表。"""
    try:
        init_medications_table()
    except Exception:
        pass
    conn = _db()
    try:
        sql = 'SELECT * FROM medications'
        args = []
        clauses = []
        if member_id:
            clauses.append('member_id = ?')
            args.append(member_id)
        if not include_inactive:
            clauses.append('active = 1')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY member_id, active DESC, id'
        rows = conn.execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _row_to_dict(r):
    d = dict(r)
    try:
        d['times'] = json.loads(d.get('times') or '[]')
    except Exception:
        d['times'] = []
    return d


def _validate(payload, partial=False):
    """校验并规范化入参，返回 (clean, error)。"""
    clean = {}
    if not partial or 'member_id' in payload:
        try:
            clean['member_id'] = int(payload.get('member_id'))
        except (TypeError, ValueError):
            return None, 'member_id 必须是整数'
    if not partial or 'name' in payload:
        name = str(payload.get('name') or '').strip()
        if not name:
            return None, '药名不能为空'
        if len(name) > 100:
            return None, '药名过长'
        clean['name'] = name
    for k in ('dose', 'indication', 'start_date', 'end_date', 'notes'):
        if k in payload:
            clean[k] = str(payload.get(k) or '').strip()[:500]
    if 'times_per_day' in payload:
        try:
            clean['times_per_day'] = max(1, min(int(payload['times_per_day']), 6))
        except (TypeError, ValueError):
            return None, 'times_per_day 必须是 1-6 的整数'
    if 'times' in payload:
        times = payload['times']
        if not isinstance(times, list):
            return None, 'times 必须是时间数组，如 ["08:00"]'
        norm = []
        for t in times[:6]:
            t = str(t).strip()
            if not t:
                continue
            try:
                datetime.strptime(t, '%H:%M')
            except ValueError:
                return None, f'服药时间格式错误：{t}（应为 HH:MM）'
            if t not in norm:
                norm.append(t)
        clean['times'] = json.dumps(sorted(norm), ensure_ascii=False)
    if 'meal_relation' in payload:
        mr = str(payload.get('meal_relation') or '任意').strip()
        if mr not in _MEAL_RELATIONS:
            return None, f'meal_relation 必须是：{" / ".join(_MEAL_RELATIONS)}'
        clean['meal_relation'] = mr
    if 'meal_minutes' in payload:
        try:
            clean['meal_minutes'] = max(0, min(int(payload['meal_minutes']), 120))
        except (TypeError, ValueError):
            return None, 'meal_minutes 必须是 0-120 的整数'
    for k in ('remind', 'active'):
        if k in payload:
            clean[k] = 1 if payload.get(k) in (1, True, '1', 'true', 'on') else 0
    return clean, None


def create_medication(payload):
    clean, err = _validate(payload)
    if err:
        return None, err
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _db()
    try:
        cur = conn.execute(
            'INSERT INTO medications (member_id, name, dose, indication, times_per_day,'
            ' times, meal_relation, meal_minutes, start_date, end_date, notes,'
            ' remind, active, created_at, updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (clean.get('member_id'), clean.get('name'), clean.get('dose', ''),
             clean.get('indication', ''), clean.get('times_per_day', 1),
             clean.get('times', '[]'), clean.get('meal_relation', '任意'),
             clean.get('meal_minutes', 0), clean.get('start_date', ''),
             clean.get('end_date', ''), clean.get('notes', ''),
             clean.get('remind', 1), clean.get('active', 1), now, now))
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()


def update_medication(med_id, payload):
    clean, err = _validate(payload, partial=True)
    if err:
        return err
    if not clean:
        return '没有可更新的字段'
    clean['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _db()
    try:
        sets = ', '.join(f'{k} = ?' for k in clean)
        cur = conn.execute(f'UPDATE medications SET {sets} WHERE id = ?',
                           (*clean.values(), med_id))
        conn.commit()
        if cur.rowcount == 0:
            return '用药记录不存在'
        return None
    finally:
        conn.close()


def delete_medication(med_id):
    conn = _db()
    try:
        cur = conn.execute('DELETE FROM medications WHERE id = ?', (med_id,))
        conn.commit()
        return None if cur.rowcount else '用药记录不存在'
    finally:
        conn.close()


# ── HTTP handlers ────────────────────────────────────────────────

async def handle_list_medications(request):
    member_id = request.query.get('member_id')
    include_inactive = request.query.get('active') != '1'
    try:
        mid = int(member_id) if member_id else None
    except ValueError:
        mid = None
    meds = list_medications(member_id=mid, include_inactive=include_inactive)
    return web.json_response({'success': True, 'medications': meds})


async def handle_create_medication(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': '请求体必须是 JSON'}, status=400)
    med_id, err = create_medication(payload)
    if err:
        return web.json_response({'success': False, 'error': err}, status=400)
    return web.json_response({'success': True, 'id': med_id, 'message': '用药记录已创建'})


async def handle_update_medication(request):
    med_id = request.match_info.get('id')
    try:
        med_id = int(med_id)
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'ID 无效'}, status=400)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': '请求体必须是 JSON'}, status=400)
    err = update_medication(med_id, payload)
    if err:
        return web.json_response({'success': False, 'error': err}, status=400)
    return web.json_response({'success': True, 'message': '用药记录已更新'})


async def handle_delete_medication(request):
    med_id = request.match_info.get('id')
    try:
        med_id = int(med_id)
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'ID 无效'}, status=400)
    err = delete_medication(med_id)
    if err:
        return web.json_response({'success': False, 'error': err}, status=404)
    return web.json_response({'success': True, 'message': '用药记录已删除'})


def register_routes(app_or_router):
    """注册路由。app_or_router 需支持 add_routes 或 router 方式。"""
    init_medications_table()
    routes = [
        web.get('/api/medications', handle_list_medications),
        web.post('/api/medications', handle_create_medication),
        web.put('/api/medications/{id}', handle_update_medication),
        web.delete('/api/medications/{id}', handle_delete_medication),
    ]
    app_or_router.add_routes(routes)
