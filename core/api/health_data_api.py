"""
健康数据API(完整版)
包含设备详细信息、自动关联功能
"""
import json
import sqlite3
import os
from aiohttp import web
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
# fetch_health.py 生成的数据文件路径
FETCH_HEALTH_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "health_data.json"


def identify_member_by_health_data(data_type: str, value: float, extra_values: dict = None, device_mac: str = None):
    """根据健康数据识别家庭成员
    
    Args:
        data_type: 数据类型 (weight, blood_pressure, heart_rate, etc.)
        value: 主要数值
        extra_values: 额外数值 (如血压的收缩压/舒张压)
        device_mac: 设备MAC地址（优先用于设备绑定识别）
    
    Returns:
        member_id or None
    """
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # 优先使用设备绑定识别
        if device_mac:
            cursor.execute("SELECT member_id FROM health_device_bindings WHERE device_id = ? AND auto_associate = 1", (device_mac,))
            binding_row = cursor.fetchone()
            if binding_row:
                return binding_row[0]
        
        # 获取所有家庭成员ID
        cursor.execute("SELECT id FROM family_members")
        member_ids = [row[0] for row in cursor.fetchall()]

        best_match = None
        min_diff = float('inf')

        # 首先检查基准数据(health_data WHERE is_baseline=1),用于系统初始化阶段的识别
        # 如果找到精确匹配的基准数据,立即返回(优先级最高)
        for member_id in member_ids:
            if data_type == 'weight':
                # 检查基准数据
                cursor.execute("SELECT value_main FROM health_data WHERE member_id = ? AND data_type = 'weight' AND is_baseline = 1", (member_id,))
                init_row = cursor.fetchone()
                if init_row and init_row[0] is not None:
                    init_weight = init_row[0]
                    # 基准体重差异:±3kg以内认为是同一个人
                    diff = abs(value - init_weight)
                    if diff < min_diff and diff <= 3.0:
                        min_diff = diff
                        best_match = member_id
                        # 如果差异非常小(几乎相同),立即返回
                        if diff <= 0.5:
                            return best_match

            elif data_type == 'blood_pressure' and extra_values:
                systolic = extra_values.get('systolic')
                diastolic = extra_values.get('diastolic')

                if systolic and diastolic:
                    # 检查基准数据
                    cursor.execute("SELECT value_main, value_extra FROM health_data WHERE member_id = ? AND data_type = 'blood_pressure' AND is_baseline = 1", (member_id,))
                    init_row = cursor.fetchone()
                    if init_row and init_row[0] is not None:
                        init_sys = init_row[0]
                        init_extra = json.loads(init_row[1]) if init_row[1] else {}
                        init_dia = init_extra.get('diastolic', 0)
                        if init_dia:
                            # 基准血压差异:收缩压±15mmHg,舒张压±12mmHg(放宽阈值以适应日常波动)
                            sys_diff = abs(systolic - init_sys)
                            dia_diff = abs(diastolic - init_dia)
                            total_diff = sys_diff + dia_diff

                            if total_diff < min_diff and sys_diff <= 15 and dia_diff <= 12:
                                min_diff = total_diff
                                best_match = member_id
                                # 如果差异非常小(几乎相同),立即返回
                                if sys_diff <= 1 and dia_diff <= 1:
                                    return best_match

            elif data_type == 'heart_rate':
                # 检查基准数据
                cursor.execute("SELECT value_main FROM health_data WHERE member_id = ? AND data_type = 'heart_rate' AND is_baseline = 1", (member_id,))
                init_row = cursor.fetchone()
                if init_row and init_row[0] is not None:
                    init_hr = init_row[0]
                    # 基准心率差异:±10bpm以内认为是同一个人
                    diff = abs(value - init_hr)
                    if diff < min_diff and diff <= 10:
                        min_diff = diff
                        best_match = member_id
                        # 如果差异非常小(几乎相同),立即返回
                        if diff <= 1:
                            return best_match

            elif data_type == 'blood_oxygen':
                # 检查基准数据
                cursor.execute("SELECT value_main FROM health_data WHERE member_id = ? AND data_type = 'blood_oxygen' AND is_baseline = 1", (member_id,))
                init_row = cursor.fetchone()
                if init_row and init_row[0] is not None:
                    init_oxygen = init_row[0]
                    # 基准血氧差异:±3%以内认为是同一个人
                    diff = abs(value - init_oxygen)
                    if diff < min_diff and diff <= 3:
                        min_diff = diff
                        best_match = member_id
                        # 如果差异非常小(几乎相同),立即返回
                        if diff <= 0.5:
                            return best_match

            elif data_type == 'glucose':
                # 检查基准数据
                cursor.execute("SELECT value_main FROM health_data WHERE member_id = ? AND data_type = 'glucose' AND is_baseline = 1", (member_id,))
                init_row = cursor.fetchone()
                if init_row and init_row[0] is not None:
                    init_glucose = init_row[0]
                    # 基准血糖差异:±0.5mmol/L以内认为是同一个人
                    diff = abs(value - init_glucose)
                    if diff < min_diff and diff <= 0.5:
                        min_diff = diff
                        best_match = member_id
                        # 如果差异非常小(几乎相同),立即返回
                        if diff <= 0.1:
                            return best_match

        # 如果没有找到匹配的初始数据,或者系统已运行一段时间,则使用历史平均值
        if best_match is None:
            for member_id in member_ids:
                if data_type == 'weight':
                    # 计算该成员最近7天的体重平均值
                    cursor.execute("""
                        SELECT AVG(CAST(value_main AS REAL)) as avg_value
                        FROM health_data
                        WHERE member_id = ? AND data_type = 'weight'
                        AND timestamp >= datetime('now', '-7 days')
                    """, (member_id,))
                    result = cursor.fetchone()
                    if result and result[0] is not None:
                        weekly_avg = result[0]
                        # 体重差异:±2kg以内认为是同一个人
                        diff = abs(value - weekly_avg)
                        if diff < min_diff and diff <= 2.0:
                            min_diff = diff
                            best_match = member_id

                elif data_type == 'blood_pressure' and extra_values:
                    systolic = extra_values.get('systolic')
                    diastolic = extra_values.get('diastolic')

                    if systolic and diastolic:
                        # 计算该成员最近7天的血压平均值
                        cursor.execute("""
                            SELECT
                                AVG(CAST(extra_values_parsed.systolic AS REAL)) as avg_sys,
                                AVG(CAST(extra_values_parsed.diastolic AS REAL)) as avg_dia
                            FROM (
                                SELECT json_extract(value_extra, '$.systolic') as systolic,
                                       json_extract(value_extra, '$.diastolic') as diastolic
                                FROM health_data
                                WHERE member_id = ? AND data_type = 'blood_pressure'
                                AND timestamp >= datetime('now', '-7 days')
                            ) as extra_values_parsed
                            WHERE systolic IS NOT NULL AND diastolic IS NOT NULL
                        """, (member_id,))
                        result = cursor.fetchone()

                        if result and result[0] is not None and result[1] is not None:
                            avg_sys = result[0]
                            avg_dia = result[1]
                            # 血压差异:收缩压±8mmHg,舒张压±5mmHg(更精确的范围)
                            sys_diff = abs(systolic - avg_sys)
                            dia_diff = abs(diastolic - avg_dia)
                            total_diff = sys_diff + dia_diff

                            if total_diff < min_diff and sys_diff <= 8 and dia_diff <= 5:
                                min_diff = total_diff
                                best_match = member_id

                elif data_type == 'heart_rate':
                    # 计算该成员最近7天的心率平均值
                    cursor.execute("""
                        SELECT AVG(CAST(value_main AS REAL)) as avg_value
                        FROM health_data
                        WHERE member_id = ? AND data_type = 'heart_rate'
                        AND timestamp >= datetime('now', '-7 days')
                    """, (member_id,))
                    result = cursor.fetchone()
                    if result and result[0] is not None:
                        weekly_avg = result[0]
                        # 心率差异:±8bpm以内认为是同一个人
                        diff = abs(value - weekly_avg)
                        if diff < min_diff and diff <= 8:
                            min_diff = diff
                            best_match = member_id

                elif data_type == 'blood_oxygen':
                    # 计算该成员最近7天的血氧平均值
                    cursor.execute("""
                        SELECT AVG(CAST(value_main AS REAL)) as avg_value
                        FROM health_data
                        WHERE member_id = ? AND data_type = 'blood_oxygen'
                        AND timestamp >= datetime('now', '-7 days')
                    """, (member_id,))
                    result = cursor.fetchone()
                    if result and result[0] is not None:
                        weekly_avg = result[0]
                        # 血氧差异:±2%以内认为是同一个人
                        diff = abs(value - weekly_avg)
                        if diff < min_diff and diff <= 2:
                            min_diff = diff
                            best_match = member_id

                elif data_type == 'glucose':
                    # 计算该成员最近7天的血糖平均值
                    cursor.execute("""
                        SELECT AVG(CAST(value_main AS REAL)) as avg_value
                        FROM health_data
                        WHERE member_id = ? AND data_type = 'glucose'
                        AND timestamp >= datetime('now', '-7 days')
                    """, (member_id,))
                    result = cursor.fetchone()
                    if result and result[0] is not None:
                        weekly_avg = result[0]
                        # 血糖差异:±0.3mmol/L以内认为是同一个人
                        diff = abs(value - weekly_avg)
                        if diff < min_diff and diff <= 0.3:
                            min_diff = diff
                            best_match = member_id

        return best_match

    except Exception as e:
        print(f"识别家庭成员失败: {e}")
        return None
    finally:
        if conn:
            conn.close()


