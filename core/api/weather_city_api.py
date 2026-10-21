"""
天气城市列表API - 为控制台提供和风天气城市数据
"""

import requests
from aiohttp import web
from pathlib import Path
import yaml
import json
import logging
from config.config_loader import load_yaml_expanded

TAG = __name__
logger = logging.getLogger(TAG)

CONFIG_DIR = Path(__file__).parent.parent.parent / "data"
CONFIG_FILE = CONFIG_DIR / ".config.yaml"

def get_weather_api_config() -> dict:
    """读取天气API配置"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
                config = load_yaml_expanded(f)
                if config and 'plugins' in config and 'get_weather' in config['plugins']:
                    return config['plugins']['get_weather']
    except Exception as e:
        logger.error(f"读取天气配置失败: {e}")
    return {
        'api_host': 'n43aaqvnh7.re.qweatherapi.com',
        'api_key': 'YOUR_QWEATHER_API_KEY'
    }

async def handle_city_lookup(request) -> web.Response:
    """城市搜索 - 支持按名称搜索城市"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '')
        lang = request.query.get('lang', 'zh')
        
        if not location:
            return web.json_response({
                'success': False,
                'error': '请提供搜索关键词',
                'cities': []
            })
        
        # 调用和风天气城市搜索API
        url = f"https://{api_host}/geo/v2/city/lookup?key={api_key}&location={location}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            cities = []
            for loc in data.get('location', []):
                cities.append({
                    'id': loc.get('id', ''),
                    'name': loc.get('name', ''),
                    'adm1': loc.get('adm1', ''),  # 省/直辖市
                    'adm2': loc.get('adm2', ''),  # 地市
                    'country': loc.get('country', ''),
                    'type': loc.get('type', ''),
                    'lat': loc.get('lat', ''),
                    'lon': loc.get('lon', '')
                })
            
            return web.json_response({
                'success': True,
                'cities': cities,
                'count': len(cities)
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'cities': []
            })
            
    except Exception as e:
        logger.error(f"城市搜索失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'cities': []
        })

async def handle_city_list(request) -> web.Response:
    """获取城市列表 - 按省份获取城市"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        province = request.query.get('province', '')
        lang = request.query.get('lang', 'zh')
        
        if not province:
            return web.json_response({
                'success': False,
                'error': '请提供省份名称',
                'cities': []
            })
        
        # 调用和风天气城市搜索API，按省份搜索
        url = f"https://{api_host}/geo/v2/city/lookup?key={api_key}&location={province}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            cities = []
            for loc in data.get('location', []):
                if loc.get('type') == 'city':
                    cities.append({
                        'id': loc.get('id', ''),
                        'name': loc.get('name', ''),
                        'adm1': loc.get('adm1', ''),
                        'adm2': loc.get('adm2', ''),
                        'country': loc.get('country', '')
                    })
            
            return web.json_response({
                'success': True,
                'cities': cities,
                'count': len(cities)
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'cities': []
            })
            
    except Exception as e:
        logger.error(f"获取城市列表失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'cities': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })

async def handle_lifestyle_indices(request) -> web.Response:
    """获取天气生活指数"""
    try:
        config = get_weather_api_config()
        api_key = config.get('api_key', '')
        api_host = config.get('api_host', '')
        
        location = request.query.get('location', '101020100')  # 默认上海
        # 中国生活指数类型：1运动 2洗车 3穿衣 4钓鱼 5紫外线 7过敏 8舒适度 10空气污染
        indices_type = request.query.get('type', '1,2,3,4,5,7,8,10')
        lang = request.query.get('lang', 'zh')
        
        url = f"https://{api_host}/v7/indices/1d?type={indices_type}&location={location}&key={api_key}&lang={lang}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('code') == '200':
            indices = []
            for item in data.get('daily', []):
                indices.append({
                    'type': item.get('type', ''),
                    'name': item.get('name', ''),
                    'level': item.get('level', ''),
                    'category': item.get('category', ''),
                    'text': item.get('text', '')
                })
            
            return web.json_response({
                'success': True,
                'indices': indices,
                'count': len(indices),
                'updateTime': data.get('updateTime', '')
            })
        else:
            return web.json_response({
                'success': False,
                'error': data.get('status', '未知错误'),
                'indices': []
            })
            
    except Exception as e:
        logger.error(f"获取生活指数失败: {e}")
        return web.json_response({
            'success': False,
            'error': str(e),
            'indices': []
        })
