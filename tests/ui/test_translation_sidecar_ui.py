"""Translation sidecar P2 — Chinese bubble gate, TTS start, timeout/failure."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.llm.chat_reply import ChatSegment


def _qt_app_or_skip():  # type: ignore[no-untyped-def]
    qtwidgets = pytest.importorskip("PySide6.QtWidgets")
    if not hasattr(qtwidgets, "QApplication") or not hasattr(qtwidgets, "QWidget"):
        pytest.skip("当前测试环境只提供了 PySide6 stub。")
    return qtwidgets.QApplication.instance() or qtwidgets.QApplication([])


class _DummyLabel:
    def __init__(self) -> None:
        self.text = ""

    def clear(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


class _DelayedTTS:
    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self.on_started = None
        self.on_finished = None

    def speak(self, text: str, tone: str, on_finished=None, on_started=None):  # type: ignore[no-untyped-def]
        self.spoken.append((text, tone))
        self.on_started = on_started
        self.on_finished = on_finished

    def discard_prepared(self, _handle):  # type: ignore[no-untyped-def]
        return None


def _build_controller(label: _DummyLabel, tts: _DelayedTTS, applied: list[ChatSegment]):
    from app.ui.subtitle_controller import SubtitleController
    from app.voice import VoicePlaybackController

    return SubtitleController(
        label,  # type: ignore[arg-type]
        VoicePlaybackController(tts, lambda *_args, **_kwargs: None),
        "zh",
        lambda *_args, **_kwargs: None,
        applied.append,
        lambda: None,
        lambda: True,
    )


def test_pending_chinese_subtitle_never_exposes_japanese_while_tts_starts() -> None:
    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    segment = ChatSegment("おはよう。", "开心", "", "站立待机")

    controller.start_waiting_indicator()
    waiting_text = label.text
    controller.begin_translation_gate(timeout_seconds=6, interaction_id="turn-1")
    controller.show_segments([segment])

    assert applied == [segment]
    assert tts.spoken == [("おはよう。", "开心")]
    assert tts.on_started is not None
    assert controller.waiting_indicator_active
    assert "おはよう" not in label.text
    assert label.text in {waiting_text, ".", "..", "...", "....", ".....", "......"}

    tts.on_started()

    assert controller.waiting_indicator_active
    assert "おはよう" not in label.text
    assert not controller.current_segment_speech_done
    controller.cancel_reply_flow()


def test_existing_zh_fast_path_does_not_hold_waiting_or_change_timing() -> None:
    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    segment = ChatSegment("おはよう。", "开心", "早安。", "站立待机")

    controller.start_waiting_indicator()
    controller.show_segments([segment])
    assert tts.on_started is not None
    tts.on_started()

    assert not controller.waiting_indicator_active
    assert label.text == "早安。"
    assert controller.current_segment_speech_done is True
    controller.cancel_reply_flow()


def test_translation_success_displays_chinese_and_releases_progression() -> None:
    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    segment = ChatSegment("ねえ。", "温柔", "", "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([segment])
    tts.on_started()
    assert "ねえ" not in label.text

    updated = ChatSegment("ねえ。", "温柔", "喂。", "站立待机")
    controller.current_segment = updated
    controller.release_translation_gate(fallback=False)
    controller.set_speech(updated.display_text("zh"), pulse=False, instant=True)

    assert label.text == "喂。"
    assert "ねえ" not in label.text
    assert controller.current_segment_speech_done is True
    assert not controller.waiting_indicator_active
    controller.cancel_reply_flow()


def test_provider_failure_releases_japanese_without_deadlock() -> None:
    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    completed: list[str] = []
    from app.ui.subtitle_controller import SubtitleController
    from app.voice import VoicePlaybackController

    controller = SubtitleController(
        label,  # type: ignore[arg-type]
        VoicePlaybackController(tts, lambda *_args, **_kwargs: None),
        "zh",
        lambda *_args, **_kwargs: None,
        applied.append,
        lambda: completed.append("done"),
        lambda: True,
    )
    segment = ChatSegment("ねえ。", "温柔", "", "站立待机")
    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([segment])
    tts.on_started()
    tts.on_finished()
    assert completed == []
    assert not controller.current_segment_speech_done

    controller.release_translation_gate(fallback=True)

    assert "ねえ" in label.text
    assert controller.current_segment_speech_done is True
    assert completed == ["done"]
    controller.cancel_reply_flow()


def test_gate_timeout_releases_japanese_without_waiting_full_tts() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    segment = ChatSegment("おはよう。", "开心", "", "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=0)
    controller.show_segments([segment])
    tts.on_started()
    QCoreApplication.processEvents()

    assert "おはよう" in label.text
    assert controller.current_segment_speech_done is True
    assert not getattr(controller, "_translation_gate_active", True)
    controller.cancel_reply_flow()


def test_default_gate_timeout_is_six_seconds() -> None:
    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    controller.begin_translation_gate()
    timer = controller._translation_gate_timer
    assert timer.isActive()
    assert timer.interval() == 6000
    controller.cancel_reply_flow()


def test_multi_segment_missing_zh_holds_until_success_then_patches_and_completes() -> None:
    from PySide6.QtCore import QCoreApplication

    from app.ui.pet_window import PetWindow

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    completed: list[str] = []
    from app.ui.subtitle_controller import SubtitleController
    from app.voice import VoicePlaybackController

    controller = SubtitleController(
        label,  # type: ignore[arg-type]
        VoicePlaybackController(tts, lambda *_args, **_kwargs: None),
        "zh",
        lambda *_args, **_kwargs: None,
        applied.append,
        lambda: completed.append("done"),
        lambda: True,
    )
    controller.set_display_speed(5, 0)
    first = ChatSegment("おはよう。", "开心", "", "站立待机")
    second = ChatSegment("ねえ。", "温柔", "", "站立待机")
    history = MagicMock()
    window = SimpleNamespace(
        history_store=history,
        reply_history_segments=[first, second],
        subtitle_controller=controller,
        subtitle_language="zh",
    )

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6, interaction_id="turn-multi")
    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    assert "おはよう" not in label.text
    assert "ねえ" not in label.text
    assert controller.waiting_indicator_active
    assert len(controller.pending_reply_segments) == 1

    PetWindow._apply_subtitle_translations(
        window,
        texts=["おはよう。", "ねえ。"],
        translations=["早安。", "喂。"],
        history_ids=[1, 2],
    )

    assert label.text == "早安。"
    assert "おはよう" not in label.text
    assert controller.current_segment is not None
    assert controller.current_segment.translation == "早安。"
    assert controller.pending_reply_segments[0].translation == "喂。"
    assert not controller._translation_gate_active
    history.update_translation.assert_any_call(1, "早安。")
    history.update_translation.assert_any_call(2, "喂。")

    tts.on_finished()
    QCoreApplication.processEvents()
    assert tts.on_started is not None
    tts.on_started()
    assert label.text == "喂。"
    assert "ねえ" not in label.text
    tts.on_finished()
    QCoreApplication.processEvents()
    assert completed == ["done"]
    controller.cancel_reply_flow()


def test_multi_segment_exposes_japanese_only_after_one_turn_level_timeout() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    completed: list[str] = []
    from app.ui.subtitle_controller import SubtitleController
    from app.voice import VoicePlaybackController

    controller = SubtitleController(
        label,  # type: ignore[arg-type]
        VoicePlaybackController(tts, lambda *_args, **_kwargs: None),
        "zh",
        lambda *_args, **_kwargs: None,
        applied.append,
        lambda: completed.append("done"),
        lambda: True,
    )
    controller.set_display_speed(5, 0)
    first = ChatSegment("おはよう。", "开心", "", "站立待机")
    second = ChatSegment("ねえ。", "温柔", "", "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=0, interaction_id="turn-timeout")
    controller.show_segments([first, second])
    tts.on_started()
    assert "おはよう" not in label.text
    assert "ねえ" not in label.text

    QCoreApplication.processEvents()
    assert "おはよう" in label.text
    assert not controller._translation_gate_active

    tts.on_finished()
    QCoreApplication.processEvents()
    tts.on_started()
    assert "ねえ" in label.text
    tts.on_finished()
    QCoreApplication.processEvents()
    assert completed == ["done"]
    controller.cancel_reply_flow()
