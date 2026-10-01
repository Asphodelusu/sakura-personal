"""Scheduled screen observation is one arbiter source: privacy first, silence allowed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from app.agent.initiative import InitiativeArbiter
from app.agent.runtime import AgentRuntime
from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.core_host.real_chat import RealChatBoundary, _ScreenAttachment
from app.core_host.screen_awareness_settings import (
    DEFAULT_BLOCKED_PROCESSES,
    ScreenAwarenessSettingsBoundary,
    load_screen_privacy,
)
from app.llm.api_client import ChatMessage, OpenAICompatibleClient
from app.llm.chat_reply import ChatReply, ChatSegment
from app.storage.timeline import TimelineKind, TimelineStore

GENERATION_ID = "00000000-0000-4000-8000-000000004401"
GENERATION_CREDENTIAL = "44" * 16


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_privacy_defaults_apply_without_any_config(tmp_path: Path) -> None:
    processes, keywords = load_screen_privacy(tmp_path)
    assert processes == DEFAULT_BLOCKED_PROCESSES
    assert "online banking" in keywords


def test_qt_era_privacy_list_is_used_and_casefolded(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "config" / "system_config.yaml",
        {"proactive": {"privacy": {"blocked_processes": ["Vault.EXE", " "], "blocked_title_keywords": ["Secret"]}}},
    )
    assert load_screen_privacy(tmp_path) == (("vault.exe",), ("secret",))


def test_current_privacy_wins_and_an_empty_list_clears_the_defaults(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "config" / "system_config.yaml",
        {"proactive": {"privacy": {"blocked_processes": ["old.exe"]}}},
    )
    _write(
        tmp_path / "config" / "system_config.yaml",
        {"config_version": 1, "screen_awareness": {"privacy": {"blocked_processes": [], "blocked_title_keywords": ["x"]}}},
    )
    assert load_screen_privacy(tmp_path) == ((), ("x",))


def test_privacy_request_returns_only_the_lists(tmp_path: Path) -> None:
    boundary = ScreenAwarenessSettingsBoundary(GENERATION_ID, GENERATION_CREDENTIAL, tmp_path)
    request = {
        "id": "privacy",
        "name": "screen_awareness.privacy.get",
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "protocolMajor": 2,
        "protocolMinor": 2,
        "payload": {},
    }
    payload = boundary.handle(request)["payload"]
    assert set(payload) == {"schemaVersion", "blockedProcesses", "blockedTitleKeywords"}
    assert payload["blockedProcesses"] == list(DEFAULT_BLOCKED_PROCESSES)
    request["payload"] = {"extra": True}
    assert boundary.handle(request)["error"]["code"] == "INVALID_REQUEST"


SILENT = json.dumps({"silent": True, "segments": []})


def _client(*contents: str) -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.complete_with_tools.side_effect = [MagicMock(content=content, tool_calls=[]) for content in contents]
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def test_silence_is_adopted_only_where_it_is_allowed() -> None:
    spoken = json.dumps({"segments": [{"ja": "\u306d\u3048\u3002", "zh": "\u5582\u3002", "tone": "\u4e2d\u6027"}]})
    quiet_client = _client(SILENT)
    runtime = AgentRuntime(quiet_client, "SYNTHETIC_PERSONA")
    with runtime.allow_silent_reply():
        quiet = runtime.handle_user_message([ChatMessage(role="user", content="screen")])
    assert quiet.reply.segments == []
    assert quiet_client.complete_with_tools.call_count == 1

    chat_client = _client(SILENT, spoken)
    runtime = AgentRuntime(chat_client, "SYNTHETIC_PERSONA")
    answered = runtime.handle_user_message([ChatMessage(role="user", content="hi")])
    assert [segment.text for segment in answered.reply.segments] == ["\u306d\u3048\u3002"]
    assert runtime._silent_reply_allowed is False


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _screen_arbiter(*, idle: float = 0.0, cooldown: float = 600.0):
    clock = _Clock()
    arbiter = InitiativeArbiter(clock=clock, idle_seconds=lambda: idle)
    arbiter.configure(RelationshipInitiativeSettings(proactive_enabled=True).normalized())
    arbiter.configure_screen(enabled=True, cooldown_seconds=cooldown)
    return arbiter, clock


def test_screen_gate_follows_the_qt_era_order() -> None:
    arbiter, clock = _screen_arbiter()
    assert arbiter.screen_gate_reason() == "silence"
    clock.now += 15
    assert arbiter.screen_gate_reason() == "eligible"
    assert arbiter.screen_gate_reason(continuation=True) == "continuation"
    assert arbiter.screen_gate_reason(busy=True) == "busy"

    arbiter.mark_screen_silent()
    clock.now += 299
    assert arbiter.screen_gate_reason() == "cooldown"
    clock.now += 1
    assert arbiter.screen_gate_reason() == "eligible"

    arbiter.mark_screen_spoken(relationship_motive=False)
    clock.now += 599
    assert arbiter.screen_gate_reason() == "cooldown"
    clock.now += 1
    assert arbiter.screen_gate_reason() == "eligible"

    arbiter.configure_screen(enabled=False, cooldown_seconds=600)
    assert arbiter.screen_gate_reason() == "disabled"


def test_screen_speech_with_a_motive_counts_as_relationship_speech() -> None:
    arbiter, clock = _screen_arbiter()
    clock.now += 10_000
    assert arbiter.gate_reason() == "eligible"
    arbiter.mark_screen_spoken(relationship_motive=True)
    assert arbiter.gate_reason() == "cooldown"

    other, other_clock = _screen_arbiter()
    other_clock.now += 10_000
    other.mark_screen_spoken(relationship_motive=False)
    assert other.gate_reason() == "eligible"
    assert other.screen_gate_reason() == "cooldown"


def test_relationship_speech_also_cools_the_screen_down() -> None:
    arbiter, clock = _screen_arbiter()
    clock.now += 10_000
    arbiter.mark_spoken()
    assert arbiter.screen_gate_reason() == "cooldown"


def test_input_idle_does_not_block_screen_observation() -> None:
    arbiter, clock = _screen_arbiter(idle=600)
    clock.now += 10_000
    assert arbiter.screen_gate_reason() == "eligible"


class _Pipeline:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[list[dict[str, object]]] = []

    def run_user_message(self, messages, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append([dict(message) for message in messages])
        return self.result


def _screen_boundary(tmp_path: Path, result, *, relationship_ready: bool = False):
    clock = _Clock()
    runtime = AgentRuntime(SimpleNamespace(), "system", character_id="sakura", character_name="Sakura")
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=relationship_ready).normalized(),
        client=None,
        clock=clock,
        idle_seconds=lambda: 0.0,
    )
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    clock.now += 10_000
    pipeline = _Pipeline(result)
    events: list[dict[str, object]] = []
    session = SimpleNamespace(
        character=SimpleNamespace(id="sakura", display_name="Sakura"),
        runtime=runtime,
        pipeline=pipeline,
    )
    store = TimelineStore(tmp_path / "timeline.sqlite3")
    store.initialize()
    boundary = RealChatBoundary(
        GENERATION_ID,
        GENERATION_CREDENTIAL,
        tmp_path,
        session_provider=lambda: session,
        timeline_store=store,
        event_publisher=events.append,
    )
    return runtime, pipeline, boundary, events, store, clock


def _screen_send(boundary: RealChatBoundary, operation_id: str) -> None:
    attachment_id = "screen-" + operation_id.encode().hex().ljust(32, "0")[:32]
    boundary._pending_screen_attachment = _ScreenAttachment(
        attachment_id=attachment_id,
        observations=(
            SimpleNamespace(
                data_url="data:image/jpeg;base64,aa",
                width=2,
                height=2,
                captured_at="2026-09-01T00:00:00+00:00",
                screen_name="desk",
            ),
        ),
        item_ids=("shot-" + "a" * 32,),
        source="screen_awareness",
        visual_id=None,
    )
    request = {
        "id": operation_id,
        "kind": "request",
        "name": "chat.send",
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "payload": {"message": "desktop prompt", "operationId": operation_id, "attachmentId": attachment_id},
    }
    boundary.reserve_send(request)
    boundary.handle_send(request)


_OBSERVED = {"summary": "an editor with code", "confidence": 0.8, "sensitive_redacted": False}


def test_gated_screen_turn_is_silent_and_leaves_no_trace(tmp_path: Path) -> None:
    runtime, pipeline, boundary, events, store, clock = _screen_boundary(
        tmp_path, SimpleNamespace(reply=ChatReply([]), actions=[])
    )
    runtime.note_initiative_user_turn()
    _screen_send(boundary, "op-gated")

    assert events[-1]["name"] == "chat.completed"
    assert events[-1]["payload"]["reply"] == {"segments": []}
    assert pipeline.calls == []
    assert store.read_all("sakura") == []
    boundary.close()


def test_quiet_screen_turn_keeps_only_the_observation(tmp_path: Path) -> None:
    runtime, pipeline, boundary, events, store, clock = _screen_boundary(
        tmp_path, SimpleNamespace(reply=ChatReply([]), actions=[], visual_observation=_OBSERVED)
    )
    _screen_send(boundary, "op-quiet")

    assert events[-1]["payload"]["reply"] == {"segments": []}
    prompt = pipeline.calls[0][-1]["content"][0]["text"]
    assert '"silent": true' in prompt
    assert "desktop prompt" not in prompt
    assert "[\u5173\u7cfb\u52a8\u673a]" not in prompt
    kinds = [entry.kind for entry in store.read_all("sakura")]
    assert TimelineKind.ASSISTANT not in kinds
    assert kinds.count(TimelineKind.OBSERVATION) == 2
    assert runtime.screen_gate_reason() == "cooldown"
    boundary.close()


def test_screen_turn_that_speaks_with_a_relationship_motive(tmp_path: Path) -> None:
    reply = ChatReply([ChatSegment("\u305d\u308c\u3001\u4f55\uff1f", "\u4e2d\u6027", "\u90a3\u662f\u4ec0\u4e48\uff1f")])
    runtime, pipeline, boundary, events, store, clock = _screen_boundary(
        tmp_path,
        SimpleNamespace(reply=reply, actions=[], visual_observation=_OBSERVED),
        relationship_ready=True,
    )
    assert runtime.initiative_gate_reason() == "eligible"
    _screen_send(boundary, "op-speak")

    prompt = pipeline.calls[0][-1]["content"][0]["text"]
    assert "[\u5173\u7cfb\u52a8\u673a]" in prompt
    entries = store.read_all("sakura")
    assert [(entry.kind, entry.origin) for entry in entries if entry.kind is TimelineKind.ASSISTANT] == [
        (TimelineKind.ASSISTANT, "proactive")
    ]
    assert runtime.initiative_gate_reason() == "cooldown"
    assert runtime.screen_gate_reason() == "cooldown"
    boundary.close()


def test_ungated_runtimes_keep_the_desktop_prompt(tmp_path: Path) -> None:
    runtime, pipeline, boundary, events, store, clock = _screen_boundary(
        tmp_path, SimpleNamespace(reply=ChatReply([ChatSegment("x", "\u4e2d\u6027", "x")]), actions=[])
    )
    runtime._initiative.screen_cooldown_seconds = None
    _screen_send(boundary, "op-legacy")
    assert pipeline.calls[0][-1]["content"][0]["text"] == "desktop prompt"
    boundary.close()


def test_screen_gate_rereads_the_settings_each_time() -> None:
    runtime = AgentRuntime(SimpleNamespace(), "system", character_id="sakura")
    clock = _Clock()
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=True).normalized(),
        client=None,
        clock=clock,
        idle_seconds=lambda: 0.0,
    )
    state = {"enabled": False}
    runtime.configure_screen_initiative(loader=lambda: (state["enabled"], 600.0))
    clock.now += 10_000
    assert runtime.screen_initiative_gated is True
    assert runtime.screen_gate_reason() == "disabled"
    state["enabled"] = True
    assert runtime.screen_gate_reason() == "eligible"

    def _broken():
        raise ValueError("config")

    runtime.configure_screen_initiative(loader=_broken)
    assert runtime.screen_gate_reason() == "disabled"


def test_adapter_gates_screen_turns_with_the_user_settings(tmp_path: Path) -> None:
    import shutil
    from threading import Event

    from app.agent.tools import ToolRegistry
    from app.core_host.assistant_adapter import AssistantAdapter

    fixture = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"
    root = tmp_path / "root"
    shutil.copytree(fixture, root)
    config = root / "config" / "system_config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    data["screen_awareness"] = {"enabled": False}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    adapter = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None)
    runtime = adapter.initialize(Event()).session.runtime
    try:
        assert runtime.screen_initiative_gated is True
        assert runtime.screen_gate_reason() == "disabled"
    finally:
        runtime.close()


def _legacy_source(tmp_path: Path, system: dict[str, object]) -> tuple[Path, Path]:
    source, staged = tmp_path / "source", tmp_path / "staged"
    config = source / "data" / "config"
    config.mkdir(parents=True)
    (config / "api.yaml").write_text("model_slots: {}\n", encoding="utf-8")
    (config / "system_config.yaml").write_text(yaml.safe_dump(system, allow_unicode=True), encoding="utf-8")
    return source, staged


def _migrated_screen(tmp_path: Path, system: dict[str, object]) -> dict[str, object]:
    from app.legacy_import.configuration import migrate_configuration

    source, staged = _legacy_source(tmp_path, system)
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")
    return yaml.safe_load((staged / "config" / "system_config.yaml").read_text(encoding="utf-8"))["screen_awareness"]


def test_qt_era_proactive_observation_becomes_the_screen_source(tmp_path: Path) -> None:
    screen = _migrated_screen(
        tmp_path,
        {
            "proactive": {
                "enabled": True,
                "timer_seconds": 480.0,
                "cooldown_seconds": 600.0,
                "privacy": {"blocked_processes": ["vault.exe", 3], "blocked_title_keywords": ["secret"]},
            },
            "screen_awareness": {
                "enabled": False,
                "screen_context_enabled": False,
                "check_interval_minutes": 2,
                "screen_context_resolution": "1080p",
            },
        },
    )
    assert screen == {
        "enabled": True,
        "check_interval_minutes": 8,
        "cooldown_minutes": 10,
        "screen_context_batch_limit": 1,
        "screen_context_resolution": "1080p",
        "privacy": {"blocked_processes": ["vault.exe"], "blocked_title_keywords": ["secret"]},
    }


def test_disabled_proactive_observation_stays_off(tmp_path: Path) -> None:
    screen = _migrated_screen(tmp_path, {"proactive": {"enabled": False}})
    assert screen == {"enabled": False}
