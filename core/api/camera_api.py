"""
摄像头配置API
支持摄像头流地址的服务器端持久化存储
"""
import json
import os
import signal
import sqlite3
import asyncio
import subprocess
from pathlib import Path
from aiohttp import web
from config.logger import setup_logging

def _ffmpeg_bin() -> str:
    """定位 ffmpeg 可执行文件（PATH 无关）。
    生产服务由 launchd/nohup 启动时 PATH 常不含 /opt/homebrew/bin，
    裸命令 'ffmpeg' 会找不到导致摄像头流/抓图全部失败。"""
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).exists():
            return cand
    return "ffmpeg"


DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
logger = setup_logging()
TAG = "camera_api"

# 【临时隔离开关 2026-09-29】诊断唤醒词/ASR 期间临时断开摄像头流循环。
# 根因：流结束时 finally 里同步 process.wait(timeout=3) 阻塞事件循环，
#       页面左栏 5 秒轮播 + 嵌入窗 2 秒断线重连，每轮触发一次 → 音频/ASR 停摆。
# 【2026-10-01 恢复】唤醒词诊断已结束（标点 bug 已修、判停走 ollaya）；
# stream 读取改 Popen+to_thread，snapshot 改 to_thread，
# 且 finally 的进程清理也整体挪进线程（见 _terminate_ffmpeg），
# 事件循环已不再被摄像头占用。此开关仅保留为紧急止血手段，默认常开 False。
# 摄像头模块职责：只做取流与显示，不参与任何音频/唤醒词逻辑。
CAMERA_ISOLATED = False


def _terminate_ffmpeg(process, timeout: float = 3.0) -> None:
    """结束 ffmpeg 子进程：terminate → wait → kill 兜底。

    必须放到线程里执行（asyncio.to_thread）。原因：在异步上下文直接调用
    process.wait() 会同步阻塞事件循环最多 timeout 秒；摄像头流因客户端
    断连而进入 finally 是高频动作（页面轮播/断线重连），叠加后会让
    音频采集与 ASR 反复停摆。此函数只做进程清理，不含任何 I/O 阻塞业务。
    """
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        pass


# ┌─────────────────────────────────────────────────────────────────┐
# │ [2026-10-13 主人要求修] ffmpeg 僵尸进程清理                       │
# ├─────────────────────────────────────────────────────────────────┤
# │ 实测抓到：PID 65598 已运行 3 天 10 小时、CPU 0.0%、仍在拉        │
# │ rtsp://…@YOUR_CAMERA_HOST:554/… —— 连接早死、进程没退的僵尸。      │
# │ 与 Frigate 无关：本文件自己直接拉 RTSP 转 MJPEG，Frigate 是       │
# │ plugins.describe_camera_event.frigate_api 那条路（15000）。       │
# │                                                                   │
# │ 两处根因：                                                        │
# │  ① 读流无超时：process.stdout.read 会无限阻塞，上游断了而 ffmpeg  │
# │     不退出时 while True 出不来 → finally 永远执行不到。           │
# │  ② finally 里的清理被 await：客户端断开 → aiohttp 取消 handler，  │
# │     若清理任务还排在线程池队列里没开始跑，future.cancel() 会成功，│
# │     任务被直接丢弃 → 没有任何人去杀 ffmpeg。                      │
# │     （旧注释断言「to_thread 被取消时线程仍会跑完」不成立）        │
# └─────────────────────────────────────────────────────────────────┘
# 单次读流的上限。上游 RTSP 断掉而 ffmpeg 不退时，read 会永久卡住，
# 必须靠这个超时把 while 循环顶出来，才轮得到 finally 去杀进程。
_STREAM_READ_TIMEOUT = 10.0

# 判定「僵尸」的最短运行时长（分钟）。正常拉流会话远短于此；
# 刻意留足余量，避免误杀刚起来正在干活的重负载进程。
_STALE_FFMPEG_MINUTES = 10


