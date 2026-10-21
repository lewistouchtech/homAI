"""健康事件表与 API（2026-10-11，主人裁定"内容尽量全、一次做对"）

设计原则：
- **事件表不合并现有任何表**：family_members=名册、health_data=设备高频测量、
  medications=当前用药状态（提醒器读），health_events=人工报告的低频事件（原来没地方存）
- 通用字段做正式列（可查询/可索引），类型特有字段全放 detail JSON（避免将来加列）
- status 支持「待确认」流：LLM 从飞书抽取后先建 pending，用户回"对"才生效
- 用药类事件自动联动 medications（单一写入入口，事件是日志、medications 是投影）

event_type 全集（detail 字段见 EVENT_DETAIL_SPEC）：
  medication_start / medication_adjust / medication_stop   用药开始/调量/停药
  visit_outpatient / visit_emergency / visit_hospitalization  门诊/急诊/住院
  checkup     体检（含设备测不了的数值：血脂/尿酸/糖化等，存 items 键值）
  symptom     症状主诉（头晕/心悸/胸闷…）
  lifestyle   生活方式改变（戒烟/戒酒/开始运动/饮食调整）
  inquiry_reply  主动问询的回复（闭环回填）
  measurement_manual  人工测量补录（数值写 health_data，事件留引用）
  note        其他健康备注
"""
import json
import os
import re
import sqlite3
from datetime import datetime

from aiohttp import web

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')

# ── 事件类型与 detail 字段规范（校验/抽取提示词/确认卡共用的单一事实源）──
EVENT_DETAIL_SPEC = {
    'medication_start': {
        'label': '开始用药',
        'required': ['drug_name'],
        'fields': {
            'drug_name': '药名', 'dose': '剂量(如5mg)', 'frequency': '每日次数(数字)',
            'times': '服药时间数组["08:00"]', 'meal_relation': '饭前/饭后/随餐/空腹/任意',
            'meal_minutes': '饭前/饭后分钟数(整数)', 'indication': '治什么',
            'prescribed_by': '开药医生', 'hospital': '开药医院', 'start_date': '开始日期YYYY-MM-DD',
        },
    },
    'medication_adjust': {
        'label': '调整剂量',
        'required': ['drug_name'],
        'fields': {
            'drug_name': '药名', 'old_dose': '原剂量', 'new_dose': '新剂量', 'reason': '调整原因',
        },
    },
    'medication_stop': {
        'label': '停药',
        'required': ['drug_name'],
        'fields': {
            'drug_name': '药名', 'reason': '停药原因(医嘱/自行/副作用)',
            'side_effect': '若有副作用，具体表现',
        },
    },
    'visit_outpatient': {
        'label': '门诊就诊',
        'required': [],
        'fields': {
            'hospital': '医院', 'department': '科室', 'doctor': '医生',
            'chief_complaint': '主诉(哪里不舒服)', 'diagnosis': '诊断结果',
            'advice': '医嘱', 'follow_up_date': '复诊日期YYYY-MM-DD',
        },
    },
    'visit_emergency': {
        'label': '急诊',
        'required': [],
        'fields': {
            'hospital': '医院', 'department': '科室', 'chief_complaint': '主诉',
            'diagnosis': '诊断', 'advice': '医嘱',
        },
    },
    'visit_hospitalization': {
        'label': '住院',
        'required': [],
        'fields': {
            'hospital': '医院', 'department': '科室', 'diagnosis': '诊断',
            'admit_date': '入院日期', 'discharge_date': '出院日期',
            'discharge_summary': '出院小结', 'advice': '医嘱',
        },
    },
    'checkup': {
        'label': '体检',
        'required': [],
        'fields': {
            'institution': '体检机构', 'items': '指标键值对{"总胆固醇":"5.2","尿酸":"420"}（设备测不了的）',
            'conclusion': '总检结论', 'suggestions': '体检建议',
            'attachments': '附件路径/图片key数组',
        },
    },
    'symptom': {
        'label': '症状',
        'required': ['symptom'],
        'fields': {
            'symptom': '症状名(头晕/心悸/胸闷/乏力…)', 'severity': '轻/中/重',
            'duration': '持续多久', 'trigger': '诱因', 'relieved_by': '如何缓解',
        },
    },
    'lifestyle': {
        'label': '生活方式改变',
        'required': ['change'],
        'fields': {
            'change': '改变内容(戒烟/戒酒/开始运动/饮食调整)',
            'target': '目标(如每天快走30分钟)',
        },
    },
    'inquiry_reply': {
        'label': '问询回复',
        'required': ['question', 'answer'],
        'fields': {
            'question': '系统问的原话', 'answer': '用户回答原话',
            'interpretation': '结构化解读(开始用药/就诊/休息不足/测量误差/无异常)',
            'related_metric': '关联指标(如血压)',
        },
    },
    'measurement_manual': {
        'label': '人工测量',
        'required': ['metric', 'value'],
        'fields': {
            'metric': '指标(血压/血糖/体温…)', 'value': '数值',
            'unit': '单位', 'context': '场景(餐前/餐后/晨起/睡前)',
            'health_data_id': '写入 health_data 后的行id',
        },
    },
    'note': {
        'label': '健康备注',
        'required': [],
        'fields': {},
    },
}

