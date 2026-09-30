"""Optional intimacy director layer: keyword entry, guide injection and exit (no auto-continue here)."""

from __future__ import annotations

import shutil
from pathlib import Path
from threading import Event

import pytest

from app.agent.intimacy import IntimacyModeState, apply_intimacy_user_utterance, create_set_intimacy_mode_tool
from app.agent.runtime import AgentRuntime
from app.agent.tools import ToolRegistry
from app.core_host.real_chat import _note_intimacy_user_turn

GUIDE = "SYNTHETIC_INTIMACY_GUIDE"
PERSONA = (
    "\u3010\u8eab\u4efd\u951a\u3011\nANCHOR\n\n"
    "\u3010\u4eba\u683c\u8bbe\u5b9a\u3011\n## \u6838\u5fc3\nCORE_TRAIT\n\n## \u5174\u8da3\nDAILY_HOBBY_DETAIL\n\n"
    "\u3010\u6f14\u51fa\u7ea6\u675f\u3011\nGUARD_TAIL"
)


def _runtime(*, guide: str = GUIDE) -> AgentRuntime:
    runtime = AgentRuntime(object(), PERSONA, character_id="sakura", reply_tones=["\u4e2d\u6027"])
    runtime.configure_intimacy(guide)
    return runtime


@pytest.mark.parametrize("text", ["\u8d34\u7d27", "\u300c\u8d34\u7d27\u300d\uff01", " \u8d34\u7d27\u2026 "])
def test_whole_sentence_keyword_enters_when_a_guide_exists(text: str) -> None:
    state = IntimacyModeState()

    assert apply_intimacy_user_utterance(text, state, available=True) == "entered"
    assert state.active and state.opened_by_keyword


def test_keyword_needs_a_guide_and_a_whole_sentence() -> None:
    state = IntimacyModeState()

    assert apply_intimacy_user_utterance("\u8d34\u7d27", state, available=False) == "unavailable"
    assert apply_intimacy_user_utterance("\u8d34\u7d27\u4e00\u70b9\u5427", state, available=True) is None
    assert not state.active


@pytest.mark.parametrize("text", ["\u82f9\u679c", "\u6211\u4e0d\u8212\u670d", "\u5148\u8fd9\u6837\u5427"])
def test_safe_word_or_cool_down_exits(text: str) -> None:
    state = IntimacyModeState()
    apply_intimacy_user_utterance("\u8d34\u7d27", state, available=True)

    assert apply_intimacy_user_utterance(text, state, available=True) == "exited"
    assert not state.active


def test_tool_can_only_close_the_layer() -> None:
    state = IntimacyModeState()
    tool = create_set_intimacy_mode_tool(state)

    assert tool.handler({"on": True})["intimacy_mode"] == "off"
    assert not state.active
    apply_intimacy_user_utterance("\u8d34\u7d27", state, available=True)
    assert tool.handler({"on": False}) == {"intimacy_mode": "off"}
    assert not state.active


def test_inactive_layer_injects_only_the_entry_hint() -> None:
    prompt = _runtime()._build_tool_system_prompt()

    assert "\u8d34\u7d27" in prompt
    assert GUIDE not in prompt
    assert "DAILY_HOBBY_DETAIL" in prompt


def test_active_layer_injects_guide_softens_card_and_adds_tones() -> None:
    runtime = _runtime()
    runtime.note_intimacy_user_turn("\u8d34\u7d27")

    prompt = runtime._build_tool_system_prompt()

    assert GUIDE in prompt and "\u7ea6\u5b9a\u5165\u53e3" in prompt
    assert "CORE_TRAIT" in prompt and "GUARD_TAIL" in prompt
    assert "DAILY_HOBBY_DETAIL" not in prompt
    assert "\u4eb2\u5bc6" in runtime._effective_reply_tones() and "H" in runtime._effective_reply_tones()


def test_ordinary_reply_while_active_refreshes_the_continuation_budget() -> None:
    runtime = _runtime()
    runtime.note_intimacy_user_turn("\u8d34\u7d27")
    state = runtime._intimacy
    assert state.consume_turn() and state.consume_turn()

    runtime.note_intimacy_user_turn("\u55ef\uff0c\u518d\u8fd1\u4e00\u70b9")

    assert state.active
    assert [state.consume_turn() for _ in range(4)] == [True, True, True, False]
    assert "\u7ea6\u5b9a\u5165\u53e3" not in runtime._build_tool_system_prompt()


def test_no_guide_means_no_layer_at_all() -> None:
    runtime = _runtime(guide="")
    runtime.note_intimacy_user_turn("\u8d34\u7d27")

    prompt = runtime._build_tool_system_prompt()

    assert not runtime._intimacy.active
    assert "set_intimacy_mode" not in prompt


def test_real_chat_helper_tolerates_runtimes_without_the_layer() -> None:
    _note_intimacy_user_turn(object(), "\u8d34\u7d27")
    runtime = _runtime()
    _note_intimacy_user_turn(runtime, "\u8d34\u7d27")
    assert runtime._intimacy.active


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"


def test_adapter_loads_the_guide_and_registers_the_close_tool(tmp_path: Path) -> None:
    from app.core_host.assistant_adapter import AssistantAdapter

    root = tmp_path / "root"
    shutil.copytree(FIXTURE_ROOT, root)
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "intimacy_guide.txt").write_text(GUIDE, encoding="utf-8")
    registry = ToolRegistry()
    runtime = AssistantAdapter(root, tool_registry=registry, mcp_provider=None).initialize(Event()).session.runtime
    try:
        assert registry.get("set_intimacy_mode") is not None
        runtime.note_intimacy_user_turn("\u8d34\u7d27")
        assert GUIDE in runtime._build_tool_system_prompt()
    finally:
        runtime.close()
