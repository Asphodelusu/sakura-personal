"""中文字幕 sidecar 配置。默认对公开项目关闭。"""

from __future__ import annotations

from dataclasses import dataclass


TRANSLATION_GATE_TIMEOUT_MIN_SECONDS = 0
TRANSLATION_GATE_TIMEOUT_MAX_SECONDS = 60
TRANSLATION_GATE_TIMEOUT_DEFAULT_SECONDS = 6
TRANSLATION_MAX_ATTEMPTS_MIN = 1
TRANSLATION_MAX_ATTEMPTS_MAX = 2
TRANSLATION_MAX_ATTEMPTS_DEFAULT = 2
TRANSLATION_VALIDATOR_MODE_DEFAULT = "v2"
TRANSLATION_VALIDATOR_MODES = ("legacy", "v2")
TRANSLATION_REQUEST_SHAPE_DEFAULT = "serial"
TRANSLATION_REQUEST_SHAPES = ("serial", "split_batch")
TRANSLATION_LATE_PATCH_GRACE_DEFAULT_MS = 1200
TRANSLATION_LATE_PATCH_GRACE_MIN_MS = 0
TRANSLATION_LATE_PATCH_GRACE_MAX_MS = 10000


@dataclass(frozen=True)
class TranslationSettings:
    """异步中文字幕翻译开关与边界。"""

    enabled: bool = False
    gate_timeout_seconds: int = TRANSLATION_GATE_TIMEOUT_DEFAULT_SECONDS
    max_attempts: int = TRANSLATION_MAX_ATTEMPTS_DEFAULT
    validator_mode: str = TRANSLATION_VALIDATOR_MODE_DEFAULT
    request_shape: str = TRANSLATION_REQUEST_SHAPE_DEFAULT
    late_patch_grace_ms: int = TRANSLATION_LATE_PATCH_GRACE_DEFAULT_MS

    def normalized(self) -> "TranslationSettings":
        timeout = max(
            TRANSLATION_GATE_TIMEOUT_MIN_SECONDS,
            min(
                TRANSLATION_GATE_TIMEOUT_MAX_SECONDS,
                int(self.gate_timeout_seconds),
            ),
        )
        attempts = max(
            TRANSLATION_MAX_ATTEMPTS_MIN,
            min(TRANSLATION_MAX_ATTEMPTS_MAX, int(self.max_attempts)),
        )
        mode = str(self.validator_mode or "").strip().lower()
        if mode not in TRANSLATION_VALIDATOR_MODES:
            mode = TRANSLATION_VALIDATOR_MODE_DEFAULT
        shape = str(self.request_shape or "").strip().lower()
        if shape not in TRANSLATION_REQUEST_SHAPES:
            shape = TRANSLATION_REQUEST_SHAPE_DEFAULT
        grace = max(
            TRANSLATION_LATE_PATCH_GRACE_MIN_MS,
            min(
                TRANSLATION_LATE_PATCH_GRACE_MAX_MS,
                int(self.late_patch_grace_ms),
            ),
        )
        return TranslationSettings(
            enabled=bool(self.enabled),
            gate_timeout_seconds=timeout,
            max_attempts=attempts,
            validator_mode=mode,
            request_shape=shape,
            late_patch_grace_ms=grace,
        )