EVENT_TYPES = tuple(EVENT_DETAIL_SPEC.keys())

# 用药类事件类型（触发 medications 联动）
_MED_TYPES = ('medication_start', 'medication_adjust', 'medication_stop')

# 抽取预筛关键词（飞书入站先用它粗筛，避免每条消息都过 LLM）
HEALTH_KEYWORDS = re.compile(
    r'药|血压|血糖|血脂|尿酸|体检|医院|就诊|复查|门诊|急诊|住院|'
    r'毫克|ml|mg|粒|片|吃了|停药|换药|调量|头晕|心悸|胸闷|乏力|'
    r'戒烟|戒酒|运动|不舒服|医生|诊断|医嘱|手术')


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_health_events_table():
    """建表（幂等）。字段按"尽量全、一次做对"设计，详见模块 docstring。"""
    conn = _db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER,                -- 可为 NULL：待确认时还不知道是谁
                event_type TEXT NOT NULL,
                event_time TEXT DEFAULT '',       -- 发生时间 YYYY-MM-DD [HH:MM]
                record_time TEXT,                 -- 录入时间（自动）
                title TEXT DEFAULT '',            -- 一句话摘要（列表/报告显示用）
                detail TEXT DEFAULT '{}',         -- 类型特有字段 JSON
                note TEXT DEFAULT '',             -- 用户原话/自由备注（溯源）
                source TEXT DEFAULT 'manual',     -- feishu/voice/web/assistant
                source_user TEXT DEFAULT '',      -- 录入者（open_id 或声纹名）
                status TEXT DEFAULT 'confirmed',  -- pending(待确认)/confirmed/cancelled
                related_id INTEGER,               -- 关联记录（如 medications.id）
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_health_events_member_time'
            ' ON health_events(member_id, event_time DESC)')
        conn.commit()
    finally:
        conn.close()


def _validate(payload, partial=False):
    """校验入参，返回 (clean, error)。"""
    clean = {}
    if not partial or 'event_type' in payload:
        et = str(payload.get('event_type') or '').strip()
        if et not in EVENT_DETAIL_SPEC:
            return None, f'event_type 必须是：{" / ".join(EVENT_TYPES)}'
        clean['event_type'] = et
    if not partial or 'member_id' in payload:
        mid = payload.get('member_id')
        if mid in (None, ''):
            clean['member_id'] = None
        else:
            try:
                clean['member_id'] = int(mid)
            except (TypeError, ValueError):
                return None, 'member_id 必须是整数'
    for k in ('event_time', 'title', 'source', 'source_user'):
        if k in payload:
            clean[k] = str(payload.get(k) or '').strip()[:500]
    if 'event_time' in clean and clean['event_time']:
        if not re.match(r'^\d{4}-\d{2}-\d{2}', clean['event_time']):
            return None, 'event_time 应以 YYYY-MM-DD 开头'
    if 'detail' in payload:
        d = payload.get('detail')
        if not isinstance(d, dict):
            return None, 'detail 必须是对象'
        clean['detail'] = json.dumps(d, ensure_ascii=False)
    if 'note' in payload:
        clean['note'] = str(payload.get('note') or '').strip()[:2000]
    if 'status' in payload:
        st = str(payload.get('status') or '').strip()
        if st not in ('pending', 'confirmed', 'cancelled'):
            return None, 'status 必须是 pending/confirmed/cancelled'
        clean['status'] = st
    if 'related_id' in payload:
        rid = payload.get('related_id')
        clean['related_id'] = int(rid) if rid not in (None, '') else None
    return clean, None


