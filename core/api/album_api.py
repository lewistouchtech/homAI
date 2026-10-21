"""
家庭相册API模块 - 为前端提供照片数据
"""
import os
import json
import sqlite3
import shutil
from aiohttp import web
from config.logger import setup_logging
import mimetypes
from pathlib import Path
from datetime import datetime

TAG = __name__
logger = setup_logging()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
ALBUM_DIR = str(Path.home() / "homebrain" / "photos")

class AlbumAPI:
    def __init__(self, config=None):
        self.config = config or {}

    def _get_album_path_from_db(self):
        """从数据库读取相册路径配置 - 优先从 dashboard_config 读取(控制台保存的位置),兼容 config_storage 表"""
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # 首先从 dashboard_config 读取(控制台保存的位置)
            cursor.execute("SELECT config_value FROM dashboard_config WHERE user_id='default' AND config_key='album'")
            row = cursor.fetchone()

            if row:
                album_config = json.loads(row[0])
                album_path = album_config.get('path', '')
                if album_path:
                    logger.bind(tag=TAG).info(f"从 dashboard_config 读取相册路径: {album_path}")
                    return album_path

            # 兼容旧版:从 config_storage 表读取
            cursor.execute("SELECT value FROM config_storage WHERE key='dashboard'")
            row = cursor.fetchone()

            if row:
                config = json.loads(row[0])
                album_config = config.get('album', {})
                album_path = album_config.get('path', '')
                logger.bind(tag=TAG).info(f"从 config_storage 读取相册路径: {album_path}")
                return album_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"从数据库读取相册配置失败: {e}")
        finally:
            if conn:
                conn.close()

        return ''

    async def get_album_photos(self, request):
        """获取相册照片列表 - 始终从固定目录读取"""
        try:
            album_path = ALBUM_DIR
            os.makedirs(album_path, exist_ok=True)

            supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.mp4', '.mov', '.avi', '.mkv', '.webm'}

            photos = []
            for filename in os.listdir(album_path):
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext in supported_formats:
                    filepath = os.path.join(album_path, filename)
                    if os.path.isfile(filepath):
                        stat = os.stat(filepath)
                        photo_info = {
                            'name': filename,
                            'url': f'/api/album/photo?path={filepath}',
                            'size': stat.st_size,
                            'modified': int(stat.st_mtime * 1000),
                            'path': filepath
                        }
                        photos.append(photo_info)

            photos.sort(key=lambda x: x['modified'], reverse=True)

            return web.json_response({
                'success': True,
                'photos': photos,
                'path': album_path,
                'count': len(photos)
            })

        except Exception as e:
            logger.bind(tag=TAG).error(f"获取相册照片失败: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            })

    async def get_photo_by_path(self, request):
        """
        根据路径获取单张照片
        """
        try:
            photo_path = request.query.get('path')

            if not photo_path:
                return web.json_response({'success': False, 'error': '缺少照片路径'}, status=400)

            # 安全检查
            photo_path = os.path.abspath(photo_path)

            if not os.path.exists(photo_path) or not os.path.isfile(photo_path):
                return web.json_response({'success': False, 'error': '照片不存在'}, status=404)

            # 检查文件扩展名
            _, ext = os.path.splitext(photo_path.lower())
            supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.mp4', '.mov', '.avi', '.mkv', '.webm'}

            if ext not in supported_formats:
                return web.json_response({'success': False, 'error': '不支持的文件格式'}, status=400)

            # 读取并返回图片
            with open(photo_path, 'rb') as f:
                image_data = f.read()

            mime_type, _ = mimetypes.guess_type(photo_path)
            if not mime_type:
                mime_type = 'application/octet-stream'

            return web.Response(
                body=image_data,
                content_type=mime_type,
                headers={'Cache-Control': 'public, max-age=3600'}
            )

        except Exception as e:
            logger.bind(tag=TAG).error(f"获取照片失败: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)

    async def upload_photos(self, request):
        """接收上传的照片，复制到固定相册目录"""
        try:
            reader = await request.multipart()
            uploaded = []
            os.makedirs(ALBUM_DIR, exist_ok=True)
            
            while True:
                field = await reader.next()
                if field is None:
                    break
                if field.name == 'photos':
                    filename = field.filename
                    if not filename:
                        continue
                    # 保留原文件名，重名加时间戳
                    ext = os.path.splitext(filename)[1].lower()
                    supported = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.mp4', '.mov', '.avi', '.mkv', '.webm'}
                    if ext not in supported:
                        continue
                    dest = os.path.join(ALBUM_DIR, filename)
                    if os.path.exists(dest):
                        base, e = os.path.splitext(filename)
                        dest = os.path.join(ALBUM_DIR, f"{base}_{int(datetime.now().timestamp())}{e}")
                    with open(dest, 'wb') as f:
                        while True:
                            chunk = await field.read_chunk()
                            if not chunk:
                                break
                            f.write(chunk)
                    uploaded.append(os.path.basename(dest))
                    logger.bind(tag=TAG).info(f"上传照片: {os.path.basename(dest)}")
            
            return web.json_response({
                'success': True,
                'uploaded': uploaded,
                'count': len(uploaded)
            })
        except Exception as e:
            logger.bind(tag=TAG).error(f"上传照片失败: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)

    async def delete_photo(self, request):
        """删除固定目录中的照片"""
        try:
            data = await request.json()
            filename = data.get('filename')
            if not filename:
                return web.json_response({'success': False, 'error': '缺少文件名'}, status=400)
            # 防止路径穿越
            filepath = os.path.join(ALBUM_DIR, os.path.basename(filename))
            if not os.path.exists(filepath):
                return web.json_response({'success': False, 'error': '文件不存在'}, status=404)
            os.remove(filepath)
            logger.bind(tag=TAG).info(f"删除照片: {filename}")
            return web.json_response({'success': True})
        except Exception as e:
            logger.bind(tag=TAG).error(f"删除照片失败: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)

# 创建全局实例
album_api = None

def setup_album_api(config=None):
    global album_api
    album_api = AlbumAPI(config)
    return album_api
