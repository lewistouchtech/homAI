"""MOSS-TTS-Nano 本地代理 - 通过 HTTP API 调用本地 ONNX 推理服务（8086）

模型: OpenMOSS/MOSS-TTS-Nano 0.1B，纯 ONNX Runtime CPU 推理。
音色 = 零样本克隆，voice 参数映射到服务端参考音频表。
数字归一化由服务端 cn2an 处理，客户端不做转换。
"""
import os
import time
import asyncio
import sqlite3
import requests
from datetime import datetime
from pathlib import Path
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

# 音色缓存：避免每次请求都读数据库
_VOICE_CACHE_TTL = 5  # 缓存 5 秒（音色切换后最多 5s 生效）
_voice_cache = {"value": None, "timestamp": 0}


class TTSProvider(TTSProviderBase):
    """MOSS-TTS-Nano 本地代理"""

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_url = config.get("api_url", "http://127.0.0.1:8086/api/tts")
        # 连接复用：keep-alive，避免每句一次 TCP 握手（实测无空闲衰减，纯省握手开销）
        self._session = requests.Session()
        # 音色必须来自后台配置；这里不设置写死的兜底，避免掩盖后台漏配。
        self._voice = str(config.get("voice") or "").strip()
        self._backend_module = str(config.get("_module_name") or "").strip()
        if not self._voice:
            logger.bind(tag=TAG).error("MOSS-TTS-Nano 后台未配置音色（voice），合成前需先在后台设置")
        try:
            self.speed = float(config.get("speed", 1.0))
        except (ValueError, TypeError):
            self.speed = 1.0
        # 2026-09-28 语速调节放开：服务端 np.interp 二维数组 bug 已修复
        # （waveform 展平后再重采样），speed 取值 0.5~2.0 均可用。
        # MOSS-Nano 默认语速偏慢，主人实测可调 1.1~1.2 接近正常语速。
        self.speed = max(0.5, min(2.0, self.speed))
        try:
            self.volume = float(config.get("volume", 5.0))
        except (ValueError, TypeError):
            self.volume = 5.0
        logger.bind(tag=TAG).info(f"MOSS-TTS-Nano 服务地址: {self.api_url}, 音色: {self._voice}, speed: {self.speed}, volume: {self.volume}")

    @staticmethod
    def _normalize_voice(value) -> str:
        """后台音色原样生效；这里只做字符串清理，不提供静态兜底。"""
        return str(value or "").strip()

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{datetime.now().strftime('%H%M%S%f')[:-3]}.{self.audio_file_type}",
        )

    def _get_current_voice(self):
        """读取当前后台模块的热切换音色；跨引擎配置不生效。"""
        global _voice_cache
        now = time.time()
        # 缓存命中且未过期，直接返回
        if _voice_cache["value"] is not None and (now - _voice_cache["timestamp"]) < _VOICE_CACHE_TTL:
            return _voice_cache["value"]
        
        if not self._backend_module:
            return self._voice

        # 缓存未命中或已过期，从数据库读取
        db_path = Path(__file__).parent.parent.parent.parent / "data" / "devices.db"
        if db_path.exists():
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT config_key, config_value FROM tts_config "
                    "WHERE config_key IN ('provider', 'voice')"
                )
                backend = dict(cursor.fetchall())
                db_owner = str(backend.get("provider") or "").strip()
                db_voice = str(backend.get("voice") or "").strip()
                if db_owner == self._backend_module and db_voice:
                    voice = self._normalize_voice(db_voice)
                    _voice_cache["value"] = voice
                    _voice_cache["timestamp"] = now
                    return voice
            except Exception as e:
                logger.bind(tag=TAG).warning(f"读取数据库音色配置失败: {e}，使用上次读取的值 {self._voice}")
            finally:
                if conn:
                    conn.close()
        
        # 数据库读取失败，使用缓存或默认值
        if _voice_cache["value"] is not None:
            return _voice_cache["value"]
        _voice_cache["value"] = self._voice
        _voice_cache["timestamp"] = now
        return self._voice
    
    def _update_voice_from_db(self):
        """主动刷新音色配置（用于控制台切换）"""
        global _voice_cache
        voice = self._get_current_voice()
        if voice != self._voice:
            logger.bind(tag=TAG).info(f"音色已从控制台切换: {self._voice} -> {voice}")
            self._voice = voice
            # 清除缓存，确保下次请求读取最新值
            _voice_cache["value"] = None
            _voice_cache["timestamp"] = 0
        return voice

    async def text_to_speak(self, text, output_file):
        """通过 HTTP API 调用 MOSS-TTS-Nano 本地服务"""
        try:
            voice = self._get_current_voice()
            if not voice:
                raise ValueError("MOSS-TTS-Nano 后台未配置音色（voice）")
            request_data = {
                "text": text,
                "voice": voice,
                "speed": self.speed,
                "volume": self.volume,
            }
            response = await asyncio.to_thread(
                self._session.post,
                self.api_url,
                json=request_data,
                timeout=60,
            )
            if response.status_code == 200:
                audio_data = response.content
                if output_file:
                    with open(output_file, "wb") as f:
                        f.write(audio_data)
                    logger.bind(tag=TAG).info(f"音频已保存: {output_file}")
                return audio_data
            else:
                error_msg = f"MOSS-TTS-Nano 服务返回错误: {response.status_code}"
                raise Exception(error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"MOSS-TTS-Nano 服务请求失败: {e}"
            raise Exception(error_msg)
