# -*- coding: utf-8 -*-
"""
补充控制台需要的 API 路由
前端调用但后端缺失的 API
"""

import os
import json
import time
import asyncio
import logging
import aiohttp
import sqlite3
from functools import lru_cache

logger = logging.getLogger(__name__)
from aiohttp import web
from datetime import datetime, timedelta
from config.config_loader import load_yaml_expanded

# ==================== API 响应缓存 ====================
# 缓存配置：减少对 HomeAssistant 的重复请求
_API_CACHE = {}
_CACHE_TTL = {
    'stats': 30,       # 统计数据缓存30秒
    'devices': 60,     # 设备列表缓存60秒
    'notifications': 10,  # 通知缓存10秒
    'smart_home': 30,  # 智能家居状态缓存30秒
    'cameras': 60,     # 摄像头列表缓存60秒
}

def _get_cached(key):
    """获取缓存数据"""
    entry = _API_CACHE.get(key)
    if entry and time.time() - entry['time'] < _CACHE_TTL.get(key.split(':')[0], 30):
        return entry['data']
    return None

def _set_cached(key, data):
    """设置缓存数据"""
    _API_CACHE[key] = {'data': data, 'time': time.time()}
    # 清理过期缓存（简单实现）
    if len(_API_CACHE) > 100:
        expired = [k for k, v in _API_CACHE.items() if time.time() - v['time'] > 120]
        for k in expired:
            del _API_CACHE[k]

# ==================== 技能搜索和管理 ====================
# 2026-09-11 改造：移除 ClawHub 假商店与假安装，改为本地真实插件数据。
# 真实列表/启停走 /api/plugins/list 与 /api/plugins/toggle（core/api/plugins_api.py）。

async def search_skills(request):
    """搜索本地已安装插件（保留旧路由兼容，数据源改为真实插件目录）"""
    from core.api.plugins_api import list_all_plugins
    keyword = request.query.get('keyword', '').lower().strip()
    try:
        plugins = list_all_plugins()
        skills = []
        for p in plugins:
            desc = p["functions"][0]["description"] if p["functions"] else ""
            name = p["id"]
            text = f"{name} {desc}".lower()
            if keyword and keyword not in text:
                continue
            skills.append({
                "id": p["id"],
                "name": name,
                "desc": desc,
                "category": "本地插件",
                "version": "-",
                "author": "本地",
                "enabled": p["enabled"],
                "source": "local",
            })
        return web.json_response({
            "success": True,
            "skills": skills,
            "total": len(skills),
            "source": "local",
        })
    except Exception as e:
        return web.json_response({
            "success": False, "error": str(e), "skills": [], "total": 0,
        }, status=500)


async def install_skill(request):
    """旧的在线安装入口已停用：返回真实安装指引（不再假装安装成功）"""
    return web.json_response({
        "success": False,
        "message": "在线商店已下线。添加技能的方式：① 将小智社区插件 .py 文件放入 "
                   "plugins_func/functions/ 目录并重启服务；② 接入 MCP 服务器（如飞书官方 lark-mcp）。",
        "guide": "https://github.com/xinnan-tech/xiaozhi-esp32-server/tree/main/main/xiaozhi-server/plugins_func/functions",
    }, status=410)


async def install_local_skill(request):
    """旧的本地安装入口已停用：返回真实安装指引"""
    return web.json_response({
        "success": False,
        "message": "请将插件 .py 文件放入 server/homebrain-server/plugins_func/functions/ 目录，"
                   "重启服务后自动发现并注册。",
    }, status=410)


# 房间管理 API - 简化版本，使用现有的 HouseAPI
async def get_rooms(request):
    """获取所有房间列表"""
    conn = None
    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM rooms ORDER BY id')
        rows = cursor.fetchall()
        rooms = [{"id": r[0], "name": r[1] or f"房间 {r[0]}"} for r in rows]
        return web.json_response({"success": True, "rooms": rooms})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e), "rooms": []})
    finally:
        if conn:
            conn.close()


async def get_room(request):
    """获取单个房间详情"""
    room_id = request.match_info.get('id')
    
    return web.json_response({
        "success": True,
        "room": {
            "id": room_id,
            "name": f"房间 {room_id}",
            "type": "general"
        }
    })


