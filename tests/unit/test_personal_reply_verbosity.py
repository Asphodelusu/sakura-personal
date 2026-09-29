"""The inner thought's interest sets this turn's reply length, as in the Qt runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.inner_thought import InnerThoughtSettings
from app.agent.reply_verbosity import decision_from_interest, format_verbosity_guidance
from app.agent.runtime import AgentRuntime


class _Client:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete_raw(self, *_args: object, **_kwargs: object) -> str:
        return self.text


def _runtime(text: str) -> AgentRuntime:
    runtime = AgentRuntime(object(), "system prompt", character_id="sakura", character_name="Sakura")
    runtime.configure_inner_thought(
        settings=InnerThoughtSettings(join_timeout_seconds=3),
        client=_Client(text),
        source_slot="chat",
    )
    return runtime


def _think(runtime: AgentRuntime, turn_id: str) -> None:
    assert runtime.start_inner_thought(turn_id, [{"role": "user", "content": "\u5728\u5417"}]) is True
    assert runtime._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    runtime.join_inner_thought(turn_id)


def _verbosity(runtime: AgentRuntime) -> str | None:
    fragments = runtime._session_state_fragments(SimpleNamespace())  # type: ignore[arg-type]
    found = [f.content for f in fragments if f.fragment_id == "runtime.reply_verbosity"]
    return found[0] if found else None


@pytest.mark.parametrize(("interest", "segments"), [("low", "1-2"), ("mid", "1-3"), ("high", "3-5")])
def test_interest_maps_to_the_old_segment_ranges(interest: str, segments: str) -> None:
    decision = decision_from_interest(interest)
    assert decision is not None
    assert f"{decision.min_segments}-{decision.max_segments}" == segments
    assert segments in format_verbosity_guidance(decision)


def test_unknown_interest_gives_no_guidance() -> None:
    assert decision_from_interest("") is None
    assert decision_from_interest("maybe") is None


def test_committed_interest_reaches_the_turn_context() -> None:
    runtime = _runtime("interest: high\n\u4eca\u65e5\u306f\u8a71\u3057\u305f\u3044\u6c17\u5206\u3002")

    _think(runtime, "op-a")

    guidance = _verbosity(runtime)
    assert guidance is not None and "3-5" in guidance


def test_guidance_does_not_leak_into_a_later_turn() -> None:
    runtime = _runtime("interest: low\n\u773a\u3044\u3002")
    _think(runtime, "op-a")
    assert _verbosity(runtime) is not None

    runtime.invalidate_inner_thought()

    assert _verbosity(runtime) is None


def test_no_thought_means_no_length_block() -> None:
    runtime = AgentRuntime(object(), "system prompt", character_id="sakura")

    assert _verbosity(runtime) is None
