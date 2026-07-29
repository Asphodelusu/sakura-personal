# -*- coding: utf-8 -*-
"""气泡 orphan 收尾：Observer / SPEAKING 超时 / 空分段。"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from app.llm.chat_reply import ChatSegment
from app.ui.pet_window import PetWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_proactive_playback_finish_refreshes_history_buttons_without_interaction() -> None:
    _app()
    calls: list[object] = []

    class Stub:
        _on_reply_playback_finished = PetWindow._on_reply_playback_finished
        _finalize_orphan_playback = PetWindow._finalize_orphan_playback
        active_interaction_id = ""
        last_proactive_interaction_at = 0.0
        _portrait_reset_timer = None
        portrait_controller = SimpleNamespace(reset_to_default=lambda: None)
        ui_state = SimpleNamespace(
            finish=lambda outcome: calls.append(("finish", outcome))
        )
        bubble_auto_hide = SimpleNamespace(
            notify_settled=lambda: calls.append("settled")
        )

        def _end_interaction(self, outcome: str) -> None:
            calls.append(("end", outcome))

        def _stop_speaking_state_watchdog(self) -> None:
            calls.append("stop_wd")

        def _collapse_auto_fit_bubble_height(self) -> None:
            calls.append("collapse")

        def _update_reply_history_buttons(self) -> None:
            calls.append("buttons")

    stub = Stub()
    stub._on_reply_playback_finished()
    assert ("end", "reply_completed") not in calls
    assert ("finish", "proactive_reply_completed") in calls
    assert "buttons" in calls
    assert "settled" in calls
    assert "collapse" in calls


def test_normal_playback_finish_still_ends_interaction() -> None:
    calls: list[object] = []

    class Stub:
        _on_reply_playback_finished = PetWindow._on_reply_playback_finished
        active_interaction_id = "interaction-1"

        def _end_interaction(self, outcome: str) -> None:
            calls.append(("end", outcome))

    Stub()._on_reply_playback_finished()
    assert calls == [("end", "reply_completed")]


def test_speaking_timeout_without_interaction_refreshes_buttons() -> None:
    from app.ui.state import PetUiState

    calls: list[object] = []

    class SubtitleStub:
        def is_reply_sequence_active(self) -> bool:
            return True

        def cancel_reply_flow(self) -> None:
            calls.append("cancel")

    class Stub:
        _handle_speaking_state_timeout = PetWindow._handle_speaking_state_timeout
        _finalize_orphan_playback = PetWindow._finalize_orphan_playback
        active_interaction_id = ""
        last_proactive_interaction_at = 0.0
        _portrait_reset_timer = None
        portrait_controller = SimpleNamespace(reset_to_default=lambda: None)
        subtitle_controller = SubtitleStub()
        ui_state = SimpleNamespace(
            state=PetUiState.SPEAKING,
            finish=lambda outcome: calls.append(("finish", outcome)),
        )
        bubble_auto_hide = SimpleNamespace(
            notify_settled=lambda: calls.append("settled")
        )

        def _collapse_auto_fit_bubble_height(self) -> None:
            calls.append("collapse")

        def _stop_speaking_state_watchdog(self) -> None:
            calls.append("stop_wd")

        def _update_reply_history_buttons(self) -> None:
            calls.append("buttons")

        def _end_interaction(self, outcome: str) -> None:
            calls.append(("end", outcome))

    Stub()._handle_speaking_state_timeout()
    assert "cancel" in calls
    assert ("end", "speaking_timeout") not in calls
    assert ("finish", "speaking_timeout") in calls
    assert "buttons" in calls
    assert "settled" in calls


def test_empty_reply_segments_ends_interaction() -> None:
    calls: list[object] = []

    class Stub:
        _show_reply_segments = PetWindow._show_reply_segments
        active_interaction_id = "interaction-9"
        character_profile = None
        subtitle_controller = SimpleNamespace(show_segments=lambda segs: calls.append(("show", segs)))

        def _cancel_backchannel(self) -> None:
            calls.append("cancel_bc")

        def _exit_reply_history_review(self, update_buttons: bool = True) -> None:
            calls.append(("exit_review", update_buttons))

        def _remember_reply_history_segments(self, segments) -> None:
            calls.append(("remember", len(segments)))

        def _end_interaction(self, outcome: str) -> None:
            calls.append(("end", outcome))

        def _finalize_orphan_playback(self, outcome: str, **kwargs) -> None:
            calls.append(("orphan", outcome))

    Stub()._show_reply_segments(
        [ChatSegment("", "中性", "", "站立待机"), ChatSegment("   ", "中性", "", "站立待机")]
    )
    assert ("end", "empty_reply") in calls
    assert not any(isinstance(c, tuple) and c[0] == "show" for c in calls)


def test_history_review_does_not_block_proactive_observer() -> None:
    """翻历史只改展示，不能永久挡住 Observer。"""
    class Stub:
        _proactive_observer_busy_reason = PetWindow._proactive_observer_busy_reason
        startup_initializing = False
        worker_thread = None
        active_reminder_id = None
        active_event_type = ""
        pending_tool_action = None
        pending_screen_observation_messages = None
        screen_observation_followup_in_progress = False
        screen_observation_encode_thread = None
        active_interaction_id = ""
        reply_history_review_active = True
        input_edit = SimpleNamespace(hasFocus=lambda: False)
        speech_timer = SimpleNamespace(isActive=lambda: False)
        last_proactive_interaction_at = 0.0
        subtitle_controller = None

    import time

    stub = Stub()
    stub.last_proactive_interaction_at = time.perf_counter() - 10_000
    assert stub._proactive_observer_busy_reason() == ""


def test_reply_history_at_latest_exits_review() -> None:
    calls: list[object] = []
    segments = [
        ChatSegment("旧", "中性", "", "站立待机"),
        ChatSegment("新", "中性", "", "站立待机"),
    ]

    class Stub:
        _show_reply_history_at = PetWindow._show_reply_history_at
        reply_history_segments = segments
        reply_history_index = 0
        reply_history_review_active = False
        subtitle_language = "zh"
        portrait_controller = SimpleNamespace(
            apply_for_segment=lambda seg: calls.append(("portrait", seg.text))
        )
        subtitle_controller = SimpleNamespace(
            show_text_immediately=lambda text: calls.append(("text", text))
        )

        def _can_review_reply_history(self) -> bool:
            return True

        def _log_interaction_stage(self, *args, **kwargs) -> None:
            return None

        def _update_reply_history_buttons(self) -> None:
            calls.append("buttons")

    stub = Stub()
    stub._show_reply_history_at(0)
    assert stub.reply_history_review_active is True
    stub._show_reply_history_at(1)
    assert stub.reply_history_review_active is False
