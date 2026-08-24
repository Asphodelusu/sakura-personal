from types import SimpleNamespace

from app.agent.actions import AgentResult
from app.llm.chat_reply import ChatReply
from app.perception.observer import ProactiveSpeakPayload
from app.ui.pet_window import PetWindow


def test_stale_relationship_payload_is_dropped() -> None:
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=2,
        _is_proactive_observer_busy=lambda: "",
    )

    def consume(result: AgentResult, record_history: bool = True, *, message_source: str = "") -> None:
        consumed.append(message_source)

    window._consume_agent_result = consume
    payload = ProactiveSpeakPayload(
        text="こっち。",
        translation="过来。",
        tone="温柔",
        source="relationship",
        generation=1,
    )
    PetWindow._show_proactive_comment(window, payload)
    assert consumed == []


def test_relationship_payload_uses_distinct_history_source() -> None:
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=3,
        _is_proactive_observer_busy=lambda: "",
    )
    window._consume_agent_result = (
        lambda result, record_history=True, *, message_source="": consumed.append(message_source)
    )
    payload = ProactiveSpeakPayload(
        text="こっち。",
        translation="过来。",
        source="relationship",
        generation=3,
    )
    PetWindow._show_proactive_comment(window, payload)
    assert consumed == ["relationship"]


def test_busy_drops_relationship_speak_without_enabling_intimacy() -> None:
    from app.agent.builtin_tools import intimacy_mode_state

    intimacy_mode_state.exit()
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=1,
        _is_proactive_observer_busy=lambda: "subtitle_active",
    )
    window._consume_agent_result = (
        lambda result, record_history=True, *, message_source="": consumed.append(message_source)
    )
    PetWindow._show_proactive_comment(
        window,
        ProactiveSpeakPayload(text="こっち。", source="relationship", generation=1),
    )
    assert consumed == []
    assert intimacy_mode_state.active is False


def test_init_and_character_switch_wire_b_without_qt() -> None:
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("app", "ui", "pet_window.py").read_text(encoding="utf-8")
    assert "load_relationship_initiative_settings" in text
    assert "proactive_enabled" in text
    assert "_relationship_generation" in text
    assert "RelationshipInitiative" in text
    assert "_restart_proactive_observer" in text
    assert "set_relationship_guide" in text
    assert "build_continuity_context" in text
