"""
天气API模块 - 直接调用和风天气API获取天气数据
"""
import json
import sqlite3
import requests
import yaml
from pathlib import Path
from aiohttp import web
from config.logger import setup_logging

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / ".config.yaml"

TAG = __name__
logger = setup_logging()

# 天气图标映射
WEATHER_ICONS = {
    "100": "☀️", "101": "⛅", "102": "⛅", "103": "⛅", "104": "☁️",
    "150": "☀️", "151": "⛅", "152": "⛅", "153": "⛅",
    "300": "🌦️", "301": "🌧️", "302": "⛈️", "303": "⛈️", "304": "⛈️",
    "305": "🌧️", "306": "🌧️", "307": "🌧️", "308": "🌧️", "309": "🌧️",
    "310": "⛈️", "311": "⛈️", "312": "⛈️", "313": "🌧️", "314": "🌧️",
    "315": "🌧️", "316": "🌧️", "317": "⛈️", "318": "⛈️",
    "350": "🌦️", "351": "🌧️", "399": "🌧️",
    "400": "🌨️", "401": "🌨️", "402": "❄️", "403": "❄️", "404": "🌨️",
    "405": "🌨️", "406": "🌨️", "407": "❄️", "408": "❄️", "409": "❄️",
    "410": "❄️", "456": "🌨️", "457": "❄️", "499": "❄️",
    "500": "🌫️", "501": "🌫️", "502": "😷", "503": "😷", "504": "😷",
    "507": "😷", "508": "😷", "509": "🌫️", "510": "🌫️", "511": "😷",
    "512": "😷", "513": "😷", "514": "🌫️", "515": "🌫️",
    "900": "🔥", "901": "❄️", "999": "🌈"
}

