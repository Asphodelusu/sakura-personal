"""tests/unit/test_intimacy_mode.py — 亲密模式状态机与工具测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.agent.builtin_tools import (
    INTIMACY_ENTER_PHRASE,
    INTIMACY_EXIT_PHRASE,
    INTIMACY_CONTINUE_MARKER,
    INTIMACY_CONTINUE_SYSTEM_TEXT,
    IntimacyModeState,
    _SET_INTIMACY_MODE_DESCRIPTION,
    _handle_set_intimacy_mode,
    apply_intimacy_user_utterance,
    create_builtin_tool_registry,
    intimacy_mode_state,
    user_declines_or_exits_intimacy,
    user_requests_intimacy_entry,
    user_requests_intimacy_exit,
)
from app.agent.prompt_builder import _intimacy_entry_hint_text
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
        assert state._AUTO_EXIT_TURNS == 3
        for _ in range(2):
            assert state.consume_turn() is True
        assert state._turns_left == 1
        state.refresh_user_reply()
        assert state.active is True
        assert state._turns_left == 3

    def test_third_continuation_stays_active_until_expire_after_silence(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(3):
            assert state.consume_turn() is True
            assert state.active is True
        assert state._turns_left == 0
        state.expire_after_silence()
        assert state.active is False
        assert state.pending is False
        assert state._turns_left == 0
        assert state.opened_by_keyword is False
        assert state.needs_reentry_hint is True

    def test_reenter_resets_counter(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(3):
            state.consume_turn()
        state.expire_after_silence()
        state.enter()
        assert state.needs_reentry_hint is False
        for _ in range(3):
            assert state.consume_turn() is True
            assert state.active is True
        state.expire_after_silence()
        assert state.active is False

    def test_auto_exit_then_keyword_reenter(self) -> None:
        intimacy_mode_state.exit()
        intimacy_mode_state.enter()
        for _ in range(3):
            intimacy_mode_state.consume_turn()
        intimacy_mode_state.expire_after_silence()
        assert intimacy_mode_state.active is False
        assert intimacy_mode_state.needs_reentry_hint is True
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            assert apply_intimacy_user_utterance(INTIMACY_ENTER_PHRASE) == "entered"
        assert intimacy_mode_state.active is True
        assert intimacy_mode_state.opened_by_keyword is True
        assert intimacy_mode_state.needs_reentry_hint is False

    def test_keyword_enter_sets_flag(self) -> None:
        state = IntimacyModeState()
        state.enter(by_keyword=True)
        assert state.active is True
        assert state.opened_by_keyword is True
        state.refresh_user_reply()
        # last_user_text 为空 → 清掉刚开启标记
        assert state.opened_by_keyword is False

    def test_voluntary_exit_clears_reentry_hint(self) -> None:
        state = IntimacyModeState()
        state.enter()
        for _ in range(3):
            state.consume_turn()
        state.expire_after_silence()
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
        for _ in range(3):
            assert state.consume_turn() is True
            assert state.active is True
        state.expire_after_silence()
        assert state.active is False


class TestHandleSetIntimacyMode:
    """_handle_set_intimacy_mode 工具处理器测试。"""

    def setup_method(self) -> None:
        intimacy_mode_state.exit()

    def test_tool_on_true_does_not_enter(self) -> None:
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            result = _handle_set_intimacy_mode({"on": True})
        assert result["intimacy_mode"] == "off"
        assert result.get("entry") == "keyword_only"
        assert INTIMACY_ENTER_PHRASE in result.get("instruction", "")
        assert intimacy_mode_state.active is False

    def test_tool_on_true_when_already_active(self) -> None:
        intimacy_mode_state.enter()
        result = _handle_set_intimacy_mode({"on": True})
        assert result["intimacy_mode"] == "on"
        assert result.get("entry") == "keyword_only"

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


class TestIntimacyToolBoundaryCopy:
    """工具描述应写清：开启靠约定词，工具只关引导/节奏/续投。"""

    def test_description_mentions_keyword_entry(self) -> None:
        text = _SET_INTIMACY_MODE_DESCRIPTION
        assert "引导" in text
        assert INTIMACY_ENTER_PHRASE in text
        assert "on=true" in text
        assert "苹果" in text
        assert "节奏" in text
        assert "关闭身体亲密" not in text

    def test_tool_description_controls_guidance_pacing_only(self) -> None:
        text = _SET_INTIMACY_MODE_DESCRIPTION
        assert "引导" in text
        assert "节奏" in text
        assert "续投" in text
        assert "只影响" in text
        assert "当身体亲密自然结束" not in text


class TestIntimacyConsentClassifiers:
    def test_paired_control_phrases(self) -> None:
        assert INTIMACY_ENTER_PHRASE == "贴紧"
        assert INTIMACY_EXIT_PHRASE == "苹果"

    @pytest.mark.parametrize("text", ["苹果", "苹果。", "『苹果』"])
    def test_safe_word_exits_as_whole_utterance(self, text: str) -> None:
        assert user_requests_intimacy_exit(text) is True

    @pytest.mark.parametrize("text", ["我买了苹果", "苹果很好吃", "太好了", "准备好了", "好了吗"])
    def test_safe_word_and_ambiguous_phrases_do_not_false_exit(self, text: str) -> None:
        assert user_requests_intimacy_exit(text) is False
        assert user_declines_or_exits_intimacy(text) is False

    @pytest.mark.parametrize("text", ["停下", "不要继续", "我不舒服", "やめて"])
    def test_explicit_refusal_exits_without_safe_word(self, text: str) -> None:
        assert user_declines_or_exits_intimacy(text) is True

    def test_keyword_and_decline(self) -> None:
        assert INTIMACY_ENTER_PHRASE == "贴紧"
        assert user_requests_intimacy_entry("贴紧")
        assert user_requests_intimacy_entry("贴紧！")
        assert user_requests_intimacy_entry("「贴紧」")
        assert not user_requests_intimacy_entry("准了")
        assert not user_requests_intimacy_entry("要")
        assert not user_requests_intimacy_entry("好的")
        assert not user_requests_intimacy_entry("我想贴紧")
        assert not user_requests_intimacy_entry("摸摸我的头")
        assert user_declines_or_exits_intimacy("你先冷静一下")
        assert user_declines_or_exits_intimacy("好了，不闹了")
        assert user_declines_or_exits_intimacy("先这样吧")

    def test_apply_keyword_enter_and_exit(self) -> None:
        intimacy_mode_state.exit()
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            assert apply_intimacy_user_utterance("贴紧") == "entered"
        assert intimacy_mode_state.active is True
        assert intimacy_mode_state.opened_by_keyword is True

        intimacy_mode_state.exit()
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=True):
            assert apply_intimacy_user_utterance("摸摸头就好") is None
        assert intimacy_mode_state.active is False

        intimacy_mode_state.enter()
        assert apply_intimacy_user_utterance("你先冷静一下") == "exited"
        assert intimacy_mode_state.active is False

        intimacy_mode_state.exit()
        with patch("app.agent.builtin_tools.intimacy_mode_available", return_value=False):
            assert apply_intimacy_user_utterance("贴紧") == "unavailable"
        assert intimacy_mode_state.active is False

    def test_registry_uses_boundary_description(self, tmp_path: Path) -> None:
        registry = create_builtin_tool_registry(tmp_path)
        tool = registry.get("set_intimacy_mode")
        assert tool is not None
        assert "引导" in tool.description
        assert INTIMACY_ENTER_PHRASE in tool.description
        on_desc = tool.parameters["properties"]["on"]["description"]
        assert "引导" in on_desc or "节奏" in on_desc or "续投" in on_desc
        assert "准备或正在身体亲密" not in on_desc
        assert "回到日常或对方已停下" not in on_desc


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
        assert system_msg["_sakura_transient_progress"] is True
        assert "根据当前姿势、呼吸和对方最后的反应自然回应" in system_msg["content"]
        assert "若当前已不是身体亲密场景" not in system_msg["content"]
        assert "不再需要详细引导、连续节奏或自动续投" in system_msg["content"]
        assert "若当前已不是身体亲密场景" not in INTIMACY_CONTINUE_SYSTEM_TEXT
        assert "不再需要详细引导、连续节奏或自动续投" in INTIMACY_CONTINUE_SYSTEM_TEXT
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

    def test_inactive_mode_is_optional_guidance_not_behavior_gate(self) -> None:
        body = _intimacy_entry_hint_text()
        assert "不会自动开启" in body
        assert "自然升温" in body
        assert "未开启不限制" in body
        assert "不要动手描写" not in body
        assert "开启后才能" not in body

    def test_entry_hint_when_mode_inactive_with_guide(self) -> None:
        runtime = self._runtime_with_guide()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert section.section_id == "persona.intimacy_entry"
        assert INTIMACY_ENTER_PHRASE in section.body
        assert "set_intimacy_mode(on=true)" in section.body
        assert "INTIMACY_GUIDE_MARKER" not in section.body

    def test_no_entry_hint_without_guide(self) -> None:
        runtime = self._runtime_with_guide("")
        assert runtime._build_intimacy_section() is None

    def test_intimacy_tone_no_longer_opens_mode(self) -> None:
        from app.llm.chat_reply import ChatReply, ChatSegment

        runtime = self._runtime_with_guide()
        intimacy_mode_state.exit()
        reply = ChatReply(
            [
                ChatSegment("……いいよ。", "亲密", "……好吧。", ""),
            ]
        )
        sealed = runtime._seal_reply_tones(reply)
        assert intimacy_mode_state.active is False
        assert intimacy_mode_state.pending is False
        assert sealed.segments[0].tone != "亲密"

    def test_keyword_hard_entry_injects_note(self) -> None:
        runtime = self._runtime_with_guide()
        intimacy_mode_state.enter(by_keyword=True)
        intimacy_mode_state.note_user_text(INTIMACY_ENTER_PHRASE)
        section = runtime._build_intimacy_section()
        assert section is not None
        assert "约定入口" in section.body
        assert INTIMACY_ENTER_PHRASE in section.body
        assert "请求启用详细 guide 与连续节奏" in section.body
        assert "总体许可" not in section.body
        assert "INTIMACY_GUIDE_MARKER" in section.body

    def test_inactive_keeps_configured_intimacy_tones(self) -> None:
        from app.llm.chat_reply import ChatReply, ChatSegment

        runtime = self._runtime_with_guide()
        runtime.reply_tones = ["中性", "害羞", "亲密", "H"]
        intimacy_mode_state.exit()
        effective = runtime._effective_reply_tones()
        assert "亲密" in effective
        assert "H" in effective
        sealed = runtime._seal_reply_tones(
            ChatReply([ChatSegment("……いいよ。", "亲密", "……好吧。", "")])
        )
        assert sealed.segments[0].tone == "亲密"
        intimacy_mode_state.enter()
        assert "亲密" in runtime._effective_reply_tones()
        assert "H" in runtime._effective_reply_tones()

    def test_reentry_hint_after_auto_exit_without_guide_body(self) -> None:
        runtime = self._runtime_with_guide()
        intimacy_mode_state.enter()
        for _ in range(3):
            intimacy_mode_state.consume_turn()
        intimacy_mode_state.expire_after_silence()
        section = runtime._build_intimacy_section()
        assert section is not None
        assert section.section_id == "persona.intimacy_reentry"
        assert "引导与自动续投已关闭" in section.body
        assert "仍按当前关系和意愿自然回应" in section.body
        assert "不要自行动手" not in section.body
        assert INTIMACY_ENTER_PHRASE in section.body
        assert "set_intimacy_mode(on=true)" in section.body
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
    """inactive 保留角色已配置 tone；active 为未配置角色补齐扩展 tone。"""

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

    def test_inactive_does_not_strip_configured_extra_tones(self) -> None:
        runtime = self._runtime(["中性", "亲密", "H"])
        effective = runtime._effective_reply_tones()
        assert "亲密" in effective
        assert "H" in effective

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
