"""
健康指标异常检测模块
自动基准切换逻辑：
- 测量次数 < 7：使用初始测量值作为基准
- 测量次数 >= 7：使用最近7次加权平均值作为基准
"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"

# 各指标的正常波动范围（用于判断是否异常）
THRESHOLDS = {
    'blood_pressure': {
        'systolic': {'max_diff': 15, 'unit': 'mmHg'},   # 收缩压波动超过15视为异常
        'diastolic': {'max_diff': 10, 'unit': 'mmHg'},   # 舒张压波动超过10视为异常
    },
    'blood_oxygen': {
        'spo2': {'max_diff': 3, 'unit': '%'},            # 血氧波动超过3%视为异常
    },
    'heart_rate': {
        'heart_rate': {'max_diff': 12, 'unit': 'bpm'},   # 心率波动超过12视为异常
    },
    'weight': {
        'weight': {'max_diff': 2.0, 'unit': 'kg'},       # 体重波动超过2kg视为异常
    },
    'glucose': {
        'glucose': {'max_diff': 0.8, 'unit': 'mmol/L'},  # 血糖波动超过0.8视为异常
    },
    'body_temperature': {
        'temp': {'max_diff': 0.5, 'unit': '°C'},         # 体温波动超过0.5°C视为异常
    },
}


def get_member_initial_measurement(member_id, data_type):
    """获取成员的基准值（从 health_data WHERE is_baseline=1 读取）
    
    Args:
        member_id: 成员ID
        data_type: 数据类型 (glucose, blood_oxygen, weight, etc.)
    
    Returns:
        基准值 (float) 或 None
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            if data_type == 'blood_pressure':
                # 血压需要从 value_extra 解析收缩压
                cursor.execute(
                    "SELECT value_main FROM health_data WHERE member_id = ? AND data_type = 'blood_pressure' AND is_baseline = 1 ORDER BY timestamp DESC LIMIT 1",
                    (member_id,)
                )
            else:
                cursor.execute(
                    "SELECT value_main FROM health_data WHERE member_id = ? AND data_type = ? AND is_baseline = 1 ORDER BY timestamp DESC LIMIT 1",
                    (member_id, data_type)
                )
        
            row = cursor.fetchone()
        finally:
            conn.close()
        
        if row and row[0] is not None:
            return float(row[0])
        return None
    except Exception as e:
        print(f"获取基准值失败: {e}")
        return None


