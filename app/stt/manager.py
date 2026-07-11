"""STT 管理器：后台录音，主线程识别"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

import numpy as np

from app.stt.capture import AudioCapture, SAMPLE_RATE
from app.stt.recognizer import SenseVoiceRecognizer

logger = logging.getLogger("sakura.stt")
if not logger.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [STT] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


class STTManager:

    def __init__(self) -> None:
        self._capture = AudioCapture()
        self._recognizer: SenseVoiceRecognizer | None = None
        self._listening = False
        self._thread: threading.Thread | None = None
        self._audio: np.ndarray | None = None

    @property
    def available(self) -> bool:
        return SenseVoiceRecognizer.is_available()

    @property
    def latest_level(self) -> float:
        return self._capture.latest_level

    def start_listening(self) -> None:
        if self._listening:
            return
        logger.info("▶ 开始录音")
        self._listening = True
        self._audio = None
        self._capture.latest_level = 0.0
        self._thread = threading.Thread(target=self._record, daemon=True)
        self._thread.start()

    def _record(self) -> None:
        try:
            self._audio = self._capture.record()
        except Exception:
            logger.exception("录音异常")
            self._audio = None
        finally:
            logger.info("■ 录音线程结束: audio=%s",
                        f"{len(self._audio)}采样" if self._audio is not None else "None")
            self._listening = False

    def stop(self, timeout: float = 2.0) -> None:
        logger.info("■ 手动停止录音")
        self._capture.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.info("录音线程仍在运行（超时）")
            else:
                logger.info("录音线程已结束")

    def is_listening(self) -> bool:
        return self._listening

    def transcribe(self) -> Optional[str]:
        if self._audio is None:
            logger.info("transcribe: _audio=None")
            return None
        if len(self._audio) == 0:
            logger.info("transcribe: _audio 为空")
            return None

        logger.info("🔊 识别中: %d 采样 (%.1fs)",
                    len(self._audio), len(self._audio) / SAMPLE_RATE)
        try:
            if self._recognizer is None:
                self._recognizer = SenseVoiceRecognizer.get_instance()
            text = self._recognizer.transcribe(self._audio, SAMPLE_RATE)
            logger.info("📝 识别结果: '%s'", text)
            return text.strip() if text else None
        except Exception:
            logger.exception("识别异常")
            return None
