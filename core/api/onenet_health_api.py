"""
OneNET 云平台健康数据API
从中国移动物联网开放平台读取真实的健康监测数据

API文档：
- 设备属性获取：https://iot-api.heclouds.com/thingmodel/get-device-property
- 服务调用：https://iot-api.heclouds.com/thingmodel/call-service
- 数据推送：HTTP POST方式接收设备上报数据

健康设备类型：
- 血压计(80): systolic, diastolic, pulse, battery, status
- 血氧仪(85): spo2, pr, prcode, battery, mode
- 血糖仪(82): glucose, battery
- 体温计(93): temp, code, type
- 体脂秤(81): weight, unit, adc, bodyfat, bmi
"""

import os
import json
import yaml
import time
import hashlib
import hmac
import base64
import urllib.parse
import sqlite3
from datetime import datetime
from pathlib import Path
import aiohttp
import asyncio
from typing import Dict, Any, Optional, List
from aiohttp import web

# Import the health data identification function from health_data_api
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from api.health_data_api import identify_member_by_health_data

# 配置文件路径
CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "onenet_config.yaml"
DATA_DB_PATH = Path(__file__).parent.parent.parent / "data" / "health_data.db"


class OneNetHealthAPI:
    """OneNET云平台健康数据API客户端"""
    
    def __init__(self, config_path: str = None):
        """初始化API客户端"""
        self.config = self._load_config(config_path or str(CONFIG_PATH))
        self.api_url = self.config.get('platform', {}).get('api_url', 'https://iot-api.heclouds.com')
        self.product_id = self.config.get('product', {}).get('id', '')
        self.device_name = self.config.get('device', {}).get('name', '')
        self.device_sn = self.config.get('device', {}).get('sn', '')
        self.device_iccid = self.config.get('device', {}).get('iccid', '')
        self.device_model = self.config.get('device', {}).get('model', '')
        self.access_token = self.config.get('auth', {}).get('access_token', '')
        self.api_key = self.config.get('auth', {}).get('api_key', '')
        
        # 初始化数据库
        self._init_database()
    
    def _load_config(self, config_path: str) -> Dict:
        """加载配置文件"""
        try:
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"加载配置文件失败: {e}")
        return {}
    
    def _init_database(self):
        """初始化数据库表"""
        db_path = str(DATA_DB_PATH)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
        
            # 创建健康数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_health_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_type INTEGER NOT NULL,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            # 创建血压数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_blood_pressure (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    systolic INTEGER NOT NULL,
                    diastolic INTEGER NOT NULL,
                    pulse INTEGER,
                    battery INTEGER,
                    status INTEGER DEFAULT 0,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            # 创建血氧数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_blood_oxygen (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spo2 INTEGER NOT NULL,
                    pr INTEGER,
                    prcode INTEGER,
                    battery INTEGER,
                    mode INTEGER,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            # 创建血糖数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_glucose (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    glucose TEXT NOT NULL,
                    battery INTEGER,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            # 创建体温数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_temperature (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    temp TEXT NOT NULL,
                    code INTEGER,
                    type INTEGER,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            # 创建体重数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS onenet_weight (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    weight TEXT NOT NULL,
                    unit INTEGER,
                    adc TEXT,
                    bodyfat TEXT,
                    bmi TEXT,
                    device_mac TEXT,
                    measurement_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
            conn.commit()
        finally:
            conn.close()
    
    def _generate_token(self) -> str:
        """生成 OneNET 项目鉴权 Token
        
        关键点：
            1. 签名算法必须是 sha1（不是 sha256！）
            2. 资源格式是 projects/{project_id}（不是 products/{product_id}）
            3. project_key 需要先 base64 解码
            4. 签名字符串格式：et + "\n" + method + "\n" + res + "\n" + version
        """
        project_id = self.config.get('auth', {}).get('project_id', '')
        project_key = self.config.get('auth', {}).get('project_key', '')
        
        if not project_id or not project_key:
            # 回退到旧方式
            if self.api_key:
                return self.api_key
            return self.access_token
        
        version = '2022-05-01'
        et = int(time.time()) + 3600  # 1小时有效期
        
        # 资源格式：projects/{project_id}
        res = f'projects/{project_id}'
        res_encoded = urllib.parse.quote(res, safe='')
        
        # 签名算法：sha1（重要！）
        method = 'sha1'
        
        # 签名字符串
        string_to_sign = f'{et}\n{method}\n{res}\n{version}'
        
        # project_key 先 base64 解码
        key_bytes = base64.b64decode(project_key)
        
        # HMAC-SHA1 签名
        sign = base64.b64encode(
            hmac.new(key_bytes, string_to_sign.encode('utf-8'), hashlib.sha1).digest()
        ).decode('utf-8')
        sign_encoded = urllib.parse.quote(sign, safe='')
        
        # 组装 Token
        token = f'version={version}&res={res_encoded}&et={et}&method={method}&sign={sign_encoded}'
        
        return token
    
    def _get_headers(self) -> Dict[str, str]:
        """获取API请求头"""
        token = self._generate_token()
        return {
            "Content-Type": "application/json",
            "Authorization": token,
            "Accept": "application/json"
        }
    
    async def _call_api(self, endpoint: str, method: str = "GET", data: Dict = None) -> Dict:
        """调用OneNET API"""
        url = f"{self.api_url}{endpoint}"
        headers = self._get_headers()
        
        try:
            async with aiohttp.ClientSession() as session:
                if method == "GET":
                    async with session.get(url, headers=headers) as resp:
                        return await resp.json()
                elif method == "POST":
                    async with session.post(url, headers=headers, json=data) as resp:
                        return await resp.json()
        except Exception as e:
            print(f"API调用失败: {e}")
            return {"error": str(e)}
    
    async def get_device_property(self, property_name: str) -> Dict:
        """获取设备属性值
        
        Args:
            property_name: 属性名称，如 ble_subdevices, ble_subdevice_event
        
        Returns:
            属性值数据
        """
        if not self.product_id or not self.device_name:
            return {"error": "缺少产品ID或设备名称配置"}
        
        # 使用项目级别的API调用（根据文档，产品级别API可能鉴权失败）
        # 根据记忆文件，应该使用项目级别的API
        endpoint = "/thingmodel/call-service"
        data = {
            "product_id": self.product_id,
            "device_name": self.device_name,
            "service_name": "get_property",
            "params": {
                "property": property_name
            }
        }
        
        result = await self._call_api(endpoint, "POST", data)
        
        # 如果产品级别API调用失败（鉴权错误），尝试使用设备ID方式
        if result and (result.get("code") == 10403 or result.get("errno") != 0):
            # 尝试使用设备ID进行调用
            data_alt = {
                "did": "2484237223",  # 已知的设备ID
                "service_name": "get_property",
                "params": {
                    "property": property_name
                }
            }
            result = await self._call_api(endpoint, "POST", data_alt)
        
        return result
    
    async def get_ble_subdevices(self) -> List[Dict]:
        """获取蓝牙健康子设备列表"""
        result = await self.get_device_property("ble_subdevices")
        
        if result is None or "error" in result:
            return []
        
        # 解析子设备列表
        devices = []
        if result and "data" in result and result["data"] and "value" in result["data"]:
            value = result["data"]["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except:
                    value = []
            
            for device in value:
                devices.append({
                    "mac": device.get("mac", ""),
                    "type": device.get("type", 0),
                    "name": device.get("name", ""),
                    "audible": device.get("audible", 0)
                })
        
        return devices
    
    async def get_health_data_history(self, start_time: int = None, end_time: int = None) -> List[Dict]:
        """获取健康数据历史记录
        
        Args:
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
        
        Returns:
            健康数据列表
        """
        # 从数据库读取历史数据
        conn = sqlite3.connect(str(DATA_DB_PATH))
        try:
            cursor = conn.cursor()
        
            query = "SELECT * FROM onenet_health_data"
            params = []
        
            if start_time:
                query += " WHERE measurement_time >= ?"
                params.append(datetime.fromtimestamp(start_time/1000))
        
            if end_time:
                if start_time:
                    query += " AND measurement_time <= ?"
                else:
                    query += " WHERE measurement_time <= ?"
                params.append(datetime.fromtimestamp(end_time/1000))
        
            query += " ORDER BY measurement_time DESC LIMIT 100"
        
            cursor.execute(query, params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        
        data = []
        for row in rows:
            data.append({
                "id": row[0],
                "device_type": row[1],
                "device_mac": row[2],
                "measurement_time": row[3],
                "data": json.loads(row[4]) if row[4] else {}
            })
        
        return data
    
    def save_health_data(self, device_type: int, device_mac: str, data: Dict) -> bool:
        """保存健康数据到数据库
        
        Args:
            device_type: 设备类型（80=血压计, 85=血氧仪, 82=血糖仪, 93=体温计, 81=体脂秤）
            device_mac: 设备MAC地址
            data: 健康数据字典
        
        Returns:
            是否保存成功
        """
        conn = sqlite3.connect(str(DATA_DB_PATH))
        cursor = conn.cursor()
        
        try:
            # 保存原始数据
            data_json = json.dumps(data)
            
            # 第一步：检查是否已有相同数据（去重）
            # 获取数据中的timestamp（如果有的话）
            data_timestamp = data.get('timestamp')
            duplicate_check_sql = None
            duplicate_params = None
            
            if data_timestamp:
                # 如果有timestamp，使用timestamp和设备类型/MAC检查重复
                duplicate_check_sql = """
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ? 
                    AND data_json LIKE ?
                    AND created_at > datetime('now', '-10 minutes')
                """
                duplicate_params = (device_type, device_mac, f'%\"timestamp\": \"{data_timestamp}\"%')
            else:
                # 如果没有timestamp，使用完整数据JSON检查
                duplicate_check_sql = """
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ? 
                    AND data_json = ?
                    AND created_at > datetime('now', '-10 minutes')
                """
                duplicate_params = (device_type, device_mac, data_json)
            
            cursor.execute(duplicate_check_sql, duplicate_params)
            existing_record = cursor.fetchone()
            
            if existing_record:
                print(f"⚠️  发现重复数据，跳过插入: 设备{device_type}({device_mac}), timestamp={data_timestamp}")
                conn.close()
                return True  # 返回True表示"已处理"，但实际没插入
            
            # 第二步：检查是否在合理时间内有类似数据
            # 避免过于频繁的记录
            if device_type == 81:  # 体重秤 - 至少间隔1小时
                cursor.execute("""
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ?
                    AND created_at > datetime('now', '-1 hour')
                    ORDER BY created_at DESC LIMIT 1
                """, (device_type, device_mac))
            elif device_type in [80, 91]:  # 血压计 - 至少间隔30分钟
                cursor.execute("""
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ?
                    AND created_at > datetime('now', '-30 minutes')
                    ORDER BY created_at DESC LIMIT 1
                """, (device_type, device_mac))
            elif device_type == 85:  # 血氧仪 - 至少间隔15分钟
                cursor.execute("""
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ?
                    AND created_at > datetime('now', '-15 minutes')
                    ORDER BY created_at DESC LIMIT 1
                """, (device_type, device_mac))
            else:  # 其他设备 - 至少间隔5分钟
                cursor.execute("""
                    SELECT id FROM onenet_health_data 
                    WHERE device_type = ? AND device_mac = ?
                    AND created_at > datetime('now', '-5 minutes')
                    ORDER BY created_at DESC LIMIT 1
                """, (device_type, device_mac))
            
            recent_record = cursor.fetchone()
            if recent_record:
                print(f"⚠️  短时间内已有记录，跳过插入: 设备{device_type}({device_mac})")
                conn.close()
                return True
            
            # 第三步：如果没有重复或过于频繁，则插入新数据
            cursor.execute(
                "INSERT INTO onenet_health_data (device_type, device_mac, data_json) VALUES (?, ?, ?)",
                (device_type, device_mac, data_json)
            )
            
            # 根据设备类型保存到对应表
            if device_type in [80, 91]:  # 血压计 (80, 91)
                cursor.execute(
                    "INSERT INTO onenet_blood_pressure (systolic, diastolic, pulse, battery, status, device_mac) VALUES (?, ?, ?, ?, ?, ?)",
                    (data.get('systolic', 0), data.get('diastolic', 0), data.get('pulse', 0), data.get('battery', 100), data.get('status', 0), device_mac)
                )
            
            elif device_type == 85:  # 血氧仪
                cursor.execute(
                    "INSERT INTO onenet_blood_oxygen (spo2, pr, prcode, battery, mode, device_mac) VALUES (?, ?, ?, ?, ?, ?)",
                    (data.get('spo2', 0), data.get('pr', 0), data.get('prcode', 0), data.get('battery', 100), data.get('mode', 1), device_mac)
                )
            
            elif device_type == 82:  # 血糖仪
                cursor.execute(
                    "INSERT INTO onenet_glucose (glucose, battery, device_mac) VALUES (?, ?, ?)",
                    (data.get('glucose', '0'), data.get('battery', 100), device_mac)
                )
            
            elif device_type == 93:  # 体温计
                cursor.execute(
                    "INSERT INTO onenet_temperature (temp, code, type, device_mac) VALUES (?, ?, ?, ?)",
                    (data.get('temp', '0'), data.get('code', 0), data.get('type', 0), device_mac)
                )
            
            elif device_type == 81:  # 体脂秤
                cursor.execute(
                    "INSERT INTO onenet_weight (weight, unit, adc, bodyfat, bmi, device_mac) VALUES (?, ?, ?, ?, ?, ?)",
                    (data.get('weight', '0'), data.get('unit', 0), data.get('adc', '0'), data.get('bodyfat', '0'), data.get('bmi', '0'), device_mac)
                )
            
            conn.commit()
            
            # 现在将数据也保存到标准健康数据表中，以便与家庭成员关联
            # 首先尝试识别数据属于哪个家庭成员
            member_id = None
            data_type = None
            value = None
            extra_values = {}
            
            # 根据设备类型确定数据类型和值
            if device_type in [80, 91]:  # 血压计
                data_type = 'blood_pressure'
                value = data.get('systolic', 0)  # 使用收缩压作为主要识别值
                extra_values = {
                    'systolic': data.get('systolic', 0),
                    'diastolic': data.get('diastolic', 0),
                    'pulse': data.get('pulse', 0)
                }
            elif device_type == 85:  # 血氧仪
                data_type = 'blood_oxygen'
                value = data.get('spo2', 0)
                extra_values = {
                    'spo2': data.get('spo2', 0),
                    'pulse': data.get('pr', 0)
                }
            elif device_type == 82:  # 血糖仪
                data_type = 'glucose'
                value = float(data.get('glucose', '0')) if data.get('glucose') else None
            elif device_type == 93:  # 体温计
                data_type = 'body_temperature'
                value = float(data.get('temp', '0')) if data.get('temp') else None
            elif device_type == 81:  # 体脂秤
                data_type = 'weight'
                value = float(data.get('weight', '0')) if data.get('weight') else None
                extra_values = {
                    'weight': data.get('weight', '0'),
                    'bodyfat': data.get('bodyfat', '0'),
                    'bmi': data.get('bmi', '0')
                }
            
            # 如果有有效的数据类型和值，尝试识别家庭成员
            if data_type and value is not None:
                member_id = identify_member_by_health_data(data_type, value, extra_values, device_mac)
            
            # 如果识别到了家庭成员，将数据也保存到标准健康数据表中
            if member_id:
                # 关闭当前连接，建立新连接到 devices.db
                conn.close()
                
                # 连接到 devices.db
                import os
                from pathlib import Path
                DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
                std_conn = sqlite3.connect(str(DB_PATH))
                std_cursor = std_conn.cursor()
                
                try:
                    # 插入到标准健康数据表
                    std_cursor.execute("""
                        INSERT INTO health_data (member_id, device_id, device_name, manufacturer, model, 
                                         data_type, value_main, value_extra, unit, quality, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        member_id,
                        device_mac,  # 使用设备MAC作为设备ID
                        f"OneNet Device Type {device_type}",  # 设备名称
                        "OneNET",  # 制造商
                        f"Type {device_type}",  # 型号
                        data_type,
                        value,
                        json.dumps(extra_values),
                        "" if data_type in ['weight', 'glucose', 'body_temperature'] else "%" if data_type == 'blood_oxygen' else "mmHg" if data_type == 'blood_pressure' else "",
                        "good",
                        f"From OneNET device type {device_type}"
                    ))
                    
                    std_conn.commit()
                    print(f"健康数据已关联到成员ID {member_id}: {data_type} = {value}")
                    
                except Exception as e:
                    print(f"保存标准健康数据失败: {e}")
                finally:
                    std_conn.close()
                
                # 重新建立到 health_data.db 的连接
                conn = sqlite3.connect(str(DATA_DB_PATH))
                cursor = conn.cursor()
            
            return True
        
        except Exception as e:
            print(f"保存健康数据失败: {e}")
            return False
        
        finally:
            conn.close()
    
    async def _fetch_event_log(self, hours: int = 24, limit: int = 50) -> Dict:
        """通过 /device/event-log API 获取设备事件记录
        
        关键点：
            1. API 路径是 /device/event-log（不是 /thingmodel/call-service）
            2. 参数必须包含 start_time 和 end_time（毫秒）
            3. 使用 product_id + device_name（不是 did）
            4. 使用 GET 方法，参数在 URL 中
        """
        token = self._generate_token()
        headers = {
            'Content-Type': 'application/json',
            'Authorization': token,
            'Accept': 'application/json'
        }
        
        # 时间范围（毫秒）
        end_time = int(time.time() * 1000)
        start_time = end_time - hours * 3600000
        
        # API URL - GET 方法，参数在 URL 中
        url = (f"{self.api_url}/device/event-log"
               f"?product_id={self.product_id}"
               f"&device_name={self.device_name}"
               f"&start_time={start_time}"
               f"&end_time={end_time}"
               f"&limit={limit}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    return await resp.json()
        except Exception as e:
            print(f"event-log API 调用失败: {e}")
            return {"error": str(e)}
    
    def _parse_health_events(self, events: list) -> Dict:
        """解析设备事件记录，提取健康数据
        
        设备类型代码（根据 fetch_health.py 实际值）：
            91: 血压计（不是 80）
            87: 体温计（不是 93）
            85: 血氧仪
            81: 体重秤
            82: 血糖仪
        """
        result = {
            "blood_pressure": None,
            "blood_oxygen": None,
            "temperature": None,
            "weight": None,
            "glucose": None
        }
        
        # 设备类型映射（使用实际值）
        device_names = {
            80: "blood_pressure",  # 血压计（文档值）
            91: "blood_pressure",  # 血压计（实际值）
            85: "blood_oxygen",    # 血氧仪
            81: "weight",          # 体重秤
            82: "glucose",         # 血糖仪
            93: "temperature",     # 体温计（文档值）
            87: "temperature",     # 体温计（实际值）
        }
        
        for event in events:
            # 只处理蓝牙子设备事件
            if event.get("identifier") != "ble_subdevice_event":
                continue
            
            event_time = event.get("time", 0)
            value_str = event.get("value", "")
            
            try:
                # 解析事件数据
                event_data = json.loads(value_str)
                event_type = event_data.get("event_type", 0)
                device_type = event_data.get("type", 0)
                mac = event_data.get("mac", "")
                event_value_str = event_data.get("event_value", "")
                
                # event_type=2 表示数据上报
                if event_type != 2:
                    continue
                
                if not event_value_str:
                    continue
                
                # 解析健康数据
                health_data = json.loads(event_value_str)
                
                # 时间戳转换（毫秒 → 秒）
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(event_time / 1000))
                
                # 添加元数据
                health_data["timestamp"] = timestamp
                health_data["mac"] = mac
                
                # 按设备类型存储（只取第一条）
                category = device_names.get(device_type)
                if category and not result[category]:
                    result[category] = health_data
                    # 同时保存到数据库
                    self.save_health_data(device_type, mac, health_data)
            
            except Exception as e:
                print(f"解析事件失败: {e}")
                continue
        
        return result
    
    async def get_latest_vital_signs(self) -> Dict:
        """获取最新生命体征数据
        
        优先从 OneNET 平台实时拉取，失败时回退到本地数据库。
        """
        result = {
            "blood_pressure": None,
            "blood_oxygen": None,
            "temperature": None,
            "weight": None,
            "glucose": None
        }
        
        # 1. 尝试从 OneNET 平台获取最新数据
        try:
            event_result = await self._fetch_event_log(hours=24, limit=50)
            
            if event_result and event_result.get("code") == 0:
                events = event_result.get("data", {}).get("list", [])
                if events:
                    result = self._parse_health_events(events)
                    print(f"从 OneNET 获取到 {len(events)} 条事件，解析结果: "
                          f"{[k for k, v in result.items() if v]}")
                    return result
        except Exception as e:
            print(f"从 OneNET 平台获取数据失败: {e}")
        
        # 2. 回退：从本地数据库读取
        print("回退到本地数据库读取健康数据")
        conn = sqlite3.connect(str(DATA_DB_PATH))
        cursor = conn.cursor()
        
        try:
            # 最新血压
            cursor.execute("SELECT systolic, diastolic, pulse, measurement_time FROM onenet_blood_pressure ORDER BY measurement_time DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                result["blood_pressure"] = {
                    "systolic": row[0],
                    "diastolic": row[1],
                    "pulse": row[2],
                    "timestamp": row[3]
                }
            
            # 最新血氧
            cursor.execute("SELECT spo2, pr, measurement_time FROM onenet_blood_oxygen ORDER BY measurement_time DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                result["blood_oxygen"] = {
                    "spo2": row[0],
                    "pr": row[1],
                    "timestamp": row[2]
                }
            
            # 最新体温
            cursor.execute("SELECT temp, measurement_time FROM onenet_temperature ORDER BY measurement_time DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                result["temperature"] = {
                    "temp": row[0],
                    "timestamp": row[1]
                }
            
            # 最新体重
            cursor.execute("SELECT weight, bodyfat, bmi, measurement_time FROM onenet_weight ORDER BY measurement_time DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                result["weight"] = {
                    "weight": row[0],
                    "bodyfat": row[1],
                    "bmi": row[2],
                    "timestamp": row[3]
                }
            
            # 最新血糖
            cursor.execute("SELECT glucose, measurement_time FROM onenet_glucose ORDER BY measurement_time DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                result["glucose"] = {
                    "glucose": row[0],
                    "timestamp": row[1]
                }
        
        except Exception as e:
            print(f"从数据库获取最新生命体征失败: {e}")
        
        finally:
            # 如果某些指标为空，尝试从基准数据获取
            if not any(result.values()):
                try:
                    cursor.execute("""
                        SELECT data_type, value_main, value_extra
                        FROM health_data WHERE is_baseline = 1
                    """)
                    baseline_rows = cursor.fetchall()
                    now_str = datetime.now().isoformat()
                    for brow in baseline_rows:
                        dtype, vmain, vextra = brow
                        if dtype == 'blood_pressure' and not result.get('blood_pressure') and vmain:
                            extra = json.loads(vextra) if vextra else {}
                            result['blood_pressure'] = {
                                'systolic': vmain, 'diastolic': extra.get('diastolic'), 'pulse': extra.get('pulse'),
                                'timestamp': now_str
                            }
                        elif dtype == 'blood_oxygen' and not result.get('blood_oxygen') and vmain:
                            result['blood_oxygen'] = {
                                'spo2': vmain, 'pr': None,
                                'timestamp': now_str
                            }
                        elif dtype == 'glucose' and not result.get('glucose') and vmain:
                            result['glucose'] = {
                                'glucose': vmain,
                                'timestamp': now_str
                            }
                        if not result["weight"] and weight:
                            result["weight"] = {
                                "weight": weight,
                                "timestamp": datetime.now().isoformat()
                            }
                except Exception as e:
                    print(f"从初始测量获取数据失败: {e}")
            conn.close()
        
        return result
    
    def handle_push_data(self, push_data: Dict) -> bool:
        """处理HTTP推送的健康数据
        
        Args:
            push_data: OneNET平台推送的数据
        
        Returns:
            是否处理成功
        """
        try:
            # 解析推送数据格式
            msg = push_data.get("msg", {})
            message_type = msg.get("messageType", "")
            
            if message_type == "notify":
                notify_type = msg.get("notifyType", "")
                
                if notify_type == "event":
                    # 事件上报
                    data = msg.get("data", {})
                    params = data.get("params", {})
                    
                    # 检查是否是蓝牙子设备事件
                    if "ble_subdevice_event" in params:
                        event_data = params["ble_subdevice_event"].get("value", {})
                        
                        device_mac = event_data.get("mac", "")
                        device_type = event_data.get("type", 0)
                        event_type = event_data.get("event_type", 0)
                        
                        # event_type=2 表示数据上报
                        if event_type == 2:
                            event_value = event_data.get("event_value", "")
                            if event_value:
                                try:
                                    health_data = json.loads(event_value)
                                    self.save_health_data(device_type, device_mac, health_data)
                                    return True
                                except:
                                    pass
            
            return False
        
        except Exception as e:
            print(f"处理推送数据失败: {e}")
            return False


# 全局API实例
onenet_api = None


def get_onenet_api() -> OneNetHealthAPI:
    """获取OneNET API实例"""
    global onenet_api
    if onenet_api is None:
        onenet_api = OneNetHealthAPI()
    return onenet_api


# HTTP处理函数
async def handle_health_data_push(request) -> web.Response:
    """处理健康数据推送请求"""
    try:
        request_data = await request.json()
    except Exception:
        request_data = {}
    
    api = get_onenet_api()
    success = api.handle_push_data(request_data)
    
    response_data = {
        "status": "success" if success else "failed",
        "message": "健康数据已保存" if success else "数据处理失败"
    }
    
    return web.json_response(response_data)


async def get_latest_health_data(request) -> web.Response:
    """获取最新健康数据"""
    # 从查询参数获取成员名称
    member_name = request.query.get('member', None)
    
    api = get_onenet_api()
    
    # 检查是否已配置认证信息
    if not api.product_id or not api.device_name or (not api.access_token and not api.api_key):
        # 如果没有配置认证信息，返回提示信息
        return web.json_response({
            "status": "not_configured",
            "message": "OneNET平台认证信息未配置，请提供产品ID和API密钥",
            "configured_device": api.device_name,
            "instructions": "请在config/onenet_config.yaml中配置产品ID和API密钥"
        })
    
    # 如果指定了成员，从标准健康数据表中获取该成员的数据
    if member_name:
        import sqlite3
        from pathlib import Path
        DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
        
        try:
            conn = sqlite3.connect(str(DB_PATH))
            try:
                cursor = conn.cursor()
            
                # 获取成员ID
                cursor.execute("SELECT id FROM family_members WHERE name = ?", (member_name,))
                member_row = cursor.fetchone()
                if not member_row:
                    conn.close()
                    return web.json_response({"status": "member_not_found", "message": f"成员 {member_name} 不存在"})
            
                member_id = member_row[0]
            
                # 获取该成员的最新健康数据
                result = {}
            
                # 血压
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'blood_pressure'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                bp_row = cursor.fetchone()
                if bp_row:
                    extra = json.loads(bp_row[1]) if bp_row[1] else {}
                    result['blood_pressure'] = {
                        'systolic': extra.get('systolic', bp_row[0]),
                        'diastolic': extra.get('diastolic'),
                        'pulse': extra.get('pulse'),
                        'timestamp': bp_row[2]
                    }
            
                # 血氧
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'blood_oxygen'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                o2_row = cursor.fetchone()
                if o2_row:
                    extra = json.loads(o2_row[1]) if o2_row[1] else {}
                    result['blood_oxygen'] = {
                        'spo2': o2_row[0],
                        'pulse': extra.get('pulse'),
                        'timestamp': o2_row[2]
                    }
            
                # 体重
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'weight'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                wt_row = cursor.fetchone()
                if wt_row:
                    extra = json.loads(wt_row[1]) if wt_row[1] else {}
                    result['weight'] = {
                        'weight': wt_row[0],
                        'bodyfat': extra.get('bodyfat'),
                        'bmi': extra.get('bmi'),
                        'timestamp': wt_row[2]
                    }
            
                # 体温
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'body_temperature'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                temp_row = cursor.fetchone()
                if temp_row:
                    result['temperature'] = {
                        'temp': temp_row[0],
                        'timestamp': temp_row[1]
                    }
            
                # 心率
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'heart_rate'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                hr_row = cursor.fetchone()
                if hr_row:
                    result['heart_rate'] = {
                        'heart_rate': hr_row[0],
                        'timestamp': hr_row[1]
                    }
            
                # 血糖
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'glucose'
                    ORDER BY timestamp DESC LIMIT 1
                """, (member_id,))
                gluc_row = cursor.fetchone()
                if gluc_row:
                    result['glucose'] = {
                        'glucose': gluc_row[0],
                        'timestamp': gluc_row[1]
                    }
            
                # 如果某些指标为空，尝试从基准数据获取
                if not result.get('glucose') or not result.get('blood_oxygen'):
                    cursor.execute("""
                        SELECT data_type, value_main, value_extra
                        FROM health_data WHERE member_id = ? AND is_baseline = 1
                    """, (member_id,))
                    baseline_rows = cursor.fetchall()
                    now_str = datetime.now().isoformat()
                    for brow in baseline_rows:
                        dtype, vmain, vextra = brow
                        if dtype == 'glucose' and not result.get('glucose') and vmain:
                            result['glucose'] = {'glucose': vmain, 'timestamp': now_str}
                        elif dtype == 'blood_oxygen' and not result.get('blood_oxygen') and vmain:
                            result['blood_oxygen'] = {'spo2': vmain, 'pulse': None, 'timestamp': now_str}
                        elif dtype == 'blood_pressure' and not result.get('blood_pressure') and vmain:
                            extra = json.loads(vextra) if vextra else {}
                            result['blood_pressure'] = {'systolic': vmain, 'diastolic': extra.get('diastolic'), 'pulse': extra.get('pulse'), 'timestamp': now_str}
                        elif dtype == 'heart_rate' and not result.get('heart_rate') and vmain:
                            result['heart_rate'] = {'heart_rate': vmain, 'timestamp': now_str}
                        elif dtype == 'weight' and not result.get('weight') and vmain:
                            result['weight'] = {'weight': vmain, 'bodyfat': None, 'bmi': None, 'timestamp': now_str}
            
            finally:
                conn.close()
            return web.json_response(result)
            
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)})
    
    # 如果没有指定成员，返回所有数据
    data = await api.get_latest_vital_signs()
    return web.json_response(data)