class WeatherAPI:
    def __init__(self, config=None):
        self.config = config or {}
        self.api_host = None
        self.api_key = None
        self._load_weather_config()
        # 添加天气数据缓存
        self.weather_cache = {}
        self.cache_timeout = 3600  # 1小时缓存
        
    def _is_cache_valid(self, cache_time):
        """检查缓存是否有效"""
        import time
        return time.time() - cache_time < self.cache_timeout
        
    def _load_weather_config(self):
        """从配置文件加载天气API配置"""
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
                    config = load_yaml_expanded(f)
                    if config and 'plugins' in config and 'get_weather' in config['plugins']:
                        weather_config = config['plugins']['get_weather']
                        self.api_host = weather_config.get('api_host', '')
                        self.api_key = weather_config.get('api_key', '')
                        logger.bind(tag=TAG).info(f"天气API配置已加载: host={self.api_host}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载天气配置失败: {e}")
        
        # 如果配置文件中没有，从服务器配置获取
        if not self.api_host:
            weather_config = self.config.get("plugins", {}).get("get_weather", {})
            self.api_host = weather_config.get('api_host', 'n43aaqvnh7.re.qweatherapi.com')
            self.api_key = weather_config.get('api_key', 'YOUR_QWEATHER_API_KEY')
        
    def get_dashboard_config(self, user_id: str) -> dict:
        """获取看板配置 - 优先从 config_storage 读取（与控制台保存位置一致）"""
        conn = None
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            # 首先从 config_storage 读取（新的统一配置表）
            cursor.execute("SELECT value FROM config_storage WHERE key='dashboard'")
            row = cursor.fetchone()
            
            if row:
                config = json.loads(row[0])
                return {"success": True, "config": config}
            
            # 兼容旧版：从 dashboard_config 表读取
            cursor.execute("""
                SELECT config_key, config_value 
                FROM dashboard_config 
                WHERE user_id = ?
            """, (user_id,))
            
            rows = cursor.fetchall()
            
            config = {}
            for row in rows:
                try:
                    value = json.loads(row[1])
                except:
                    value = row[1]
                config[row[0]] = value
            
            return {"success": True, "config": config}
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取看板配置失败: {e}")
            return {"success": False, "message": str(e), "config": {}}
        finally:
            if conn:
                conn.close()

    def _get_location_id(self, location_name: str) -> str:
        """通过城市名称获取LocationID"""
        try:
            url = f"https://{self.api_host}/geo/v2/city/lookup?key={self.api_key}&location={location_name}&lang=zh"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            if data.get('code') == '200' and data.get('location'):
                return data['location'][0].get('id', '101020100')
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取城市ID失败: {e}")
        
        return '101020100'  # 默认上海

    async def get_current_weather(self, request):
        """获取当前天气 + 7天预报 + 生活指数"""
        try:
            # 获取位置
            location = request.query.get('location')
            no_cache = request.query.get('nocache')
            
            if not location:
                config = self.get_dashboard_config('default')
                if config.get('success') and 'weather' in config.get('config', {}):
                    weather_config = config['config']['weather']
                    location = weather_config.get('city', '上海')
            
            if not location:
                location = '上海'
            
            # 检查缓存（nocache参数可跳过缓存）
            cache_key = f"{location}_weather"
            import time
            if not no_cache and cache_key in self.weather_cache:
                cached_data, cache_time = self.weather_cache[cache_key]
                if self._is_cache_valid(cache_time):
                    logger.bind(tag=TAG).info(f"天气数据来自缓存: {location}")
                    return web.json_response({'success': True, 'data': cached_data, 'cached': True})
            
            # 获取城市ID
            location_id = self._get_location_id(location)
            
            # 调用和风天气API
            headers = {"Accept-Encoding": "gzip, deflate"}
            
            # 1. 实时天气
            now_url = f"https://{self.api_host}/v7/weather/now?location={location_id}&key={self.api_key}"
            now_response = requests.get(now_url, headers=headers, timeout=10).json()
            
            # 2. 7天预报
            forecast_url = f"https://{self.api_host}/v7/weather/7d?location={location_id}&key={self.api_key}"
            forecast_response = requests.get(forecast_url, headers=headers, timeout=10).json()
            
            # 3. 生活指数
            indices_url = f"https://{self.api_host}/v7/indices/1d?type=1,2,3,4,5,7,8,10&location={location_id}&key={self.api_key}"
            indices_response = requests.get(indices_url, headers=headers, timeout=10).json()
            
            # 组合数据
            weather_data = {
                'city': location,
                'cityId': location_id,
                'updateTime': now_response.get('updateTime', '')
            }
            
            # 实时天气
            if now_response.get('code') == '200' and now_response.get('now'):
                now = now_response['now']
                weather_data['current'] = {
                    'temp': now.get('temp', '20'),
                    'feelsLike': now.get('feelsLike', '20'),
                    'text': now.get('text', '晴'),
                    'icon': WEATHER_ICONS.get(now.get('icon', '100'), '🌈'),
                    'iconCode': now.get('icon', '100'),
                    'windDir': now.get('windDir', '北风'),
                    'windScale': now.get('windScale', '1'),
                    'windSpeed': now.get('windSpeed', '3'),
                    'humidity': now.get('humidity', '50'),
                    'precip': now.get('precip', '0.0'),
                    'pressure': now.get('pressure', '1010'),
                    'vis': now.get('vis', '10'),
                    'cloud': now.get('cloud', '0')
                }
            
            # 7天预报
            if forecast_response.get('code') == '200' and forecast_response.get('daily'):
                forecast = []
                for day in forecast_response['daily']:
                    forecast.append({
                        'date': day.get('fxDate', ''),
                        'day': self._get_day_name(day.get('fxDate', '')),
                        'tempMax': day.get('tempMax', ''),
                        'tempMin': day.get('tempMin', ''),
                        'textDay': day.get('textDay', ''),
                        'textNight': day.get('textNight', ''),
                        'iconDay': WEATHER_ICONS.get(day.get('iconDay', '100'), '🌈'),
                        'iconNight': WEATHER_ICONS.get(day.get('iconNight', '150'), '🌈'),
                        'humidity': day.get('humidity', ''),
                        'precip': day.get('precip', '0.0'),
                        'uvIndex': day.get('uvIndex', ''),
                        'sunrise': day.get('sunrise', ''),
                        'sunset': day.get('sunset', '')
                    })
                weather_data['forecast'] = forecast
            
            # 生活指数
            if indices_response.get('code') == '200' and indices_response.get('daily'):
                indices = []
                for idx in indices_response['daily']:
                    indices.append({
                        'type': idx.get('type', ''),
                        'name': idx.get('name', ''),
                        'level': idx.get('level', ''),
                        'category': idx.get('category', ''),
                        'text': idx.get('text', '')
                    })
                weather_data['indices'] = indices
            
            # 缓存数据
            self.weather_cache[cache_key] = (weather_data, time.time())
            
            return web.json_response({
                'success': True,
                'data': weather_data,
                'cached': False
            })
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取天气数据失败: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            })

    def _get_day_name(self, date_str: str) -> str:
        """根据日期获取星期名称"""
        from datetime import datetime
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d')
            weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            today = datetime.now().date()
            
            if date.date() == today:
                return '今天'
            elif date.date() == today + timedelta(days=1):
                return '明天'
            else:
                return weekdays[date.weekday()]
        except:
            return date_str

from datetime import timedelta
from config.config_loader import load_yaml_expanded

# 创建全局实例
weather_api = None

def setup_weather_api(config=None):
    global weather_api
    weather_api = WeatherAPI(config)
    return weather_api