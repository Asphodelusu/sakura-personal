"""Action reading hold, plus remaining dwell for late dialogue translations."""

from __future__ import annotations

import pytest

from app.llm.chat_reply import ChatSegment


# Displayed text "（轻轻坐到你身边）": 9 visible non-whitespace chars.
# 400 + 40 * 9 = 760, already inside 600–1200.
ACTION_JA = "（そっと隣に座る）"
ACTION_ZH = "（轻轻坐到你身边）"
ACTION_HOLD_MS = 760

# Displayed text "（坐）": 3 chars → 400 + 40 * 3 = 520 → clamp 600.
SHORT_ACTION_JA = "（座る）"
SHORT_ACTION_ZH = "（坐）"
SHORT_ACTION_HOLD_MS = 600

ACTION_DEADLINE_MS = 12_000

DIALOGUE_JA = "……このままでいて。"
DIALOGUE_ZH = "……就这样待着。"
# Displayed "……就这样待着。": 8 visible non-whitespace chars.
# 500 + 80 * 8 = 1140, already inside 1000–2500.
DIALOGUE_ZH_DWELL_MS = 1140
# Displayed "……このままでいて。": 10 chars → 500 + 80 * 10 = 1300.
DIALOGUE_JA_DWELL_MS = 1300
NEWER_JA = "次はこっち。"
NEWER_ZH = "下一句。"
HI_JA = "おはよう。"
HI_ZH = "早安。"
# Displayed "早安。": 3 chars → 500 + 80 * 3 = 740 → clamp 1000.
HI_ZH_DWELL_MS = 1000
HEY_JA = "ねえ。"
HEY_ZH = "喂。"
PROGRESS_TEXT = "检索中…"

HOLD_TIMER_ATTR = "_action_reading_hold_timer"
DEADLINE_TIMER_ATTR = "_action_translation_deadline_timer"
DWELL_TIMER_ATTR = "_dialogue_reading_dwell_timer"


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


def _build_controller(
    label: _DummyLabel,
    tts: _DelayedTTS,
    applied: list[ChatSegment],
    *,
    subtitle_language: str = "zh",
    completed: list[str] | None = None,
    stages: list[tuple[str, object]] | None = None,
):
    from app.ui.subtitle_controller import SubtitleController
    from app.voice import VoicePlaybackController

    done = completed if completed is not None else []
    log_stages = stages if stages is not None else []

    controller = SubtitleController(
        label,  # type: ignore[arg-type]
        VoicePlaybackController(tts, lambda *_args, **_kwargs: None),
        subtitle_language,
        lambda stage, payload=None: log_stages.append((stage, payload)),
        applied.append,
        lambda: done.append("done"),
        lambda: True,
    )
    controller.set_display_speed(5, 0)
    return controller


def _action_segment(*, translation: str = "", suppress_tts: bool = True) -> ChatSegment:
    return ChatSegment(
        ACTION_JA,
        "温柔",
        translation,
        "站立待机",
        suppress_tts=suppress_tts,
    )


def _dialogue_segment(*, translation: str = DIALOGUE_ZH) -> ChatSegment:
    return ChatSegment(DIALOGUE_JA, "请求", translation, "站立待机")


def _iter_qtimers(controller):
    from PySide6.QtCore import QTimer

    seen: set[int] = set()
    for name in (
        HOLD_TIMER_ATTR,
        DEADLINE_TIMER_ATTR,
        DWELL_TIMER_ATTR,
        "_translation_gate_timer",
    ):
        timer = getattr(controller, name, None)
        if timer is not None:
            seen.add(id(timer))
            yield timer
    for child in controller.findChildren(QTimer):
        if id(child) not in seen:
            yield child


def _require_timer(controller, interval_ms: int, preferred_attr: str):
    preferred = getattr(controller, preferred_attr, None)
    if preferred is not None and int(preferred.interval()) == interval_ms:
        return preferred
    matches = [
        timer
        for timer in _iter_qtimers(controller)
        if int(timer.interval()) == interval_ms
    ]
    active = [timer for timer in matches if timer.isActive()]
    chosen = active or matches
    assert chosen, (
        f"expected {preferred_attr} at {interval_ms} ms, "
        f"got {[(getattr(timer, 'interval', lambda: None)()) for timer in _iter_qtimers(controller)]}"
    )
    return chosen[0]


