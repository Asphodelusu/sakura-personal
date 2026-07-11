"""SenseVoice 语音识别器封装"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import sherpa_onnx

logger = logging.getLogger(__name__)

# 默认模型路径
DEFAULT_MODEL_DIR = Path("D:/sakura/data/models/sense-voice")


class SenseVoiceRecognizer:
    """SenseVoice 离线语音识别器（单例）。"""

    _instance: Optional["SenseVoiceRecognizer"] = None

    def __init__(self, model_dir: str = "") -> None:
        model_path = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path / "model.int8.onnx"),
            tokens=str(model_path / "tokens.txt"),
            use_itn=True,
            language="zh",
            num_threads=4,
        )
        logger.info("SenseVoice 识别器已初始化")

    @classmethod
    def get_instance(cls, model_dir: str = "") -> "SenseVoiceRecognizer":
        if cls._instance is None:
            cls._instance = cls(model_dir)
        return cls._instance

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """识别音频数据，返回文本。

        Args:
            audio: float32 单声道音频数组
            sample_rate: 采样率（模型要求 16000，不匹配时需重采样）
        """
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, audio)
        self._recognizer.decode_stream(stream)
        result = stream.result
        return result.text.strip() if result.text else ""

    @staticmethod
    def is_available() -> bool:
        """检查模型文件是否存在。"""
        model_path = DEFAULT_MODEL_DIR / "model.int8.onnx"
        tokens_path = DEFAULT_MODEL_DIR / "tokens.txt"
        return model_path.exists() and tokens_path.exists()