def _sync_medication(event_type: str, member_id, detail: dict):
    """用药类事件 → medications 投影（单一联动入口）。

    medication_start: 同名药 upsert（active=1，带上剂量/时间/服法）
    medication_adjust: 同名药更新剂量
    medication_stop:   同名药 active=0
    """
    if event_type not in _MED_TYPES or not member_id:
        return None
    from core.api import medication_api
    drug = str(detail.get('drug_name') or '').strip()
    if not drug:
        return None
    meds = medication_api.list_medications(member_id=member_id)
    same = [m for m in meds if m['name'] == drug]

    if event_type == 'medication_start':
        payload = {
            'member_id': member_id, 'name': drug,
            'dose': detail.get('dose', ''),
            'indication': detail.get('indication', ''),
            'start_date': detail.get('start_date', ''),
            'active': 1,
        }
        if detail.get('times'):
            payload['times'] = detail['times']
        if detail.get('frequency'):
            try:
                payload['times_per_day'] = int(detail['frequency'])
            except (TypeError, ValueError):
                pass
        if detail.get('meal_relation'):
            payload['meal_relation'] = detail['meal_relation']
        if detail.get('meal_minutes') is not None:
            try:
                payload['meal_minutes'] = int(detail['meal_minutes'])
            except (TypeError, ValueError):
                pass
        if same:
            medication_api.update_medication(same[0]['id'], payload)
            return same[0]['id']
        med_id, err = medication_api.create_medication(payload)
        return med_id if not err else None

    if not same:
        return None
    if event_type == 'medication_adjust':
        medication_api.update_medication(same[0]['id'], {
            'dose': detail.get('new_dose', same[0].get('dose', ''))})
        return same[0]['id']
    if event_type == 'medication_stop':
        medication_api.update_medication(same[0]['id'], {'active': 0})
        return same[0]['id']
    return None


def _row_to_dict(r):
    d = dict(r)
    try:
        d['detail'] = json.loads(d.get('detail') or '{}')
    except Exception:
        d['detail'] = {}
    return d


def create_event(payload, sync_med=True):
    """创建事件；confirmed 的用药类事件自动联动 medications。"""
    try:
        init_health_events_table()
    except Exception:
        pass
    clean, err = _validate(payload)
    if err:
        return None, err
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    detail = json.loads(clean.get('detail', '{}'))
    med_id = None
    if clean.get('status', 'confirmed') == 'confirmed' and sync_med:
        med_id = _sync_medication(clean['event_type'], clean.get('member_id'), detail)
    conn = _db()
    try:
        cur = conn.execute(
            'INSERT INTO health_events (member_id, event_type, event_time, record_time,'
            ' title, detail, note, source, source_user, status, related_id,'
            ' created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (clean.get('member_id'), clean['event_type'],
             clean.get('event_time', ''), now,
             clean.get('title', ''), clean.get('detail', '{}'),
             clean.get('note', ''), clean.get('source', 'manual'),
             clean.get('source_user', ''), clean.get('status', 'confirmed'),
             med_id if med_id is not None else clean.get('related_id'),
             now, now))
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()


