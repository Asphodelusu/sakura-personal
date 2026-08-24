"""tests/unit/test_intimacy_pet_window.py — PetWindow 亲密模式方法测试（无 Qt 依赖）。"""

from __future__ import annotations

import pytest

from app.agent.builtin_tools import (
    build_intimacy_continue_message,
    latest_is_intimacy_continue,
    message_is_intimacy_continue,
)


def _pet_window_source_contains(pattern: str) -> bool:
    """检查 pet_window.py 源码中是否包含指定模式（编译时断言，不依赖运行时）。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "ui" / "pet_window.py"
    text = src.read_text(encoding="utf-8")
    return pattern in text


def _pet_window_source_content() -> str:
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "ui" / "pet_window.py"
    return src.read_text(encoding="utf-8")


def _is_intimacy_continue_turn(messages: list[dict]) -> bool:
    """与 PetWindow._is_intimacy_continue_turn 对齐的纯函数实现。"""
    if latest_is_intimacy_continue(messages):
        return True
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        if role == "assistant":
            continue
        return message_is_intimacy_continue(msg)
    return False


class TestIsIntimacyContinueTurn:
    """_is_intimacy_continue_turn() 逻辑测试。"""

    def test_continue_turn_detected_system(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    {"role": "user", "content": "好き"},
                    {"role": "assistant", "content": "うん…"},
                    build_intimacy_continue_message(),
                ]
            )
            is True
        )

    def test_continue_turn_detected_legacy_user(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    {"role": "user", "content": "好き"},
                    {"role": "assistant", "content": "うん…"},
                    {"role": "user", "content": "（続けて）"},
                ]
            )
            is True
        )

    def test_continue_after_assistant_reply(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    build_intimacy_continue_message(),
                    {"role": "assistant", "content": "うん…"},
                ]
            )
            is True
        )

    def test_normal_turn_not_detected(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    {"role": "user", "content": "おはよう"},
                    {"role": "assistant", "content": "おはよう"},
                    {"role": "user", "content": "今日はどう？"},
                ]
            )
            is False
        )

    def test_no_user_messages(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    {"role": "assistant", "content": "うん…"},
                ]
            )
            is False
        )

    def test_empty_messages(self) -> None:
        assert _is_intimacy_continue_turn([]) is False

    def test_last_user_is_not_continue(self) -> None:
        assert (
            _is_intimacy_continue_turn(
                [
                    {"role": "user", "content": "（続けて）"},
                    {"role": "assistant", "content": "うん…"},
                    {"role": "user", "content": "待って"},
                ]
            )
            is False
        )


class TestObserverBusyGate:
    """Observer 忙碌门含亲密模式编译时检查。"""

    def test_intimacy_check_in_busy_reason(self) -> None:
        assert _pet_window_source_contains("intimacy_mode_state"), (
            "_proactive_observer_busy_reason 应导入并检查 intimacy_mode_state"
        )
        assert _pet_window_source_contains('"rhythm_focus"'), (
            "_proactive_observer_busy_reason 应在 active 时返回中性标签 'rhythm_focus'"
        )


class TestMemoryTurnSkip:
    """续投不累计记忆整理轮次编译时检查。"""

    def test_is_intimacy_continue_turn_called_in_end_interaction(self) -> None:
        assert _pet_window_source_contains("_is_intimacy_continue_turn"), (
            "_end_interaction 应调用 _is_intimacy_continue_turn 判断是否跳过记忆轮次"
        )

    def test_record_completed_memory_turn_guarded(self) -> None:
        src_check = (
            "_is_intimacy_continue_turn" in _pet_window_source_content()
            and "_record_completed_memory_turn" in _pet_window_source_content()
        )
        assert src_check, "记忆轮次应被 _is_intimacy_continue_turn 守卫"


class TestContinueDoesNotResetLifetime:
    """续投不得再调用 enter() 重置自动退出计数。"""

    def test_continue_handler_does_not_reenter(self) -> None:
        assert not _pet_window_source_contains("intimacy_mode_state.enter()"), (
            "续投计时器不应再调用 enter()，否则会拖长误开寿命"
        )
        assert _pet_window_source_contains("build_intimacy_continue_message"), (
            "续投应使用 build_intimacy_continue_message() 写入 system 信号"
        )

    def test_continue_does_not_reference_pending_exit_confirm(self) -> None:
        assert not _pet_window_source_contains("pending_exit_confirm"), (
            "已删除待确认结束机制（模型自管退出），不应再引用 pending_exit_confirm"
        )


class _FakeContinueTimer:
    def __init__(self) -> None:
        self.started_ms: int | None = None
        self.stopped = False

    def start(self, ms: int) -> None:
        self.started_ms = int(ms)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True
        self.started_ms = None


class _FakeSpeechTimer:
    def isActive(self) -> bool:
        return False


def _bind_pet_window_method(window: object, name: str) -> None:
    from app.ui.pet_window import PetWindow

    method = getattr(PetWindow, name, None)
    assert method is not None, f"PetWindow 缺少 {name}"
    setattr(window, name, method.__get__(window))


def _make_intimacy_continue_window():
    from types import SimpleNamespace

    from app.agent.builtin_tools import intimacy_mode_state
    from app.ui.pet_window import PetWindow

    started: list[list] = []
    window = SimpleNamespace(
        _intimacy_continue_count=0,
        _intimacy_continue_epoch=int(getattr(intimacy_mode_state, "continuation_epoch", 0) or 0),
        _intimacy_continue_timer_epoch=int(
            getattr(intimacy_mode_state, "continuation_epoch", 0) or 0
        ),
        _intimacy_continue_timer_generation=0,
        _intimacy_was_active=True,
        _intimacy_continue_timer=_FakeContinueTimer(),
        messages=[],
        active_interaction_id="",
        worker_thread=None,
        subtitle_controller=None,
        speech_timer=_FakeSpeechTimer(),
        _INTIMACY_CONTINUE_DELAYS_MS=getattr(PetWindow, "_INTIMACY_CONTINUE_DELAYS_MS", ()),
    )
    for name in (
        "_next_intimacy_continue_delay_ms",
        "_schedule_intimacy_continue",
        "_on_intimacy_continue_timer",
        "_cancel_intimacy_continue",
    ):
        _bind_pet_window_method(window, name)
    window._begin_interaction = lambda _source: None
    window._start_chat_worker = started.append
    return window, started


def _track_expire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from app.agent.builtin_tools import intimacy_mode_state

    expire_calls: list[str] = []
    original_expire = intimacy_mode_state.expire_after_silence

    def _tracked() -> None:
        expire_calls.append("expire")
        original_expire()

    monkeypatch.setattr(intimacy_mode_state, "expire_after_silence", _tracked)
    return expire_calls


def _make_end_interaction_window():
    from types import SimpleNamespace

    window, started = _make_intimacy_continue_window()
    _bind_pet_window_method(window, "_end_interaction")
    _bind_pet_window_method(window, "_is_intimacy_continue_turn")
    window.active_interaction_id = "intimacy-continue"
    window.active_interaction_started_at = None
    window.active_interaction_last_at = None
    window._portrait_reset_timer = None
    window.bubble_auto_hide = None
    window.ui_state = SimpleNamespace(finish=lambda _outcome: None)
    window._log_interaction_stage = lambda *_args, **_kwargs: None
    window._emit_bus_event = lambda *_args, **_kwargs: None
    window._update_reply_history_buttons = lambda: None
    window._collapse_auto_fit_bubble_height = lambda: None
    window._record_completed_memory_turn = lambda: None
    window.messages = [build_intimacy_continue_message()]
    return window, started


class TestProgressiveContinueDelays:
    """三步静默续投延迟：20s / 35s / 60s，第四步停止。"""

    def test_next_delay_follows_three_step_budget(self) -> None:
        window, _started = _make_intimacy_continue_window()
        assert window._next_intimacy_continue_delay_ms() == 20_000
        window._intimacy_continue_count = 1
        assert window._next_intimacy_continue_delay_ms() == 35_000
        window._intimacy_continue_count = 2
        assert window._next_intimacy_continue_delay_ms() == 60_000
        window._intimacy_continue_count = 3
        assert window._next_intimacy_continue_delay_ms() is None


class TestSleepAfterThirdContinuationPlayback:
    """第三档耗尽只取消 timer 进入静默休眠；真实用户周期才把续投计数归零。"""

    def setup_method(self) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        intimacy_mode_state.exit()
        intimacy_mode_state.enter()

    def teardown_method(self) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        intimacy_mode_state.exit()

    def test_third_continuation_playback_sleeps_without_expire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agent.builtin_tools import intimacy_mode_state, message_is_intimacy_continue

        window, started = _make_intimacy_continue_window()
        expire_calls = _track_expire(monkeypatch)

        # 用户首轮回复播完：只排队第一段延迟，模式仍 active（tone/prompt 尚未切走）。
        window._intimacy_continue_count = 0
        window._schedule_intimacy_continue()
        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert window._intimacy_continue_timer.started_ms == 20_000

        # 第一、第二轮续投回复播完：继续排队，仍不 expire。
        window._intimacy_continue_count = 1
        window._schedule_intimacy_continue()
        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert window._intimacy_continue_timer.started_ms == 35_000

        window._intimacy_continue_count = 2
        window._schedule_intimacy_continue()
        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert window._intimacy_continue_timer.started_ms == 60_000

        # 第三轮计时到期：写入续投信号时仍须 active，供该轮 prompt/tone 选择。
        window._on_intimacy_continue_timer()
        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert window._intimacy_continue_count == 3
        assert started
        assert message_is_intimacy_continue(window.messages[-1])

        # 第三轮续投回复播完后进入静默休眠：取消 timer，保持 active，不追加第四条信号。
        before = len(window.messages)
        window._schedule_intimacy_continue()
        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert intimacy_mode_state.needs_reentry_hint is False
        assert window._intimacy_continue_count == 3
        assert window._intimacy_continue_timer.stopped is True
        window._on_intimacy_continue_timer()
        assert len(window.messages) == before
        assert len(started) == 1

    def test_user_cycle_after_sleep_restarts_first_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        window, _started = _make_intimacy_continue_window()
        expire_calls = _track_expire(monkeypatch)
        window._intimacy_continue_count = 3
        window._schedule_intimacy_continue()
        assert expire_calls == []
        assert window._intimacy_continue_count == 3

        intimacy_mode_state.refresh_user_reply()
        window._schedule_intimacy_continue()

        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert window._intimacy_continue_count == 0
        assert window._intimacy_continue_timer.started_ms == 20_000
        window._intimacy_continue_count = 1
        window._schedule_intimacy_continue()
        assert window._intimacy_continue_timer.started_ms == 35_000
        window._intimacy_continue_count = 2
        window._schedule_intimacy_continue()
        assert window._intimacy_continue_timer.started_ms == 60_000

    def test_system_continue_does_not_reset_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        window, started = _make_intimacy_continue_window()
        expire_calls = _track_expire(monkeypatch)
        window._intimacy_continue_count = 1
        epoch_before = getattr(intimacy_mode_state, "continuation_epoch", None)
        intimacy_mode_state.consume_turn()
        window._schedule_intimacy_continue()

        assert expire_calls == []
        assert getattr(intimacy_mode_state, "continuation_epoch", None) == epoch_before
        assert window._intimacy_continue_count == 1
        assert window._intimacy_continue_timer.started_ms == 35_000
        assert started == []

    @pytest.mark.parametrize("outcome", ["empty_reply", "error", "speaking_timeout"])
    def test_third_continuation_terminal_outcomes_sleep(
        self, outcome: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        window, _started = _make_end_interaction_window()
        expire_calls = _track_expire(monkeypatch)
        window._intimacy_continue_count = 3

        window._end_interaction(outcome)

        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert intimacy_mode_state.needs_reentry_hint is False
        assert window._intimacy_continue_count == 3

    @pytest.mark.parametrize("outcome", ["empty_reply", "error", "speaking_timeout"])
    def test_pre_third_terminal_outcomes_do_not_sleep(
        self, outcome: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        window, started = _make_end_interaction_window()
        expire_calls = _track_expire(monkeypatch)
        window._intimacy_continue_count = 2

        window._end_interaction(outcome)

        assert expire_calls == []
        assert intimacy_mode_state.active is True
        assert started == []

    def test_inactive_schedule_cancels_pending_timer(self) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        intimacy_mode_state.exit()
        window, _started = _make_intimacy_continue_window()
        window._intimacy_continue_timer.start(20_000)

        window._schedule_intimacy_continue()

        assert window._intimacy_continue_timer.stopped is True
        assert window._intimacy_continue_count == 0

    def test_stale_timer_from_previous_user_cycle_is_ignored(self) -> None:
        from app.agent.builtin_tools import intimacy_mode_state

        window, started = _make_intimacy_continue_window()
        window._schedule_intimacy_continue()
        scheduled_epoch = window._intimacy_continue_timer_epoch
        stale_generation = window._intimacy_continue_timer_generation

        intimacy_mode_state.refresh_user_reply()
        assert intimacy_mode_state.continuation_epoch != scheduled_epoch
        window._schedule_intimacy_continue()
        assert window._intimacy_continue_timer_generation != stale_generation

        window._on_intimacy_continue_timer(stale_generation)

        assert started == []
        assert window.messages == []
        assert window._intimacy_continue_count == 0


class _EmptyInput:
    def text(self) -> str:
        return ""


def test_empty_send_does_not_cancel_pending_intimacy_timer() -> None:
    from types import SimpleNamespace

    timer = _FakeContinueTimer()
    timer.start(35_000)
    window = SimpleNamespace(
        startup_initializing=False,
        _intimacy_continue_timer=timer,
        input_edit=_EmptyInput(),
        pending_manual_screen_observation=None,
        worker_thread=None,
        active_interaction_id="",
        _log_interaction_stage=lambda *_args, **_kwargs: None,
    )
    _bind_pet_window_method(window, "send_message")

    window.send_message()

    assert timer.stopped is False
    assert timer.started_ms == 35_000