def _reap_stale_ffmpeg() -> int:
    """扫一遍本机的 ffmpeg，把「跑太久且不干活」的僵尸杀掉。

    判据（三条同时满足才杀，宁可放过不可误杀）：
      ① 命令行含 ffmpeg；
      ② CPU 占用 <= 0.5%（真在转码的进程 CPU 是 20~30%，实测 27.2%）；
      ③ 已运行 >= _STALE_FFMPEG_MINUTES 分钟。

    Returns:
        实际发出 SIGTERM 的进程数。任何异常都吞掉并返回 0 ——
        这是清理动作，绝不能因为它失败而让开流请求挂掉。
    """
    killed = 0
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,etime=,%cpu=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[ffmpeg巡检] ps 执行失败，跳过: {e}")
        return 0

    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, etime, cpu_s, cmd = parts
        if "ffmpeg" not in cmd:
            continue
        try:
            pid = int(pid_s)
            cpu = float(cpu_s)
        except ValueError:
            continue
        if cpu > 0.5:
            continue  # 还在干活，放过

        # etime 三种形态：MM:SS / HH:MM:SS / D-HH:MM:SS
        try:
            days = 0
            body = etime
            if "-" in body:
                d, body = body.split("-", 1)
                days = int(d)
            segs = [int(x) for x in body.split(":")]
            while len(segs) < 3:
                segs.insert(0, 0)
            hh, mm, _ss = segs[-3], segs[-2], segs[-1]
            total_min = days * 1440 + hh * 60 + mm
        except (ValueError, IndexError):
            continue
        if total_min < _STALE_FFMPEG_MINUTES:
            continue  # 刚起来的，放过

        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            logger.bind(tag=TAG).warning(
                f"[ffmpeg巡检] 清理僵尸 ffmpeg pid={pid}"
                f"（已运行 {etime}，CPU {cpu}%）"
            )
        except Exception as e:
            logger.bind(tag=TAG).debug(f"[ffmpeg巡检] 杀 pid={pid} 失败: {e}")
    return killed


def _isolated_response():
    return web.json_response(
        {"success": False, "error": "camera temporarily isolated for audio debugging"},
        status=503,
    )


