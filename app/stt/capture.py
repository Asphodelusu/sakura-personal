"""麦克风音频采集 + 简单 VAD"""

from __future__ import annotations

import logging
import sys
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger("sakura.stt")
if not logger.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [STT] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

SAMPLE_RATE = 16000
BLOCK_SIZE = 512
SILENCE_THRESHOLD = 0.012
SILENCE_BLOCKS = 30  # ~1s 静音后自动停止（15=0.5s 太短，呼吸容易触发）
MAX_DURATION_SEC = 15.0
LEVEL_NORMALIZE = 0.08

LevelCallback = Callable[[float], None]


class AudioCapture:

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        block_size: int = BLOCK_SIZE,
        silence_threshold: float = SILENCE_THRESHOLD,
        silence_blocks: int = SILENCE_BLOCKS,
        max_duration_sec: float = MAX_DURATION_SEC,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.silence_threshold = silence_threshold
        self.silence_blocks = silence_blocks
        self.max_duration_sec = max_duration_sec

        self._frames: list[np.ndarray] = []
        self._silence_counter = 0
        self._started = False
        self._done = False
        self._max_frames = int(max_duration_sec * sample_rate / block_size)
        self._level_callback: LevelCallback | None = None
        self.latest_level: float = 0.0
        self._stop_requested: bool = False

    def set_level_callback(self, cb: LevelCallback | None) -> None:
        self._level_callback = cb

    def stop(self) -> None:
        self._stop_requested = True

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            logger.warning("状态异常: %s", status)

        mono = indata[:, 0] if indata.ndim > 1 else indata
        energy = float(np.sqrt(np.mean(mono**2)))

        self.latest_level = min(energy / LEVEL_NORMALIZE, 1.0)

        cb = self._level_callback
        if cb is not None:
            cb(self.latest_level)

        if not self._started:
            if energy > self.silence_threshold:
                self._started = True
                self._frames.append(mono.copy())
                self._silence_counter = 0
                logger.info("VAD 触发: energy=%.4f > %.4f", energy, self.silence_threshold)
            return

        self._frames.append(mono.copy())

        if energy < self.silence_threshold:
            self._silence_counter += 1
        else:
            self._silence_counter = 0

        if self._silence_counter >= self.silence_blocks:
            logger.info("VAD 停止: %d 连续静音块", self._silence_counter)
            self._done = True
        if len(self._frames) >= self._max_frames:
            self._done = True

    def record(self, device: int | None = None) -> np.ndarray | None:
        self._frames.clear()
        self._silence_counter = 0
        self._started = False
        self._done = False
        self.latest_level = 0.0
        self._stop_requested = False

        logger.info("🎤 录音开始 (threshold=%.4f)", self.silence_threshold)
        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                callback=self._callback,
                blocksize=self.block_size,
                dtype="float32",
                device=device,
            ):
                # 短 sleep 循环：每 50ms 检查停止条件
                total_loops = int((self.max_duration_sec * 1000 + 2000) / 50)
                for _ in range(total_loops):
                    if self._stop_requested or self._done:
                        break
                    sd.sleep(50)
        except sd.CallbackStop:
            pass
        except sd.CallbackAbort:
            pass
        except Exception:
            logger.exception("录音流异常")

        self.latest_level = 0.0
        if not self._frames:
            logger.info("录音结束: 无帧 (VAD 未触发)")
            return None

        audio = np.concatenate(self._frames)
        peak = float(np.max(np.abs(audio)))
        logger.info("录音结束: %.1fs, %d 帧, peak=%.4f",
                    len(audio) / self.sample_rate, len(self._frames), peak)
        return audio

    @staticmethod
    def list_devices() -> list[dict]:
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append({
                    "index": i,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "default_samplerate": dev["default_samplerate"],
                })
        return devices
