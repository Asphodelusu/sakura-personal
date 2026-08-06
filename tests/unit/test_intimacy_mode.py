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

    def test_user_reply_refreshes_instead_of_consuming(self) -> None:
        state = IntimacyModeState()
        state.enter()
        assert state._AUTO_EXIT_TURNS == 8
        for _ in range(3):
            assert state.consume_turn() is True
        assert state._turns_left == 5
        state.refresh_user_reply()
        assert state.active is True
        assert state._turns_left == 8

    def test_continue_consume_auto_exits_at_eight(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(7):
            assert state.consume_turn() is True
            assert state.active is True
        assert state.consume_turn() is False
        assert state.active is False
        assert state.needs_reentry_hint is True

    def test_reenter_resets_counter(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(3):
            state.consume_turn()
        state.enter()
        assert state.needs_reentry_hint is False
        for _ in range(7):
            assert state.consume_turn() is True
        assert state.consume_turn() is False

    def test_auto_exit_then_tool_reenter(self) -> None:
        intimacy_mode_state.exit()
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            assert _handle_set_intimacy_mode({"on": True}) == {"intimacy_mode": "on"}
        for _ in range(8):
            intimacy_mode_state.consume_turn()
        assert intimacy_mode_state.active is False
        assert intimacy_mode_state.needs_reentry_hint is True
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            assert _handle_set_intimacy_mode({"on": True}) == {"intimacy_mode": "on"}
        assert intimacy_mode_state.active is True
        assert intimacy_mode_state.needs_reentry_hint is False

    def test_voluntary_exit_clears_reentry_hint(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(8):
            state.consume_turn()
        assert state.needs_reentry_hint is True
        state.exit()
        assert state.needs_reentry_hint is False

    def test_active_exit_does_not_set_reentry_hint(self) -> None:
        state = IntimacyModeState()
        state.enter()
        state.exit()
        assert state.active is False
        assert state.needs_reentry_hint is False

    def test_consume_when_inactive_returns_false(self) -> None:
        state = IntimacyModeState()
        assert state.consume_turn() is False
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
        for _ in range(7):
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

    def test_turn_off_directly_exits_when_active(self) -> None:
        intimacy_mode_state.enter()
        result = _handle_set_intimacy_mode({"on": False})
        assert result == {"intimacy_mode": "off"}
        assert intimacy_mode_state.active is False
        assert intimacy_mode_state.needs_reentry_hint is False

    def test_turn_off_when_already_off(self) -> None:
        result = _handle_set_intimacy_mode({"on": False})
        assert result == {"intimacy_mode": "off"}
        assert intimacy_mode_state.active is False

    def test_defaults_to_off_when_inactive(self) -> None:
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
        assert "准备" in text
        assert "技术" in text or "工作" in text
        assert "日常" in text
        assert "节奏" in text
        assert "退出" in text
        assert "不开也可以" not in text

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

    def test_continue_helpers_system_and_legacy(self) -> None:
        from app.agent.builtin_tools import (
            build_intimacy_continue_message,
            latest_is_intimacy_continue,
            message_is_intimacy_continue,
        )

        system_msg = build_intimacy_continue_message()
        assert system_msg["role"] == "system"
        assert INTIMACY_CONTINUE_MARKER in system_msg["content"]
        assert message_is_intimacy_continue(system_msg)
        assert latest_is_intimacy_continue([{"role": "user", "content": "hi"}, system_msg])

        legacy = {"role": "user", "content": INTIMACY_CONTINUE_MARKER}
        assert message_is_intimacy_continue(legacy)
        assert not message_is_intimacy_continue({"role": "user", "content": "待って"})


class TestIntimacyGuidePromptGate:
    """非亲密模式不得把本地 intimacy guide 注入 system prompt。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def teardown_method(self) -> None:
        intimacy_mode_state.exit()

    def _runtime_with_guide(self, guide: str = "INTIMACY_GUIDE_MARKER"):
        from app.agent.runtime import AgentRuntime

        runtime = object.__new__(AgentRuntime)
        runtime._intimacy_guide = guide
        runtime.reply_tones = ["中性", "害羞", "请求"]
        runtime.system_prompt = with_desktop_pet_context(
            "我是夜乃桜。\n" + ("日常设定。" * 200),
            system_guards="- 勿复述战力",
        )
        runtime.prompt_patches = []
        return runtime

    def test_entry_hint_when_mode_inactive_with_guide(self) -> None:
        runtime = self._runtime_with_guide()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert section.section_id == "persona.intimacy_entry"
        assert "准备" in section.body
        assert "set_intimacy_mode(on=true)" in section.body
        assert "INTIMACY_GUIDE_MARKER" not in section.body

    def test_no_entry_hint_without_guide(self) -> None:
        runtime = self._runtime_with_guide("")
        assert runtime._build_intimacy_section() is None

    def test_auto_enter_when_reply_uses_intimacy_tone(self) -> None:
        from app.llm.chat_reply import ChatReply, ChatSegment
        from unittest.mock import patch

        runtime = self._runtime_with_guide()
        intimacy_mode_state.exit()
        reply = ChatReply(
            [
                ChatSegment("……いいよ。", "亲密", "……好吧。", ""),
            ]
        )
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            sealed = runtime._seal_reply_tones(reply)
        assert intimacy_mode_state.active is True
        assert sealed.segments[0].tone == "亲密"

    def test_daily_tones_exclude_intimacy_extras(self) -> None:
        runtime = self._runtime_with_guide()
        runtime.reply_tones = ["中性", "害羞", "亲密", "H"]
        intimacy_mode_state.exit()
        assert "亲密" not in runtime._effective_reply_tones()
        intimacy_mode_state.enter()
        assert "亲密" in runtime._effective_reply_tones()
        assert "H" in runtime._effective_reply_tones()

    def test_reentry_hint_after_auto_exit_without_guide_body(self) -> None:
        runtime = self._runtime_with_guide()
        intimacy_mode_state.enter()
        for _ in range(8):
            intimacy_mode_state.consume_turn()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert section.section_id == "persona.intimacy_reentry"
        assert "自动关闭" in section.body
        assert "再次" in section.body or "重新开启" in section.body
        assert "INTIMACY_GUIDE_MARKER" not in section.body

    def test_tool_description_mentions_reentry(self) -> None:
        assert "不会自动恢复" in _SET_INTIMACY_MODE_DESCRIPTION
        assert "再次" in _SET_INTIMACY_MODE_DESCRIPTION

    def test_visible_when_mode_active(self) -> None:
        runtime = self._runtime_with_guide()
        intimacy_mode_state.enter()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert "INTIMACY_GUIDE_MARKER" in section.body
        assert section.section_id == "persona.intimacy"
        assert "退出" in section.body
        assert "set_intimacy_mode(on=false)" in section.body

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


class TestEffectiveReplyTones:
    """亲密开启时追加扩展 tone；关闭时不开放。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def teardown_method(self) -> None:
        intimacy_mode_state.exit()

    def _runtime(self, tones: list[str] | None = None):
        from app.agent.runtime import AgentRuntime

        runtime = object.__new__(AgentRuntime)
        runtime.reply_tones = list(tones or ["中性", "害羞", "温柔"])
        return runtime

    def test_extra_tones_only_when_intimacy_active(self) -> None:
        runtime = self._runtime()
        assert "亲密" not in runtime._effective_reply_tones()
        assert "H" not in runtime._effective_reply_tones()
        intimacy_mode_state.enter()
        effective = runtime._effective_reply_tones()
        assert "亲密" in effective
        assert "H" in effective
        assert effective.index("亲密") < effective.index("H")

    def test_does_not_duplicate_existing(self) -> None:
        runtime = self._runtime(["中性", "亲密", "H"])
        intimacy_mode_state.enter()
        effective = runtime._effective_reply_tones()
        assert effective.count("亲密") == 1
        assert effective.count("H") == 1

    def test_segment_instruction_does_not_embed_private_tone_rules(self) -> None:
        from app.llm.prompts.recipes import build_segmented_reply_instruction

        text = build_segmented_reply_instruction(["中性", "亲密", "H"])
        assert "叫床" not in text
        assert "喘息" not in text
        plain = build_segmented_reply_instruction(["中性", "害羞"])
        assert "叫床" not in plain