def list_events(member_id=None, event_type=None, status=None, limit=100, days=None):
    """查询事件（按发生时间倒序）。"""
    try:
        init_health_events_table()
    except Exception:
        pass
    conn = _db()
    try:
        sql = 'SELECT * FROM health_events'
        args = []
        clauses = []
        if member_id:
            clauses.append('member_id = ?')
            args.append(int(member_id))
        if event_type:
            clauses.append('event_type = ?')
            args.append(event_type)
        if status:
            clauses.append('status = ?')
            args.append(status)
        if days:
            since = (datetime.now() - __import__('datetime').timedelta(days=int(days))).strftime('%Y-%m-%d')
            clauses.append('event_time >= ?')
            args.append(since)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY event_time DESC, id DESC LIMIT ?'
        args.append(int(limit))
        return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def get_event(event_id):
    try:
        init_health_events_table()
    except Exception:
        pass
    conn = _db()
    try:
        r = conn.execute('SELECT * FROM health_events WHERE id = ?',
                         (int(event_id),)).fetchone()
        return _row_to_dict(r) if r else None
    finally:
        conn.close()


def update_event(event_id, payload, sync_med=True):
    """更新事件；状态变为 confirmed 时联动 medications。"""
    clean, err = _validate(payload, partial=True)
    if err:
        return err
    if not clean:
        return '没有可更新的字段'
    ev = get_event(event_id)
    if not ev:
        return '事件不存在'
    # 状态 → confirmed 的用药事件补联动
    new_status = clean.get('status', ev['status'])
    if sync_med and new_status == 'confirmed' and ev['status'] != 'confirmed':
        med_id = _sync_medication(ev['event_type'], ev['member_id'], ev['detail'])
        if med_id is not None:
            clean['related_id'] = med_id
    clean['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _db()
    try:
        sets = ', '.join(f'{k} = ?' for k in clean)
        cur = conn.execute(f'UPDATE health_events SET {sets} WHERE id = ?',
                           (*clean.values(), int(event_id)))
        conn.commit()
        return None if cur.rowcount else '事件不存在'
    finally:
        conn.close()


def delete_event(event_id):
    conn = _db()
    try:
        cur = conn.execute('DELETE FROM health_events WHERE id = ?', (int(event_id),))
        conn.commit()
        return None if cur.rowcount else '事件不存在'
    finally:
        conn.close()


def confirm_event(event_id):
    """确认卡「对」→ confirmed + 联动。"""
    return update_event(event_id, {'status': 'confirmed'})


def cancel_event(event_id):
    return update_event(event_id, {'status': 'cancelled'}, sync_med=False)


# ── HTTP handlers ──────────────────────────────────────────────

async def handle_list_events(request):
    try:
        member_id = int(request.query['member_id']) if request.query.get('member_id') else None
    except (KeyError, ValueError):
        member_id = None
    events = list_events(
        member_id=member_id,
        event_type=request.query.get('event_type'),
        status=request.query.get('status'),
        limit=request.query.get('limit', 100),
        days=request.query.get('days'))
    return web.json_response({'success': True, 'events': events})


async def handle_create_event(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': '请求体必须是 JSON'}, status=400)
    eid, err = create_event(payload)
    if err:
        return web.json_response({'success': False, 'error': err}, status=400)
    return web.json_response({'success': True, 'id': eid, 'message': '事件已创建'})


async def handle_update_event(request):
    try:
        eid = int(request.match_info['id'])
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'ID 无效'}, status=400)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': '请求体必须是 JSON'}, status=400)
    err = update_event(eid, payload)
    if err:
        return web.json_response({'success': False, 'error': err}, status=400)
    return web.json_response({'success': True, 'message': '事件已更新'})


async def handle_delete_event(request):
    try:
        eid = int(request.match_info['id'])
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'ID 无效'}, status=400)
    err = delete_event(eid)
    if err:
        return web.json_response({'success': False, 'error': err}, status=404)
    return web.json_response({'success': True, 'message': '事件已删除'})


def register_routes(app):
    init_health_events_table()
    app.add_routes([
        web.get('/api/health-events', handle_list_events),
        web.post('/api/health-events', handle_create_event),
        web.put('/api/health-events/{id}', handle_update_event),
        web.delete('/api/health-events/{id}', handle_delete_event),
    ])