def _fire_timer(timer) -> None:
    timer.stop()
    timer.timeout.emit()


def test_already_translated_action_waits_hand_calculated_reading_hold() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    applied: list[ChatSegment] = []
    controller = _build_controller(label, tts, applied)
    action = _action_segment(translation=ACTION_ZH)
    dialogue = _dialogue_segment()

    controller.show_segments([action, dialogue])

    assert label.text == ACTION_ZH
    assert tts.spoken == []
    QCoreApplication.processEvents()
    assert label.text == ACTION_ZH
    assert controller.current_segment is action
    assert DIALOGUE_ZH not in label.text
    hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()

    _fire_timer(hold)
    if tts.on_started is not None:
        tts.on_started()
    QCoreApplication.processEvents()

    assert label.text == DIALOGUE_ZH
    controller.cancel_reply_flow()


def test_short_action_reading_hold_clamps_to_600() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    action = ChatSegment(
        SHORT_ACTION_JA,
        "温柔",
        SHORT_ACTION_ZH,
        "站立待机",
        suppress_tts=True,
    )

    controller.show_segments([action, _dialogue_segment()])
    QCoreApplication.processEvents()

    assert label.text == SHORT_ACTION_ZH
    hold = _require_timer(controller, SHORT_ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()
    controller.cancel_reply_flow()


def test_missing_zh_action_stays_held_after_ordinary_gate_timeout() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])

    controller.start_waiting_indicator()
    waiting_text = label.text
    controller.begin_translation_gate(timeout_seconds=0, interaction_id="turn-action")
    controller.show_segments([_action_segment(), _dialogue_segment()])

    assert controller.waiting_indicator_active
    assert ACTION_JA not in label.text
    assert ACTION_ZH not in label.text
    assert not controller.current_segment_speech_done

    QCoreApplication.processEvents()

    assert controller.waiting_indicator_active
    assert not controller.current_segment_speech_done
    assert ACTION_JA not in label.text
    assert DIALOGUE_ZH not in label.text
    assert DIALOGUE_JA not in label.text
    assert label.text in {waiting_text, ".", "..", "...", "....", ".....", "......"}
    controller.cancel_reply_flow()


def test_action_translation_success_shows_chinese_then_holds() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    action = _action_segment()
    dialogue = _dialogue_segment()

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([action, dialogue])
    assert controller.waiting_indicator_active

    updated = _action_segment(translation=ACTION_ZH)
    controller.current_segment = updated
    controller.release_translation_gate(fallback=False)
    controller.set_speech(updated.display_text("zh"), pulse=False, instant=True)

    assert label.text == ACTION_ZH
    assert ACTION_JA not in label.text
    assert not controller.waiting_indicator_active
    QCoreApplication.processEvents()
    assert label.text == ACTION_ZH
    assert DIALOGUE_ZH not in label.text
    hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()

    _fire_timer(hold)
    if tts.on_started is not None:
        tts.on_started()
    QCoreApplication.processEvents()
    assert label.text == DIALOGUE_ZH
    controller.cancel_reply_flow()


def test_action_translation_failure_shows_japanese_then_holds() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([_action_segment(), _dialogue_segment()])

    controller.release_translation_gate(fallback=True)

    assert ACTION_JA in label.text
    assert ACTION_ZH not in label.text
    QCoreApplication.processEvents()
    assert ACTION_JA in label.text
    assert DIALOGUE_JA not in label.text
    hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()
    controller.cancel_reply_flow()


def test_action_translation_hard_deadline_shows_japanese_then_holds() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=0, interaction_id="turn-deadline")
    controller.show_segments([_action_segment(), _dialogue_segment()])
    QCoreApplication.processEvents()
    assert controller.waiting_indicator_active
    assert ACTION_JA not in label.text

    deadline = _require_timer(controller, ACTION_DEADLINE_MS, DEADLINE_TIMER_ATTR)
    assert deadline.isActive()
    _fire_timer(deadline)

    assert ACTION_JA in label.text
    assert ACTION_ZH not in label.text
    QCoreApplication.processEvents()
    assert ACTION_JA in label.text
    assert DIALOGUE_JA not in label.text
    hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()
    controller.cancel_reply_flow()


