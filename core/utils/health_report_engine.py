"""健康报告分析引擎（2026-10-11）

健康报告 v2 的核心：把原始测量序列变成「重点提醒 + 长期趋势 + 分段建议」。
与前端约定：报告 content 内嵌 `<!-- HB_DATA: {json} -->`，前端解析后按 v2 版式渲染；
解析不到则回退旧版 markdown 渲染。

设计要点（与主人 2026-10-11 裁定一致）：
- 按天聚合：设备每小时自动测量会产生大量重复点，趋势图必须按天取平均
- 脏数据剔除：舒张压≥收缩压、超出人体可能范围——只剔除出统计，不动原始记录
- 干预感知：用药档案（medications 表）是权威源，以 start_date 分段统计服药前/后；
  无用药记录时不瞎猜，措辞谨慎
- 提醒分级：连续超标 ≥7 天 🔴 建议就医 / 3-6 天 🟡 保持观察（主人裁定：一周以上才提醒就医）
- 建议双轨：详细书面版进报告，3-4 句口播摘要走语音
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

def find_family_member(db_path, name):
    """按名字查家庭成员，带"我"兜底（2026-10-11）。

    返回 member_info dict 或 None。解析规则：
    1. 精确命中 name（或去掉末尾"的"）→ 直接返回
    2. name 是"我/自己/本人"或为空 → 选健康数据记录最多的成员
       （设备自动测量，记录最多者基本就是被监测的主人）
    3. 全家只有一个成员 → 直接返回那个
    """
    if not os.path.exists(db_path):
        return None
    aliases = {'我', '自己', '本人', ''}
    conn = sqlite3.connect(db_path)
    try:
        def _row_to_info(row):
            return {'id': row[0], 'name': row[1], 'relation': row[2],
                    'age': row[3], 'gender': row[4], 'birthday': row[5],
                    'height': row[6], 'weight': row[7], 'occupation': row[8],
                    'health_status': row[9]}

        name = (name or '').strip().rstrip('的')
        if name and name not in aliases:
            row = conn.execute(
                'SELECT id, name, relation, age, gender, birthday, height, weight,'
                ' occupation, health_status FROM family_members WHERE name = ?',
                (name,)).fetchone()
            if row:
                return _row_to_info(row)

        rows = conn.execute(
            'SELECT id, name, relation, age, gender, birthday, height, weight,'
            ' occupation, health_status FROM family_members').fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return _row_to_info(rows[0])
        if not name or name in aliases:
            # 2026-10-11 主人裁定：身份必须以声纹为准，禁止"谁数据多就是谁"的猜测。
            # 这里只保留"全家只有一个人"的合理短路；多成员家庭的"我"由调用方走
            # "没听出来→请再说两句→温和地问是谁"的流程（见 show_health_report）。
            return None
        return None
    finally:
        conn.close()


# ── 年龄段标准（与原 console_api.get_age_standards 一致）────────────────
def get_age_standards(age):
    if age <= 3: return {'sysMin': 85, 'sysMax': 105, 'sysIdeal': 95, 'diaMin': 50, 'diaMax': 70, 'diaIdeal': 60, 'hrMin': 80, 'hrMax': 130, 'hrIdeal': 105, 'wtIdeal': 14, 'bmiMin': 14, 'bmiMax': 18, 'bmiIdeal': 16}
    if age <= 5: return {'sysMin': 90, 'sysMax': 110, 'sysIdeal': 100, 'diaMin': 55, 'diaMax': 75, 'diaIdeal': 65, 'hrMin': 75, 'hrMax': 120, 'hrIdeal': 95, 'wtIdeal': 18, 'bmiMin': 13.5, 'bmiMax': 17.5, 'bmiIdeal': 15.5}
    if age <= 8: return {'sysMin': 90, 'sysMax': 125, 'sysIdeal': 105, 'diaMin': 55, 'diaMax': 85, 'diaIdeal': 68, 'hrMin': 70, 'hrMax': 115, 'hrIdeal': 90, 'wtIdeal': 25, 'bmiMin': 13, 'bmiMax': 18, 'bmiIdeal': 15.5}
    if age <= 12: return {'sysMin': 100, 'sysMax': 120, 'sysIdeal': 110, 'diaMin': 60, 'diaMax': 80, 'diaIdeal': 70, 'hrMin': 65, 'hrMax': 110, 'hrIdeal': 85, 'wtIdeal': 38, 'bmiMin': 14, 'bmiMax': 20, 'bmiIdeal': 17}
    if age <= 15: return {'sysMin': 105, 'sysMax': 125, 'sysIdeal': 115, 'diaMin': 65, 'diaMax': 82, 'diaIdeal': 73, 'hrMin': 60, 'hrMax': 105, 'hrIdeal': 80, 'wtIdeal': 52, 'bmiMin': 16, 'bmiMax': 22, 'bmiIdeal': 19}
    if age <= 18: return {'sysMin': 105, 'sysMax': 128, 'sysIdeal': 116, 'diaMin': 65, 'diaMax': 83, 'diaIdeal': 74, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 75, 'wtIdeal': 60, 'bmiMin': 17, 'bmiMax': 23, 'bmiIdeal': 20}
    if age <= 35: return {'sysMin': 90, 'sysMax': 120, 'sysIdeal': 110, 'diaMin': 60, 'diaMax': 80, 'diaIdeal': 72, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 70, 'bmiMin': 18.5, 'bmiMax': 24, 'bmiIdeal': 21.5}
    if age <= 50: return {'sysMin': 95, 'sysMax': 130, 'sysIdeal': 115, 'diaMin': 60, 'diaMax': 85, 'diaIdeal': 75, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 72, 'bmiMin': 18.5, 'bmiMax': 24.5, 'bmiIdeal': 22}
    if age <= 65: return {'sysMin': 100, 'sysMax': 140, 'sysIdeal': 120, 'diaMin': 65, 'diaMax': 90, 'diaIdeal': 78, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 70, 'bmiMin': 19, 'bmiMax': 25, 'bmiIdeal': 22.5}
    return {'sysMin': 100, 'sysMax': 150, 'sysIdeal': 125, 'diaMin': 65, 'diaMax': 90, 'diaIdeal': 80, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 68, 'bmiMin': 20, 'bmiMax': 26, 'bmiIdeal': 23}


# ── 脏数据剔除 ─────────────────────────────────────────────────
def _is_dirty_bp(sys_v, dia_v):
    if sys_v is None or dia_v is None:
        return False  # 缺一项不算脏，交给统计时跳过
    return dia_v >= sys_v or sys_v < 50 or sys_v > 260 or dia_v < 30 or dia_v > 180


def _is_dirty_spo2(v):
    return v is not None and (v < 70 or v > 100)


def _is_dirty_weight(v):
    return v is not None and (v < 20 or v > 350)


def _is_dirty_glucose(v):
    return v is not None and (v < 1.5 or v > 40)


def _is_dirty_hr(v):
    return v is not None and (v < 25 or v > 250)


# ── 数据加载与按天聚合 ─────────────────────────────────────────
def load_member_series(db_path, member_id, limit_rows=500):
    """从 devices.db health_data 读取成员数据，脏数据剔除后按天聚合。

    返回: {
      'blood_pressure': [{'date','sys','dia','pulse'}...],   # 按日期升序
      'blood_oxygen':   [{'date','spo2'}...],
      'weight':         [{'date','weight'}...],
      'glucose':        [{'date','glucose'}...],
      'heart_rate':     [{'date','hr'}...],
      'temperature':    [{'date','temp'}...],
      'latest':         {type: 'MM-DD HH:MM'},               # 各类型最近测量时间（含脏数据，仅展示）
      'data_start':     'YYYY-MM-DD'|None,
    }
    """
    result = {k: [] for k in (
        'blood_pressure', 'blood_oxygen', 'weight', 'glucose', 'heart_rate', 'temperature')}
    latest = {}
    data_start = None
    if not os.path.exists(db_path) or not member_id:
        return {**result, 'latest': latest, 'data_start': data_start}

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            'SELECT data_type, value_main, value_extra, timestamp FROM health_data'
            ' WHERE member_id = ? ORDER BY timestamp DESC LIMIT ?',
            (member_id, limit_rows)).fetchall()
    finally:
        conn.close()

    # 原始点（升序）
    raw = {k: [] for k in result}
    for data_type, value_main, value_extra, ts in rows:
        ts = str(ts or '')
        date = ts.split(' ')[0] if ts else ''
        if not date:
            continue
        if data_start is None or date < data_start:
            data_start = date
        if data_type not in raw:
            continue
        if data_type not in latest:
            latest[data_type] = ts[5:16]  # 'MM-DD HH:MM'
        try:
            extra = json.loads(value_extra) if value_extra else {}
        except Exception:
            extra = {}
        try:
            main = float(value_main) if value_main not in (None, '') else None
        except (TypeError, ValueError):
            main = None

        if data_type == 'blood_pressure':
            sys_v = extra.get('systolic') or main
            dia_v = extra.get('diastolic')
            pulse = extra.get('pulse')
            try:
                sys_v = float(sys_v) if sys_v is not None else None
                dia_v = float(dia_v) if dia_v is not None else None
                pulse = float(pulse) if pulse is not None else None
            except (TypeError, ValueError):
                continue
            if _is_dirty_bp(sys_v, dia_v):
                continue
            raw[data_type].append({'date': date, 'sys': sys_v, 'dia': dia_v, 'pulse': pulse})
        elif data_type == 'blood_oxygen':
            spo2 = extra.get('spo2') or main
            try:
                spo2 = float(spo2) if spo2 is not None else None
            except (TypeError, ValueError):
                continue
            if _is_dirty_spo2(spo2):
                continue
            raw[data_type].append({'date': date, 'spo2': spo2})
        elif data_type == 'weight':
            w = main
            if _is_dirty_weight(w):
                continue
            if w is not None:
                raw[data_type].append({'date': date, 'weight': w})
        elif data_type == 'glucose':
            g = main
            if _is_dirty_glucose(g):
                continue
            if g is not None:
                raw[data_type].append({'date': date, 'glucose': g})
        elif data_type == 'heart_rate':
            hr = main
            if _is_dirty_hr(hr):
                continue
            if hr is not None:
                raw[data_type].append({'date': date, 'hr': hr})
        elif data_type == 'temperature':
            temp = main or extra.get('temp')
            try:
                temp = float(temp) if temp is not None else None
            except (TypeError, ValueError):
                continue
            if temp is not None and 30 <= temp <= 45:
                raw[data_type].append({'date': date, 'temp': temp})

    # 按天聚合（同一天多次取平均）
    for k in result:
        by_day = {}
        for p in raw[k]:
            d = by_day.setdefault(p['date'], {kk: [] for kk in p if kk != 'date'})
            for kk, vv in p.items():
                if kk != 'date' and vv is not None:
                    d[kk].append(vv)
        for date in sorted(by_day):
            entry = {'date': date}
            for kk, vals in by_day[date].items():
                entry[kk] = round(sum(vals) / len(vals), 1) if vals else None
            result[k].append(entry)
    return {**result, 'latest': latest, 'data_start': data_start}


# ── 区间与判词工具 ────────────────────────────────────────────
def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _split_windows(series, date_key, value_keys, window_days=30):
    """把按天序列切成 近30天 / 前30天 两窗，返回各窗均值 dict。"""
    if not series:
        return None, None
    max_date = datetime.strptime(series[-1]['date'], '%Y-%m-%d')
    recent_start = (max_date - timedelta(days=window_days - 1)).strftime('%Y-%m-%d')
    prev_end = (max_date - timedelta(days=window_days)).strftime('%Y-%m-%d')
    prev_start = (max_date - timedelta(days=window_days * 2 - 1)).strftime('%Y-%m-%d')
    recent = [p for p in series if p['date'] >= recent_start]
    prev = [p for p in series if prev_start <= p['date'] <= prev_end]
    def agg(pts):
        return {k: _mean([p.get(k) for p in pts]) for k in value_keys}
    return (agg(recent) if recent else None), (agg(prev) if prev else None)


def _fmt_bp(sys_v, dia_v):
    if sys_v is None or dia_v is None:
        return None
    return f"{int(round(sys_v))}/{int(round(dia_v))}"


# ── 主分析入口 ────────────────────────────────────────────────
def analyze_health(member_info, series, medications=None, events=None):
    """生成 HB_DATA v2 结构化报告数据。

    member_info: family_members 行 dict（含 age/height/weight/health_status…），可为 None
    series: load_member_series() 的返回
    medications: list_medications(member_id) 的返回（active 优先）
    events: health_event_api.list_events(member_id=...) 的返回（治疗/就诊/体检时间线）
    """
    member_info = member_info or {}
    medications = [m for m in (medications or []) if m.get('active')]
    events = events or []
    member_age = 30
    try:
        member_age = int(member_info.get('age') or 30)
    except (TypeError, ValueError):
        pass
    std = get_age_standards(member_age)
    name = (member_info or {}).get('name') or '未知'

    alerts = []
    trends = []
    advice = []
    speak_bits = []

    # 用药分段基准：取最早的开始服药日
    med_start = None
    for m in medications:
        sd = str(m.get('start_date') or '').strip()
        if sd and (med_start is None or sd < med_start):
            med_start = sd

    # ── 血压 ──────────────────────────────────────────────
    bp = series.get('blood_pressure') or []
    latest_bp = bp[-1] if bp else {}
    bp_alerts, bp_trend, bp_advice, bp_speak = _analyze_bp(
        bp, std, medications, med_start, name)
    alerts += bp_alerts
    if bp_trend:
        trends.append(bp_trend)
    if bp_advice:
        advice.append(bp_advice)
    if bp_speak:
        speak_bits.append(bp_speak)

    # ── 血氧 ──────────────────────────────────────────────
    bo = series.get('blood_oxygen') or []
    bo_alerts, bo_trend, bo_advice, bo_speak = _analyze_spo2(bo)
    alerts += bo_alerts
    if bo_trend:
        trends.append(bo_trend)
    if bo_advice:
        advice.append(bo_advice)
    if bo_speak:
        speak_bits.append(bo_speak)

    # ── 体重 / BMI ────────────────────────────────────────
    wt = series.get('weight') or []
    wt_alerts, wt_trend, wt_advice, wt_speak = _analyze_weight(
        wt, member_info, std)
    alerts += wt_alerts
    if wt_trend:
        trends.append(wt_trend)
    if wt_advice:
        advice.append(wt_advice)
    if wt_speak:
        speak_bits.append(wt_speak)

    # ── 血糖 ──────────────────────────────────────────────
    glu = series.get('glucose') or []
    glu_alerts, glu_trend, glu_advice, glu_speak = _analyze_glucose(glu)
    alerts += glu_alerts
    if glu_trend:
        trends.append(glu_trend)
    if glu_advice:
        advice.append(glu_advice)
    if glu_speak:
        speak_bits.append(glu_speak)

    # ── 心率（附在血氧建议，不单独成卡）────────────────────
    hr = series.get('heart_rate') or []
    hr_note = ''
    if hr:
        hr_latest = hr[-1].get('hr')
        if hr_latest is not None:
            hr_note = f"心率 {int(hr_latest)} bpm"
            if not (std['hrMin'] <= hr_latest <= std['hrMax']):
                alerts.append({
                    'level': 'yellow', 'tag': '保持观察',
                    'title': f"心率 {int(hr_latest)} bpm 超出正常范围（{std['hrMin']}-{std['hrMax']}）",
                    'body': '单次测量超出范围，可能是测量时活动或情绪影响。',
                    'action': '建议：安静休息 5 分钟后复测，若多次超标再关注。'})

    # ── 体温 ──────────────────────────────────────────────
    tp = series.get('temperature') or []
    for p in tp[-3:]:
        t = p.get('temp')
        if t is None:
            continue
        if t >= 38:
            alerts.append({'level': 'red', 'tag': '建议就医',
                           'title': f"体温 {t}°C，达到发热标准",
                           'body': '近期出现发热测量值。',
                           'action': '建议：密切观察体温变化，持续发热请及时就医。'})
        elif t >= 37.3:
            alerts.append({'level': 'yellow', 'tag': '保持观察',
                           'title': f"体温 {t}°C，略高于正常",
                           'body': '低热范围，可能为生理性波动。',
                           'action': '建议：多喝水休息，明日复测。'})

    # ── 总体判词 ──────────────────────────────────────────
    if any(a['level'] == 'red' for a in alerts):
        overall = {'level': 'bad', 'text': '🔴 建议就医',
                   'sub': '存在持续超标指标，建议尽快复查'}
    elif any(a['level'] == 'yellow' for a in alerts):
        overall = {'level': 'warn', 'text': '🟡 需要关注',
                   'sub': '存在需注意的指标变化'}
    else:
        overall = {'level': 'ok', 'text': '🟢 整体良好',
                   'sub': '各项指标均在正常范围'}

    # ── 基本信息区 ────────────────────────────────────────
    basic = {}
    mi = member_info or {}
    if mi.get('relation'):
        basic['家庭关系'] = mi['relation']
    if mi.get('gender'):
        basic['性别'] = mi['gender']
    if member_age:
        basic['年龄'] = f"{member_age} 岁"
    if mi.get('birthday'):
        basic['生日'] = mi['birthday']
    if mi.get('height'):
        basic['身高'] = f"{mi['height']} cm"
    if mi.get('weight'):
        basic['体重'] = f"{mi['weight']} kg"
    if mi.get('health_status'):
        basic['健康状态'] = mi['health_status']
    if medications:
        med_txt = '；'.join(
            f"💊 {m['name']}" + (f" {m['dose']}" if m.get('dose') else '')
            + (f"（{m['start_date']} 起）" if m.get('start_date') else '')
            for m in medications)
        basic['当前用药'] = med_txt
    if series.get('data_start'):
        basic['数据周期'] = f"{series['data_start']} 至今"

    measure_times = ' ｜ '.join(
        f"{ {'blood_pressure':'血压','blood_oxygen':'血氧','weight':'体重','glucose':'血糖','heart_rate':'心率','temperature':'体温'}.get(k, k) } {v}"
        for k, v in series.get('latest', {}).items()) or '暂无测量记录'

    # ── 指标表 ────────────────────────────────────────────
    metrics = []
    if bp:
        sys_v = latest_bp.get('sys')
        dia_v = latest_bp.get('dia')
        recent, prev = _split_windows(bp, 'date', ('sys', 'dia', 'pulse'))
        over = sys_v is not None and dia_v is not None and (
            sys_v > std['sysMax'] or dia_v > std['diaMax'])
        on_med = bool(medications) and med_start
        metrics.append({
            'k': '血压', 'v': (_fmt_bp(sys_v, dia_v) or '--') + ' mmHg',
            'cls': 'bad' if over else 'ok',
            's': ('🔴 偏高' if over else '🟢 正常' + ('（用药控制）' if on_med and not over else '')),
            'avg30': _fmt_bp(*[(recent or {}).get(k) for k in ('sys', 'dia')]) or '--',
            'prev': (_fmt_bp(*[(prev or {}).get(k) for k in ('sys', 'dia')]) or '--') if prev else '--'})
    if bo:
        spo2 = bo[-1].get('spo2')
        recent, prev = _split_windows(bo, 'date', ('spo2',))
        low = spo2 is not None and spo2 < 95
        metrics.append({
            'k': '血氧', 'v': (f"{int(spo2)} %" if spo2 is not None else '--'),
            'cls': 'bad' if (spo2 is not None and spo2 < 93) else 'warn' if low else 'ok',
            's': '🔴 偏低' if spo2 is not None and spo2 < 93 else '🟡 略低' if low else '🟢 正常',
            'avg30': str((recent or {}).get('spo2') or '--'),
            'prev': str((prev or {}).get('spo2') or '--') if prev else '--'})
    if wt:
        w = wt[-1].get('weight')
        recent, prev = _split_windows(wt, 'date', ('weight',))
        h = (mi.get('height') or 0) / 100 if mi.get('height') else 0
        bmi = round(w / (h * h), 1) if w and h else None
        bmi_cls = 'ok'
        bmi_s = '🟢 正常'
        if bmi is not None:
            if bmi > std['bmiMax']:
                bmi_cls, bmi_s = 'warn', '🟡 略高'
            elif bmi < std['bmiMin']:
                bmi_cls, bmi_s = 'warn', '🟡 偏瘦'
        metrics.append({
            'k': '体重', 'v': (f"{w} kg" if w is not None else '--'),
            'cls': 'ok', 's': '🟢 记录中',
            'avg30': str((recent or {}).get('weight') or '--'),
            'prev': str((prev or {}).get('weight') or '--') if prev else '--'})
        metrics.append({
            'k': 'BMI', 'v': str(bmi or '--'),
            'cls': bmi_cls, 's': bmi_s,
            'avg30': '--', 'prev': '--'})
    if hr:
        hrv = hr[-1].get('hr')
        metrics.append({'k': '心率', 'v': (f"{int(hrv)} bpm" if hrv else '--'),
                        'cls': 'ok', 's': '🟢 正常',
                        'avg30': '--', 'prev': '--'})
    if glu:
        g = glu[-1].get('glucose')
        recent, prev = _split_windows(glu, 'date', ('glucose',))
        g_cls, g_s = 'ok', '🟢 正常'
        if g is not None:
            if g >= 7:
                g_cls, g_s = 'bad', '🔴 偏高'
            elif g >= 6.1:
                g_cls, g_s = 'warn', '🟡 接近上限'
        metrics.append({
            'k': '血糖', 'v': (f"{g} mmol/L" if g is not None else '--'),
            'cls': g_cls, 's': g_s,
            'avg30': str((recent or {}).get('glucose') or '--'),
            'prev': str((prev or {}).get('glucose') or '--') if prev else '--'})

    speak_summary = (f"主人，{ '；'.join(speak_bits) }" if speak_bits else
                     f"主人，{name}目前各项健康指标都在正常范围。")

    # ── 治疗与就诊时间线 + 最新体检（2026-10-11，事件表接入）──
    timeline = []
    for ev in events:
        if ev.get('status') != 'confirmed':
            continue
        et = ev.get('event_type', '')
        if et.startswith(('medication_', 'visit_')) or et == 'lifestyle':
            icon = ('💊' if et.startswith('medication_')
                    else '🏥' if et.startswith('visit_') else '🏃')
            timeline.append({
                'date': (ev.get('event_time') or '')[:10],
                'icon': icon,
                'type': et,
                'title': ev.get('title') or ev.get('note', '')[:40],
            })
    # 无开药事件时，用当前用药档案的开始日期补时间线（不然档案录了却不显示）
    have_med_start = any(t['type'] == 'medication_start' for t in timeline)
    if not have_med_start:
        for m in medications:
            sd = str(m.get('start_date') or '').strip()
            if sd:
                dose = f" {m['dose']}" if m.get('dose') else ''
                timeline.append({
                    'date': sd[:10], 'icon': '💊', 'type': 'medication_start',
                    'title': f"开始服用{m['name']}{dose}",
                })
    timeline.sort(key=lambda x: x['date'])
    checkup = None
    for ev in events:
        if ev.get('event_type') == 'checkup' and ev.get('status') == 'confirmed':
            d = ev.get('detail') or {}
            items = d.get('items') or {}
            checkup = {
                'date': (ev.get('event_time') or '')[:10],
                'institution': d.get('institution', ''),
                'items': items,
                'conclusion': d.get('conclusion', ''),
            }
            break

    hb_data = {
        'version': 2,
        'member_name': name,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'overall': overall,
        'basic': basic,
        'measure_times': measure_times,
        'alerts': alerts,
        'trends': trends,
        'metrics': metrics,
        'advice': advice,
        'timeline': timeline,
        'checkup': checkup,
        'speak_summary': speak_summary,
        'note': '报告由 homAi 自动生成，仅供参考，不能替代专业医疗建议。如出现明显不适请及时就医。',
    }
    return hb_data


# ── 各指标分析（alerts, trend, advice, speak）────────────────
def _analyze_bp(bp, std, medications, med_start, name):
    alerts, trend, advice, speak = [], None, None, ''
    if not bp:
        return alerts, trend, advice, speak

    latest = bp[-1]
    over = lambda p: (p.get('sys') is not None and p.get('dia') is not None
                      and (p['sys'] > std['sysMax'] or p['dia'] > std['diaMax']))
    # 连续超标天数（从最近往前数）
    streak = 0
    for p in reversed(bp):
        if over(p):
            streak += 1
        else:
            break

    series = [
        {'color': '#ef6a6a', 'label': '收缩压',
         'points': [[p['date'], p['sys']] for p in bp if p.get('sys') is not None]},
        {'color': '#7ba7ff', 'label': '舒张压',
         'points': [[p['date'], p['dia']] for p in bp if p.get('dia') is not None]},
    ]

    # 用药分段
    med_effective = False
    pre_txt = post_txt = None
    if med_start:
        pre = [p for p in bp if p['date'] < med_start]
        post = [p for p in bp if p['date'] >= med_start]
        if pre and post:
            pre_txt = _fmt_bp(_mean([p['sys'] for p in pre]), _mean([p['dia'] for p in pre]))
            post_txt = _fmt_bp(_mean([p['sys'] for p in post]), _mean([p['dia'] for p in post]))
            post_over = [p for p in post if over(p)]
            med_effective = len(post_over) == 0

    if med_start and med_effective:
        # 服药后控制良好
        days = len([p for p in bp if p['date'] >= med_start])
        alerts.append({
            'level': 'green', 'tag': '控制良好',
            'title': f"服药后血压已回到正常范围，切勿自行停药",
            'body': (f"{med_start} 开始服用降压药后，血压从服药前均值 {pre_txt} 降至 {post_txt}，"
                     f"已连续 {days} 天达标。提醒：血压正常是药物作用的结果，不是病好了——"
                     f"自行停药后血压通常会回升。"),
            'action': '建议：遵医嘱规律服药，每天固定时间测量；如出现头晕等不适，及时复诊而非自行调量。'})
        compare = f"服药前 <b>{pre_txt}</b> → 服药后 <b>{post_txt}</b>"
        verdict, vtext = 'up', '📈 服药后控制良好'
        foot = f"虚线之间为正常范围（{std['sysMin']}-{std['sysMax']}）；竖线 = 开始服药（{med_start[5:]}）"
        advice = {
            'icon': '🩺',
            'title': '血压：药物控制有效，关键是坚持',
            'paras': [
                (f"{med_start} 开始服用降压药后，血压从服药前均值 {pre_txt} 降至 {post_txt}，"
                 f"已连续 {days} 天达标——说明当前用药方案对症。"),
                ('最重要的一条：不要自行停药或减量。血压正常是药物维持的结果，停药后绝大多数人会回升，'
                 '而血压的反复波动比持续偏高伤害更大。如果服药期间出现头晕、乏力，可能是药量偏大，'
                 '正确做法是带着这份记录去复诊，让医生调量。日常继续保持低盐饮食和规律作息，'
                 '它们是药物的"助攻"而不是替代。')]}
        speak = f"您 {med_start[5:]} 开始吃降压药以后，血压从 {pre_txt} 降到了 {post_txt}，控制得很好，记得按时吃药别自行停"
    elif med_start and not med_effective and post_txt:
        # 服药后仍超标
        alerts.append({
            'level': 'red', 'tag': '建议就医',
            'title': '服药后血压仍未达标，建议复诊调药',
            'body': (f"{med_start} 开始服药，服药后均值 {post_txt} 仍超出正常范围"
                     f"（{std['sysMax']}/{std['diaMax']} 以下）。"),
            'action': '建议：尽快复诊，医生会根据记录调整用药方案；就诊前不要自行加量。'})
        compare = f"服药前 <b>{pre_txt}</b> → 服药后 <b>{post_txt}</b>（未达标）"
        verdict, vtext = 'down', '📉 服药后仍未达标'
        foot = f"虚线之间为正常范围；竖线 = 开始服药（{med_start[5:]}）"
        advice = {
            'icon': '🩺',
            'title': '血压：当前用药方案控制不足',
            'paras': [
                (f"服药后血压均值 {post_txt}，仍高于目标值。这不是『没效果』，而是『还不够』——"
                 f"常见原因包括药量不足、单一药物不合适、或盐摄入/作息影响。"),
                ('请带着这份测量记录尽快复诊，医生调整方案后通常 1-2 周可见效。'
                 '同时严格低盐饮食，否则药效会打折扣。')]}
        speak = f"您吃药以后血压还在 {post_txt} 左右，没降到目标值，建议尽快复诊调药"
    else:
        # 无用药记录
        recent, prev = _split_windows(bp, 'date', ('sys', 'dia'))
        if streak >= 7:
            alerts.append({
                'level': 'red', 'tag': '建议就医',
                'title': f"血压连续 {streak} 天超出正常范围",
                'body': (f"最近 {streak} 天测量均值 {_fmt_bp(latest.get('sys'), latest.get('dia'))}，"
                         f"收缩压正常应 ≤{std['sysMax']}、舒张压 ≤{std['diaMax']}。"
                         f"持续性偏高不是偶发现象。"),
                'action': '建议：尽快到医院心内科复查，测量记录已自动保存，复诊时可直接给医生看这份报告。'})
            verdict, vtext = 'down', f'📉 连续 {streak} 天超标'
            foot = f"虚线之间为正常范围（{std['sysMin']}-{std['sysMax']}），红点 = 超标测量"
        elif streak >= 3:
            alerts.append({
                'level': 'yellow', 'tag': '保持观察',
                'title': f"血压连续 {streak} 天偏高（未达就医提醒线）",
                'body': (f"最近 {streak} 天血压 {_fmt_bp(latest.get('sys'), latest.get('dia'))} 超出正常范围，"
                         f"持续观察中，若连续 7 天仍超标将升级为就医建议。"),
                'action': '建议：近期注意低盐饮食、规律作息，继续观察。'})
            verdict, vtext = 'down', f'📉 连续 {streak} 天偏高'
            foot = f"虚线之间为正常范围（{std['sysMin']}-{std['sysMax']}），红点 = 超标测量"
        else:
            verdict, vtext = 'flat', '➡️ 正常范围'
            foot = f"虚线之间为正常范围（{std['sysMin']}-{std['sysMax']}）"
        r_txt = _fmt_bp(*[(recent or {}).get(k) for k in ('sys', 'dia')])
        p_txt = _fmt_bp(*[(prev or {}).get(k) for k in ('sys', 'dia')]) if prev else None
        compare = f"近30天 <b>{r_txt}</b>" + (f" ｜ 上期 <b>{p_txt}</b>" if p_txt else " ｜ 上期 --")
        if streak == 0:
            advice = {
                'icon': '🩺', 'title': '血压：目前控制平稳',
                'paras': [
                    f"近期血压在正常范围（{r_txt}），保持当前生活方式即可。",
                    ('继续每日规律测量。血压受季节、情绪、睡眠影响明显，'
                     '连续记录才能看出真实趋势。')]}
            speak = f"您最近血压 {r_txt}，很正常"
        else:
            advice = {
                'icon': '🩺', 'title': '血压：近期持续偏高',
                'paras': [
                    (f"近期血压均值 {r_txt}，低压长期偏高对心、脑、肾的损害是持续性的，"
                     f"值得认真对待。"),
                    ('每日食盐控制在 5 克以内（约一啤酒瓶盖），警惕酱油、咸菜里的"隐形盐"；'
                     '晚间避免情绪激动和剧烈运动；保证 7 小时睡眠。'
                     '若持续超标一周以上，请尽快就医。')]}
            speak = (f"您最近血压 {r_txt}，偏高 {streak} 天了，如果持续到一周以上建议去医院复查"
                     if streak < 7 else f"您最近血压 {r_txt}，连续偏高 {streak} 天了，建议尽快去医院复查")

    trend = {
        'key': 'blood_pressure', 'name': '血压 mmHg',
        'verdict': verdict, 'verdict_text': vtext,
        'compare': compare,
        'series': series,
        'band': [std['diaMin'], std['sysMax']],
        'med_start': med_start,
        'legend': '收缩压 / 舒张压',
        'foot': foot,
    }
    return alerts, trend, advice, speak


def _analyze_spo2(bo):
    alerts, trend, advice, speak = [], None, None, ''
    if not bo:
        return alerts, trend, advice, speak
    latest_v = bo[-1].get('spo2')
    low3 = [p for p in bo[-3:] if p.get('spo2') is not None and p['spo2'] < 95]
    if any(p.get('spo2') is not None and p['spo2'] < 93 for p in bo[-7:]):
        alerts.append({
            'level': 'red', 'tag': '建议就医',
            'title': f"血氧最低 {min(p['spo2'] for p in bo[-7:] if p.get('spo2')):.0f}%，低于安全线",
            'body': '近一周出现血氧低于 93% 的测量值。',
            'action': '建议：若伴胸闷、气短请及时就医；否则安静休息后复测。'})
    elif len(low3) >= 2:
        alerts.append({
            'level': 'yellow', 'tag': '保持观察',
            'title': '血氧多次略低于 95%',
            'body': '近期多次血氧在 93-95% 之间。',
            'action': '建议：注意室内通风，避免熬夜；持续偏低可咨询医生。'})

    recent, prev = _split_windows(bo, 'date', ('spo2',))
    r = (recent or {}).get('spo2')
    p = (prev or {}).get('spo2')
    if r is not None and p is not None:
        diff = round(r - p, 1)
        if abs(diff) < 0.5:
            verdict, vtext = 'flat', '➡️ 长期平稳'
        elif diff > 0:
            verdict, vtext = 'up', '📈 略有上升'
        else:
            verdict, vtext = 'down', '📉 略有下降'
    else:
        verdict, vtext = 'flat', '➡️ 正常范围'
    compare = f"近30天 <b>{r or '--'}</b>" + (f" ｜ 上期 <b>{p}</b>" if p else " ｜ 上期 --")
    trend = {
        'key': 'blood_oxygen', 'name': '血氧 %',
        'verdict': verdict, 'verdict_text': vtext, 'compare': compare,
        'series': [{
            'color': '#4dd0e1', 'label': '血氧',
            'points': [[p['date'], p['spo2']] for p in bo if p.get('spo2') is not None],
        }],
        'band': [95, 100], 'med_start': None,
        'legend': '血氧', 'foot': '全部测量 ≥95%' if not low3 else '绿带为理想范围（95-100%）',
    }
    if latest_v is not None and latest_v >= 95 and not low3:
        advice = {'icon': '🫁', 'title': '血氧：状态良好',
                  'paras': [f"血氧长期保持在正常范围（最近 {latest_v:.0f}%），无需干预，维持现状即可。"]}
        speak = f"血氧 {latest_v:.0f}%，很正常"
    elif low3:
        advice = {'icon': '🫁', 'title': '血氧：轻度偏低，留意观察',
                  'paras': ['近期血氧偶有低于 95% 的情况。血氧受测量姿势、指甲状态、环境影响较大，'
                            '测量时手指放平、保持静止 30 秒以上结果更准确。',
                            '若安静状态下多次低于 93%，或伴随胸闷气短，建议就医排查。']}
        speak = f"血氧最近偏低一点，{latest_v:.0f}%，注意休息观察"
    return alerts, trend, advice, speak


def _analyze_weight(wt, mi, std):
    alerts, trend, advice, speak = [], None, None, ''
    if not wt:
        return alerts, trend, advice, speak
    latest_w = wt[-1].get('weight')
    # 7 天内涨幅 >2kg → 提醒
    recent7 = [p for p in wt if (datetime.strptime(wt[-1]['date'], '%Y-%m-%d')
                                 - datetime.strptime(p['date'], '%Y-%m-%d')).days <= 7]
    if len(recent7) >= 2:
        w_first, w_last = recent7[0]['weight'], recent7[-1]['weight']
        if w_first and w_last and (w_last - w_first) > 2:
            alerts.append({
                'level': 'yellow', 'tag': '保持观察',
                'title': f"体重 7 天内上升 {round(w_last - w_first, 1)} kg",
                'body': '短期体重快速波动可能与水分、饮食有关。',
                'action': '建议：连续观察一周，若持续上升再调整饮食。'})

    first_w = wt[0].get('weight')
    delta = round(latest_w - first_w, 1) if (latest_w is not None and first_w is not None) else None
    h = (mi or {}).get('height')
    bmi_now = round(latest_w / ((h / 100) ** 2), 1) if latest_w and h else None

    # 判词：以整体首末变化定方向（体重趋势本来就慢，环比 30 天噪声大）
    if delta is not None and abs(delta) >= 1:
        if delta < 0:
            verdict, vtext = 'up', f'📈 下降 {abs(delta)}kg'
        else:
            verdict, vtext = 'down', f'📉 上升 {delta}kg'
    else:
        verdict, vtext = 'flat', '➡️ 基本稳定'
    compare = (f"现在 <b>{latest_w}</b> ｜ {wt[0]['date'][5:7]}月 <b>{first_w}</b>"
               if latest_w is not None and first_w is not None else '')
    foot = (f"BMI {bmi_now}，{('改善中' if bmi_now and std['bmiMin'] <= bmi_now <= std['bmiMax'] else '距正常范围还有空间')}"
            if bmi_now else '')
    trend = {
        'key': 'weight', 'name': '体重 kg',
        'verdict': verdict, 'verdict_text': vtext, 'compare': compare,
        'series': [{
            'color': '#5ecf8c', 'label': '体重',
            'points': [[p['date'], p['weight']] for p in wt if p.get('weight') is not None],
        }],
        'band': None, 'med_start': None, 'legend': '体重', 'foot': foot,
    }

    if delta is not None and delta < -1:
        advice = {
            'icon': '⚖️', 'title': f'体重：趋势很好，继续保持',
            'paras': [
                (f"从 {wt[0]['date'][:7]} 的 {first_w} kg 降到 {latest_w} kg（{delta} kg），方向正确。"
                 + (f"按当前节奏，再坚持数月可回到 BMI {std['bmiIdeal']} 的理想区间。" if bmi_now else '')),
                ('不必刻意节食，保持"自然下降"的节奏即可；每周固定条件称重（晨起空腹）最有参考价值。'
                 '若体重下降速度突然加快（每月 >3kg），反而需要排查原因。')]}
        speak = f"体重从 {first_w} 公斤降到了 {latest_w} 公斤，控制得很好"
    elif delta is not None and delta > 1:
        advice = {
            'icon': '⚖️', 'title': '体重：近期上升，注意控制',
            'paras': [
                f"从 {first_w} kg 升至 {latest_w} kg（+{delta} kg）。",
                ('建议从"隐性热量"入手：含糖饮料、夜宵、零食先砍掉；每天快走 30 分钟。'
                 '不必激进，每月减 1-2kg 最可持续。')]}
        speak = f"体重最近涨了 {delta} 公斤，要注意控制"
    else:
        advice = {'icon': '⚖️', 'title': '体重：基本稳定',
                  'paras': [f"近期体重稳定在 {latest_w} kg 左右。保持均衡饮食和日常活动量即可。"]}
        speak = f"体重 {latest_w} 公斤，比较稳定"
    return alerts, trend, advice, speak


def _analyze_glucose(glu):
    alerts, trend, advice, speak = [], None, None, ''
    if not glu:
        return alerts, trend, advice, speak
    latest_g = glu[-1].get('glucose')
    if any(p.get('glucose') is not None and p['glucose'] >= 11.1 for p in glu[-7:]):
        alerts.append({
            'level': 'red', 'tag': '建议就医',
            'title': f"血糖最高 {max(p['glucose'] for p in glu[-7:] if p.get('glucose'))} mmol/L",
            'body': '近一周出现明显高血糖测量值。',
            'action': '建议：尽快就医检查糖化血红蛋白。'})
    elif latest_g is not None and latest_g >= 7:
        alerts.append({
            'level': 'yellow', 'tag': '保持观察',
            'title': f"空腹血糖 {latest_g} mmol/L 偏高",
            'body': '正常空腹应 <6.1 mmol/L。',
            'action': '建议：控制精制糖摄入，一周后复测；持续偏高请就医。'})
    elif latest_g is not None and latest_g >= 6.1:
        alerts.append({
            'level': 'yellow', 'tag': '保持观察',
            'title': f"血糖接近上限（{latest_g} mmol/L，正常 <6.1）",
            'body': '处于正常高值，方向值得留意。',
            'action': '建议：控制精制糖摄入，每月复测一次观察走向。'})

    first_g = glu[0].get('glucose')
    if len(glu) < 3:
        verdict, vtext = 'flat', '➡️ 数据积累中'
        compare = f"本期 <b>{latest_g or '--'}</b>"
        foot = '测量点较少，暂不画趋势结论'
    else:
        delta = round(latest_g - first_g, 1) if latest_g is not None and first_g is not None else None
        if delta is not None and delta >= 0.3:
            verdict, vtext = 'down', '📉 轻微上升'
        elif delta is not None and delta <= -0.3:
            verdict, vtext = 'up', '📈 有所下降'
        else:
            verdict, vtext = 'flat', '➡️ 正常范围'
        compare = f"本期 <b>{latest_g}</b> ｜ 上期 <b>{first_g}</b>"
        foot = '正常 3.9-6.1'
    trend = {
        'key': 'glucose', 'name': '血糖 mmol/L',
        'verdict': verdict, 'verdict_text': vtext, 'compare': compare,
        'series': [{
            'color': '#f6b34d', 'label': '血糖',
            'points': [[p['date'], p['glucose']] for p in glu if p.get('glucose') is not None],
        }],
        'band': [3.9, 6.1], 'med_start': None, 'legend': '血糖', 'foot': foot,
    }
    if latest_g is not None and latest_g < 6.1:
        advice = {
            'icon': '🍬', 'title': '血糖：目前正常，留意走向',
            'paras': [
                f"最近测量 {latest_g} mmol/L，在正常范围内。",
                '建议：减少精制糖（甜饮料、糕点）；主食里掺三分之一粗粮；'
                '晚餐后散步 20 分钟对血糖和血压一箭双雕。']}
        speak = f"血糖 {latest_g}，正常"
    elif latest_g is not None:
        advice = {
            'icon': '🍬', 'title': '血糖：偏高，需要干预',
            'paras': [
                f"最近测量 {latest_g} mmol/L，超出正常上限 6.1。",
                '建议控制精制糖和精制碳水摄入，增加餐后活动；一周后复测，'
                '若持续 ≥7 请就医检查。']}
        speak = f"血糖 {latest_g} 偏高了，要控制饮食，下周复测"
    return alerts, trend, advice, speak