async def get_health_history(request) -> web.Response:
    """获取健康数据历史"""
    # 从查询参数获取成员名称和时间范围
    member_name = request.query.get('member', None)
    start_time = request.query.get('start_time', None)
    end_time = request.query.get('end_time', None)
    days = request.query.get('days', 30)  # 默认30天
    
    if start_time:
        start_time = int(start_time)
    if end_time:
        end_time = int(end_time)
    
    # 如果指定了成员，从标准健康数据表中获取该成员的历史数据
    if member_name:
        import sqlite3
        from pathlib import Path
        from datetime import datetime, timedelta
        DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
        
        try:
            conn = sqlite3.connect(str(DB_PATH))
            try:
                cursor = conn.cursor()
            
                # 获取成员ID
                cursor.execute("SELECT id FROM family_members WHERE name = ?", (member_name,))
                member_row = cursor.fetchone()
                if not member_row:
                    conn.close()
                    return web.json_response({"status": "member_not_found", "message": f"成员 {member_name} 不存在"})
            
                member_id = member_row[0]
            
                # 计算时间范围
                if not start_time:
                    start_date = datetime.now() - timedelta(days=int(days))
                    start_time = int(start_date.timestamp() * 1000)
                if not end_time:
                    end_time = int(datetime.now().timestamp() * 1000)
            
                result = {
                    'blood_pressure': [],
                    'blood_oxygen': [],
                    'weight': [],
                    'temperature': [],
                    'heart_rate': [],
                    'glucose': []
                }
            
                # 血压历史
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'blood_pressure'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    extra = json.loads(row[1]) if row[1] else {}
                    result['blood_pressure'].append({
                        'systolic': extra.get('systolic', row[0]),
                        'diastolic': extra.get('diastolic'),
                        'pulse': extra.get('pulse'),
                        'time': row[2]
                    })
            
                # 血氧历史 - 先从 health_data 表读取，如果没有则从 onenet_blood_oxygen 表读取
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'blood_oxygen'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    extra = json.loads(row[1]) if row[1] else {}
                    result['blood_oxygen'].append({
                        'spo2': row[0],
                        'pulse': extra.get('pulse'),
                        'time': row[2]
                    })
            
                # 如果 health_data 表没有血氧数据，从 onenet_blood_oxygen 表读取
                if not result['blood_oxygen']:
                    conn.close()
                    conn = sqlite3.connect(str(DATA_DB_PATH))
                    try:
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT spo2, pr, measurement_time FROM onenet_blood_oxygen
                            WHERE measurement_time >= ?
                            ORDER BY measurement_time DESC LIMIT 100
                        """, (datetime.fromtimestamp(start_time/1000).strftime('%Y-%m-%d %H:%M:%S'),))
                        for row in cursor.fetchall():
                            result['blood_oxygen'].append({
                                'spo2': row[0],
                                'pulse': row[1],
                                'time': row[2]
                            })
                    finally:
                        conn.close()
                    conn = sqlite3.connect(str(DB_PATH))
                    cursor = conn.cursor()
            
                # 体重历史
                cursor.execute("""
                    SELECT value_main, value_extra, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'weight'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    extra = json.loads(row[1]) if row[1] else {}
                    result['weight'].append({
                        'weight': row[0],
                        'bodyfat': extra.get('bodyfat'),
                        'bmi': extra.get('bmi'),
                        'time': row[2]
                    })
            
                # 体温历史
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'body_temperature'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    result['temperature'].append({
                        'temp': row[0],
                        'time': row[1]
                    })
            
                # 心率历史
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'heart_rate'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    result['heart_rate'].append({
                        'heart_rate': row[0],
                        'time': row[1]
                    })
            
                # 血糖历史
                cursor.execute("""
                    SELECT value_main, timestamp FROM health_data
                    WHERE member_id = ? AND data_type = 'glucose'
                    AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT 100
                """, (member_id, datetime.fromtimestamp(start_time/1000).isoformat()))
                for row in cursor.fetchall():
                    result['glucose'].append({
                        'glucose': row[0],
                        'time': row[1]
                    })
            
                # 如果某些指标为空，尝试从基准数据（health_data WHERE is_baseline=1）获取
                if not result['glucose'] or not result['blood_oxygen'] or not result['heart_rate']:
                    cursor.execute("""
                        SELECT data_type, value_main, value_extra
                        FROM health_data WHERE member_id = ? AND is_baseline = 1
                    """, (member_id,))
                    baseline_rows = cursor.fetchall()
                    now_str = datetime.now().isoformat()
                    for brow in baseline_rows:
                        dtype, vmain, vextra = brow
                        if dtype == 'glucose' and not result['glucose'] and vmain:
                            result['glucose'].append({'glucose': vmain, 'time': now_str})
                        elif dtype == 'blood_oxygen' and not result['blood_oxygen'] and vmain:
                            result['blood_oxygen'].append({'spo2': vmain, 'pulse': None, 'time': now_str})
                        elif dtype == 'heart_rate' and not result['heart_rate'] and vmain:
                            result['heart_rate'].append({'heart_rate': vmain, 'time': now_str})
                        elif dtype == 'blood_pressure' and not result['blood_pressure'] and vmain:
                            extra = json.loads(vextra) if vextra else {}
                            result['blood_pressure'].append({'systolic': vmain, 'diastolic': extra.get('diastolic'), 'pulse': None, 'time': now_str})
                        elif dtype == 'weight' and not result['weight'] and vmain:
                            result['weight'].append({'weight': vmain, 'bodyfat': None, 'bmi': None, 'time': now_str})
            
            finally:
                conn.close()
            return web.json_response(result)
            
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)})
    
    # 如果没有指定成员，返回所有数据
    api = get_onenet_api()
    data = await api.get_health_data_history(start_time, end_time)
    return web.json_response(data)


async def sync_from_cloud(request) -> web.Response:
    """从云平台同步健康数据"""
    api = get_onenet_api()
    
    # 获取蓝牙子设备列表
    devices = await api.get_ble_subdevices()
    
    # 获取最新健康数据
    latest = await api.get_latest_vital_signs()
    
    data = {
        "devices": devices,
        "latest_data": latest,
        "sync_time": datetime.now().isoformat()
    }
    
    return web.json_response(data)