async def get_ble_health_data(request):
    """获取 BLE 健康设备实时数据(从 fetch_health.py 生成的文件读取)"""
    try:
        if os.path.exists(FETCH_HEALTH_DATA_PATH):
            with open(FETCH_HEALTH_DATA_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return web.json_response({
                'success': True,
                'data': data
            })
        else:
            return web.json_response({
                'success': False,
                'message': 'BLE 健康数据文件不存在'
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def get_health_records(request):
    """获取健康数据(包含设备详细信息)"""
    conn = None
    try:
        # 从请求参数获取过滤条件
        member_id = request.query.get('member_id')
        data_type = request.query.get('data_type')
        device_type = request.query.get('device_type')
        days = int(request.query.get('days', 7))
        limit = int(request.query.get('limit', 50))

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 计算时间范围
        start_time = datetime.now() - timedelta(days=days)

        # 构建查询
        query = """
            SELECT hd.id, hd.member_id, fm.name as member_name,
                   hd.device_id, hd.device_name, hd.manufacturer, hd.model,
                   hd.data_type, hd.value_main, hd.value_extra, hd.unit, hd.quality,
                   hd.timestamp, hd.notes
            FROM health_data hd
            LEFT JOIN family_members fm ON hd.member_id = fm.id
            WHERE hd.timestamp >= ?
        """
        params = [start_time.isoformat()]

        if member_id:
            query += " AND hd.member_id = ?"
            params.append(member_id)

        if data_type:
            query += " AND hd.data_type = ?"
            params.append(data_type)

        if device_type:
            query += " AND hd.device_id LIKE ?"
            params.append(f"{device_type}%")

        query += " ORDER BY hd.timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # 构建返回数据
        data = []
        for row in rows:
            record = {
                'id': row[0],
                'member_id': row[1],
                'member_name': row[2],
                'device': {
                    'device_id': row[3],
                    'device_name': row[4],
                    'manufacturer': row[5],
                    'model': row[6]
                },
                'data_type': row[7],
                'value': row[8],
                'extra_values': json.loads(row[9]) if row[9] else None,
                'unit': row[10],
                'quality': row[11],
                'timestamp': row[12],
                'notes': row[13]
            }
            data.append(record)

        return web.json_response({
            'success': True,
            'count': len(data),
            'data': data
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })
    finally:
        if conn:
            conn.close()

async def add_health_data(request):
    """添加健康数据(自动关联到家庭成员)"""
    conn = None
    try:
        data = await request.json()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 获取设备信息
        device_id = data.get('device_id')
        cursor.execute("SELECT device_name, manufacturer, model FROM health_devices WHERE device_id=?", (device_id,))
        device_row = cursor.fetchone()

        if not device_row:
            # 如果设备不存在,创建新设备
            cursor.execute("""
                INSERT INTO health_devices (device_id, device_name, device_type, manufacturer, model)
                VALUES (?, ?, ?, ?, ?)
            """, (
                device_id,
                data.get('device_name', device_id),
                data.get('device_type', 'unknown'),
                data.get('manufacturer', 'Unknown'),
                data.get('model', 'Unknown')
            ))
            device_name = data.get('device_name', device_id)
            manufacturer = data.get('manufacturer', 'Unknown')
            model = data.get('model', 'Unknown')
        else:
            device_name, manufacturer, model = device_row

        # 自动关联:查找绑定该设备的家庭成员
        member_id = data.get('member_id')
        if not member_id:
            cursor.execute("""
                SELECT member_id FROM health_device_bindings
                WHERE device_id=? AND auto_associate=1
                LIMIT 1
            """, (device_id,))
            binding_row = cursor.fetchone()
            if binding_row:
                member_id = binding_row[0]

        # 如果仍然没有找到成员ID,尝试通过健康数据识别
        if not member_id:
            data_type = data.get('data_type')
            value = data.get('value')
            extra_values = data.get('extra_values', {})

            if data_type and value is not None:
                member_id = identify_member_by_health_data(data_type, value, extra_values)

        # 如果还是无法确定成员,可以考虑将数据标记为"未分配"或使用默认成员
        if not member_id:
            # 可以选择将数据暂时标记为未分配,或者询问用户
            # 这里我们暂时跳过,不保存未识别的数据
            # 或者可以设置一个默认成员ID,比如主人
            cursor.execute("SELECT id FROM family_members WHERE name='主人' LIMIT 1")
            default_member = cursor.fetchone()
            if default_member:
                member_id = default_member[0]
            else:
                # 如果连默认成员都没有,就随便选一个
                cursor.execute("SELECT id FROM family_members LIMIT 1")
                any_member = cursor.fetchone()
                if any_member:
                    member_id = any_member[0]

        # 插入健康数据
        cursor.execute("""
            INSERT INTO health_data (member_id, device_id, device_name, manufacturer, model,
                                     data_type, value_main, value_extra, unit, quality, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            member_id,
            device_id,
            device_name,
            manufacturer,
            model,
            data.get('data_type'),
            data.get('value'),
            json.dumps(data.get('extra_values', {})),
            data.get('unit'),
            data.get('quality', 'good'),
            data.get('notes')
        ))

        data_id = cursor.lastrowid
        conn.commit()

        # 异常检测:将新数据与基准对比
        anomaly_result = None
        try:
            from .health_anomaly import check_anomaly
            data_type = data.get('data_type')
            value = data.get('value')
            extra_values = data.get('extra_values', {})
            if data_type and value is not None and member_id:
                anomaly_result = check_anomaly(member_id, data_type, value, extra_values)
        except Exception as e:
            print(f"异常检测失败: {e}")

        response = {
            'success': True,
            'data_id': data_id,
            'member_id': member_id,
            'device': {
                'device_id': device_id,
                'device_name': device_name,
                'manufacturer': manufacturer,
                'model': model
            },
            'message': f'{data.get("data_type")} 数据已记录'
        }

        if anomaly_result:
            response['anomaly_check'] = anomaly_result
            if anomaly_result.get('is_anomaly'):
                response['warning'] = '检测到健康指标异常,请关注'

        return web.json_response(response)
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })
    finally:
        if conn:
            conn.close()

async def get_latest_weight(request):
    """获取最新体重数据(包含设备信息)"""
    conn = None
    try:
        member_id = request.query.get('member_id')

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        if member_id:
            cursor.execute("""
                SELECT hd.value_main, hd.unit, hd.timestamp, hd.member_id, fm.name,
                       hd.device_id, hd.device_name, hd.manufacturer, hd.model,
                       hd.value_extra
                FROM health_data hd
                LEFT JOIN family_members fm ON hd.member_id = fm.id
                WHERE hd.member_id=? AND hd.data_type='weight'
                ORDER BY hd.timestamp DESC LIMIT 1
            """, (member_id,))
        else:
            cursor.execute("""
                SELECT hd.value_main, hd.unit, hd.timestamp, hd.member_id, fm.name,
                       hd.device_id, hd.device_name, hd.manufacturer, hd.model,
                       hd.value_extra
                FROM health_data hd
                LEFT JOIN family_members fm ON hd.member_id = fm.id
                WHERE hd.data_type='weight'
                ORDER BY hd.timestamp DESC LIMIT 1
            """)

        row = cursor.fetchone()

        if row:
            return web.json_response({
                'success': True,
                'weight': {
                    'value': row[0],
                    'unit': row[1],
                    'timestamp': row[2],
                    'member': {
                        'id': row[3],
                        'name': row[4]
                    },
                    'device': {
                        'device_id': row[5],
                        'device_name': row[6],
                        'manufacturer': row[7],
                        'model': row[8]
                    },
                    'extra_data': json.loads(row[9]) if row[9] else None
                }
            })
        else:
            return web.json_response({
                'success': False,
                'message': '无体重数据'
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })
    finally:
        if conn:
            conn.close()

async def get_health_summary(request):
    """获取健康数据摘要(用于家庭看板)"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 首先获取所有家庭成员基本信息
        cursor.execute("""
            SELECT id, name, avatar, age, gender
            FROM family_members
        """)
        members = cursor.fetchall()

        # 为每个成员获取最近的健康数据
        summary = {}
        for member in members:
            member_id = member[0]
            summary[member_id] = {
                'id': member_id,
                'name': member[1],
                'avatar': member[2],
                'age': member[3],
                'gender': member[4],
                'health_data': {}
            }

            # 获取该成员最近的各类健康数据
            for data_type in ['blood_pressure', 'blood_oxygen', 'body_temperature', 'heart_rate', 'weight', 'glucose']:
                cursor.execute("""
                    SELECT value_main, value_extra, unit, timestamp,
                           device_name, manufacturer, model
                    FROM health_data
                    WHERE member_id = ? AND data_type = ?
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id, data_type))

                row = cursor.fetchone()
                if row:
                    # 根据数据类型构建健康数据对象
                    if data_type == 'blood_pressure' and row[1]:  # value_extra contains systolic/diastolic
                        import json
                        extra = json.loads(row[1])
                        summary[member_id]['health_data'][data_type] = {
                            'value': row[0],
                            'unit': row[2],
                            'timestamp': row[3],
                            'device': {
                                'name': row[4],
                                'manufacturer': row[5],
                                'model': row[6]
                            }
                        }
                    else:
                        summary[member_id]['health_data'][data_type] = {
                            'value': row[0],
                            'unit': row[2],
                            'timestamp': row[3],
                            'device': {
                                'name': row[4],
                                'manufacturer': row[5],
                                'model': row[6]
                            }
                        }

        return web.json_response({
            'success': True,
            'summary': summary
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })
    finally:
        if conn:
            conn.close()

async def get_health_devices(request):
    """获取健康设备列表"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT hd.device_id, hd.device_name, hd.device_type,
                   hd.manufacturer, hd.model, hd.capabilities, hd.status, hd.last_seen
            FROM health_devices hd
            ORDER BY hd.status DESC, hd.device_name
        """)

        rows = cursor.fetchall()

        devices = []
        for row in rows:
            devices.append({
                'device_id': row[0],
                'device_name': row[1],
                'device_type': row[2],
                'manufacturer': row[3],
                'model': row[4],
                'capabilities': json.loads(row[5]) if row[5] else {},
                'status': row[6],
                'last_seen': row[7]
            })

        return web.json_response({
            'success': True,
            'count': len(devices),
            'devices': devices
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })
    finally:
        if conn:
            conn.close()