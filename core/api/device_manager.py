import json
import time
import sqlite3
import os
from typing import Dict, List, Optional
from aiohttp import web
from datetime import datetime

from core.api.base_handler import BaseHandler
from core.utils.place_manager import PLACES_TABLE_DDL

TAG = __name__


class DeviceManager(BaseHandler):
    """设备管理 API
    
    功能：
    1. 设备注册：设备首次连接时自动注册
    2. 设备列表：查看所有已注册设备
    3. 设备配置：修改设备 OTA 地址、WebSocket 地址等
    4. 设备状态：查看设备在线状态、最后连接时间
    5. 固件管理：上传/删除固件文件
    """
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.db_path = os.path.join(os.getcwd(), "data", "devices.db")
        self._init_database()
        
        # 固件存储目录
        self.bin_dir = os.path.join(os.getcwd(), "data", "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        
        # 配置回退地址（当本地服务器不可用时）
        self.fallback_url = config.get("fallback_server", "")
    
    def _init_database(self):
        """初始化 SQLite 数据库"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
        
            # 设备表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT UNIQUE NOT NULL,
                    client_id TEXT,
                    device_model TEXT DEFAULT 'default',
                    device_version TEXT DEFAULT '0.0.0',
                    ota_url TEXT,
                    websocket_url TEXT,
                    fallback_url TEXT,
                    status TEXT DEFAULT 'offline',
                    last_seen INTEGER,
                    created_at INTEGER DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                    prompt TEXT,
                    room_id INTEGER
                )
            """)
        
            # 配置历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    config_type TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    changed_at INTEGER DEFAULT (strftime('%s', 'now')),
                    changed_by TEXT DEFAULT 'system'
                )
            """)

            # 兼容旧数据库：添加缺失的字段
            columns_to_add = [
                ("prompt", "TEXT"),
                ("room_id", "INTEGER"),
            ]
            cursor.execute("PRAGMA table_info(devices)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            for col_name, col_type in columns_to_add:
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE devices ADD COLUMN {col_name} {col_type}")
                    self.logger.bind(tag=TAG).info(f"数据库迁移：添加字段 {col_name} {col_type}")

            # 系统配置表（音乐配置等使用）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    category TEXT DEFAULT 'general',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 配置存储表（天气配置、相册配置等使用）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config_storage (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    category TEXT DEFAULT 'general',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 家庭成员表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS family_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    relation TEXT DEFAULT '',
                    avatar TEXT DEFAULT '',
                    age INTEGER DEFAULT 0,
                    gender TEXT DEFAULT '',
                    birthday TEXT DEFAULT '',
                    height REAL DEFAULT 0.0,
                    weight REAL DEFAULT 0.0,
                    occupation TEXT DEFAULT '',
                    position TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    health_enabled BOOLEAN DEFAULT 0,
                    health_status TEXT DEFAULT 'normal',
                    phone TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    voice_print_vector BLOB
                )
            """)

            # 兼容旧库：家庭成员表补 nickname 字段（主人要求的昵称称呼）
            cursor.execute("PRAGMA table_info(family_members)")
            fam_cols = {row[1] for row in cursor.fetchall()}
            if "nickname" not in fam_cols:
                cursor.execute("ALTER TABLE family_members ADD COLUMN nickname TEXT DEFAULT ''")
                self.logger.bind(tag=TAG).info("数据库迁移：family_members 添加字段 nickname TEXT")

            # [2026-10-05] 兼容旧库：补 life_stage / relation_detail
            #   life_stage      人生阶段，驱动「我上学了/我上班了」的话术与提醒过滤
            #   relation_detail 血缘路径（自由文本），解决「小花朵的爷爷」这类第三人称歧义
            for col, ddl in (
                ("life_stage", "TEXT DEFAULT ''"),
                ("relation_detail", "TEXT DEFAULT ''"),
            ):
                if col not in fam_cols:
                    cursor.execute(f"ALTER TABLE family_members ADD COLUMN {col} {ddl}")
                    self.logger.bind(tag=TAG).info(f"数据库迁移：family_members 添加字段 {col} TEXT")

            # [2026-10-05] 地点簿：家庭成员的常去地点（公司/学校/亲友家…）
            # 「家」由 houses 表首条迁移而来（is_primary=1，全局唯一）
            cursor.executescript(PLACES_TABLE_DDL)

            # 兼容旧库：houses 表首条还没有同步进 places 时自动补一条。
            # ⚠️ 必须先探测 houses 表有哪些列 —— 不同版本的 houses 表列不一样
            # （早期只有 id/name/address/lng/lat，省/市/区县是后加的）。
            # 直接 SELECT province 会让整个服务启动失败，必须逐列探测。
            cursor.execute("SELECT id FROM places WHERE is_primary=1 LIMIT 1")
            if cursor.fetchone() is None:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='houses'"
                )
                if cursor.fetchone() is not None:
                    cursor.execute("PRAGMA table_info(houses)")
                    house_cols = {r[1] for r in cursor.fetchall()}
                    wanted = ["id", "name", "address", "lng", "lat",
                              "province", "city", "district", "adcode"]
                    picked = [c for c in wanted if c in house_cols]
                    cursor.execute(
                        f"SELECT {', '.join(picked)} FROM houses ORDER BY id LIMIT 1"
                    )
                    home_row = cursor.fetchone()
                    if home_row:
                        h = dict(zip(picked, home_row))
                        cursor.execute(
                            "INSERT INTO places (name, type, member_id, address, lng, lat, "
                            "province, city, district, adcode, aliases, is_primary, notes) "
                            "VALUES (?, 'home', NULL, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (
                                h.get("name") or "家",
                                h.get("address") or "",
                                h.get("lng"),
                                h.get("lat"),
                                h.get("province") or "",
                                h.get("city") or "",
                                h.get("district") or "",
                                h.get("adcode") or "",
                                "家,我家,家里,回家,到家,住的地方",
                                f"由 houses 表 id={h.get('id')} 自动迁移（2026-10-05）",
                            ),
                        )
                        self.logger.bind(tag=TAG).info(
                            f"数据库迁移：houses(id={h.get('id')}) 已同步到 places 的 is_primary 记录"
                        )

            # 健康数据表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS health_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER NOT NULL,
                    device_id TEXT DEFAULT '',
                    device_name TEXT DEFAULT '',
                    manufacturer TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    data_type TEXT NOT NULL,
                    value_main TEXT DEFAULT '',
                    value_extra TEXT DEFAULT '',
                    unit TEXT DEFAULT '',
                    quality TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    is_baseline BOOLEAN DEFAULT 0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (member_id) REFERENCES family_members(id)
                )
            """)

            # 健康设备绑定表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS health_device_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    binding_type TEXT DEFAULT 'primary',
                    auto_associate BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (member_id) REFERENCES family_members(id),
                    UNIQUE(member_id, device_id)
                )
            """)

            # 如果家庭成员表为空，创建默认成员
            cursor.execute("SELECT COUNT(*) FROM family_members")
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    INSERT INTO family_members (name, relation, avatar, age, gender, health_enabled, health_status)
                    VALUES ('主人', '户主', '👤', 30, '', 1, 'normal')
                """)

            # 健康设备表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS health_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER,
                    device_name TEXT NOT NULL,
                    device_type TEXT DEFAULT '',
                    manufacturer TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    mac_address TEXT DEFAULT '',
                    status TEXT DEFAULT 'offline',
                    last_seen TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (member_id) REFERENCES family_members(id)
                )
            """)

            conn.commit()
        finally:
            conn.close()
        self.logger.bind(tag=TAG).info(f"设备管理数据库初始化完成：{self.db_path}")
    
    def _get_db_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    async def handle_list(self, request):
        """获取设备列表
        
        GET /api/devices
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM devices ORDER BY last_seen DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            
            devices = []
            # ⚠️ devices.status 会残留 'online'（异常中断时 update_device_offline
            # 不会被调用）→ 主控台「通道状态」「AI 终端设备」会显示「10/31 台设备在线」。
            # 以服务端内存里的活跃 WebSocket 连接为准（主人 2026-10-07 实测纠正）。
            from core.api.config_api import get_live_device_ids
            live_ids = get_live_device_ids()
            for row in rows:
                did = row["device_id"]
                live_status = ("online" if (live_ids and did in live_ids)
                               else "offline")
                devices.append({
                    "id": row["id"],
                    "device_id": did,
                    "client_id": row["client_id"],
                    "device_model": row["device_model"],
                    "device_version": row["device_version"],
                    "ota_url": row["ota_url"],
                    "websocket_url": row["websocket_url"],
                    "fallback_url": row["fallback_url"],
                    "status": live_status,          # 真实在线状态（内存口径）
                    "db_status": row["status"],     # 库里的原始值，备查
                    "last_seen": row["last_seen"],
                    "last_seen_str": datetime.fromtimestamp(row["last_seen"]).strftime("%Y-%m-%d %H:%M:%S") if row["last_seen"] else "从未连接",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "prompt": row["prompt"] if "prompt" in row.keys() else None,
                    "room_id": row["room_id"] if "room_id" in row.keys() else None,
                })
            
            response = web.Response(
                text=json.dumps({"success": True, "devices": devices}, indent=2),
                content_type="application/json"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取设备列表失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_get(self, request):
        """获取单个设备详情
        
        GET /api/devices/{device_id}
        """
        try:
            device_id = request.match_info.get("device_id", "")
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                raise web.HTTPNotFound(text="Device not found")

            # status 以内存活跃连接为准（同 handle_list）
            from core.api.config_api import get_live_device_ids
            live_ids = get_live_device_ids()
            device = {
                "id": row["id"],
                "device_id": row["device_id"],
                "client_id": row["client_id"],
                "device_model": row["device_model"],
                "device_version": row["device_version"],
                "ota_url": row["ota_url"],
                "websocket_url": row["websocket_url"],
                "fallback_url": row["fallback_url"],
                "status": ("online" if (live_ids and row["device_id"] in live_ids)
                           else "offline"),
                "db_status": row["status"],
                "last_seen": row["last_seen"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "prompt": row["prompt"] if "prompt" in row.keys() else None,
                "room_id": row["room_id"] if "room_id" in row.keys() else None,
            }
            
            response = web.Response(
                text=json.dumps({"success": True, "device": device}, indent=2),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取设备详情失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_register(self, request):
        """设备注册/更新
        
        POST /api/devices/register
        """
        try:
            data = await request.json()
            device_id = data.get("device_id", "")
            client_id = data.get("client_id", "")
            device_model = data.get("device_model", "default")
            device_version = data.get("device_version", "0.0.0")
            
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # 检查设备是否已存在
            cursor.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
            existing = cursor.fetchone()
            
            now = int(time.time())
            
            if existing:
                # 更新现有设备
                cursor.execute("""
                    UPDATE devices SET
                        client_id = ?,
                        device_model = ?,
                        device_version = ?,
                        status = 'online',
                        last_seen = ?,
                        updated_at = ?
                    WHERE device_id = ?
                """, (client_id, device_model, device_version, now, now, device_id))
                self.logger.bind(tag=TAG).info(f"设备 {device_id} 已更新")
            else:
                # 注册新设备
                cursor.execute("""
                    INSERT INTO devices (device_id, client_id, device_model, device_version, status, last_seen)
                    VALUES (?, ?, ?, ?, 'online', ?)
                """, (device_id, client_id, device_model, device_version, now))
                self.logger.bind(tag=TAG).info(f"设备 {device_id} 已注册")
            
            conn.commit()
            conn.close()
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "Device registered"}),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"设备注册失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_update_config(self, request):
        """更新设备配置
        
        PUT /api/devices/{device_id}/config
        Body: {"ota_url": "...", "websocket_url": "...", "fallback_url": "..."}
        """
        try:
            device_id = request.match_info.get("device_id", "")
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")
            
            data = await request.json()
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            
            # 检查设备是否存在
            cursor.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
            existing = cursor.fetchone()
            if not existing:
                conn.close()
                raise web.HTTPNotFound(text="Device not found")
            
            # 构建更新字段
            updates = []
            params = []
            
            if "ota_url" in data:
                updates.append("ota_url = ?")
                params.append(data["ota_url"])
                # 记录配置历史
                cursor.execute("""
                    INSERT INTO config_history (device_id, config_type, old_value, new_value)
                    VALUES (?, 'ota_url', ?, ?)
                """, (device_id, existing["ota_url"], data["ota_url"]))
            
            if "websocket_url" in data:
                updates.append("websocket_url = ?")
                params.append(data["websocket_url"])

            if "fallback_url" in data:
                updates.append("fallback_url = ?")
                params.append(data["fallback_url"])

            # 设备专属配置字段
            if "prompt" in data:
                updates.append("prompt = ?")
                params.append(data["prompt"])

            if "device_model" in data:
                updates.append("device_model = ?")
                params.append(data["device_model"])

            if "device_version" in data:
                updates.append("device_version = ?")
                params.append(data["device_version"])
                cursor.execute("""
                    INSERT INTO config_history (device_id, config_type, old_value, new_value)
                    VALUES (?, 'websocket_url', ?, ?)
                """, (device_id, existing["websocket_url"], data["websocket_url"]))
            
            if "fallback_url" in data:
                updates.append("fallback_url = ?")
                params.append(data["fallback_url"])
                cursor.execute("""
                    INSERT INTO config_history (device_id, config_type, old_value, new_value)
                    VALUES (?, 'fallback_url', ?, ?)
                """, (device_id, existing["fallback_url"], data["fallback_url"]))
            
            if updates:
                params.append(int(time.time()))
                params.append(device_id)
                cursor.execute(f"""
                    UPDATE devices SET
                        {", ".join(updates)},
                        updated_at = ?
                    WHERE device_id = ?
                """, params)
                conn.commit()
                self.logger.bind(tag=TAG).info(f"设备 {device_id} 配置已更新")
            
            conn.close()
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "Config updated"}),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新设备配置失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_assign_room(self, request):
        """分配设备到房间

        PUT /api/devices/{device_id}/room
        {"room_id": 1}
        """
        try:
            device_id = request.match_info.get("device_id", "")
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")

            data = await request.json()
            room_id = data.get("room_id")
            if room_id is None:
                raise web.HTTPBadRequest(text="room_id required")

            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT id FROM devices WHERE device_id = ?", (device_id,))
            if not cursor.fetchone():
                conn.close()
                raise web.HTTPNotFound(text="Device not found")

            cursor.execute(
                "UPDATE devices SET room_id = ?, updated_at = ? WHERE device_id = ?",
                (room_id, int(time.time()), device_id)
            )
            conn.commit()
            conn.close()

            response = web.Response(
                text=json.dumps({"success": True, "message": "房间分配成功"}),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"分配房间失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_delete(self, request):
        """删除设备
        
        DELETE /api/devices/{device_id}
        """
        try:
            device_id = request.match_info.get("device_id", "")
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            
            if deleted == 0:
                raise web.HTTPNotFound(text="Device not found")
            
            self.logger.bind(tag=TAG).info(f"设备 {device_id} 已删除")
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "Device deleted"}),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"删除设备失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_firmware_list(self, request):
        """获取固件列表
        
        GET /api/firmware
        """
        try:
            import glob
            import re
            
            files_by_model = {}
            pattern = os.path.join(self.bin_dir, "*.bin")
            
            for path in glob.glob(pattern):
                fname = os.path.basename(path)
                m = re.match(r"^(.+?)_([0-9][A-Za-z0-9\.\-_]*)\.bin$", fname)
                if m:
                    model = m.group(1)
                    version = m.group(2)
                    size = os.path.getsize(path)
                    files_by_model.setdefault(model, []).append({
                        "filename": fname,
                        "version": version,
                        "size": size,
                        "size_mb": round(size / 1024 / 1024, 2)
                    })
            
            # 按版本降序排序
            for model in files_by_model:
                files_by_model[model].sort(key=lambda x: x["version"], reverse=True)
            
            response = web.Response(
                text=json.dumps({"success": True, "firmware": files_by_model}, indent=2),
                content_type="application/json"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取固件列表失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_firmware_upload(self, request):
        """上传固件
        
        POST /api/firmware/upload
        Content-Type: multipart/form-data
        """
        try:
            reader = await request.multipart()
            field = await reader.next()
            
            if not field or field.name != "file":
                raise web.HTTPBadRequest(text="file field required")
            
            filename = field.filename
            if not filename.endswith(".bin"):
                raise web.HTTPBadRequest(text="Only .bin files allowed")
            
            # 保存文件
            file_path = os.path.join(self.bin_dir, filename)
            with open(file_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            
            file_size = os.path.getsize(file_path)
            self.logger.bind(tag=TAG).info(f"固件 {filename} 上传成功 ({file_size} bytes)")
            
            response = web.Response(
                text=json.dumps({
                    "success": True,
                    "message": "Firmware uploaded",
                    "filename": filename,
                    "size": file_size,
                    "size_mb": round(file_size / 1024 / 1024, 2)
                }),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"固件上传失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_firmware_delete(self, request):
        """删除固件
        
        DELETE /api/firmware/{filename}
        """
        try:
            filename = request.match_info.get("filename", "")
            if not filename:
                raise web.HTTPBadRequest(text="filename required")
            
            file_path = os.path.join(self.bin_dir, filename)
            if not os.path.isfile(file_path):
                raise web.HTTPNotFound(text="File not found")
            
            # 安全检查：确保文件在 bin 目录内
            if not os.path.realpath(file_path).startswith(os.path.realpath(self.bin_dir)):
                raise web.HTTPForbidden(text="Forbidden")
            
            os.remove(file_path)
            self.logger.bind(tag=TAG).info(f"固件 {filename} 已删除")
            
            response = web.Response(
                text=json.dumps({"success": True, "message": "Firmware deleted"}),
                content_type="application/json"
            )
        except web.HTTPError as e:
            response = e
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"删除固件失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
    
    async def handle_config_history(self, request):
        """获取配置历史
        
        GET /api/devices/{device_id}/history
        """
        try:
            device_id = request.match_info.get("device_id", "")
            if not device_id:
                raise web.HTTPBadRequest(text="device_id required")
            
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM config_history 
                WHERE device_id = ? 
                ORDER BY changed_at DESC 
                LIMIT 50
            """, (device_id,))
            rows = cursor.fetchall()
            conn.close()
            
            history = []
            for row in rows:
                history.append({
                    "id": row["id"],
                    "device_id": row["device_id"],
                    "config_type": row["config_type"],
                    "old_value": row["old_value"],
                    "new_value": row["new_value"],
                    "changed_at": row["changed_at"],
                    "changed_at_str": datetime.fromtimestamp(row["changed_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "changed_by": row["changed_by"]
                })
            
            response = web.Response(
                text=json.dumps({"success": True, "history": history}, indent=2),
                content_type="application/json"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取配置历史失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_discover(self, request):
        """小智设备发现API - 返回已注册设备列表
        
        GET /api/devices/discover
        
        注意：小智使用6位数字配对码流程，不是UDP广播发现
        1. ESP32设备联网后显示6位数字
        2. 用户在控制台输入6位数字
        3. 系统自动完成配对和注册
        """
        try:
            # 小智的发现逻辑：返回已注册设备列表
            # 实际配对通过6位数字码完成
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM devices ORDER BY last_seen DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            
            devices = []
            # 同 handle_list：status 以服务端内存活跃连接为准，避免残留 'online'
            from core.api.config_api import get_live_device_ids
            live_ids = get_live_device_ids()
            for row in rows:
                devices.append({
                    "device_id": row["device_id"],
                    "device_model": row["device_model"],
                    "device_version": row["device_version"],
                    "status": ("online" if (live_ids and row["device_id"] in live_ids)
                               else "offline"),
                    "last_seen": row["last_seen"],
                    "last_seen_str": datetime.fromtimestamp(row["last_seen"]).strftime("%Y-%m-%d %H:%M:%S") if row["last_seen"] else "从未连接"
                })
            
            response = web.Response(
                text=json.dumps({
                    "success": True, 
                    "devices": devices,
                    "count": len(devices),
                    "note": "小智使用6位数字配对码流程。ESP32设备联网后显示6位数字，在控制台输入即可完成配对。"
                }, indent=2),
                content_type="application/json"
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取设备列表失败：{e}")
            response = web.Response(
                text=json.dumps({"success": False, "message": str(e)}),
                content_type="application/json",
                status=500
            )
        finally:
            self._add_cors_headers(response)
            return response
