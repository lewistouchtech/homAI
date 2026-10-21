"""Mac 系统音频 AEC 适配层：macos-system-aec 独立包 ↔ homebrain 接线。

仅浏览器（web-terminal）PCM 上行路径启用；ESP32 opus 路径 v1 不接。

[2026-10-13 音乐不停对话·第三段] 参考源从 ScreenCaptureKit（capture_sysaudio）
换成 Process Tap（processtap_collector，Core Audio 私有 API，macOS 14.2+）：
- 桥接用 macos_sys_aec.processtap.ProcessTapCollector（线程 + 子进程，
  崩溃/断流自动退避重启；「系统音频录制」授权已批，禁止重编译采集器二进制，
  详见 packages/macos-system-aec/processtap_collector/README.md）；
- 参考帧（约 93 帧/秒，int16 单声道 16k，帧头带墙钟时间戳）喂 AECSession
  参考缓冲，对齐/消除仍走包内 GCC-PHAT 对齐器 + pywebrtc AEC3
  （离线实测 ERLE 20.9~30.6dB）；
- 每帧参考同时喂「音乐在放」能量信号（music_auto_pause.note_reference_energy，
  供声纹闸门选阈值档；旧 osascript 轮询已随降音量流程删除）。

config.yaml:

system_aec:
  enabled: true
  capture_enabled: true      # 参考采集开关（关闭时 AEC 旁路、音乐信号保守为真）
  device_latency_ms:
    browser: 80
    default: 80
  max_restart_attempts: 5
"""

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_session = None
_collector = None


class _PercentLogAdapter:
    """macos_sys_aec 包内部用 stdlib 风格 logger.info("...%s", arg)，
    而 loguru 的 bind logger 是 {} 格式化、带 %s 参数会原样丢参。
    本适配器先做 % 格式化再交给 loguru（只适配包内用到的四个级别）。"""

    def __init__(self, bound):
        self._b = bound

    @staticmethod
    def _fmt(msg, args):
        if not args:
            return str(msg)
        try:
            return str(msg) % args
        except Exception:
            return str(msg)

    def info(self, msg, *a):
        self._b.info(self._fmt(msg, a))

    def warning(self, msg, *a):
        self._b.warning(self._fmt(msg, a))

    def error(self, msg, *a):
        self._b.error(self._fmt(msg, a))

    def debug(self, msg, *a):
        self._b.debug(self._fmt(msg, a))


def _device_type(conn) -> str:
    device = getattr(conn, "hello_device_id", "") or ""
    return "browser" if device.startswith("web-terminal") else "default"


def _on_reference_frame(ts_ms: int, payload: bytes, frame_count: int) -> None:
    """ProcessTapCollector 帧回调（采集线程，约 93 次/秒）。

    两个去向：
    ① AEC 参考缓冲（时间戳 = payload 首样本墙钟采集时间，与 processor.process
       传入的到达时间戳同一 epoch 毫秒时间轴，由 GCC-PHAT 对齐器估延迟）；
    ② 「音乐在放」能量信号（滑动窗口 RMS，见 music_auto_pause）。
    """
    if _session is not None:
        try:
            _session.feed_reference(ts_ms, payload)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"[系统AEC] 参考帧写入失败: {e}")
    try:
        from core.utils.music_auto_pause import note_reference_energy
        note_reference_energy(payload)
    except Exception:
        pass  # 音乐信号是增强功能，失败不得影响 AEC 参考链


def get_processor(conn):
    """返回该连接的 AECProcessor；禁用/失败返回 None。结果缓存于 conn.system_aec_processor。

    生命周期：首个 PCM 帧到达时懒启动（一台 Mac 一个采集器进程，服务级单例），
    与 launchd 重启兼容——进程重启后随首个连接重新拉起；采集器崩溃/断流由
    ProcessTapCollector 自带退避重启（2s 退避，超 5 次转 30s 长退避，永不放弃）。
    """
    global _session, _collector
    if getattr(conn, "_system_aec_ready", False):
        return getattr(conn, "system_aec_processor", None)
    conn._system_aec_ready = True
    conn.system_aec_processor = None
    try:
        cfg = conn.config.get("system_aec") or {}
        if not cfg.get("enabled", False):
            return None
        from macos_sys_aec import AECSession
        if _session is None:
            # capture_enabled=False：不再用包内 ScreenCaptureKit 采集（CaptureManager），
            # 参考源改由下面的 ProcessTapCollector 喂入（对齐器/处理器不变）。
            _session = AECSession(
                capture_enabled=False,
                logger=_PercentLogAdapter(logger.bind(tag="macos_sys_aec")),
            )
            if cfg.get("capture_enabled", True):
                from macos_sys_aec.processtap import ProcessTapCollector
                _collector = ProcessTapCollector(
                    on_frame=_on_reference_frame,
                    restart_max=int(cfg.get("max_restart_attempts", 5)),
                    logger=_PercentLogAdapter(logger.bind(tag="processtap")),
                )
                _collector.start()
                logger.bind(tag=TAG).info(
                    "[系统AEC] Process Tap 采集器已启动（参考源；授权已批，首帧预期秒级到达）")
            else:
                logger.bind(tag=TAG).warning(
                    "[系统AEC] capture_enabled=false：无参考信号，AEC 旁路，"
                    "音乐活跃信号将保持保守（is_music_active 恒 True）")
        latency = (cfg.get("device_latency_ms") or {}).get(_device_type(conn), 80)
        conn.system_aec_processor = _session.create_processor(
            initial_offset_ms=float(latency))
        logger.bind(tag=TAG).info(
            f"[系统AEC] 处理器已创建: device={_device_type(conn)}, init_offset={latency}ms")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[系统AEC] 初始化失败，本连接禁用: {e}")
    return conn.system_aec_processor


def shutdown():
    """停止 Process Tap 采集器子进程。服务退出时必须调用，否则采集器会变成
    孤儿进程继续采集系统音频（每次重启遗留一个，多个进程重复采集浪费资源，
    且旧实例的参考帧时间轴会干扰排障）。2026-09-18 补：此前 AECSession.stop()
    从未被调用；2026-10-13 起停的是 ProcessTapCollector。"""
    global _session, _collector
    if _collector is not None:
        try:
            _collector.stop()
            logger.bind(tag=TAG).info("[系统AEC] Process Tap 采集器已停止")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"[系统AEC] 采集器停止失败: {e}")
        finally:
            _collector = None
    if _session is None:
        return
    try:
        _session.stop()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[系统AEC] 停止失败: {e}")
    finally:
        _session = None
