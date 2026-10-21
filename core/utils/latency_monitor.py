"""端到端延迟监控工具"""
import time
import threading
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class LatencyMonitor:
    """延迟监控器：追踪从唤醒到音频播放的完整流程"""
    
    def __init__(self, session_id):
        self.session_id = session_id
        self.timestamps = {}
        self.lock = threading.Lock()
    
    def mark(self, event_name, **kwargs):
        """记录一个时间点"""
        with self.lock:
            timestamp = time.time()
            self.timestamps[event_name] = {
                'time': timestamp,
                'kwargs': kwargs
            }
            logger.bind(tag=TAG).info(
                f"[延迟监控] {event_name} @ {timestamp:.3f}" + 
                (f" | {kwargs}" if kwargs else "")
            )
    
    def get_latency(self, start_event, end_event):
        """计算两个事件之间的延迟（秒）"""
        with self.lock:
            if start_event not in self.timestamps or end_event not in self.timestamps:
                return None
            start = self.timestamps[start_event]['time']
            end = self.timestamps[end_event]['time']
            return end - start
    
    def report(self):
        """报告完整的延迟分析"""
        with self.lock:
            if not self.timestamps:
                return
            
            logger.bind(tag=TAG).info("=" * 80)
            logger.bind(tag=TAG).info(f"[延迟监控报告] Session: {self.session_id}")
            logger.bind(tag=TAG).info("=" * 80)
            
            # 按时间排序输出所有事件
            sorted_events = sorted(self.timestamps.items(), key=lambda x: x[1]['time'])
            first_time = sorted_events[0][1]['time'] if sorted_events else 0
            
            for event_name, data in sorted_events:
                relative_time = (data['time'] - first_time) * 1000
                kwargs_str = f" | {data['kwargs']}" if data['kwargs'] else ""
                logger.bind(tag=TAG).info(
                    f"  +{relative_time:7.1f}ms  {event_name}{kwargs_str}"
                )
            
            # 计算关键延迟
            logger.bind(tag=TAG).info("-" * 80)
            logger.bind(tag=TAG).info("关键延迟:")
            
            key_latencies = [
                ('VAD检测→ASR完成', 'vad_voice_detected', 'asr_complete'),
                ('ASR→LLM请求', 'asr_complete', 'llm_request_start'),
                ('LLM请求→首token', 'llm_request_start', 'llm_first_token'),
                ('LLM首token→TTS开始', 'llm_first_token', 'tts_start'),
                ('TTS开始→音频播放', 'tts_start', 'audio_play_start'),
                ('端到端(VAD→播放)', 'vad_voice_detected', 'audio_play_start'),
            ]
            
            for name, start_event, end_event in key_latencies:
                latency = self.get_latency(start_event, end_event)
                if latency is not None:
                    logger.bind(tag=TAG).info(f"  {name}: {latency*1000:.1f}ms")
            
            logger.bind(tag=TAG).info("=" * 80)
    
    def clear(self):
        """清除所有时间戳"""
        with self.lock:
            self.timestamps.clear()


# 全局延迟监控器管理
_monitors = {}
_monitors_lock = threading.Lock()


def get_monitor(session_id):
    """获取或创建指定 session 的监控器"""
    with _monitors_lock:
        if session_id not in _monitors:
            _monitors[session_id] = LatencyMonitor(session_id)
        return _monitors[session_id]


def remove_monitor(session_id):
    """移除指定 session 的监控器"""
    with _monitors_lock:
        if session_id in _monitors:
            del _monitors[session_id]
