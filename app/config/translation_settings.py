"""中文字幕 sidecar 配置。默认对公开项目关闭。"""

from __future__ import annotations

from dataclasses import dataclass


TRANSLATION_GATE_TIMEOUT_MIN_SECONDS = 0
TRANSLATION_GATE_TIMEOUT_MAX_SECONDS = 60
TRANSLATION_GATE_TIMEOUT_DEFAULT_SECONDS = 6
TRANSLATION_MAX_ATTEMPTS_MIN = 1
TRANSLATION_MAX_ATTEMPTS_MAX = 2
TRANSLATION_MAX_ATTEMPTS_DEFAULT = 2


@dataclass(frozen=True)
class TranslationSettings:
    """异步中文字幕翻译开关与边界。"""

    enabled: bool = False
    gate_timeout_seconds: int = TRANSLATION_GATE_TIMEOUT_DEFAULT_SECONDS
    max_attempts: int = TRANSLATION_MAX_ATTEMPTS_DEFAULT

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
        return TranslationSettings(
            enabled=bool(self.enabled),
            gate_timeout_seconds=timeout,
            max_attempts=attempts,
        )
