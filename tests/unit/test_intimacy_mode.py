"""tests/unit/test_intimacy_mode.py — 亲密模式状态机与工具测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.agent.builtin_tools import (
    INTIMACY_CONTINUE_MARKER,
    IntimacyModeState,
    _SET_INTIMACY_MODE_DESCRIPTION,
    _handle_set_intimacy_mode,
    create_builtin_tool_registry,
    intimacy_mode_state,
)
from app.llm.prompts.blocks import with_desktop_pet_context


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
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
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

    def test_turn_on_blocked_without_guide(self) -> None:
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=False):
            result = _handle_set_intimacy_mode({"on": True})
        assert result == {"intimacy_mode": "off", "available": False}
        assert intimacy_mode_state.active is False


class TestIntimacyToolBoundaryCopy:
    """工具描述应写清开/关边界，避免闲聊误开。"""

    def test_description_mentions_when_not_to_enable(self) -> None:
        text = _SET_INTIMACY_MODE_DESCRIPTION
        assert "身体亲密" in text
        assert "技术" in text or "工作" in text
        assert "日常" in text
        assert "节奏" in text

    def test_registry_uses_boundary_description(self, tmp_path: Path) -> None:
        registry = create_builtin_tool_registry(tmp_path)
        tool = registry.get("set_intimacy_mode")
        assert tool is not None
        assert "身体亲密" in tool.description
        assert "日常" in tool.description


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

    def test_continue_marker_constant(self) -> None:
        assert INTIMACY_CONTINUE_MARKER == "（続けて）"


class TestIntimacyGuidePromptGate:
    """非亲密模式不得把 H guide 注入 system prompt。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def teardown_method(self) -> None:
        intimacy_mode_state.exit()

    def _runtime_with_guide(self, guide: str = "H_GUIDE_MARKER_台詞見本"):
        from app.agent.runtime import AgentRuntime

        runtime = object.__new__(AgentRuntime)
        runtime._intimacy_guide = guide
        runtime.system_prompt = with_desktop_pet_context(
            "我是夜乃桜。\n" + ("日常设定。" * 200),
            system_guards="- 勿复述战力",
        )
        runtime.prompt_patches = []
        return runtime

    def test_hidden_when_mode_inactive(self) -> None:
        runtime = self._runtime_with_guide()
        assert runtime._build_intimacy_section() is None

    def test_visible_when_mode_active(self) -> None:
        runtime = self._runtime_with_guide()
        intimacy_mode_state.enter()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert "H_GUIDE_MARKER_台詞見本" in section.body
        assert section.section_id == "persona.intimacy"

    def test_empty_guide_stays_hidden_even_when_active(self) -> None:
        runtime = self._runtime_with_guide("")
        intimacy_mode_state.enter()
        assert runtime._build_intimacy_section() is None

    def test_persona_softened_when_intimacy_focus(self) -> None:
        runtime = self._runtime_with_guide()
        full = runtime._persona_sections(intimacy_focus=False)[0].body
        soft = runtime._persona_sections(intimacy_focus=True)[0].body
        assert "【当下专注】" in soft
        assert len(soft) < len(full)
        assert "勿复述战力" in soft
