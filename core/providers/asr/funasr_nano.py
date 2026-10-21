import time
import os
import sys
import io
from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

TAG = __name__
logger = setup_logging()


# 捕获标准输出
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._original_stdout = sys.stdout
        sys.stdout = self._output

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        self.output = self._output.getvalue()
        self._output.close()

        if self.output:
            logger.bind(tag=TAG).info(self.output.strip())


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")
        self.delete_audio_file = delete_audio_file

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

        # 验证模型目录存在
        if not os.path.isdir(self.model_dir):
            raise FileNotFoundError(f"FunASR-Nano 模型目录不存在: {self.model_dir}")

        # 使用 FunASR AutoModel 加载（原生格式，非 sherpa-onnx）
        with CaptureOutput():
            from funasr import AutoModel
            self.model = AutoModel(
                model=self.model_dir,
                disable_update=True,
                disable_log=True,
            )

        logger.bind(tag=TAG).info(f"FunASR-Nano 模型加载完成 (AutoModel): {self.model_dir}")

    def requires_file(self) -> bool:
        return True

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """语音转文本"""
        file_path = None
        try:
            if artifacts is None:
                return "", None
            file_path = artifacts.file_path

            if not os.path.isfile(file_path):
                logger.bind(tag=TAG).error(f"音频文件不存在: {file_path}")
                return "", file_path

            start_time = time.time()
            res = self.model.generate(input=file_path)

            text = ""
            if res and len(res) > 0:
                text = res[0].get('text', res[0].get('pred', '')).strip()

            elapsed = time.time() - start_time
            logger.bind(tag=TAG).info(
                f"FunASR-Nano 识别耗时: {elapsed:.3f}s | 结果: {text}"
            )

            return text, file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"FunASR-Nano 语音识别失败: {e}", exc_info=True)
            return "", file_path
