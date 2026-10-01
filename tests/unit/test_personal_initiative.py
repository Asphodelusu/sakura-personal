"""Relationship initiative: one arbiter, Qt-era gates, silence allowed."""

from __future__ import annotations

import json

import pytest

from app.agent.initiative import (
    InitiativeArbiter,
    build_relationship_decision_messages,
    decision_to_reply,
    parse_relationship_decision,
)
from app.agent.runtime import AgentRuntime
from app.config.relationship_initiative import RelationshipInitiativeSettings


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _arbiter(*, enabled=True, idle=0.0, **overrides):
    clock = _Clock()
    arbiter = InitiativeArbiter(clock=clock, idle_seconds=lambda: idle)
    arbiter.configure(RelationshipInitiativeSettings(proactive_enabled=enabled, **overrides).normalized())
    return arbiter, clock


def test_disabled_initiative_never_passes() -> None:
    arbiter, clock = _arbiter(enabled=False)
    clock.now += 10_000

    assert arbiter.gate_reason() == "disabled"


def test_recent_user_message_holds_the_silence_gate() -> None:
    arbiter, clock = _arbiter()
    arbiter.note_user_spoke()
    clock.now += 299

    assert arbiter.gate_reason() == "silence"
    clock.now += 2
    assert arbiter.gate_reason() == "eligible"


def test_busy_and_continuation_take_priority() -> None:
    arbiter, clock = _arbiter()
    clock.now += 10_000

    assert arbiter.gate_reason(busy=True) == "busy"
    assert arbiter.gate_reason(continuation=True) == "continuation"


def test_spoken_initiative_cools_down_for_an_hour() -> None:
    arbiter, clock = _arbiter()
    clock.now += 10_000
    arbiter.mark_spoken()

    clock.now += 3599
    assert arbiter.gate_reason() == "cooldown"
    clock.now += 2
    assert arbiter.gate_reason() == "eligible"


def test_repeated_silence_backs_off_and_user_speech_resets_it() -> None:
    arbiter, clock = _arbiter()
    clock.now += 10_000
    waits = []
    for _ in range(5):
        arbiter.mark_silent()
        start = clock.now
        while arbiter.gate_reason() == "cooldown":
            clock.now += 10
        waits.append(round(clock.now - start, -1))
    assert waits == [300, 600, 1200, 1800, 1800]

    arbiter.mark_silent()
    arbiter.note_user_spoke()
    clock.now += 301
    assert arbiter.gate_reason() == "eligible"


def test_user_away_from_the_desktop_is_not_disturbed() -> None:
    arbiter, clock = _arbiter(idle=900.0)
    clock.now += 10_000

    assert arbiter.gate_reason() == "desktop_idle"


def test_user_speech_invalidates_an_in_flight_attempt() -> None:
    arbiter, _clock = _arbiter()
    attempt = arbiter.begin_attempt()

    arbiter.note_user_spoke()

    assert not arbiter.is_current(attempt)


def test_decision_prompt_carries_persona_guide_bias_and_context() -> None:
    system, messages = build_relationship_decision_messages(
        system_prompt="\u3010\u8eab\u4efd\u951a\u3011\nANCHOR\n\n\u3010\u4eba\u683c\u8bbe\u5b9a\u3011\nCARD",
        relationship_guide="## A. \u65e5\u5e38\u4e3b\u52a8\u5f3a\u5ea6\nGUIDE_CORE\n\n## \u79c1\u4e0b\u5347\u6e29\nPRIVATE",
        expression_bias="restrained",
        now_iso="2026-09-30T20:00:00+08:00",
        since_user_seconds=1800,
        recent_dialogue="RECENT_LINES",
        relationship_facts="FACTS",
        drive_summary="DRIVE",
    )

    assert "ANCHOR" in system and "GUIDE_CORE" in system and "PRIVATE" not in system
    assert "should_speak" in system and "restrained" in system
    user = messages[0]["content"]
    for part in ("2026-09-30T20:00:00+08:00", "1800s", "RECENT_LINES", "FACTS", "DRIVE"):
        assert part in user


@pytest.mark.parametrize("raw", [
    '{"should_speak": true, "reason": "r", "comment": "\u306d\u3048", "translation": "\u5582", "tone": "\u6e29\u67d4"}',
    '```json\n{"should_speak": true, "comment": "\u306d\u3048"}\n```',
    'noise {"should_speak": false, "reason": "quiet"} tail',
])
def test_decision_json_is_extracted_from_model_text(raw: str) -> None:
    assert isinstance(parse_relationship_decision(raw), dict)


def test_unparseable_decision_is_none() -> None:
    assert parse_relationship_decision("just talking") is None