def init_camera_table():
    """初始化摄像头表"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                type TEXT DEFAULT 'rtsp',
                room_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 旧库迁移：设备角色（normal=普通摄像头 / doorbell=门口机）与两个独立功能开关
        # （主人 2026-09-09 修正：设备类型只管分类；"徘徊逗留检测""访客提醒"是两个
        # 可独立开关的功能，开启后各设时长，不与设备类型绑定）
        cols = {r[1] for r in cursor.execute("PRAGMA table_info(cameras)").fetchall()}
        if "role" not in cols:
            cursor.execute(
                "ALTER TABLE cameras ADD COLUMN role TEXT DEFAULT 'normal'")
        if "dwell_enabled" not in cols:
            cursor.execute(
                "ALTER TABLE cameras ADD COLUMN dwell_enabled INTEGER DEFAULT 1")
        if "dwell_seconds" not in cols:
            cursor.execute(
                "ALTER TABLE cameras ADD COLUMN dwell_seconds INTEGER DEFAULT 60")
        if "visitor_enabled" not in cols:
            cursor.execute(
                "ALTER TABLE cameras ADD COLUMN visitor_enabled INTEGER DEFAULT 1")
        if "visitor_seconds" not in cols:
            cursor.execute(
                "ALTER TABLE cameras ADD COLUMN visitor_seconds INTEGER DEFAULT 60")
        conn.commit()
    finally:
        conn.close()


def _cam_features(data):
    """从请求 JSON 提取摄像头功能配置（含默认值兜底）"""
    return (
        data.get("role", "normal") or "normal",
        1 if data.get("dwellEnabled", True) else 0,
        int(data.get("dwellSeconds") or 60),
        1 if data.get("visitorEnabled", True) else 0,
        int(data.get("visitorSeconds") or 60),
    )


async def handle_get_cameras(request):
    """获取所有摄像头配置"""
    try:
        init_camera_table()
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, url, type, room_id, created_at, role,
                       dwell_enabled, dwell_seconds, visitor_enabled, visitor_seconds
                FROM cameras ORDER BY created_at DESC
            ''')
            rows = cursor.fetchall()
        finally:
            conn.close()

        cameras = []
        for row in rows:
            cameras.append({
                "id": row[0],
                "name": row[1],
                "url": row[2],
                "type": row[3],
                "roomId": row[4],
                "createdAt": row[5],
                "role": row[6] or "normal",
                "dwellEnabled": bool(row[7]) if row[7] is not None else True,
                "dwellSeconds": row[8] if row[8] is not None else 60,
                "visitorEnabled": bool(row[9]) if row[9] is not None else True,
                "visitorSeconds": row[10] if row[10] is not None else 60
            })

        return web.json_response({
            "success": True,
            "cameras": cameras
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def handle_add_camera(request):
    """添加摄像头"""
    conn = None
    try:
        data = await request.json()
        name = data.get("name", "").strip()
        url = data.get("url", "").strip()
        cam_type = data.get("type", "rtsp")
        room_id = data.get("roomId")
        # 设备类型 + 两个独立功能开关（徘徊逗留检测/访客提醒，开启后各设时长）
        role, dwell_on, dwell, visitor_on, visitor_s = _cam_features(data)

        if not name or not url:
            return web.json_response({
                "success": False,
                "error": "摄像头名称和地址不能为空"
            }, status=400)

        init_camera_table()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cameras (name, url, type, room_id, role,
                                 dwell_enabled, dwell_seconds,
                                 visitor_enabled, visitor_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, url, cam_type, room_id, role,
              dwell_on, dwell, visitor_on, visitor_s))
        camera_id = cursor.lastrowid
        conn.commit()

        return web.json_response({
            "success": True,
            "message": "摄像头添加成功",
            "camera": {
                "id": camera_id,
                "name": name,
                "url": url,
                "type": cam_type,
                "roomId": room_id,
                "role": role,
                "dwellEnabled": bool(dwell_on),
                "dwellSeconds": dwell,
                "visitorEnabled": bool(visitor_on),
                "visitorSeconds": visitor_s
            }
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def handle_update_camera(request):
    """更新摄像头"""
    conn = None
    try:
        camera_id = request.match_info.get("id")
        data = await request.json()
        name = data.get("name", "").strip()
        url = data.get("url", "").strip()
        cam_type = data.get("type", "rtsp")
        room_id = data.get("roomId")
        role, dwell_on, dwell, visitor_on, visitor_s = _cam_features(data)

        if not name or not url:
            return web.json_response({
                "success": False,
                "error": "摄像头名称和地址不能为空"
            }, status=400)

        init_camera_table()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE cameras
            SET name = ?, url = ?, type = ?, room_id = ?, role = ?,
                dwell_enabled = ?, dwell_seconds = ?,
                visitor_enabled = ?, visitor_seconds = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (name, url, cam_type, room_id, role,
              dwell_on, dwell, visitor_on, visitor_s, camera_id))
        conn.commit()

        return web.json_response({
            "success": True,
            "message": "摄像头更新成功"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def handle_delete_camera(request):
    """删除摄像头"""
    conn = None
    try:
        camera_id = request.match_info.get("id")

        init_camera_table()
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
        conn.commit()

        return web.json_response({
            "success": True,
            "message": "摄像头已删除"
        })
    except Exception as e:
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
    finally:
        if conn:
            conn.close()


async def handle_camera_stream(request):
    # [2026-10-13] 开流前先清一遍历史僵尸。放在开流入口而不是新起
    # 定时器：摄像头流是低频动作，借它顺手巡检足够，且不引入常驻任务。
    try:
        _n = await asyncio.to_thread(_reap_stale_ffmpeg)
        if _n:
            logger.bind(tag=TAG).info(
                f"[摄像头流] 开流前清理了 {_n} 个僵尸 ffmpeg 进程"
            )
    except Exception as e:
        logger.bind(tag=TAG).debug(f"[摄像头流] 僵尸巡检失败（不影响开流）: {e}")
    """摄像头流代理 - 将 RTSP 转为 MJPEG 流"""
    if CAMERA_ISOLATED:
        return _isolated_response()
    try:
        rtsp_url = request.query.get('url', '')
        
        if not rtsp_url:
            return web.json_response({
                "success": False,
                "error": "缺少摄像头 URL"
            }, status=400)
        
        # 使用 FFmpeg 将 RTSP 转为 MJPEG 流
        # 注意：这需要 FFmpeg 已安装
        # 【2026-09-29】清晰度/帧率改为 URL 参数可调（原硬编码 320x180@5 过糊且延迟大）：
        #   ?w=1280&h=720&fps=10（默认 720p@10fps），加低延迟参数去掉解码缓冲累积
        def _int_param(q, key, default, lo, hi):
            try:
                v = int(q.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        w = _int_param(request.query, 'w', 1280, 160, 1920)
        h = _int_param(request.query, 'h', 720, 90, 1080)
        fps = _int_param(request.query, 'fps', 10, 1, 30)

        ffmpeg_cmd = [
            _ffmpeg_bin(),
            '-rtsp_transport', 'tcp',
            '-fflags', 'nobuffer',
            '-flags', 'low_delay',
            '-i', rtsp_url,
            '-an',
            '-vf', f'fps={fps},scale={w}:{h}',
            '-c:v', 'mjpeg',
            '-q:v', '4',
            '-f', 'mpjpeg',
            '-'  # 输出到标准输出
        ]
        
        # 创建响应流
        response = web.StreamResponse()
        response.content_type = 'multipart/x-mixed-replace; boundary=ffmpeg'
        await response.prepare(request)
        
        # 启动 FFmpeg 进程
        # 【修复 2026-09-06】改为异步子进程：
        # 原实现 process.stdout.read() 同步阻塞会卡死整个 asyncio 事件循环（摄像头一开全服务卡顿），
        # 且 stderr=PIPE 不消费会被 ffmpeg 日志填满导致死锁（表现为流 0 字节）
        # 不用 asyncio.create_subprocess_exec：macOS 上若事件循环不在主线程会因 child watcher 启动失败。
        # 用 Popen + 线程读：不阻塞事件循环、不依赖 child watcher；stderr 丢弃避免管道填满死锁。
        # 【2026-10-01】Popen 本身（fork+exec）也放进线程，连同 finally 的清理，
        # 整条链路不占用事件循环任何时间片。
        process = await asyncio.to_thread(
            subprocess.Popen,
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        logger.bind(tag=TAG).info(f"摄像头流已启动 ffmpeg pid={process.pid}")
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.to_thread(process.stdout.read, 4096),
                        timeout=_STREAM_READ_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.bind(tag=TAG).warning(
                        f"[摄像头流] 读流超过 {_STREAM_READ_TIMEOUT}s 无数据，判定上游已断，结束推送并清理 ffmpeg"
                    )
                    break

                if not chunk:
                    break
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as e:
            logger.bind(tag=TAG).error(f"流传输错误: {e}")
        finally:
            # 【2026-10-01 修复】整段进程清理挪进线程执行。
            # 原代码在此处同步 poll/terminate/wait/kill，wait 最多阻塞事件循环
            # 3 秒；客户端每轮断连都触发一次，叠加后导致音频/ASR 反复停摆。
            # to_thread 被取消时线程仍会跑完（run_in_executor 不可中断），
            # 因此 ffmpeg 一定能被清理干净，不会残留进程占住摄像头。
            try:
                # [2026-10-13] 改成 fire-and-forget，不再 await。
                # 旧写法 await asyncio.to_thread(...) 有个致命前提错误：
                # 客户端断开 → aiohttp 取消整个 handler，若清理任务还排在线程池
                # 队列里没开始跑，future.cancel() 会成功、任务被直接丢弃 →
                # 没有任何人去杀 ffmpeg（实测留下跑了 3 天 10 小时的僵尸）。
                # run_in_executor 不 await，任务一旦提交就不受 handler 取消影响。
                try:
                    asyncio.get_running_loop().run_in_executor(
                        None, _terminate_ffmpeg, process
                    )
                except Exception:
                    # 拿不到事件循环时降级为直接调用（宁可阻塞也要杀干净）
                    _terminate_ffmpeg(process)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.bind(tag=TAG).warning(f"ffmpeg 进程清理异常: {e}")
        
        return response
        
    except Exception as e:
        logger.bind(tag=TAG).error(f"摄像头流代理错误: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def handle_camera_snapshot(request):
    """摄像头快照 - 从 RTSP 获取单帧图像"""
    if CAMERA_ISOLATED:
        return _isolated_response()
    try:
        rtsp_url = request.query.get('url', '')
        
        if not rtsp_url:
            return web.json_response({
                "success": False,
                "error": "缺少摄像头 URL"
            }, status=400)
        
        # 使用 FFmpeg 获取单帧快照
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp_path = tmp.name
        
        ffmpeg_cmd = [
            _ffmpeg_bin(),
            '-rtsp_transport', 'tcp',
            '-t', '5',
            '-i', rtsp_url,
            '-vf', 'scale=320:180',
            '-frames:v', '1',
            '-q:v', '5',
            '-y',
            '-update', '1',
            tmp_path
        ]
        
        # 【修复 2026-09-06】放到线程执行，避免同步 subprocess.run 阻塞事件循环（最长 15s 卡全服务）
        process = await asyncio.to_thread(
            subprocess.run,
            ffmpeg_cmd,
            capture_output=True,
            timeout=15
        )
        
        if process.returncode == 0:
            # 读取快照文件并返回
            with open(tmp_path, 'rb') as f:
                image_data = f.read()
            
            # 清理临时文件
            import os
            os.unlink(tmp_path)
            
            return web.Response(
                body=image_data,
                content_type='image/jpeg'
            )
        else:
            # 清理临时文件
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            
            return web.json_response({
                "success": False,
                "error": "获取快照失败"
            }, status=500)
            
    except Exception as e:
        logger.bind(tag=TAG).error(f"摄像头快照错误: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)