def test_translated_dialogue_visible_during_tts_keeps_existing_advance_timing() -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtTest import QTest

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    first = ChatSegment(HI_JA, "开心", HI_ZH, "站立待机")
    second = ChatSegment(HEY_JA, "温柔", HEY_ZH, "站立待机")

    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    assert label.text == HI_ZH
    # TTS already covers the 1000 ms minimum for "早安。"; remaining dwell is 0.
    QTest.qWait(HI_ZH_DWELL_MS + 200)
    tts.on_finished()
    QCoreApplication.processEvents()
    assert tts.on_started is not None
    tts.on_started()

    assert label.text == HEY_ZH
    hold = getattr(controller, HOLD_TIMER_ATTR, None)
    assert hold is None or not hold.isActive()
    dwell = getattr(controller, DWELL_TIMER_ATTR, None)
    assert dwell is None or not dwell.isActive()
    controller.cancel_reply_flow()


def test_late_translated_dialogue_dwells_after_tts_completion() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    first = ChatSegment(DIALOGUE_JA, "请求", "", "站立待机")
    second = ChatSegment(HEY_JA, "温柔", HEY_ZH, "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    tts.on_finished()
    assert controller.waiting_indicator_active
    assert DIALOGUE_ZH not in label.text
    assert DIALOGUE_JA not in label.text

    updated = ChatSegment(DIALOGUE_JA, "请求", DIALOGUE_ZH, "站立待机")
    controller.current_segment = updated
    controller.release_translation_gate(fallback=False)
    controller.set_speech(updated.display_text("zh"), pulse=False, instant=True)

    assert label.text == DIALOGUE_ZH
    QCoreApplication.processEvents()
    assert label.text == DIALOGUE_ZH
    assert HEY_ZH not in label.text
    dwell = _require_timer(controller, DIALOGUE_ZH_DWELL_MS, DWELL_TIMER_ATTR)
    assert dwell.isActive()
    hold = getattr(controller, HOLD_TIMER_ATTR, None)
    assert hold is None or not hold.isActive()

    _fire_timer(dwell)
    if tts.on_started is not None:
        tts.on_started()
    QCoreApplication.processEvents()
    assert label.text == HEY_ZH
    controller.cancel_reply_flow()


def test_late_fallback_dialogue_dwells_after_tts_completion() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    first = ChatSegment(DIALOGUE_JA, "请求", "", "站立待机")
    second = ChatSegment(HEY_JA, "温柔", "", "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    tts.on_finished()
    assert not controller.current_segment_speech_done

    controller.release_translation_gate(fallback=True)

    assert DIALOGUE_JA in label.text
    QCoreApplication.processEvents()
    assert DIALOGUE_JA in label.text
    assert HEY_JA not in label.text
    dwell = _require_timer(controller, DIALOGUE_JA_DWELL_MS, DWELL_TIMER_ATTR)
    assert dwell.isActive()

    _fire_timer(dwell)
    if tts.on_started is not None:
        tts.on_started()
    QCoreApplication.processEvents()
    assert HEY_JA in label.text
    controller.cancel_reply_flow()


def test_late_dialogue_dwell_clamps_to_1000() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    first = ChatSegment(HI_JA, "开心", "", "站立待机")
    second = ChatSegment(HEY_JA, "温柔", HEY_ZH, "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    tts.on_finished()
    updated = ChatSegment(HI_JA, "开心", HI_ZH, "站立待机")
    controller.current_segment = updated
    controller.release_translation_gate(fallback=False)
    controller.set_speech(updated.display_text("zh"), pulse=False, instant=True)
    QCoreApplication.processEvents()

    assert label.text == HI_ZH
    assert HEY_ZH not in label.text
    dwell = _require_timer(controller, HI_ZH_DWELL_MS, DWELL_TIMER_ATTR)
    assert dwell.isActive()
    controller.cancel_reply_flow()


def test_stale_dialogue_dwell_cannot_advance_newer_reply() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    first = ChatSegment(DIALOGUE_JA, "请求", "", "站立待机")
    second = ChatSegment(HEY_JA, "温柔", HEY_ZH, "站立待机")

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6)
    controller.show_segments([first, second])
    assert tts.on_started is not None
    tts.on_started()
    tts.on_finished()
    updated = ChatSegment(DIALOGUE_JA, "请求", DIALOGUE_ZH, "站立待机")
    controller.current_segment = updated
    controller.release_translation_gate(fallback=False)
    controller.set_speech(updated.display_text("zh"), pulse=False, instant=True)
    stale_dwell = _require_timer(controller, DIALOGUE_ZH_DWELL_MS, DWELL_TIMER_ATTR)

    controller.start_waiting_indicator()
    controller.show_segments([ChatSegment(NEWER_JA, "中性", NEWER_ZH, "站立待机")])
    assert tts.on_started is not None
    tts.on_started()
    assert label.text == NEWER_ZH

    _fire_timer(stale_dwell)
    QCoreApplication.processEvents()

    assert label.text == NEWER_ZH
    assert HEY_ZH not in label.text
    controller.cancel_reply_flow()


def test_generic_silent_progress_does_not_use_action_reading_hold() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])
    progress = ChatSegment(PROGRESS_TEXT, "中性", "", suppress_tts=True)
    dialogue = _dialogue_segment()

    controller.show_segments([progress, dialogue])
    QCoreApplication.processEvents()
    if tts.on_started is not None:
        tts.on_started()

    assert DIALOGUE_ZH in label.text
    hold = getattr(controller, HOLD_TIMER_ATTR, None)
    assert hold is None or not hold.isActive()
    controller.cancel_reply_flow()


