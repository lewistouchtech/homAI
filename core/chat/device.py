"""
设备管理模块 — 从 connection.py 拆分

职责：设备自动注册、离线状态更新
"""
import os
import time

TAG = __name__


async def auto_register_device(conn):
    """自动注册设备到 devices.db"""
    if not conn.device_id:
        conn.logger.bind(tag=TAG).warning("无法自动注册：缺少 device_id")
        return

    db_conn = None
    try:
        import sqlite3
        db_path = os.path.join(os.getcwd(), "data", "devices.db")
        if not os.path.exists(db_path):
            conn.logger.bind(tag=TAG).warning(f"devices.db 不存在: {db_path}")
            return

        db_conn = sqlite3.connect(db_path)
        cursor = db_conn.cursor()

        cursor.execute("SELECT * FROM devices WHERE device_id = ?", (conn.device_id,))
        existing = cursor.fetchone()

        now = int(time.time())

        if existing:
            cursor.execute("""
                UPDATE devices SET
                    client_id = ?,
                    status = 'online',
                    last_seen = ?,
                    updated_at = ?
                WHERE device_id = ?
            """, (conn.client_id, now, now, conn.device_id))
            conn.logger.bind(tag=TAG).info(f"设备 {conn.device_id} 已更新为在线状态")
        else:
            cursor.execute("""
                INSERT INTO devices (device_id, client_id, device_model, device_version, status, last_seen)
                VALUES (?, ?, 'ESP32-S3-Box-3', '1.0.0', 'online', ?)
            """, (conn.device_id, conn.client_id, now))
            conn.logger.bind(tag=TAG).info(f"设备 {conn.device_id} 已自动注册")

        db_conn.commit()

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"自动注册设备失败: {e}")
    finally:
        if db_conn:
            db_conn.close()


async def update_device_offline(conn):
    """更新设备状态为 offline"""
    if not conn.device_id:
        return

    db_conn = None
    try:
        import sqlite3
        db_path = os.path.join(os.getcwd(), "data", "devices.db")
        if not os.path.exists(db_path):
            return

        db_conn = sqlite3.connect(db_path)
        cursor = db_conn.cursor()

        now = int(time.time())

        cursor.execute("""
            UPDATE devices SET
                status = 'offline',
                last_seen = ?,
                updated_at = ?
            WHERE device_id = ?
        """, (now, now, conn.device_id))

        db_conn.commit()

        conn.logger.bind(tag=TAG).info(f"设备 {conn.device_id} 已更新为离线状态")

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"更新设备离线状态失败: {e}")
    finally:
        if db_conn:
            db_conn.close()
