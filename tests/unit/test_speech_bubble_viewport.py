# -*- coding: utf-8 -*-
"""主气泡视口：一次出全文 + 语音结束后露后半。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.ui.speech_bubble_viewport import (
    build_speech_text_scroll,
    layout_speech_label_in_scroll,
    measure_speech_label_height,
    sync_speech_scroll,
)
from app.ui.subtitle_controller import SubtitleController


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_speech_scroll_has_no_translucent_hole_attribute() -> None:
    _app()
    host = QWidget()
    scroll, label = build_speech_text_scroll(host)
    assert not scroll.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not scroll.viewport().testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not label.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not scroll.autoFillBackground()
    assert not scroll.viewport().autoFillBackground()
    assert not label.autoFillBackground()
    host.close()


def test_short_text_scroll_centers_content_vertically() -> None:
    _app()
    host = QWidget()
    scroll, label = build_speech_text_scroll(host)
    host.resize(320, 160)
    scroll.setGeometry(0, 0, 300, 120)
    host.show()
    _app().processEvents()

    label.setText("短句")
    sync_speech_scroll(scroll, label, reveal_tail=False)
    assert scroll.alignment() & Qt.AlignmentFlag.AlignVCenter
    host.close()


def test_overflow_stays_top_while_speaking_then_reveals_tail() -> None:
    _app()
    host = QWidget()
    scroll, label = build_speech_text_scroll(host)
    host.resize(320, 100)
    scroll.setGeometry(0, 0, 280, 60)
    host.show()
    _app().processEvents()

    label.setText(("很长的一句说明文字，用来撑高气泡内容。" * 12).strip())
    content_h, vh = layout_speech_label_in_scroll(scroll, label)
    assert content_h > vh

    sync_speech_scroll(scroll, label, reveal_tail=False)
    bar = scroll.verticalScrollBar()
    assert bar is not None
    assert bar.maximum() > 0
    assert bar.value() == 0
    assert scroll.alignment() & Qt.AlignmentFlag.AlignTop

    sync_speech_scroll(scroll, label, reveal_tail=True)
    assert bar.value() == bar.maximum()
    host.close()


def test_short_text_label_uses_content_height_not_viewport() -> None:
    _app()
    host = QWidget()
    scroll, label = build_speech_text_scroll(host)
    host.resize(320, 160)
    scroll.setGeometry(0, 0, 300, 120)
    host.show()
    _app().processEvents()

    label.setText("短句")
    content_h, vh = layout_speech_label_in_scroll(scroll, label)
    assert content_h < vh
    assert label.height() == content_h
    label.setFixedSize(300, vh)
    assert measure_speech_label_height(label, 300) == content_h
    host.close()


def test_set_speech_instant_marks_segment_speech_done() -> None:
    _app()
    label = QLabel()
    done: list[str] = []

    class Voice:
        last_speak_duration_ms = None

        def speak_segment(self, *args, **kwargs) -> None:
            return None

        def prepare_next(self, *args, **kwargs) -> None:
            return None

        def discard_prepared(self) -> None:
            return None

    controller = SubtitleController(
        label,
        voice_playback=Voice(),
        subtitle_language="zh",
        log_stage=lambda *_a, **_k: None,
        apply_segment=lambda *_a, **_k: None,
        on_reply_completed=lambda: None,
        should_complete_reply=lambda: False,
    )
    controller.current_segment_sequence_id = 7
    controller.reply_sequence_id = 7
    controller.current_segment_speech_done = False

    def _mark(seq: int) -> None:
        done.append(f"speech:{seq}")
        controller.current_segment_speech_done = True

    controller._mark_segment_speech_done = _mark  # type: ignore[method-assign]
    controller.set_speech("一次出完整句。", instant=True)
    assert label.text() == "一次出完整句。"
    assert controller.speech_index == len("一次出完整句。")
    assert done == ["speech:7"]
    assert not controller.speech_timer.isActive()


def test_segment_tts_done_triggers_reveal_callback() -> None:
    calls: list[str] = []

    class Voice:
        last_speak_duration_ms = None

        def speak_segment(self, *args, **kwargs) -> None:
            return None

        def prepare_next(self, *args, **kwargs) -> None:
            return None

        def discard_prepared(self) -> None:
            return None

    controller = SubtitleController(
        QLabel(),
        voice_playback=Voice(),
        subtitle_language="zh",
        log_stage=lambda *_a, **_k: None,
        apply_segment=lambda *_a, **_k: None,
        on_reply_completed=lambda: None,
        should_complete_reply=lambda: False,
        on_segment_audio_finished=lambda: calls.append("reveal"),
    )
    controller.reply_sequence_id = 3
    controller.current_segment_sequence_id = 3
    controller.current_segment_speech_done = True
    controller.pending_reply_segments = []
    controller._mark_segment_tts_done(3)
    assert calls == ["reveal"]
    assert controller.current_segment_tts_done is True
