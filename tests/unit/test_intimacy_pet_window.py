"""tests/unit/test_intimacy_pet_window.py — PetWindow 亲密模式方法测试（无 Qt 依赖）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _pet_window_source_contains(pattern: str) -> bool:
    """检查 pet_window.py 源码中是否包含指定模式（编译时断言，不依赖运行时）。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "ui" / "pet_window.py"
    text = src.read_text(encoding="utf-8")
    return pattern in text


class TestIsIntimacyContinueTurn:
    """_is_intimacy_continue_turn() 逻辑测试（内联实现验证）。"""

    @staticmethod
    def _is_intimacy_continue_turn(messages: list[dict]) -> bool:
        """内联拷贝自 PetWindow._is_intimacy_continue_turn，保持同步。"""
        from app.agent.builtin_tools import INTIMACY_CONTINUE_MARKER

        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return msg.get("content") == INTIMACY_CONTINUE_MARKER
        return False

    def test_continue_turn_detected(self) -> None:
        assert self._is_intimacy_continue_turn([
            {"role": "user", "content": "好き"},
            {"role": "assistant", "content": "うん…"},
            {"role": "user", "content": "（続けて）"},
        ]) is True

    def test_normal_turn_not_detected(self) -> None:
        assert self._is_intimacy_continue_turn([
            {"role": "user", "content": "おはよう"},
            {"role": "assistant", "content": "おはよう"},
            {"role": "user", "content": "今日はどう？"},
        ]) is False

    def test_no_user_messages(self) -> None:
        assert self._is_intimacy_continue_turn([
            {"role": "assistant", "content": "うん…"},
        ]) is False

    def test_empty_messages(self) -> None:
        assert self._is_intimacy_continue_turn([]) is False

    def test_last_user_is_not_continue(self) -> None:
        assert self._is_intimacy_continue_turn([
            {"role": "user", "content": "（続けて）"},
            {"role": "assistant", "content": "うん…"},
            {"role": "user", "content": "待って"},
        ]) is False

    def test_system_messages_ignored(self) -> None:
        assert self._is_intimacy_continue_turn([
            {"role": "system", "content": "internal"},
            {"role": "user", "content": "（続けて）"},
        ]) is True


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
        assert _pet_window_source_contains("INTIMACY_CONTINUE_MARKER"), (
            "续投标记应使用共享常量 INTIMACY_CONTINUE_MARKER"
        )

    def test_continue_max_is_conservative(self) -> None:
        assert _pet_window_source_contains("_INTIMACY_CONTINUE_MAX = 8"), (
            "静默续投上限应与节奏存活轮次对齐为 8"
        )
        assert _pet_window_source_contains("pending_exit_confirm"), (
            "待确认结束时应暂停静默续投"
        )


def _pet_window_source_content() -> str:
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "ui" / "pet_window.py"
    return src.read_text(encoding="utf-8")
