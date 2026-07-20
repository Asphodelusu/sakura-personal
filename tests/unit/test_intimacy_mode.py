"""tests/unit/test_intimacy_mode.py — 亲密模式状态机与工具测试。"""

from __future__ import annotations

import pytest

from app.agent.builtin_tools import IntimacyModeState, _handle_set_intimacy_mode, intimacy_mode_state


class TestIntimacyModeState:
    """IntimacyModeState 状态机基础测试。"""

    def test_initial_state(self) -> None:
        state = IntimacyModeState()
        assert state.active is False
        assert state.consume_turn() is False

    def test_enter_exit(self) -> None:
        state = IntimacyModeState()
        state.enter()
        assert state.active is True

        state.exit()
        assert state.active is False

    def test_consume_turn_decrements(self) -> None:
        state = IntimacyModeState()
        state.enter()  # _AUTO_EXIT_TURNS = 6

        for _ in range(5):
            assert state.consume_turn() is True
            assert state.active is True

        # 第 6 次耗尽
        assert state.consume_turn() is False
        assert state.active is False

    def test_reenter_resets_counter(self) -> None:
        state = IntimacyModeState()
        state.enter()
        # 消耗 3 轮
        for _ in range(3):
            state.consume_turn()

        # 重新进入重置计数
        state.enter()
        for _ in range(5):
            assert state.consume_turn() is True
        assert state.consume_turn() is False

    def test_consume_when_inactive_returns_false(self) -> None:
        state = IntimacyModeState()
        assert state.consume_turn() is False
        # 不应修改 active
        assert state.active is False

    def test_exit_then_consume(self) -> None:
        state = IntimacyModeState()
        state.enter()
        state.exit()
        assert state.active is False
        assert state.consume_turn() is False

    def test_multiple_enter_is_idempotent(self) -> None:
        state = IntimacyModeState()
        state.enter()
        state.enter()
        state.enter()
        # 应仍是 6
        for _ in range(5):
            assert state.consume_turn() is True
        assert state.consume_turn() is False


class TestHandleSetIntimacyMode:
    """_handle_set_intimacy_mode 工具处理器测试。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def test_turn_on(self) -> None:
        result = _handle_set_intimacy_mode({"on": True})
        assert result == {"intimacy_mode": "on"}
        assert intimacy_mode_state.active is True

    def test_turn_off(self) -> None:
        intimacy_mode_state.enter()
        result = _handle_set_intimacy_mode({"on": False})
        assert result == {"intimacy_mode": "off"}
        assert intimacy_mode_state.active is False

    def test_turn_off_when_already_off(self) -> None:
        result = _handle_set_intimacy_mode({"on": False})
        assert result == {"intimacy_mode": "off"}
        assert intimacy_mode_state.active is False

    def test_defaults_to_off(self) -> None:
        result = _handle_set_intimacy_mode({})
        assert result == {"intimacy_mode": "off"}
        assert intimacy_mode_state.active is False


class TestModuleLevelSingleton:
    """模块级单例 intimacy_mode_state 隔离测试。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def test_singleton_is_shared(self) -> None:
        intimacy_mode_state.enter()
        from app.agent.builtin_tools import intimacy_mode_state as ims2

        assert ims2.active is True
        ims2.exit()
        assert intimacy_mode_state.active is False