def get_measurement_count(member_id, data_type):
    """获取成员某指标的测量次数"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM health_data WHERE member_id = ? AND data_type = ?",
                (member_id, data_type)
            )
            count = cursor.fetchone()[0]
        finally:
            conn.close()
        return count
    except Exception as e:
        print(f"获取测量次数失败: {e}")
        return 0


def get_recent_average(member_id, data_type, n=7):
    """计算最近N次测量的简单平均值
    
    Args:
        member_id: 成员ID
        data_type: 数据类型
        n: 取最近几次测量（默认7）
    
    Returns:
        (平均值, 实际使用的记录数) 或 (None, 0)
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
        
            cursor.execute("""
                SELECT value_main FROM health_data
                WHERE member_id = ? AND data_type = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (member_id, data_type, n))
        
            rows = cursor.fetchall()
        finally:
            conn.close()
        
        if not rows:
            return None, 0
        
        values = [float(row[0]) for row in rows if row[0] is not None]
        if not values:
            return None, len(rows)
        
        return sum(values) / len(values), len(rows)
    except Exception as e:
        print(f"计算平均值失败: {e}")
        return None, 0


def get_baseline(member_id, data_type):
    """获取对比基准值
    
    自动切换逻辑：
    - 测量次数 < 7：使用初始测量值
    - 测量次数 >= 7：使用最近7次平均值
    
    Args:
        member_id: 成员ID
        data_type: 数据类型
    
    Returns:
        (基准值, 基准类型, 测量次数)
        基准类型: 'initial' 初始值, 'avg' 平均值, None 无基准
    """
    count = get_measurement_count(member_id, data_type)
    
    if count >= 7:
        # 阶段二：使用最近7次平均值
        avg_val, _ = get_recent_average(member_id, data_type, 7)
        if avg_val is not None:
            return avg_val, 'avg', count
    
    # 阶段一：使用初始测量值
    init_val = get_member_initial_measurement(member_id, data_type)
    if init_val is not None:
        return init_val, 'initial', count
    
    return None, None, count


def check_anomaly(member_id, data_type, value, extra_values=None):
    """检查新测量数据是否异常
    
    Args:
        member_id: 成员ID
        data_type: 数据类型
        value: 主要测量值
        extra_values: 额外值（如血压的收缩压/舒张压）
    
    Returns:
        {
            'is_anomaly': bool,
            'baseline': float or None,
            'baseline_type': 'initial' or 'avg' or None,
            'measurement_count': int,
            'details': [{'metric': str, 'value': float, 'baseline': float, 'diff': float, 'threshold': float, 'is_abnormal': bool}]
        }
    """
    result = {
        'is_anomaly': False,
        'baseline': None,
        'baseline_type': None,
        'measurement_count': 0,
        'details': []
    }
    
    baseline, baseline_type, count = get_baseline(member_id, data_type)
    result['baseline'] = baseline
    result['baseline_type'] = baseline_type
    result['measurement_count'] = count
    
    if baseline is None:
        return result  # 没有基准，无法判断
    
    thresholds = THRESHOLDS.get(data_type, {})
    
    if data_type == 'blood_pressure' and extra_values:
        # 血压：分别检查收缩压和舒张压
        systolic = extra_values.get('systolic')
        diastolic = extra_values.get('diastolic')
        
        if systolic is not None:
            sys_threshold = thresholds.get('systolic', {}).get('max_diff', 15)
            sys_diff = abs(float(systolic) - baseline)
            sys_abnormal = sys_diff > sys_threshold
            result['details'].append({
                'metric': 'systolic',
                'value': float(systolic),
                'baseline': baseline,
                'diff': round(sys_diff, 1),
                'threshold': sys_threshold,
                'is_abnormal': sys_abnormal
            })
            if sys_abnormal:
                result['is_anomaly'] = True
        
        if diastolic is not None:
            # 舒张压的基准从初始测量获取
            dia_baseline = get_member_initial_measurement(member_id, 'blood_pressure_diastolic')
            if dia_baseline is None:
                # 如果没有单独的舒张压基准，使用收缩压基准的比例估算
                dia_baseline = baseline * 0.65  # 正常舒张压约为收缩压的65%
            
            dia_threshold = thresholds.get('diastolic', {}).get('max_diff', 10)
            dia_diff = abs(float(diastolic) - dia_baseline)
            dia_abnormal = dia_diff > dia_threshold
            result['details'].append({
                'metric': 'diastolic',
                'value': float(diastolic),
                'baseline': dia_baseline,
                'diff': round(dia_diff, 1),
                'threshold': dia_threshold,
                'is_abnormal': dia_abnormal
            })
            if dia_abnormal:
                result['is_anomaly'] = True
    
    elif data_type == 'blood_oxygen':
        spo2 = float(value)
        threshold = thresholds.get('spo2', {}).get('max_diff', 3)
        diff = abs(spo2 - baseline)
        abnormal = diff > threshold
        result['details'].append({
            'metric': 'spo2',
            'value': spo2,
            'baseline': baseline,
            'diff': round(diff, 1),
            'threshold': threshold,
            'is_abnormal': abnormal
        })
        if abnormal:
            result['is_anomaly'] = True
    
    elif data_type == 'glucose':
        glucose = float(value)
        threshold = thresholds.get('glucose', {}).get('max_diff', 0.8)
        diff = abs(glucose - baseline)
        abnormal = diff > threshold
        result['details'].append({
            'metric': 'glucose',
            'value': glucose,
            'baseline': baseline,
            'diff': round(diff, 1),
            'threshold': threshold,
            'is_abnormal': abnormal
        })
        if abnormal:
            result['is_anomaly'] = True
    
    elif data_type == 'heart_rate':
        hr = float(value)
        threshold = thresholds.get('heart_rate', {}).get('max_diff', 12)
        diff = abs(hr - baseline)
        abnormal = diff > threshold
        result['details'].append({
            'metric': 'heart_rate',
            'value': hr,
            'baseline': baseline,
            'diff': round(diff, 1),
            'threshold': threshold,
            'is_abnormal': abnormal
        })
        if abnormal:
            result['is_anomaly'] = True
    
    elif data_type == 'weight':
        wt = float(value)
        threshold = thresholds.get('weight', {}).get('max_diff', 2.0)
        diff = abs(wt - baseline)
        abnormal = diff > threshold
        result['details'].append({
            'metric': 'weight',
            'value': wt,
            'baseline': baseline,
            'diff': round(diff, 1),
            'threshold': threshold,
            'is_abnormal': abnormal
        })
        if abnormal:
            result['is_anomaly'] = True
    
    elif data_type == 'body_temperature':
        temp = float(value)
        threshold = thresholds.get('body_temperature', {}).get('max_diff', 0.5)
        diff = abs(temp - baseline)
        abnormal = diff > threshold
        result['details'].append({
            'metric': 'temp',
            'value': temp,
            'baseline': baseline,
            'diff': round(diff, 1),
            'threshold': threshold,
            'is_abnormal': abnormal
        })
        if abnormal:
            result['is_anomaly'] = True
    
    return result


def inject_initial_to_health_data(member_id, member_name):
    """将基准数据写入 health_data 表（is_baseline=1）
    
    注意：initial_measurements 表已删除，此函数保留为向后兼容。
    新的基准数据直接通过 save_initial_measurement API 写入 health_data。
    
    Args:
        member_id: 成员ID
        member_name: 成员名称
    
    Returns:
        {
            'success': bool,
            'injected': [str]  # 已注入的数据类型列表
        }
    """
    # initial_measurements 表已删除，此函数不再需要
    return {'success': False, 'injected': [], 'message': '基准数据已统一到 health_data 表'}