def test_only_a_speaking_decision_with_text_becomes_a_reply() -> None:
    speak = {"should_speak": True, "comment": "\u306d\u3048\u3001\u8d77\u304d\u3066\u308b\uff1f",
             "translation": "\u5582\uff0c\u9192\u7740\u5417\uff1f", "tone": "\u6e29\u67d4"}

    reply = decision_to_reply(speak, allowed_tones=["\u4e2d\u6027", "\u6e29\u67d4"])

    assert reply is not None
    assert [(s.text, s.translation, s.tone) for s in reply.segments] == [
        ("\u306d\u3048\u3001\u8d77\u304d\u3066\u308b\uff1f", "\u5582\uff0c\u9192\u7740\u5417\uff1f", "\u6e29\u67d4")]
    assert decision_to_reply({"should_speak": False, "comment": "x"}, allowed_tones=[]) is None
    assert decision_to_reply({"should_speak": True, "comment": " "}, allowed_tones=[]) is None
    assert decision_to_reply(None, allowed_tones=[]) is None


class _DecisionClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    def complete_raw(self, system_prompt, messages, temperature=0.8, **kwargs):
        self.calls.append((system_prompt, messages))
        return self.text


def _runtime(client, *, enabled=True):
    runtime = AgentRuntime(object(), "\u3010\u8eab\u4efd\u951a\u3011\nANCHOR", character_id="sakura",
                           reply_tones=["\u4e2d\u6027"])
    clock = _Clock()
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=enabled).normalized(),
        client=client, clock=clock, idle_seconds=lambda: 0.0,
    )
    clock.now += 10_000
    return runtime, clock


def test_runtime_speaks_when_eligible_and_decision_says_so() -> None:
    client = _DecisionClient(json.dumps({"should_speak": True, "comment": "\u306d\u3048",
                                         "translation": "\u5582", "tone": "\u4e2d\u6027"}, ensure_ascii=False))
    runtime, _clock = _runtime(client)

    reply = runtime.run_relationship_initiative([], relationship_facts="")

    assert reply is not None and reply.segments[0].text == "\u306d\u3048"
    assert runtime.initiative_gate_reason() == "cooldown"


def test_runtime_stays_silent_without_calling_the_model_when_gated() -> None:
    client = _DecisionClient("{}")
    runtime, _clock = _runtime(client, enabled=False)

    assert runtime.run_relationship_initiative([], relationship_facts="") is None
    assert client.calls == []


def test_silent_decision_backs_off() -> None:
    client = _DecisionClient('{"should_speak": false, "reason": "quiet"}')
    runtime, _clock = _runtime(client)

    assert runtime.run_relationship_initiative([], relationship_facts="") is None
    assert runtime.initiative_gate_reason() == "cooldown"


def test_user_turn_during_decision_discards_the_result() -> None:
    runtime, _clock = _runtime(None)

    class _Racing(_DecisionClient):
        def complete_raw(self, *args, **kwargs):
            runtime.note_initiative_user_turn()
            return json.dumps({"should_speak": True, "comment": "\u306d\u3048"}, ensure_ascii=False)

    runtime._initiative_client = _Racing("")

    assert runtime.run_relationship_initiative([], relationship_facts="") is None


def test_explicit_away_blocks_relationship_initiative_until_a_real_return() -> None:
    runtime, clock = _runtime(None)
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    runtime.note_user_message("晚安")
    assert runtime.initiative_gate_reason() == "away"
    assert runtime.screen_gate_reason() == "away"
    assert runtime.run_relationship_initiative([], relationship_facts="") is None
    runtime.note_user_message("我还在，失眠了想聊天")
    clock.now += 301
    assert runtime.initiative_gate_reason() == "eligible"
    assert runtime.screen_gate_reason() == "eligible"


def test_relationship_facts_keep_only_the_standing_profile() -> None:
    from app.llm.prompts.types import ContextFragment
    from app.plugins.models import ContextProviderContribution

    runtime = AgentRuntime(object(), "system", character_id="sakura", character_name="Sakura")
    requests = []

    def _memory(request):
        requests.append(request)
        return [
            ContextFragment("core_profile:sakura", "plugin", "profile"),
            ContextFragment("memory.recall", "plugin", "recalled"),
        ]

    def _broken(_request):
        raise RuntimeError("down")

    runtime.set_context_providers(
        [
            ContextProviderContribution("broken", "", _broken),
            ContextProviderContribution("memory", "", _memory),
        ]
    )

    assert runtime.relationship_facts() == "profile"
    assert requests[0].current_input == ""
    assert requests[0].character_id == "sakura"


def test_adapter_configures_the_arbiter_and_a_decision_client(tmp_path) -> None:
    import shutil
    from pathlib import Path
    from threading import Event

    from app.agent.tools import ToolRegistry
    from app.core_host.assistant_adapter import AssistantAdapter

    fixture = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"
    root = tmp_path / "root"
    shutil.copytree(fixture, root)
    config = root / "config" / "system_config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\nrelationship_initiative:\n  proactive_enabled: true\n  proactive_cooldown_seconds: 1800\n",
        encoding="utf-8",
    )
    session = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None).initialize(Event()).session
    runtime = session.runtime
    try:
        assert runtime._initiative.settings.proactive_enabled is True
        assert runtime._initiative.settings.proactive_cooldown_seconds == 1800
        assert callable(getattr(runtime._initiative_client, "complete_raw", None))
    finally:
        runtime.close()
