# HomeBrain 额外 API 扩展

import os
import sys
import json
import sqlite3
import subprocess
from datetime import datetime

HEALTH_DB_PATH = os.path.expanduser("~/.openclaw/workspace/projects/ongoing/health-monitor/health_data.db")

def get_health_data(date=None):
    """获取健康数据"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    data = {
        "date": date,
        "metrics": {}
    }
    
    try:
        if os.path.exists(HEALTH_DB_PATH):
            conn = sqlite3.connect(HEALTH_DB_PATH)
            try:
                cursor = conn.cursor()

                # 查询该日期的所有健康指标
                cursor.execute("""
                    SELECT metric_type, metric_value, unit, timestamp 
                    FROM health_metrics 
                    WHERE date(timestamp, 'unixepoch') = ?
                    ORDER BY timestamp DESC
                """, (date,))

                rows = cursor.fetchall()

                for row in rows:
                    metric_type, value, unit, timestamp = row
                    data["metrics"][metric_type] = {
                        "value": value,
                        "unit": unit,
                        "timestamp": timestamp
                    }
            finally:
                conn.close()

            # 如果数据库没有数据，返回演示数据
            if not data["metrics"]:
                data["metrics"] = {
                    "blood_pressure_systolic": {"value": 120, "unit": "mmHg"},
                    "blood_pressure_diastolic": {"value": 80, "unit": "mmHg"},
                    "heart_rate": {"value": 72, "unit": "bpm"},
                    "glucose": {"value": 5.6, "unit": "mmol/L"},
                    "weight": {"value": 72.5, "unit": "kg"},
                    "body_fat": {"value": 18.5, "unit": "%"},
                    "spo2": {"value": 98, "unit": "%"},
                    "temperature": {"value": 36.5, "unit": "°C"},
                    "steps": {"value": 8500, "unit": "步"}
                }
    except Exception as e:
        print(f"健康数据查询失败：{e}")
        # 返回演示数据
        data["metrics"] = {
            "blood_pressure_systolic": {"value": 120, "unit": "mmHg"},
            "blood_pressure_diastolic": {"value": 80, "unit": "mmHg"},
            "heart_rate": {"value": 72, "unit": "bpm"},
            "glucose": {"value": 5.6, "unit": "mmol/L"},
            "weight": {"value": 72.5, "unit": "kg"},
            "body_fat": {"value": 18.5, "unit": "%"},
            "spo2": {"value": 98, "unit": "%"},
            "temperature": {"value": 36.5, "unit": "°C"},
            "steps": {"value": 8500, "unit": "步"}
        }
    
    return data

def clear_cache():
    """清理系统缓存"""
    try:
        cache_dirs = [
            "tmp/",
            "data/cache/",
            "__pycache__/"
        ]
        
        cleared = 0
        for cache_dir in cache_dirs:
            if os.path.exists(cache_dir):
                for root, dirs, files in os.walk(cache_dir):
                    for file in files:
                        try:
                            os.remove(os.path.join(root, file))
                            cleared += 1
                        except:
                            pass
                    for dir in dirs:
                        try:
                            os.rmdir(os.path.join(root, dir))
                        except:
                            pass
        
        return {"success": True, "cleared": cleared}
    except Exception as e:
        return {"success": False, "error": str(e)}

def restart_server():
    """重启服务"""
    try:
        # 使用脚本重启
        script_path = os.path.join(os.path.dirname(__file__), "../restart.sh")
        if os.path.exists(script_path):
            # 确保脚本可执行
            os.chmod(script_path, 0o755)
            # 在后台启动重启脚本，避免阻塞当前请求
            subprocess.Popen(
                ["bash", script_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return {"success": True, "message": "服务正在重启，请等待 5-10 秒后刷新页面"}
        else:
            return {"success": False, "message": "重启脚本不存在: " + script_path}
    except Exception as e:
        return {"success": False, "error": str(e)}