async def update_room(request):
    """更新房间信息"""
    room_id = request.match_info.get('id')
    
    try:
        data = await request.json()
        return web.json_response({
            "success": True,
            "message": "房间更新成功"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


# 统计数据 API
async def get_stats(request):
    """获取统计数据 - 使用缓存减少 HomeAssistant 请求"""
    # 检查缓存
    cached = _get_cached('stats')
    if cached:
        return web.json_response({
            "success": True,
            "stats": cached,
            "cached": True
        })
    
    try:
        from core.utils.ha_client import HAClient
        ha = HAClient()
        devices = await ha.get_all_devices()  # 使用 await 调用 async 方法
        
        # 获取基本统计
        stats = {
            "devices": {
                "total": len(devices),
                "online": len([d for d in devices if d.get("state") not in ["unavailable", "unknown"]])
            },
            "members": 3,
            "tasks": 0,
            "scenes": 0
        }
        
        # 缓存结果
        _set_cached('stats', stats)
        
        return web.json_response({
            "success": True,
            "stats": stats,
            "cached": False
        })
    except Exception as e:
        # 失败时返回缓存数据或默认值
        if cached:
            return web.json_response({
                "success": True,
                "stats": cached,
                "cached": True,
                "error": str(e)
            })
        return web.json_response({
            "success": False,
            "error": str(e),
            "stats": {
                "devices": {"total": 0, "online": 0},
                "members": 0,
                "tasks": 0,
                "scenes": 0
            }
        }, status=500)


# 通知和日志 API
async def get_notifications(request):
    """获取通知"""
    return web.json_response({
        "success": True,
        "notifications": []
    })


async def get_activity_logs(request):
    """获取活动日志"""
    limit = int(request.query.get('limit', 20))
    
    return web.json_response({
        "success": True,
        "logs": [],
        "total": 0
    })


async def get_server_logs(request):
    """获取服务器日志 - 读取实际运行的 http_only_server.py 日志"""
    lines = int(request.query.get('lines', 200))
    
    # 日志文件路径 - 指向实际运行的服务器日志
    log_file = os.path.join(os.path.dirname(__file__), '..', '..', 'tmp', 'server.log')
    
    logs = []
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
            recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
            for line in recent:
                line = line.strip()
                if not line:
                    continue
                # 尝试解析日志级别
                level = 'info'
                if 'ERROR' in line or 'error' in line.lower():
                    level = 'error'
                elif 'WARNING' in line or 'WARN' in line.lower():
                    level = 'warning'
                elif 'DEBUG' in line.lower():
                    level = 'debug'
                logs.append({'message': line, 'level': level})
    except FileNotFoundError:
        logs.append({'message': '日志文件不存在: ' + log_file, 'level': 'warning'})
    except Exception as e:
        logs.append({'message': f'读取日志失败: {str(e)}', 'level': 'error'})
    
    return web.json_response({
        "success": True,
        "logs": logs,
        "total": len(logs)
    })


# 健康状态判断辅助函数
def _get_bp_status(sys, dia):
    """血压状态判断"""
    try:
        sys = float(sys) if sys and sys != '--' else 0
        dia = float(dia) if dia and dia != '--' else 0
        if sys == 0 or dia == 0: return '暂无数据'
        if sys > 180 or dia > 120: return '🔴 危险'
        if sys > 140 or dia > 90: return '🟡 偏高'
        if sys < 90 or dia < 60: return '🟡 偏低'
        return '🟢 正常'
    except: return '暂无数据'

def _get_hr_status(hr):
    """心率状态判断"""
    try:
        hr = float(hr) if hr and hr != '--' else 0
        if hr == 0: return '暂无数据'
        if hr > 120: return '🔴 过快'
        if hr > 100: return '🟡 偏快'
        if hr < 50: return '🟡 偏慢'
        return '🟢 正常'
    except: return '暂无数据'

def _get_o2_status(spo2):
    """血氧状态判断"""
    try:
        spo2 = float(spo2) if spo2 and spo2 != '--' else 0
        if spo2 == 0: return '暂无数据'
        if spo2 < 90: return '🔴 危险'
        if spo2 < 95: return '🟡 偏低'
        return '🟢 正常'
    except: return '暂无数据'

def _get_weight_status(weight, member_info):
    """体重状态判断"""
    try:
        weight = float(weight) if weight and weight != '--' else 0
        if weight == 0: return '暂无数据'
        # 简单判断，实际应该结合身高计算BMI
        return '🟢 记录中'
    except: return '暂无数据'

def _get_bf_status(bf, member_info):
    """体脂状态判断"""
    try:
        bf = float(bf) if bf and bf != '--' else 0
        if bf == 0: return '暂无数据'
        gender = member_info.get('gender', '') if member_info else ''
        if '男' in gender:
            if bf > 25: return '🟡 偏高'
            if bf < 10: return '🟡 偏低'
            return '🟢 正常'
        else:
            if bf > 32: return '🟡 偏高'
            if bf < 18: return '🟡 偏低'
            return '🟢 正常'
    except: return '暂无数据'

def _get_bmi_status(bmi):
    """BMI状态判断"""
    try:
        bmi = float(bmi) if bmi and bmi != '--' else 0
        if bmi == 0: return '暂无数据'
        if bmi > 30: return '🔴 肥胖'
        if bmi > 25: return '🟡 超重'
        if bmi < 18.5: return '🟡 偏轻'
        return '🟢 正常'
    except: return '暂无数据'

async def generate_health_report(request):
    """生成健康报告 - 基于家庭成员真实数据 + 多技能综合分析（按需生成，不存数据库）"""
    import logging
    try:
        data = await request.json()
        skills = data.get('skills', [])
        member_name = data.get('member_name', '未知')
        
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        health_db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'health_data.db')
        
        # 读取家庭成员信息
        member_info = None
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, name, relation, age, gender, birthday, height, weight, occupation, health_status '
                'FROM family_members WHERE name = ?',
                (member_name,)
            )
            row = cursor.fetchone()
            if row:
                member_info = {
                    'id': row[0],
                    'name': row[1],
                    'relation': row[2],
                    'age': row[3],
                    'gender': row[4],
                    'birthday': row[5],
                    'height': row[6],
                    'weight': row[7],
                    'occupation': row[8],
                    'health_status': row[9]
                }
        except Exception as e:
                pass
        finally:
            if conn:
                conn.close()
        
        # 计算年龄（如果数据库没有）
        age = member_info.get('age') if member_info else None
        if not age and member_info and member_info.get('birthday'):
            try:
                from datetime import date
                bd = member_info['birthday']
                if isinstance(bd, str) and '-' in bd:
                    parts = bd.split('-')
                    birth_year = int(parts[0])
                    age = date.today().year - birth_year
                    member_info['age'] = age
            except:
                pass
        
        # 读取真实健康数据
        health_data = _collect_health_data(health_db_path, db_path, member_info)
        
        # 构建基本信息段落
        basic_info_lines = []
        if member_info:
            if member_info.get('relation'):
                basic_info_lines.append(f"- 家庭关系：{member_info['relation']}")
            if member_info.get('gender'):
                basic_info_lines.append(f"- 性别：{member_info['gender']}")
            if age:
                basic_info_lines.append(f"- 年龄：{age}岁")
            if member_info.get('birthday'):
                basic_info_lines.append(f"- 生日：{member_info['birthday']}")
            if member_info.get('height'):
                basic_info_lines.append(f"- 身高：{member_info['height']}cm")
            if member_info.get('weight'):
                basic_info_lines.append(f"- 体重：{member_info['weight']}kg")
            if member_info.get('health_status'):
                basic_info_lines.append(f"- 健康状态：{member_info['health_status']}")
        
        basic_info = '\n'.join(basic_info_lines) if basic_info_lines else '- 暂无详细档案信息'
        
        # 生成报告内容 - 包含图表数据
        # 构建图表数据 JSON（兼容两种数据格式）
        def _get_bp_sys(d):
            return d.get('systolic') or (d.get('extra', {}) or {}).get('systolic')
        def _get_bp_dia(d):
            return d.get('diastolic') or (d.get('extra', {}) or {}).get('diastolic')
        def _get_bp_pulse(d):
            return d.get('pulse') or (d.get('extra', {}) or {}).get('pulse')
        def _get_time(d):
            t = d.get('time', '') or d.get('timestamp', '') or ''
            return t.split()[0] if ' ' in str(t) else str(t)
        
        bp_data = health_data.get('blood_pressure', [])
        bo_data = health_data.get('blood_oxygen', [])
        wt_data = health_data.get('weight', [])
        
        # 最新值（先定义，后面雷达图和 latest_values 都要用）
        latest_bp = bp_data[0] if bp_data else {}
        latest_bo = bo_data[0] if bo_data else {}
        latest_wt = wt_data[0] if wt_data else {}
        latest_tp = health_data.get('temperature', [{}])[0] if health_data.get('temperature') else {}
        
        # 计算雷达图评分（按年龄段）
        member_age = int(member_info.get('age', 30)) if member_info else 30
        def get_age_standards(age):
            if age <= 3: return {'sysMin': 85, 'sysMax': 105, 'sysIdeal': 95, 'diaMin': 50, 'diaMax': 70, 'diaIdeal': 60, 'hrMin': 80, 'hrMax': 130, 'hrIdeal': 105, 'wtIdeal': 14, 'bmiMin': 14, 'bmiMax': 18, 'bmiIdeal': 16}
            elif age <= 5: return {'sysMin': 90, 'sysMax': 110, 'sysIdeal': 100, 'diaMin': 55, 'diaMax': 75, 'diaIdeal': 65, 'hrMin': 75, 'hrMax': 120, 'hrIdeal': 95, 'wtIdeal': 18, 'bmiMin': 13.5, 'bmiMax': 17.5, 'bmiIdeal': 15.5}
            elif age <= 8: return {'sysMin': 90, 'sysMax': 125, 'sysIdeal': 105, 'diaMin': 55, 'diaMax': 85, 'diaIdeal': 68, 'hrMin': 70, 'hrMax': 115, 'hrIdeal': 90, 'wtIdeal': 25, 'bmiMin': 13, 'bmiMax': 18, 'bmiIdeal': 15.5}
            elif age <= 12: return {'sysMin': 100, 'sysMax': 120, 'sysIdeal': 110, 'diaMin': 60, 'diaMax': 80, 'diaIdeal': 70, 'hrMin': 65, 'hrMax': 110, 'hrIdeal': 85, 'wtIdeal': 38, 'bmiMin': 14, 'bmiMax': 20, 'bmiIdeal': 17}
            elif age <= 15: return {'sysMin': 105, 'sysMax': 125, 'sysIdeal': 115, 'diaMin': 65, 'diaMax': 82, 'diaIdeal': 73, 'hrMin': 60, 'hrMax': 105, 'hrIdeal': 80, 'wtIdeal': 52, 'bmiMin': 16, 'bmiMax': 22, 'bmiIdeal': 19}
            elif age <= 18: return {'sysMin': 105, 'sysMax': 128, 'sysIdeal': 116, 'diaMin': 65, 'diaMax': 83, 'diaIdeal': 74, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 75, 'wtIdeal': 60, 'bmiMin': 17, 'bmiMax': 23, 'bmiIdeal': 20}
            elif age <= 35: return {'sysMin': 90, 'sysMax': 120, 'sysIdeal': 110, 'diaMin': 60, 'diaMax': 80, 'diaIdeal': 72, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 70, 'bmiMin': 18.5, 'bmiMax': 24, 'bmiIdeal': 21.5}
            elif age <= 50: return {'sysMin': 95, 'sysMax': 130, 'sysIdeal': 115, 'diaMin': 60, 'diaMax': 85, 'diaIdeal': 75, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 72, 'bmiMin': 18.5, 'bmiMax': 24.5, 'bmiIdeal': 22}
            elif age <= 65: return {'sysMin': 100, 'sysMax': 140, 'sysIdeal': 120, 'diaMin': 65, 'diaMax': 90, 'diaIdeal': 78, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 70, 'bmiMin': 19, 'bmiMax': 25, 'bmiIdeal': 22.5}
            else: return {'sysMin': 100, 'sysMax': 150, 'sysIdeal': 125, 'diaMin': 65, 'diaMax': 90, 'diaIdeal': 80, 'hrMin': 60, 'hrMax': 100, 'hrIdeal': 72, 'wtIdeal': 68, 'bmiMin': 20, 'bmiMax': 26, 'bmiIdeal': 23}
        
        std = get_age_standards(member_age)
        
        def calc_score(val, ideal, min_v, max_v):
            if val is None: return 100  # 未测量默认满分
            try: val = float(val)
            except: return 100
            if min_v <= val <= max_v: return 100  # 正常范围内返回满分
            return max(0, round(100 - abs(val - ideal) * 2.5))
        
        latest_sys = _get_bp_sys(latest_bp) if latest_bp else None
        latest_dia = _get_bp_dia(latest_bp) if latest_bp else None
        latest_pulse = _get_bp_pulse(latest_bp) if latest_bp else None
        latest_o2 = latest_bo.get('spo2') if latest_bo else None
        latest_wt_val = latest_wt.get('weight') or latest_wt.get('value') if latest_wt else None
        
        # BMI计算
        bmi_value = None
        if member_info and member_info.get('height') and member_info.get('weight'):
            try:
                h = float(member_info['height']) / 100
                w = float(member_info['weight'])
                bmi_value = w / (h * h)
            except: pass
        if bmi_value is None and latest_wt:
            bmi_value = latest_wt.get('bmi')
        
        radar_scores = {
            'cardiovascular': calc_score(latest_sys, std['sysIdeal'], std['sysMin'], std['sysMax']) if latest_sys else 100,
            'blood_oxygen': (90 + (latest_o2 - 95) * 2) if latest_o2 and latest_o2 >= 95 else (max(0, latest_o2) if latest_o2 else 100),
            'metabolism': calc_score(latest_wt_val, std['wtIdeal'], std['wtIdeal'] * 0.8, std['wtIdeal'] * 1.2) if latest_wt_val else 100,
            'body_composition': calc_score(bmi_value, std['bmiIdeal'], std['bmiMin'], std['bmiMax']) if bmi_value else 100,
            'temperature': calc_score(latest_tp.get('temp') or latest_tp.get('value'), 36.5, 36.0, 37.2) if latest_tp.get('temp') or latest_tp.get('value') else 100,
        }
        radar_scores['overall'] = round(sum(radar_scores.values()) / len(radar_scores))
        
        chart_data = {
            'blood_pressure': {
                'systolic': [v for v in [_get_bp_sys(d) for d in bp_data] if v is not None][-30:],
                'diastolic': [v for v in [_get_bp_dia(d) for d in bp_data] if v is not None][-30:],
                'dates': [_get_time(d) for d in bp_data][-30:]
            },
            'blood_oxygen': {
                'spo2': [d.get('spo2') for d in bo_data if d.get('spo2') is not None][-30:],
                'dates': [_get_time(d) for d in bo_data][-30:]
            },
            'weight': {
                'weight': [d.get('weight') or d.get('value') for d in wt_data if d.get('weight') or d.get('value')][-30:],
                'bodyfat': [d.get('bodyfat') or (d.get('extra', {}) or {}).get('body_fat') for d in wt_data if d.get('bodyfat') or (d.get('extra', {}) or {}).get('body_fat')][-30:],
                'bmi': [d.get('bmi') or (d.get('extra', {}) or {}).get('bmi') for d in wt_data if d.get('bmi') or (d.get('extra', {}) or {}).get('bmi')][-30:],
                'dates': [_get_time(d) for d in wt_data][-30:]
            },
            'heart_rate': {
                'values': [v for v in [_get_bp_pulse(d) for d in bp_data] if v is not None][-30:],
                'dates': [_get_time(d) for d in bp_data][-30:]
            },
            'radar': radar_scores
        }
        
        # ===== 健康报告 v2：干预感知分析引擎（2026-10-11）=====
        # 按天聚合 + 判词规则引擎 + 用药分段统计 + 双轨建议
        latest_values = {
            'systolic': _get_bp_sys(latest_bp) or '--',
            'diastolic': _get_bp_dia(latest_bp) or '--',
            'spo2': latest_bo.get('spo2', '--') if latest_bo else '--',
            'weight': latest_wt.get('weight') or latest_wt.get('value') or '--',
            'bodyfat': latest_wt.get('bodyfat') or (latest_wt.get('extra', {}) or {}).get('body_fat') or '--',
            'bmi': latest_wt.get('bmi') or (latest_wt.get('extra', {}) or {}).get('bmi') or '--',
            'pulse': _get_bp_pulse(latest_bp) or '--',
            'temperature': latest_tp.get('temp') or latest_tp.get('value') or '--'
        }
        from core.utils.health_report_engine import (
            load_member_series, analyze_health, find_family_member)
        from core.api import medication_api, health_event_api
        # "我"等指代兜底：单成员家庭短路；多成员未指名由前端/工具层先行解析（2026-10-11）
        if member_info is None:
            member_info = find_family_member(db_path, member_name)
        medications = medication_api.list_medications(
            member_id=member_info['id'] if member_info else None)
        series = load_member_series(db_path, member_info['id'] if member_info else None)
        events = (health_event_api.list_events(
            member_id=member_info['id'], status='confirmed', limit=200)
            if member_info else [])
        hb = analyze_health(member_info, series, medications, events)

        # 生成报告内容：markdown 各节 + 内嵌 HB_DATA 结构化数据（前端 v2 版式渲染用）
        alert_lines = []
        for a in hb['alerts']:
            icon = {'red': '🔴', 'yellow': '🟡', 'green': '🟢'}.get(a['level'], 'ℹ️')
            alert_lines.append(f"- {icon} 【{a['tag']}】{a['title']}\n  {a['body']}\n  👉 {a['action']}")
        alerts_md = '\n'.join(alert_lines) if alert_lines else '- ✅ 本次无异常提醒'

        trend_lines = [f"- **{t['name']}**：{t['verdict_text']}（{t['compare']}）" for t in hb['trends']]
        trends_md = '\n'.join(trend_lines) if trend_lines else '- 数据积累中'

        metric_rows = '\n'.join(
            f"| {m['k']} | {m['v']} | {m['s']} | {m['avg30']} | {m['prev']} |"
            for m in hb['metrics'])

        advice_secs = []
        for sec in hb['advice']:
            paras = '\n\n'.join(sec['paras'])
            advice_secs.append(f"### {sec['icon']} {sec['title']}\n{paras}")
        advice_md = '\n\n'.join(advice_secs) if advice_secs else '- 暂无建议'

        # 治疗与就诊时间线 / 体检记录（markdown 回退版式用）
        timeline_md = ''
        if hb.get('timeline'):
            rows = '\n'.join(f"- {t['date']} {t['icon']} {t['title']}" for t in hb['timeline'][-10:])
            timeline_md = f"\n## 治疗与就诊记录\n{rows}\n"
        checkup_md = ''
        if hb.get('checkup'):
            ck = hb['checkup']
            items = '、'.join(f"{k} {v}" for k, v in (ck.get('items') or {}).items())
            checkup_md = (f"\n## 体检记录\n- {ck.get('date', '')} {ck.get('institution', '')}"
                          f"：{items or '详见报告'}"
                          + (f"，结论：{ck['conclusion']}" if ck.get('conclusion') else '') + "\n")

        basic_lines = '\n'.join(f"- {k}：{v}" for k, v in hb['basic'].items()) or '- 暂无详细档案信息'

        report_content = f"""# 📊 综合健康报告 - {member_name}

## 报告生成时间
{hb['generated_at']}

## 基本信息
{basic_lines}

🕒 各项最近一次测量：{hb['measure_times']}

## 重点提醒
{alerts_md}

## 最新健康指标
<!-- CHART_DATA: {json.dumps(chart_data)} -->
<!-- LATEST_VALUES: {json.dumps(latest_values)} -->
<!-- HB_DATA: {json.dumps(hb, ensure_ascii=False)} -->

| 指标 | 当前值 | 状态 | 近30天均值 | 上期均值 |
|------|--------|------|-----------|---------|
{metric_rows}

## 长期趋势
{trends_md}

## 健康建议
{advice_md}
{timeline_md}{checkup_md}
## 口播摘要
{hb['speak_summary']}

---
*{hb['note']}*
"""
        
        report = {
            "id": f"report_{int(time.time() * 1000)}_{member_name}",
            "title": f"{member_name} - 综合健康报告",
            "member": member_name,
            "generated_at": datetime.now().isoformat(),
            "skills_used": skills,
            "content": report_content,
            "status": "completed"
        }
        
        # 按需生成，不存数据库
        return web.json_response({
            "success": True,
            "report": report,
            "message": "健康报告已生成"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


def _collect_health_data(health_db_path, db_path, member_info):
    """收集真实健康数据（按成员过滤）
    
    设备全家共用，通过 member_id 区分数据归属。
    只从 devices.db 的 health_data 表读取（有 member_id 字段）。
    OneNet 表没有 member_id，不直接读取。
    """
    import sqlite3
    result = {
        'blood_pressure': [],
        'blood_oxygen': [],
        'weight': [],
        'temperature': [],
        'glucose': [],
        'heart_rate': []
    }
    
    if not os.path.exists(db_path):
        return result
    
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        member_id = member_info.get('id') if member_info else None
        
        if not member_id:
            return result
        
        # 从 health_data 表按 member_id 读取所有类型的健康数据
        cursor.execute('''
            SELECT data_type, value_main, value_extra, unit, timestamp 
            FROM health_data 
            WHERE member_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 100
        ''', (member_id,))
        
        for row in cursor.fetchall():
            data_type = row[0]
            value_main = row[1]
            extra = json.loads(row[2]) if row[2] else {}
            unit = row[3]
            timestamp = row[4]
            
            entry = {
                'value': value_main,
                'extra': extra,
                'unit': unit,
                'time': timestamp,
                'systolic': extra.get('systolic'),
                'diastolic': extra.get('diastolic'),
                'pulse': extra.get('pulse'),
                'spo2': extra.get('spo2'),
                'weight': extra.get('weight') or value_main,
                'bodyfat': extra.get('bodyfat'),
                'bmi': extra.get('bmi'),
                'temp': extra.get('temp') or value_main,
                'glucose': extra.get('glucose') or value_main,
                'heart_rate': extra.get('heart_rate') or value_main,
            }
            
            if data_type == 'blood_pressure':
                result['blood_pressure'].append(entry)
            elif data_type == 'blood_oxygen':
                result['blood_oxygen'].append(entry)
            elif data_type == 'weight':
                result['weight'].append(entry)
            elif data_type == 'body_temperature':
                result['temperature'].append(entry)
            elif data_type == 'glucose':
                result['glucose'].append(entry)
            elif data_type == 'heart_rate':
                result['heart_rate'].append(entry)
    except Exception as e:
        print(f"收集健康数据失败: {e}")
    finally:
        if conn:
            conn.close()
    
    return result


def _generate_health_summary(health_data, member_info):
    """生成健康数据摘要"""
    lines = []
    
    # 血压
    bp = health_data.get('blood_pressure', [])
    if bp:
        latest = bp[0]
        sys_raw = latest.get('systolic') or latest.get('extra', {}).get('systolic')
        dia_raw = latest.get('diastolic') or latest.get('extra', {}).get('diastolic')
        pulse_val = latest.get('pulse') or latest.get('extra', {}).get('pulse')
        sys_val = float(sys_raw) if sys_raw else None
        dia_val = float(dia_raw) if dia_raw else None
        if sys_val and dia_val:
            bp_status = '✅ 正常' if (90 <= sys_val <= 140 and 60 <= dia_val <= 90) else '⚠️ 偏高' if (sys_val > 140 or dia_val > 90) else '⚠️ 偏低'
            lines.append(f"- **血压**：{int(sys_val)}/{int(dia_val)} mmHg（脉搏 {pulse_val or '--'}）{bp_status}")
            if len(bp) > 1:
                avg_sys = sum(float(r.get('systolic', 0) or r.get('extra', {}).get('systolic', 0)) for r in bp if r.get('systolic') or r.get('extra', {}).get('systolic')) / len([r for r in bp if r.get('systolic') or r.get('extra', {}).get('systolic')])
                avg_dia = sum(float(r.get('diastolic', 0) or r.get('extra', {}).get('diastolic', 0)) for r in bp if r.get('diastolic') or r.get('extra', {}).get('diastolic')) / len([r for r in bp if r.get('diastolic') or r.get('extra', {}).get('diastolic')])
                lines.append(f"  - 近期平均：{avg_sys:.0f}/{avg_dia:.0f} mmHg（共 {len(bp)} 次测量）")
        else:
            lines.append("- **血压**：数据收集中")
    else:
        lines.append("- **血压**：暂无数据")
    
    # 血氧
    bo = health_data.get('blood_oxygen', [])
    if bo:
        latest = bo[0]
        spo2_raw = latest.get('spo2')
        spo2 = float(spo2_raw) if spo2_raw else None
        if spo2:
            ox_status = '✅ 正常' if spo2 >= 95 else '⚠️ 偏低'
            lines.append(f"- **血氧**：{int(spo2)}% {ox_status}")
        else:
            lines.append("- **血氧**：数据收集中")
    else:
        lines.append("- **血氧**：暂无数据")
    
    # 体重
    wt = health_data.get('weight', [])
    if wt:
        latest = wt[0]
        weight_raw = latest.get('weight') or latest.get('value')
        weight = float(weight_raw) if weight_raw else None
        bodyfat = latest.get('bodyfat') or latest.get('extra', {}).get('body_fat')
        bmi = latest.get('bmi') or latest.get('extra', {}).get('bmi')
        if weight:
            lines.append(f"- **体重**：{weight} kg" + (f"，体脂 {bodyfat}%" if bodyfat else "") + (f"，BMI {bmi}" if bmi else ""))
            if member_info and member_info.get('height'):
                h_m = member_info['height'] / 100
                calc_bmi = weight / (h_m * h_m)
                bmi_status = '✅ 正常' if (18.5 <= calc_bmi <= 24.9) else '⚠️ 偏瘦' if calc_bmi < 18.5 else '⚠️ 偏胖'
                lines.append(f"  - BMI 计算值：{calc_bmi:.1f} {bmi_status}")
    else:
        lines.append("- **体重**：暂无数据")
    
    # 体温
    tp = health_data.get('temperature', [])
    if tp:
        latest = tp[0]
        temp_raw = latest.get('temp') or latest.get('value')
        temp = float(temp_raw) if temp_raw else None
        if temp:
            temp_status = '✅ 正常' if (36.1 <= temp <= 37.2) else '⚠️ 异常'
            lines.append(f"- **体温**：{temp}°C {temp_status}")
    else:
        lines.append("- **体温**：暂无数据")
    
    # 心率
    hr = health_data.get('heart_rate', [])
    if hr:
        latest = hr[0]
        hr_raw = latest.get('value') or latest.get('extra', {}).get('heart_rate')
        hr_val = float(hr_raw) if hr_raw else None
        if hr_val:
            hr_status = '✅ 正常' if (60 <= hr_val <= 100) else '⚠️ 异常'
            lines.append(f"- **心率**：{int(hr_val)} bpm {hr_status}")
    else:
        lines.append("- **心率**：暂无数据")
    
    return '\n'.join(lines)


def _generate_health_advice(health_data, member_info):
    """根据成员档案和真实数据生成个性化健康建议"""
    age = member_info.get('age') if member_info else None
    gender = member_info.get('gender') if member_info else None
    height = member_info.get('height') if member_info else None
    weight = member_info.get('weight') if member_info else None
    
    is_child = age and age < 18
    is_adult = age and age >= 18
    is_male = gender and gender == '男'
    
    advice_sections = []
    
    # 血压建议
    bp = health_data.get('blood_pressure', [])
    if bp:
        latest = bp[0]
        sys_raw = latest.get('systolic') or latest.get('extra', {}).get('systolic', 0)
        dia_raw = latest.get('diastolic') or latest.get('extra', {}).get('diastolic', 0)
        sys_val = float(sys_raw) if sys_raw else 0
        dia_val = float(dia_raw) if dia_raw else 0
        if sys_val > 140 or dia_val > 90:
            advice_sections.append(f"""### 🩺 血压管理
- **当前血压偏高**（{int(sys_val)}/{int(dia_val)} mmHg），建议关注
- 减少盐分摄入（每日 < 5g），多吃富含钾的食物
- 保持规律运动，每周至少 150 分钟中等强度有氧运动
- 避免熬夜，保证 7-8 小时睡眠
- 建议每日早晚各测量一次血压，记录趋势
- 如持续偏高，请及时就医""")
        elif sys_val > 0:
            advice_sections.append(f"""### 🩺 血压管理
- 当前血压正常（{int(sys_val)}/{int(dia_val)} mmHg），继续保持
- 建议每周测量 1-2 次，监测趋势
- 保持低盐饮食，适量运动""")
    
    # 体重/BMI 建议
    wt = health_data.get('weight', [])
    if wt and height:
        latest = wt[0]
        weight_raw = latest.get('weight') or latest.get('value', 0)
        weight_val = float(weight_raw) if weight_raw else 0
        if weight_val > 0:
            h_m = height / 100
            bmi = weight_val / (h_m * h_m)
            if bmi > 24.9:
                advice_sections.append(f"""### ⚖️ 体重管理
- **BMI {bmi:.1f}，偏胖**，建议适当控制体重
- 标准体重范围：{(18.5 * h_m * h_m):.1f} - {(24.9 * h_m * h_m):.1f} kg
- 控制高热量食物摄入，增加蔬菜比例
- 每天坚持 30 分钟以上有氧运动""")
            elif bmi < 18.5:
                advice_sections.append(f"""### ⚖️ 体重管理
- **BMI {bmi:.1f}，偏瘦**，建议适当增加营养
- 标准体重范围：{(18.5 * h_m * h_m):.1f} - {(24.9 * h_m * h_m):.1f} kg
- 增加蛋白质摄入，适量运动增肌""")
    
    # 血氧建议
    bo = health_data.get('blood_oxygen', [])
    if bo:
        latest = bo[0]
        spo2_raw = latest.get('spo2', 0)
        spo2 = float(spo2_raw) if spo2_raw else 0
        if spo2 > 0 and spo2 < 95:
            advice_sections.append(f"""### 💨 血氧关注
- **血氧偏低**（{int(spo2)}%），建议关注
- 保持室内通风，适当进行深呼吸练习
- 如持续偏低，请及时就医""")
    
    # 年龄相关建议
    if is_child:
        advice_sections.append(f"""### 👶 儿童健康关注
- **生长发育**：定期测量身高体重，关注生长曲线
- **视力保护**：控制电子屏幕时间，每半年检查视力
- **口腔健康**：每半年口腔检查，预防龋齿
- **睡眠**：建议每天 {10 if age < 13 else 9} 小时充足睡眠
- **运动**：每天至少 60 分钟户外活动
- {'男孩' if is_male else '女孩'}发育特点：关注青春期前身体变化""")
    elif is_adult:
        advice_sections.append(f"""### 🏃 成人健康关注
- **定期体检**：建议每年一次全面体检
- **运动**：每周 150 分钟中等强度有氧运动 + 2 次力量训练
- **饮食**：均衡饮食，多蔬果，少油盐糖
- **睡眠**：保证 7-8 小时高质量睡眠
- **心理**：关注压力管理，保持良好心态
- {'男性' if is_male else '女性'}健康：关注性别特有健康筛查""")
    
    if not advice_sections:
        advice_sections.append("""### 💡 健康关注重点
- 定期测量各项健康指标，建立个人健康档案
- 保持良好生活习惯，规律作息
- 如有不适及时就医""")
    
    return '\n\n'.join(advice_sections)


async def get_health_reports(request):
    """获取健康报告列表（报告改为按需生成，返回空数组）"""
    return web.json_response({
        "success": True,
        "reports": [],
        "total": 0,
        "message": "健康报告改为按需生成，不再存储"
    })


async def delete_health_report(request):
    """删除健康报告（报告改为按需生成，无需删除）"""
    return web.json_response({
        "success": True,
        "message": "健康报告改为按需生成，无需删除"
    })


# ==================== 基准数据 API（原初始测量，已统一到 health_data） ====================

async def save_initial_measurement(request):
    """保存基准数据（写入 health_data 表，is_baseline=1）"""
    conn = None
    try:
        data = await request.json()
        member_id = data.get('member_id')
        member_name = data.get('member_name')
        
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        injected = []
        
        # 先删除该成员已有的基准数据（避免重复）
        cursor.execute('DELETE FROM health_data WHERE member_id = ? AND is_baseline = 1', (member_id,))
        
        # 心率
        if data.get('heart_rate') is not None:
            cursor.execute(
                'INSERT INTO health_data (member_id, data_type, value_main, unit, is_baseline, source, category, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, 'heart_rate', data.get('heart_rate'), 'bpm', 1, 'initial', 'vital', '基准数据')
            )
            injected.append('heart_rate')
        
        # 血压
        if data.get('systolic') is not None and data.get('diastolic') is not None:
            extra = json.dumps({'systolic': data.get('systolic'), 'diastolic': data.get('diastolic'), 'pulse': data.get('heart_rate')})
            cursor.execute(
                'INSERT INTO health_data (member_id, data_type, value_main, value_extra, unit, is_baseline, source, category, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, 'blood_pressure', data.get('systolic'), extra, 'mmHg', 1, 'initial', 'vital', '基准数据')
            )
            injected.append('blood_pressure')
        
        # 血氧
        if data.get('oxygen') is not None:
            cursor.execute(
                'INSERT INTO health_data (member_id, data_type, value_main, unit, is_baseline, source, category, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, 'blood_oxygen', data.get('oxygen'), '%', 1, 'initial', 'vital', '基准数据')
            )
            injected.append('blood_oxygen')
        
        # 血糖
        if data.get('glucose') is not None:
            cursor.execute(
                'INSERT INTO health_data (member_id, data_type, value_main, unit, is_baseline, source, category, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, 'glucose', data.get('glucose'), 'mmol/L', 1, 'initial', 'lab', '基准数据')
            )
            injected.append('glucose')
        
        # 体重
        if data.get('weight') is not None:
            cursor.execute(
                'INSERT INTO health_data (member_id, data_type, value_main, unit, is_baseline, source, category, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, 'weight', data.get('weight'), 'kg', 1, 'initial', 'body', '基准数据')
            )
            injected.append('weight')
        
        conn.commit()
        
        return web.json_response({
            "success": True,
            "message": "基准数据已保存",
            "injected_to_health_data": {"success": True, "injected": injected}
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def get_initial_measurements(request):
    """获取基准数据列表（从 health_data 读取 is_baseline=1 的记录）"""
    conn = None
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 按成员分组读取基准数据
        cursor.execute('''
            SELECT hd.member_id, fm.name, hd.data_type, hd.value_main, hd.value_extra, hd.unit, hd.timestamp
            FROM health_data hd
            LEFT JOIN family_members fm ON hd.member_id = fm.id
            WHERE hd.is_baseline = 1
            ORDER BY hd.member_id, hd.timestamp DESC
        ''')
        rows = cursor.fetchall()
        
        # 按成员聚合
        members_map = {}
        for row in rows:
            mid, mname, dtype, vmain, vextra, unit, ts = row
            if mid not in members_map:
                members_map[mid] = {"member_id": mid, "member_name": mname, "heart_rate": None, "systolic": None, "diastolic": None, "oxygen": None, "glucose": None, "weight": None, "bound_at": ts}
            if dtype == 'heart_rate':
                members_map[mid]['heart_rate'] = vmain
            elif dtype == 'blood_pressure':
                extra = json.loads(vextra) if vextra else {}
                members_map[mid]['systolic'] = extra.get('systolic', vmain)
                members_map[mid]['diastolic'] = extra.get('diastolic')
            elif dtype == 'blood_oxygen':
                members_map[mid]['oxygen'] = vmain
            elif dtype == 'glucose':
                members_map[mid]['glucose'] = vmain
            elif dtype == 'weight':
                members_map[mid]['weight'] = vmain
        
        measurements = list(members_map.values())
        
        return web.json_response({
            "success": True,
            "measurements": measurements,
            "total": len(measurements)
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e),
            "measurements": []
        }, status=500)
    finally:
        if conn:
            conn.close()


async def delete_initial_measurement(request):
    """删除基准数据（从 health_data 删除 is_baseline=1 的记录）"""
    conn = None
    try:
        # 支持从 query 参数获取 member_id
        member_id = request.query.get('member_id') or request.query.get('id')
        if not member_id:
            try:
                data = await request.json()
                member_id = data.get('member_id') or data.get('id')
            except:
                pass
        
        if not member_id:
            return web.json_response({
                "success": False,
                "error": "缺少 member_id 参数"
            }, status=400)
        
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM health_data WHERE member_id = ? AND is_baseline = 1', (member_id,))
        conn.commit()
        
        return web.json_response({
            "success": True,
            "message": "基准数据已删除"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


# 家庭活动 API
async def get_family_events(request):
    """获取家庭活动"""
    limit = int(request.query.get('limit', 20))
    event_id = request.query.get('id')
    
    if event_id:
        # 获取单个活动
        return web.json_response({
            "success": True,
            "event": None
        })
    else:
        # 获取活动列表
        return web.json_response({
            "success": True,
            "events": [],
            "total": 0
        })


# 设备状态 API
async def get_devices_status(request):
    """获取设备状态"""
    try:
        from core.utils.ha_client import HAClient
        ha = HAClient()
        devices = await ha.get_all_devices()  # 使用 await 调用 async 方法
        
        return web.json_response({
            "success": True,
            "devices": devices
        })
    except Exception as e:
        return web.json_response({
            "success": True,
            "devices": [],
            "error": str(e)
        })


# ASR 语音识别 API
async def asr_recognize(request):
    """语音识别测试 - 调用 FunASR 服务"""
    try:
        import tempfile
        import wave
        import io
        
        # 读取音频数据和模型参数
        reader = await request.multipart()
        audio_data = None
        model_param = None
        
        async for field in reader:
            if field.name == 'audio':
                audio_data = await field.read()
            elif field.name == 'model':
                model_param = await field.text()  # 读取模型参数
        
        # 如果没有传递模型参数，则从数据库读取 model 和 type
        if not model_param:
            import sqlite3
            db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM asr_config WHERE config_key IN ('model','model_dir','type','streaming','chunk_size','encoder_chunk_look_back','decoder_chunk_look_back','model_type')")
                rows = cursor.fetchall()
            finally:
                conn.close()
            db_config = {row[0]: row[1] for row in rows}
            # 优先使用 model_dir（fun_local.py 等 provider 实际使用的字段）
            model_param = db_config.get('model_dir') or db_config.get('model') or 'paraformer-zh-streaming'
            asr_type = db_config.get('type', 'fun_local')
        else:
            asr_type = None  # 前端传递了 model 参数，根据模型路径推断
        
        if not audio_data:
            return web.json_response({
                "success": False,
                "error": "未收到音频数据"
            }, status=400)
        
        # 保存音频到临时文件
        temp_path = None
        funasr_available = False
        try:
            # 尝试解析为 WAV 文件
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                f.write(audio_data)
                temp_path = f.name
            
            # 调用 FunASR 进行识别
            # 尝试多种 FunASR 调用方式
            result_text = None
            confidence = 0.0
            
            # 方式1: 尝试通过本地 FunASR HTTP 服务
            try:
                async with aiohttp.ClientSession() as session:
                    form_data = aiohttp.FormData()
                    form_data.add_field('audio', open(temp_path, 'rb'), filename='test.wav', content_type='audio/wav')
                    if model_param:
                        form_data.add_field('model', model_param)  # 添加模型参数
                    async with session.post(
                        'http://127.0.0.1:8081/api/asr',
                        data=form_data,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            resp_data = await resp.json()
                            result_text = resp_data.get('text', resp_data.get('result', ''))
                            confidence = resp_data.get('confidence', 0.95)
            except Exception:
                pass
            
            # 方式2: 尝试通过 Python FunASR 库直接调用
            if not result_text:
                try:
                    import sys
                    sys.path.insert(0, os.getcwd())
                    from funasr import AutoModel
                    
                    db_model = model_param
                    # 根据 type 字段或模型路径推断模型类型
                    effective_type = asr_type or ''
                    is_nano = effective_type == 'funasr_nano' or 'nano' in db_model.lower() or 'funasr-nano' in db_model.lower() or 'Fun-ASR-Nano' in db_model
                    is_streaming = db_model.endswith('-streaming')
                    is_sensevoice = 'SenseVoice' in db_model or 'sense_voice' in db_model.lower() or effective_type == 'sherpa_onnx_local'
                    
                    # 根据模型类型选择配置
                    if is_nano:
                        # Nano 模型：使用 AutoModel 直接加载本地模型
                        model_config = {
                            'model': db_model,
                            'disable_update': True,
                            'disable_log': True,
                        }
                    elif is_streaming:
                        model_config = {
                            'model': db_model,
                            'disable_update': True,
                            'disable_log': True
                        }
                    elif is_sensevoice:
                        # SenseVoice 通过 sherpa-onnx 加载，不用 FunASR AutoModel
                        # 跳过 FunASR 调用，让后面的 sherpa-onnx 方式处理
                        raise Exception("sherpa_onnx_sensevoice")
                    else:
                        model_config = {
                            'model': db_model,
                            'vad_model': 'fsmn-vad',
                            'punc_model': 'ct-punc',
                            'disable_update': True,
                            'disable_log': True
                        }
                    
                    # 全球单例 FunASR 模型（按模型类型缓存）
                    cache_key = f'_funasr_model_{db_model}'
                    if not hasattr(asr_recognize, cache_key):
                        logger.info(f"加载 FunASR 模型: {db_model} (type={effective_type})")
                        setattr(asr_recognize, cache_key, AutoModel(**model_config))
                    model = getattr(asr_recognize, cache_key)
                    
                    # 在线程池中运行同步的 model.generate 调用
                    res = await asyncio.to_thread(model.generate, input=temp_path)
                    if res and len(res) > 0:
                        result_text = res[0].get('text', res[0].get('pred', ''))
                        confidence = res[0].get('confidence', 0.9)
                        funasr_available = True
                except Exception as e:
                    logger.error(f"FunASR 直接调用失败: {e}")
            
            # 方式3: 尝试 sherpa-onnx 加载 SenseVoice
            if not result_text:
                try:
                    import sherpa_onnx
                    # SenseVoice 模型路径
                    sensevoice_model_dir = 'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17'
                    model_path = os.path.join(os.path.dirname(__file__), '..', '..', sensevoice_model_dir, 'model.int8.onnx')
                    tokens_path = os.path.join(os.path.dirname(__file__), '..', '..', sensevoice_model_dir, 'tokens.txt')
                    
                    if os.path.isfile(model_path) and os.path.isfile(tokens_path):
                        if not hasattr(asr_recognize, '_sherpa_sensevoice_model'):
                            logger.info("加载 sherpa-onnx SenseVoice 模型")
                            model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                                model=model_path,
                                tokens=tokens_path,
                                num_threads=2,
                                sample_rate=16000,
                                feature_dim=80,
                                decoding_method="greedy_search",
                                debug=False,
                                use_itn=True,
                            )
                            asr_recognize._sherpa_sensevoice_model = model
                        else:
                            model = asr_recognize._sherpa_sensevoice_model
                        
                        # 读取 WAV 文件
                        import wave
                        with wave.open(temp_path, 'rb') as wf:
                            assert wf.getnchannels() == 1
                            assert wf.getsampwidth() == 2
                            num_samples = wf.getnframes()
                            samples = wf.readframes(num_samples)
                            samples_int16 = np.frombuffer(samples, dtype=np.int16)
                            samples_float32 = (samples_int16.astype(np.float32) / 32768).tolist()
                            sample_rate = wf.getframerate()
                        
                        s = model.create_stream()
                        s.accept_waveform(sample_rate, samples_float32)
                        model.decode_stream(s)
                        result_text = s.result.text
                        confidence = 0.95
                        funasr_available = True
                        logger.info(f"sherpa-onnx SenseVoice 识别结果: {result_text}")
                except Exception as e:
                    logger.error(f"sherpa-onnx SenseVoice 调用失败: {e}")
            
            # 方式4: 如果都不可用，返回友好提示
            if not funasr_available:
                return web.json_response({
                    "success": True,
                    "text": "FunASR/SenseVoice 服务未启动，请在服务器部署后重试",
                    "confidence": 0.0,
                    "hint": "ASR 服务未运行"
                })
            
            return web.json_response({
                "success": True,
                "text": result_text,
                "confidence": confidence
            })
            
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
        
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


# ==================== Home Assistant 配置 API ====================

# HA 配置缓存
_HA_CONFIG_CACHE = {}

async def get_ha_config(request):
    """获取 Home Assistant 配置"""
    conn = None
    try:
        # 从数据库读取配置
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT config_key, config_value FROM system_config 
            WHERE config_key LIKE 'ha_%'
        """)
        rows = cursor.fetchall()
        
        config = {}
        for key, value in rows:
            config[key] = value
        
        return web.json_response({
            "success": True,
            "config": {
                "host": config.get('ha_host', ''),
                "port": int(config.get('ha_port', '8123')) if config.get('ha_port') else 8123,
                "token": config.get('ha_token', ''),
                "protocol": config.get('ha_protocol', 'rest')
            }
        })
    except Exception as e:
        logger.error(f"获取 HA 配置失败: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def save_ha_config(request):
    """保存 Home Assistant 配置"""
    conn = None
    try:
        data = await request.json()
        host = data.get('host', '').strip()
        port = data.get('port', 8123)
        token = data.get('token', '').strip()
        protocol = data.get('protocol', 'rest')
        
        if not host or not token:
            return web.json_response({
                "success": False,
                "error": "服务器地址和访问令牌不能为空"
            }, status=400)
        
        # 保存到数据库
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 确保表存在
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                config_key TEXT PRIMARY KEY,
                config_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 保存配置
        configs = [
            ('ha_host', host),
            ('ha_port', str(port)),
            ('ha_token', token),
            ('ha_protocol', protocol)
        ]
        for key, value in configs:
            cursor.execute("""
                INSERT OR REPLACE INTO system_config (config_key, config_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (key, value))
        
        conn.commit()
        
        # 更新内存中的配置
        _HA_CONFIG_CACHE['host'] = host
        _HA_CONFIG_CACHE['port'] = port
        _HA_CONFIG_CACHE['token'] = token
        _HA_CONFIG_CACHE['protocol'] = protocol
        
        return web.json_response({
            "success": True,
            "message": "HA 配置已保存"
        })
    except Exception as e:
        logger.error(f"保存 HA 配置失败: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def test_ha_connection(request):
    """测试 Home Assistant 连接"""
    try:
        data = await request.json()
        host = data.get('host', '').strip()
        port = data.get('port', 8123)
        token = data.get('token', '').strip()
        protocol = data.get('protocol', 'rest')
        
        if not host or not token:
            return web.json_response({
                "success": False,
                "error": "服务器地址和访问令牌不能为空"
            }, status=400)
        
        # 构建正确的 base_url，处理 host 可能包含协议前缀的情况
        if host.startswith(('http://', 'https://')):
            # host 已经包含协议前缀，直接使用
            base_url = host.rstrip('/')
        else:
            # host 只是 IP 或域名，添加 http:// 前缀
            base_url = f"http://{host}"
        
        # 如果端口不是默认的 80/443，添加端口
        if port and port not in (80, 443):
            base_url = f"{base_url}:{port}"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # 测试 REST API 连接
        if protocol == 'rest':
            try:
                async with aiohttp.ClientSession() as session:
                    # 测试获取 HA 版本信息
                    async with session.get(
                        f"{base_url}/api/config",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            ha_config = await resp.json()
                            ha_version = ha_config.get('version', '未知')
                            
                            # 获取设备数量
                            async with session.get(
                                f"{base_url}/api/states",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)
                            ) as states_resp:
                                if states_resp.status == 200:
                                    states = await states_resp.json()
                                    device_count = len([s for s in states if '.' in s.get('entity_id', '')])
                                else:
                                    device_count = 0
                            
                            return web.json_response({
                                "success": True,
                                "message": "连接成功",
                                "ha_version": ha_version,
                                "device_count": device_count,
                                "protocol": "REST API"
                            })
                        elif resp.status == 401:
                            return web.json_response({
                                "success": False,
                                "error": "认证失败，请检查访问令牌是否正确"
                            }, status=401)
                        else:
                            return web.json_response({
                                "success": False,
                                "error": f"HTTP {resp.status}: 连接失败"
                            }, status=resp.status)
            except asyncio.TimeoutError:
                return web.json_response({
                    "success": False,
                    "error": "连接超时，请检查服务器地址和端口是否正确"
                }, status=408)
            except Exception as e:
                return web.json_response({
                    "success": False,
                    "error": f"连接失败: {str(e)}"
                }, status=500)
        
        # 测试 MCP 连接
        elif protocol == 'mcp':
            # MCP 协议测试 - 使用 HTTP POST（与实际连接一致）
            try:
                import aiohttp
                mcp_url = f"http://{host}:{port}/api/mcp"
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "homAi", "version": "1.0.0"}
                    }
                }

                async with aiohttp.ClientSession() as session:
                    async with session.post(mcp_url, json=payload, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "result" in data:
                                server_info = data.get("result", {}).get("serverInfo", {})
                                ha_version = server_info.get("version", "未知")

                                return web.json_response({
                                    "success": True,
                                    "message": "MCP 连接成功",
                                    "ha_version": ha_version,
                                    "device_count": 0,
                                    "protocol": "MCP"
                                })
                            else:
                                error_msg = data.get("error", {}).get("message", "未知错误")
                                return web.json_response({
                                    "success": False,
                                    "error": f"MCP 初始化失败: {error_msg}"
                                }, status=500)
                        else:
                            return web.json_response({
                                "success": False,
                                "error": f"MCP 响应错误: HTTP {resp.status}"
                            }, status=resp.status)
            except asyncio.TimeoutError:
                return web.json_response({
                    "success": False,
                    "error": "MCP 连接超时，请检查服务器地址和端口是否正确"
                }, status=408)
            except Exception as e:
                return web.json_response({
                    "success": False,
                    "error": f"MCP 连接失败: {str(e)}"
                }, status=500)
        
        return web.json_response({
            "success": False,
            "error": "不支持的协议类型"
        }, status=400)
        
    except Exception as e:
        logger.error(f"测试 HA 连接失败: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


# ==================== 模块状态 API ====================

async def get_module_status_asr(request):
    """获取 ASR 模块状态"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            import yaml
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            cfg = load_yaml_expanded(f) or {}
        asr_cfg = cfg.get('ASR', {})
        selected = cfg.get('selected_module', {}).get('ASR', '')
        provider = asr_cfg.get(selected, {})
        return web.json_response({
            "success": True,
            "config": {
                "provider": selected or '未配置',
                "model": provider.get('model_dir', provider.get('model', '')),
                "type": provider.get('type', ''),
            }
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def get_module_status_tts(request):
    """获取 TTS 模块状态【2026-09-13 修复】读运行时合并配置(原只读根 config.yaml,
    selected_module 被 data/.config.yaml 覆盖导致 provider 报错; voiceName 原读不存在的
    'voiceName' 键, 改为读运行时真实生效的 'voice' 键)"""
    try:
        from config.config_loader import load_config
        cfg = load_config() or {}
        tts_cfg = cfg.get('TTS', {}) or {}
        selected = (cfg.get('selected_module', {}) or {}).get('TTS', '')
        provider = tts_cfg.get(selected, {}) or {}
        # 运行时音色以数据库 tts_config 为准（后台保存/热切换都写这里）
        voice_name = ''
        try:
            db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_value FROM tts_config WHERE config_key IN ('voice','voiceName') ORDER BY CASE config_key WHEN 'voice' THEN 0 ELSE 1 END LIMIT 1")
                row = cursor.fetchone()
                if row:
                    voice_name = row[0]
            finally:
                conn.close()
        except:
            pass
        # 火山豆包 2.0 的音色存在 provider.speaker（不是 voice），且与后台 tts_config
        # 无关——后台那个 voice 是 MossNano 用的。切云端后必须读 speaker，否则音色网格
        # 的「当前」高亮会错位（2026-10-07 Mac Mini 全云端部署后暴露）。
        if provider.get('type') == 'huoshan_double_stream':
            voice_name = provider.get('speaker', '')
        return web.json_response({
            "success": True,
            "config": {
                "provider": selected or '未配置',
                "voiceName": voice_name or provider.get('voice', ''),
                "type": provider.get('type', ''),
            }
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def get_module_status_llm(request):
    """获取 LLM 模块状态"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            import yaml
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            cfg = load_yaml_expanded(f) or {}
        llm_cfg = cfg.get('LLM', {})
        selected = cfg.get('selected_module', {}).get('LLM', '')
        provider = llm_cfg.get(selected, {})
        return web.json_response({
            "success": True,
            "config": {
                "provider": selected or '未配置',
                "model": provider.get('model_name', provider.get('model', '')),
                "base_url": provider.get('url', provider.get('base_url', provider.get('baseUrl', ''))),
            }
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def get_ha_status(request):
    """获取 Home Assistant 连接状态"""
    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'ha_%'")
            rows = cursor.fetchall()
        finally:
            conn.close()
        ha_cfg = {k: v for k, v in rows}
        if ha_cfg.get('ha_token'):
            import aiohttp
            host = ha_cfg.get('ha_host', '')
            port = int(ha_cfg.get('ha_port', '8123'))
            token = ha_cfg.get('ha_token', '')
            
            # 构建正确的 URL，处理 host 可能包含协议前缀的情况
            if host.startswith(('http://', 'https://')):
                # host 已经包含协议前缀，直接使用
                base_url = host.rstrip('/')
            else:
                # host 只是 IP 或域名，添加 http:// 前缀
                base_url = f"http://{host}"
            
            # 如果端口不是默认的 80/443，添加端口
            if port and port not in (80, 443):
                base_url = f"{base_url}:{port}"
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{base_url}/api/",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status in (200, 404):  # 404 means HA is running but API path different
                            return web.json_response({"success": True, "connected": True, "message": "Home Assistant 在线"})
            except:
                pass
            return web.json_response({"success": True, "connected": False, "message": "无法连接 HA"})
        return web.json_response({"success": True, "connected": False, "message": "未配置"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def get_onenet_status(request):
    """获取 OneNet 状态"""
    try:
        # 检查数据库中是否有 OneNet 配置
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM system_config WHERE config_key LIKE 'onenet_%'")
            count = cursor.fetchone()[0]
        finally:
            conn.close()
        if count > 0:
            return web.json_response({"success": True, "connected": True, "message": "OneNet 在线"})
        return web.json_response({"success": True, "connected": False, "message": "OneNet 未配置"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


# ==================== 对话历史 API ====================

async def get_dialogue_history(request):
    """获取对话历史"""
    device_id = request.query.get('device_id', '')
    page = int(request.query.get('page', 1))
    limit = int(request.query.get('limit', 20))
    offset = (page - 1) * limit

    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()

            # 确保表存在
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dialogue_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    session_id TEXT,
                    user_text TEXT,
                    ai_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

            if device_id:
                # 网页端聚合（2026-10-13 主人要求）：网页不为每台设备单列，
                # 前端「🖥️ 网页端」选项传 web:，把所有 web-* 设备的对话合在一起查
                dev_sql, dev_p = ("device_id LIKE ?", "web-%") if device_id == "web:" else ("device_id = ?", device_id)
                cursor.execute(
                    f'SELECT COUNT(*) FROM dialogue_history WHERE {dev_sql}',
                    (dev_p,)
                )
                count_row = cursor.fetchone()
                total = count_row[0] if count_row else 0

                cursor.execute(
                    f'SELECT id, device_id, session_id, user_text, ai_text, created_at FROM dialogue_history WHERE {dev_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?',
                    (dev_p, limit, offset)
                )
            else:
                cursor.execute('SELECT COUNT(*) FROM dialogue_history')
                count_row = cursor.fetchone()
                total = count_row[0] if count_row else 0

                cursor.execute(
                    'SELECT id, device_id, session_id, user_text, ai_text, created_at FROM dialogue_history ORDER BY created_at DESC LIMIT ? OFFSET ?',
                    (limit, offset)
                )

            rows = cursor.fetchall()
        finally:
            conn.close()

        records = []
        for row in rows:
            records.append({
                "id": row[0],
                "device_id": row[1],
                "session_id": row[2],
                "user_text": row[3],
                "ai_text": row[4],
                "created_at": row[5],
            })

        return web.json_response({
            "success": True,
            "records": records,
            "total": total,
            "page": page,
            "limit": limit,
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e),
            "records": [],
            "total": 0,
        }, status=500)


# ==================== 对话摘要 API ====================

async def get_conversation_summaries(request):
    """获取对话摘要列表"""
    device_id = request.query.get('device_id', '')
    start_date = request.query.get('start', '')
    end_date = request.query.get('end', '')
    keyword = request.query.get('keyword', '')
    page = int(request.query.get('page', 1))
    limit = int(request.query.get('limit', 20))
    offset = (page - 1) * limit

    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()

            # 确保表存在
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    timestamp REAL,
                    speaker TEXT,
                    topic TEXT,
                    content TEXT,
                    keywords TEXT,
                    valuable INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

            # 构建查询条件
            conditions = []
            params = []
            if device_id:
                conditions.append("session_id LIKE ?")
                # 网页端聚合（2026-10-13）：web: = 所有 web-* 会话合在一起查
                params.append("%web-%" if device_id == "web:" else f"%{device_id}%")
            if start_date:
                conditions.append("timestamp >= ?")
                params.append(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
            if end_date:
                conditions.append("timestamp <= ?")
                end_ts = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp()
                params.append(end_ts)
            if keyword:
                conditions.append("content LIKE ?")
                params.append(f"%{keyword}%")

            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

            # 查询总数
            cursor.execute(f'SELECT COUNT(*) FROM conversation_summaries {where_clause}', params)
            total = cursor.fetchone()[0]

            # 查询数据
            cursor.execute(
                f'SELECT id, session_id, timestamp, speaker, topic, content, keywords, valuable, created_at FROM conversation_summaries {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?',
                params + [limit, offset]
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        records = []
        for row in rows:
            records.append({
                "id": row[0],
                "session_id": row[1],
                "timestamp": row[2],
                "timestamp_str": datetime.fromtimestamp(row[2]).strftime("%Y-%m-%d %H:%M:%S") if row[2] else "",
                "speaker": row[3],
                "topic": row[4] or "",
                "content": row[5],
                "keywords": row[6] or "",
                "valuable": row[7],
                "created_at": row[8],
            })

        return web.json_response({
            "success": True,
            "records": records,
            "total": total,
            "page": page,
            "limit": limit,
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e),
            "records": [],
            "total": 0,
        }, status=500)


async def delete_conversation_summaries(request):
    """批量删除对话摘要"""
    try:
        body = await request.json()
        ids = body.get("ids", [])
        if not ids:
            return web.json_response({
                "success": False,
                "error": "请指定要删除的摘要ID",
            }, status=400)

        from core.utils.conversation_summary import delete_summaries
        deleted = delete_summaries(ids)
        return web.json_response({
            "success": True,
            "deleted": deleted,
            "message": f"已删除 {deleted} 条摘要",
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e),
        }, status=500)


# ==================== /xiaozhi/* 路由（manager-web 兼容层） ====================

def _ok(data=None, msg="ok"):
    """小智标准响应格式"""
    return web.json_response({"code": 0, "msg": msg, "data": data or {}})


def _err(msg="error", code=-1):
    return web.json_response({"code": code, "msg": msg, "data": None}, status=200)


async def xiaozhi_user_login(request):
    """管理员登录（模拟，直接返回 token）"""
    import uuid
    token = str(uuid.uuid4()).replace("-", "")
    return _ok({
        "token": token,
        "expire": 86400 * 7,
        "refreshExpire": 86400 * 30,
    })


async def xiaozhi_user_captcha(request):
    """返回空白验证码（跳过验证码）"""
    from aiohttp.web_response import StreamResponse
    # 返回 1x1 透明 GIF
    gif = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
    return web.Response(body=gif, content_type='image/gif')


async def xiaozhi_user_info(request):
    """获取用户信息"""
    return _ok({
        "id": 1,
        "username": "admin",
        "nickname": "Admin",
        "role": "admin",
        "status": 1,
    })


async def xiaozhi_user_pub_config(request):
    """公共配置（允许注册、短信等）"""
    return _ok({
        "allowUserRegister": False,
        "enableMobileRegister": False,
        "enableForgetPassword": False,
        "sm2PublicKey": "",
        "mobileAreaList": [],
    })


async def xiaozhi_user_config_device(request):
    """设备配置保存（模拟成功）"""
    return _ok(msg="设备配置已保存")


async def xiaozhi_agent_list(request):
    """智能体列表 - 从 devices.db 读取"""
    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, source, mac_address FROM devices ORDER BY id')
            rows = cursor.fetchall()
        finally:
            conn.close()

        agents = []
        for row in rows:
            agents.append({
                "id": row[0],
                "name": row[1] or f"Device-{row[0]}",
                "source": row[2] or "esp32",
                "macAddress": row[3] or "",
                "lastLogin": "",
                "createTime": "",
            })
        return _ok({"records": agents, "total": len(agents)})
    except Exception as e:
        return _ok({"records": [], "total": 0})


async def xiaozhi_agent_create(request):
    """创建智能体（模拟）"""
    try:
        data = await request.json()
        return _ok({"id": int(time.time() * 1000) % 100000, "name": data.get("name", "New Agent")})
    except Exception as e:
        return _err(str(e))


async def xiaozhi_agent_update(request):
    """更新智能体（模拟）"""
    return _ok(msg="更新成功")


async def xiaozhi_agent_delete(request):
    """删除智能体（模拟）"""
    return _ok(msg="删除成功")


async def xiaozhi_device_bind(request):
    """设备绑定列表 - 从 devices.db 读取"""
    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, mac_address, source FROM devices ORDER BY id')
            rows = cursor.fetchall()
        finally:
            conn.close()
        records = []
        for row in rows:
            records.append({
                "id": row[0], "name": row[1] or f"Device-{row[0]}",
                "macAddress": row[2] or "", "source": row[3] or "esp32",
            })
        return _ok({"records": records, "total": len(records)})
    except Exception as e:
        return _ok({"records": [], "total": 0})


async def xiaozhi_device_unbind(request):
    """设备解绑（模拟）"""
    return _ok(msg="解绑成功")


async def xiaozhi_device_manual_add(request):
    """手动添加设备"""
    try:
        data = await request.json()
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR IGNORE INTO devices (name, mac_address, source) VALUES (?, ?, ?)',
                (data.get('name', 'New Device'), data.get('macAddress', ''), data.get('source', 'esp32'))
            )
            conn.commit()
            new_id = cursor.lastrowid
        finally:
            conn.close()
        return _ok({"id": new_id})
    except Exception as e:
        return _err(str(e))


async def xiaozhi_device_update(request):
    """更新设备信息"""
    dev_id = request.match_info.get('id', '')
    try:
        data = await request.json()
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'devices.db')
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            if 'name' in data:
                cursor.execute('UPDATE devices SET name = ? WHERE id = ?', (data['name'], dev_id))
            conn.commit()
        finally:
            conn.close()
        return _ok(msg="更新成功")
    except Exception as e:
        return _err(str(e))


async def xiaozhi_device_bind_with_code(request):
    """通过设备码绑定（模拟）"""
    return _ok(msg="绑定成功")


# --- 以下 API 返回空列表/模拟数据 ---

async def _empty_list(request):
    return _ok({"records": [], "total": 0})


async def _empty_records(request):
    return _ok({"records": [], "total": 0})


async def xiaozhi_ota_list(request):
    """OTA 固件列表 - 扫描 data/bin/ 目录"""
    try:
        bin_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'bin')
        files = []
        if os.path.isdir(bin_dir):
            for f in os.listdir(bin_dir):
                if f.endswith('.bin'):
                    fp = os.path.join(bin_dir, f)
                    files.append({
                        "id": f,
                        "fileName": f,
                        "fileSize": os.path.getsize(fp),
                        "createTime": datetime.fromtimestamp(os.path.getmtime(fp)).isoformat(),
                    })
        return _ok({"records": files, "total": len(files)})
    except Exception as e:
        return _ok({"records": [], "total": 0})


async def xiaozhi_model_list(request):
    """模型配置列表 - 从 config.yaml 读取"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            import yaml
            cfg = yaml.safe_load(f)
        models = []
        for section in ['LLM', 'TTS', 'ASR', 'VAD', 'Memory', 'Intent']:
            if section in cfg:
                for name, params in cfg[section].items():
                    models.append({
                        "id": f"{section}_{name}",
                        "name": name,
                        "modelType": section,
                        "type": params.get('type', ''),
                        "provider": section,
                    })
        return _ok({"records": models, "total": len(models)})
    except Exception as e:
        return _ok({"records": [], "total": 0})


async def xiaozhi_model_provide_types(request):
    """模型供应器类型列表"""
    return _ok({"records": [], "total": 0})


async def xiaozhi_admin_users(request):
    """用户列表"""
    return _ok({"records": [{"id": 1, "username": "admin", "nickname": "Admin", "role": "admin", "status": 1}], "total": 1})


async def xiaozhi_admin_params_page(request):
    """参数分页"""
    return _ok({"records": [], "total": 0})


async def xiaozhi_admin_dict_page(request):
    """字典分页"""
    return _ok({"records": [], "total": 0})


async def xiaozhi_voice_clone(request):
    """音色克隆（模拟）"""
    return _ok({"records": [], "total": 0})


async def xiaozhi_address_book(request):
    """通讯录"""
    mac_address = request.match_info.get('macAddress', '')
    return _ok({"records": [], "total": 0})


# ==================== RAGflow 代理 ====================
async def ragflow_search(request):
    """代理 RAGflow 搜索，带上 API key"""
    try:
        from config.config_loader import load_config
        config = load_config() or {}
        ragflow_cfg = (config.get("plugins") or {}).get("search_from_ragflow") or {}
        base_url = (ragflow_cfg.get("base_url") or "http://localhost:9380").rstrip("/")
        api_key = ragflow_cfg.get("api_key", "")
        dataset_ids = ragflow_cfg.get("dataset_ids", [])

        # data/.config.yaml 可能缺少 dataset_ids，用完整的 5 个数据集兜底
        _ALL_DATASET_IDS = [
            'YOUR_RAGFLOW_DATASET_ID_1',  # 数据集 1
            'YOUR_RAGFLOW_DATASET_ID_2',  # 数据集 2
            'YOUR_RAGFLOW_DATASET_ID_3',  # 数据集 3
            'YOUR_RAGFLOW_DATASET_ID_4',  # 数据集 4
            'YOUR_RAGFLOW_DATASET_ID_5',  # 数据集 5
        ]
        if not dataset_ids:
            dataset_ids = _ALL_DATASET_IDS

        # 包含"我的收藏"数据集
        inbox_dataset_id = 'YOUR_RAGFLOW_INBOX_DATASET_ID'
        if inbox_dataset_id not in dataset_ids:
            dataset_ids = list(dataset_ids) + [inbox_dataset_id]

        if not api_key:
            return web.json_response({"success": False, "error": "RAGflow api_key 未配置"}, status=500)

        keyword = request.query.get('q', '').strip()
        if not keyword:
            return web.json_response({"success": False, "error": "缺少搜索关键词 q"}, status=400)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/api/v1/retrieval",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"question": keyword, "dataset_ids": dataset_ids},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    return web.json_response({"success": False, "error": f"RAGflow 错误: {body}"}, status=502)
                return web.json_response({"success": True, "data": body})
    except asyncio.TimeoutError:
        return web.json_response({"success": False, "error": "RAGflow 请求超时"}, status=504)
    except Exception as e:
        logger.error(f"RAGflow 搜索代理异常: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def ragflow_test_connection(request):
    """代理 RAGflow 连接测试，带上 API key"""
    try:
        from config.config_loader import load_config
        config = load_config() or {}
        ragflow_cfg = (config.get("plugins") or {}).get("search_from_ragflow") or {}
        base_url = (ragflow_cfg.get("base_url") or "http://localhost:9380").rstrip("/")
        api_key = ragflow_cfg.get("api_key", "")

        if not api_key:
            return web.json_response({"success": False, "error": "RAGflow api_key 未配置"}, status=500)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}/api/v1/datasets",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return web.json_response({"success": True, "message": "RAGFlow 连接成功"})
                else:
                    body = await resp.text()
                    return web.json_response({"success": False, "error": f"RAGflow 返回 {resp.status}: {body}"}, status=502)
    except asyncio.TimeoutError:
        return web.json_response({"success": False, "error": "RAGflow 连接超时"}, status=504)
    except Exception as e:
        logger.error(f"RAGflow 连接测试异常: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def ragflow_list_documents(request):
    """代理列出某个数据集的文档列表"""
    try:
        from config.config_loader import load_config
        config = load_config() or {}
        ragflow_cfg = (config.get("plugins") or {}).get("search_from_ragflow") or {}
        base_url = (ragflow_cfg.get("base_url") or "http://localhost:9380").rstrip("/")
        api_key = ragflow_cfg.get("api_key", "")
        dataset_id = request.match_info.get('dataset_id', '')

        if not api_key:
            return web.json_response({"success": False, "error": "RAGflow api_key 未配置"}, status=500)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}/api/v1/datasets/{dataset_id}/documents",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"page": 1, "page_size": 100},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    return web.json_response({"success": False, "error": f"RAGflow 错误: {body}"}, status=502)
                return web.json_response({"success": True, "data": body})
    except asyncio.TimeoutError:
        return web.json_response({"success": False, "error": "RAGflow 请求超时"}, status=504)
    except Exception as e:
        logger.error(f"RAGflow 文档列表代理异常: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def create_xiaozhi_routes():
    """创建 /xiaozhi/* 路由"""
    from aiohttp import web
    routes = [
        # 用户相关
        web.post('/xiaozhi/user/login', xiaozhi_user_login),
        web.get('/xiaozhi/user/captcha', xiaozhi_user_captcha),
        web.get('/xiaozhi/user/info', xiaozhi_user_info),
        web.get('/xiaozhi/user/pub-config', xiaozhi_user_pub_config),
        web.put('/xiaozhi/user/configDevice/{device_id}', xiaozhi_user_config_device),
        # 智能体相关
        web.get('/xiaozhi/agent/list', xiaozhi_agent_list),
        web.post('/xiaozhi/agent', xiaozhi_agent_create),
        web.put('/xiaozhi/agent/{agentId}', xiaozhi_agent_update),
        web.delete('/xiaozhi/agent/{agentId}', xiaozhi_agent_delete),
        # 设备相关
        web.get('/xiaozhi/device/bind/{agentId}', xiaozhi_device_bind),
        web.get('/xiaozhi/device/bind/{agentId}/{deviceCode}', xiaozhi_device_bind_with_code),
        web.post('/xiaozhi/device/unbind', xiaozhi_device_unbind),
        web.post('/xiaozhi/device/manual-add', xiaozhi_device_manual_add),
        web.put('/xiaozhi/device/update/{id}', xiaozhi_device_update),
        # 通讯录
        web.get('/xiaozhi/device/address-book/{macAddress}', xiaozhi_address_book),
        web.put('/xiaozhi/device/address-book/alias', _empty_list),
        web.get('/xiaozhi/device/address-book/permission', _empty_list),
        # OTA 管理
        web.get('/xiaozhi/otaMag', xiaozhi_ota_list),
        web.post('/xiaozhi/otaMag', _empty_list),
        web.get('/xiaozhi/otaMag/{id}', _empty_list),
        web.put('/xiaozhi/otaMag/{id}', _empty_list),
        web.delete('/xiaozhi/otaMag/{id}', _empty_list),
        # 模型配置
        web.get('/xiaozhi/models/list', xiaozhi_model_list),
        web.get('/xiaozhi/models/{modelType}/provideTypes', xiaozhi_model_provide_types),
        web.get('/xiaozhi/models/{modelType}/{provideCode}', _empty_list),
        web.get('/xiaozhi/models/names', _empty_list),
        # 管理员相关
        web.get('/xiaozhi/admin/users', xiaozhi_admin_users),
        web.get('/xiaozhi/admin/users/{id}', _empty_list),
        web.put('/xiaozhi/admin/users/{id}', _empty_list),
        web.delete('/xiaozhi/admin/users/{id}', _empty_list),
        web.put('/xiaozhi/admin/users/changeStatus/{status}', _empty_list),
        web.get('/xiaozhi/admin/params/page', xiaozhi_admin_params_page),
        web.post('/xiaozhi/admin/params', _empty_list),
        web.get('/xiaozhi/admin/dict/page', xiaozhi_admin_dict_page),
        web.post('/xiaozhi/admin/dict/type', _empty_list),
        web.get('/xiaozhi/admin/dict/type/page', _empty_list),
        web.put('/xiaozhi/admin/dict/type/{id}', _empty_list),
        web.post('/xiaozhi/admin/dict/type/save', _empty_list),
        web.put('/xiaozhi/admin/dict/type/update', _empty_list),
        web.delete('/xiaozhi/admin/dict/type/delete', _empty_list),
        web.delete('/xiaozhi/admin/dict/type', _empty_list),
        # 音色相关
        web.get('/xiaozhi/timbre/list', _empty_list),
        web.get('/xiaozhi/timbre', _empty_list),
        web.get('/xiaozhi/timbre/page', _empty_list),
        web.post('/xiaozhi/timbre', _empty_list),
        web.put('/xiaozhi/timbre/{id}', _empty_list),
        web.delete('/xiaozhi/timbre/delete', _empty_list),
        web.get('/xiaozhi/timbre/{id}', _empty_list),
        # 语音资源
        web.get('/xiaozhi/voiceResource/list', _empty_list),
        web.get('/xiaozhi/voiceResource', _empty_list),
        web.post('/xiaozhi/voiceResource', _empty_list),
        web.put('/xiaozhi/voiceResource/{id}', _empty_list),
        web.delete('/xiaozhi/voiceResource/{ids}', _empty_list),
        web.get('/xiaozhi/voiceResource/user/{userId}', _empty_list),
        # 知识库
        web.get('/xiaozhi/knowledgeBase/list', _empty_list),
        web.post('/xiaozhi/knowledgeBase', _empty_list),
        web.put('/xiaozhi/knowledgeBase/{id}', _empty_list),
        web.delete('/xiaozhi/knowledgeBase/{ids}', _empty_list),
        web.get('/xiaozhi/knowledgeBase/user/{userId}', _empty_list),
        web.get('/xiaozhi/knowledgeBase/{kbId}/doc/page', _empty_list),
        # 替换词
        web.get('/xiaozhi/correctWord/list', _empty_list),
        web.get('/xiaozhi/correctWord/file/list', _empty_list),
        web.post('/xiaozhi/correctWord/file', _empty_list),
        web.get('/xiaozhi/correctWord/file/select', _empty_list),
        web.get('/xiaozhi/correctWord/file/download/{id}', _empty_list),
        web.put('/xiaozhi/correctWord/file/{id}', _empty_list),
        web.delete('/xiaozhi/correctWord/file/{id}', _empty_list),
        web.delete('/xiaozhi/correctWord/file', _empty_list),
        # 音色克隆
        web.get('/xiaozhi/voiceClone', xiaozhi_voice_clone),
        web.post('/xiaozhi/voiceClone', _empty_list),
        web.put('/xiaozhi/voiceClone/upload', _empty_list),
        web.get('/xiaozhi/voiceClone/{id}', _empty_list),
        web.delete('/xiaozhi/voiceClone/{ids}', _empty_list),
        web.put('/xiaozhi/voiceClone/updateName', _empty_list),
        web.get('/xiaozhi/voiceClone/audio/{id}', _empty_list),
        web.post('/xiaozhi/voiceClone/cloneAudio', _empty_list),
        # 短信（不需要）
        web.post('/xiaozhi/user/smsVerification', _empty_list),
        web.post('/xiaozhi/user/register', _empty_list),
        web.put('/xiaozhi/user/retrieve-password', _empty_list),
        web.put('/xiaozhi/user/change-password', _empty_list),
        # 功能配置
        web.get('/xiaozhi/feature/list', _empty_list),
        web.post('/xiaozhi/feature', _empty_list),
        web.put('/xiaozhi/feature/{id}', _empty_list),
        web.delete('/xiaozhi/feature/{ids}', _empty_list),
        # 模板配置
        web.get('/xiaozhi/template/list', _empty_list),
        web.post('/xiaozhi/template', _empty_list),
        web.put('/xiaozhi/template/{id}', _empty_list),
        web.delete('/xiaozhi/template/{ids}', _empty_list),
    ]
    return routes

