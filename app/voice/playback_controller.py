from __future__ import annotations

from typing import Any, Callable

from app.llm.chat_reply import ChatSegment
from app.core.debug_log import debug_log
from app.voice.tts import TTSPreparedAudio, TTSProvider


LogStageCallback = Callable[[str, dict[str, Any] | None], None]
TTSCallback = Callable[[], None]


class VoicePlaybackController:
    """管理回复分段对应的 TTS 播放和下一段预生成。"""

    def __init__(
        self,
        tts_provider: TTSProvider,
        log_stage: LogStageCallback,
    ) -> None:
        self.tts_provider = tts_provider
        self._log_stage = log_stage
        self._prepared_next_segment: ChatSegment | None = None
        self._prepared_next_tts: TTSPreparedAudio | None = None

    def set_provider(self, tts_provider: TTSProvider) -> None:
        self.discard_prepared()
        self.tts_provider = tts_provider

    def speak_segment(
        self,
        segment: ChatSegment,
        sequence_id: int,
        on_started: TTSCallback,
        on_finished: TTSCallback,
    ) -> None:
        prepared_tts = self._take_prepared_tts_for_segment(segment)
        try:
            if prepared_tts is None:
                self._log_stage(
                    "tts_speak_requested",
                    {"sequence_id": sequence_id, "tone": segment.tone},
                )
                self.tts_provider.speak(
                    segment.text,
                    segment.tone,
                    on_finished=on_finished,
                    on_started=on_started,
                )
                return

            self._log_stage(
                "tts_prepared_speak_requested",
                {"sequence_id": sequence_id, "tone": segment.tone},
            )
            self.tts_provider.speak_prepared(
                prepared_tts,
                on_started=on_started,
                on_finished=on_finished,
            )
        except Exception as exc:  # noqa: BLE001
            debug_log(
                "TTS",
                "播放控制器捕获 TTS 异常，回退为仅显示字幕",
                {
                    "text": segment.text,
                    "tone": segment.tone,
                    "error": str(exc),
                },
            )
            print(f"[TTS] 播放失败，已继续显示字幕：{exc}")
            on_started()
            on_finished()

    def prepare_next(self, next_segment: ChatSegment | None) -> None:
        if next_segment is None:
            self.discard_prepared()
            return
        if self._prepared_next_segment is next_segment and self._prepared_next_tts is not None:
            return

        self.discard_prepared()
        self._prepared_next_segment = next_segment
        self._log_stage(
            "next_segment_tts_prepare_requested",
            {
                "text": next_segment.text,
                "tone": next_segment.tone,
                "portrait": next_segment.portrait,
            },
        )
        debug_log(
            "PetWindow",
            "预生成下一段 TTS",
            {
                "text": next_segment.text,
                "tone": next_segment.tone,
                "portrait": next_segment.portrait,
            },
        )
        try:
            self._prepared_next_tts = self.tts_provider.prepare(
                next_segment.text,
                next_segment.tone,
            )
        except Exception as exc:  # noqa: BLE001
            self._prepared_next_segment = None
            self._prepared_next_tts = None
            debug_log(
                "TTS",
                "预生成下一段 TTS 失败，后续将即时播放或仅显示字幕",
                {
                    "text": next_segment.text,
                    "tone": next_segment.tone,
                    "error": str(exc),
                },
            )
            print(f"[TTS] 预生成失败，已继续字幕流程：{exc}")

    def discard_prepared(self) -> None:
        if self._prepared_next_tts is not None:
            self.tts_provider.discard_prepared(self._prepared_next_tts)
        self._prepared_next_segment = None
        self._prepared_next_tts = None

    def _take_prepared_tts_for_segment(
        self,
        segment: ChatSegment,
    ) -> TTSPreparedAudio | None:
        if self._prepared_next_segment is not segment:
            return None

        prepared_tts = self._prepared_next_tts
        self._prepared_next_segment = None
        self._prepared_next_tts = None
        return prepared_tts