def test_japanese_subtitle_action_displays_immediately_then_holds() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [], subtitle_language="ja")

    controller.show_segments([_action_segment(translation=ACTION_ZH), _dialogue_segment()])
    QCoreApplication.processEvents()

    assert label.text == ACTION_JA
    assert ACTION_ZH not in label.text
    hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    assert hold.isActive()
    controller.cancel_reply_flow()


def test_stale_action_hold_token_cannot_advance_newer_reply() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])

    controller.show_segments([_action_segment(translation=ACTION_ZH), _dialogue_segment()])
    QCoreApplication.processEvents()
    stale_hold = _require_timer(controller, ACTION_HOLD_MS, HOLD_TIMER_ATTR)
    stale_seq = controller.reply_sequence_id
    stale_token = controller.reply_advance_token

    controller.start_waiting_indicator()
    newer = ChatSegment(NEWER_JA, "中性", NEWER_ZH, "站立待机")
    controller.show_segments([newer])
    assert tts.on_started is not None
    tts.on_started()
    shown = label.text
    assert shown == NEWER_ZH

    _fire_timer(stale_hold)
    controller._show_scheduled_next_reply_segment(stale_seq, stale_token)
    QCoreApplication.processEvents()

    assert label.text == NEWER_ZH
    assert DIALOGUE_ZH not in label.text
    assert ACTION_ZH not in label.text
    controller.cancel_reply_flow()


def test_stale_action_deadline_cannot_rewind_newer_reply() -> None:
    from PySide6.QtCore import QCoreApplication

    _qt_app_or_skip()
    label = _DummyLabel()
    tts = _DelayedTTS()
    controller = _build_controller(label, tts, [])

    controller.start_waiting_indicator()
    controller.begin_translation_gate(timeout_seconds=6, interaction_id="old-turn")
    controller.show_segments([_action_segment()])
    QCoreApplication.processEvents()
    stale_deadline = _require_timer(controller, ACTION_DEADLINE_MS, DEADLINE_TIMER_ATTR)

    controller.start_waiting_indicator()
    controller.show_segments([ChatSegment(NEWER_JA, "中性", NEWER_ZH, "站立待机")])
    assert tts.on_started is not None
    tts.on_started()
    assert label.text == NEWER_ZH

    _fire_timer(stale_deadline)
    QCoreApplication.processEvents()

    assert label.text == NEWER_ZH
    assert ACTION_JA not in label.text
    controller.cancel_reply_flow()
