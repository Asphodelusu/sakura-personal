"""Core owns self-initiated turns: the desktop only proposes, silence writes nothing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.intimacy import INTIMACY_CONTINUE_SYSTEM_TEXT
from app.agent.runtime import AgentRuntime
from app.core_host.real_chat import RealChatBoundary, RealChatRejection
from app.llm.chat_reply import ChatReply, ChatSegment
from app.storage.timeline import TimelineKind, TimelineStore


GENERATION_ID = "00000000-0000-4000-8000-000000004301"
GENERATION_CREDENTIAL = "43" * 16


class _Pipeline:
    def __init__(self, reply: ChatReply) -> None:
        self.reply = reply
        self.calls: list[list[dict[str, object]]] = []

    def run_user_message(self, messages, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append([dict(message) for message in messages])
        return SimpleNamespace(reply=self.reply, actions=[])

    def run_event(self, _event, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("initiated turns never use the event pipeline")


def _ready(tmp_path: Path, reply: ChatReply | None = None):
    runtime = AgentRuntime(SimpleNamespace(), "system", character_id="sakura", character_name="Sakura")
    runtime.relationship_facts = lambda: "facts"  # type: ignore[method-assign]
    pipeline = _Pipeline(reply or ChatReply([ChatSegment("ねえ。", "中性", "喂。")]))
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
    return runtime, pipeline, boundary, events, store


def _request(operation_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": operation_id,
        "kind": "request",
        "name": "chat.send",
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "payload": {"operationId": operation_id, **payload},
    }


def _initiated(operation_id: str, kind: str) -> dict[str, object]:
    return _request(operation_id, {"event": {"type": kind, "payload": {}}})


def _run(boundary: RealChatBoundary, request: dict[str, object]) -> None:
    boundary.reserve_send(request)
    boundary.handle_send(request)


def _entries(store: TimelineStore):
    return store.read_all("sakura")


@pytest.mark.parametrize("kind", ["intimacy_continue", "relationship_initiative"])
def test_initiated_events_are_accepted_with_an_empty_payload(tmp_path: Path, kind: str) -> None:
    _runtime, _pipeline, boundary, _events, _store = _ready(tmp_path)
    assert boundary._validate_send(_initiated("op-ok", kind))["event"]["type"] == kind
    boundary.close()


@pytest.mark.parametrize(
    "event",
    [
        {"type": "relationship_initiative", "payload": {"message": "x"}},
        {"type": "intimacy_continue", "payload": None},
        {"type": "screen_peek", "payload": {}},
        {"type": "relationship_initiative"},
    ],
)
def test_initiated_events_reject_any_other_shape(tmp_path: Path, event: dict[str, object]) -> None:
    _runtime, _pipeline, boundary, _events, _store = _ready(tmp_path)
    with pytest.raises(RealChatRejection):
        boundary._validate_send(_request("op-bad", {"event": event}))
    boundary.close()


def test_relationship_initiative_that_speaks_commits_one_proactive_assistant_entry(
    tmp_path: Path,
) -> None:
    runtime, pipeline, boundary, events, store = _ready(tmp_path)
    seen: dict[str, object] = {}

    def _decide(recent_messages, *, relationship_facts, cancel_checker=None):  # type: ignore[no-untyped-def]
        seen["facts"] = relationship_facts
        seen["recent"] = recent_messages
        return ChatReply([ChatSegment("ねえ、起きてる？", "温柔", "喂，醒着吗？")])

    runtime.run_relationship_initiative = _decide  # type: ignore[method-assign]
    _run(boundary, _initiated("op-rel", "relationship_initiative"))

    assert events[-1]["name"] == "chat.completed"
    segments = events[-1]["payload"]["reply"]["segments"]
    assert [segment["text"] for segment in segments] == ["ねえ、起きてる？"]
    entries = _entries(store)
    assert [(entry.kind, entry.origin) for entry in entries] == [(TimelineKind.ASSISTANT, "proactive")]
    assert seen["facts"] == "facts"
    assert pipeline.calls == []
    boundary.close()


def test_relationship_initiative_that_stays_quiet_writes_nothing(tmp_path: Path) -> None:
    runtime, pipeline, boundary, events, store = _ready(tmp_path)
    runtime.run_relationship_initiative = lambda *_a, **_k: None  # type: ignore[method-assign]
    _run(boundary, _initiated("op-quiet", "relationship_initiative"))

    assert events[-1]["name"] == "chat.completed"
    assert events[-1]["payload"]["reply"] == {"segments": []}
    assert _entries(store) == []
    assert pipeline.calls == []
    boundary.close()


def test_intimacy_continue_outside_intimacy_is_silent_and_never_calls_the_model(
    tmp_path: Path,
) -> None:
    runtime, pipeline, boundary, events, store = _ready(tmp_path)
    runtime.configure_intimacy("guide")
    _run(boundary, _initiated("op-idle", "intimacy_continue"))

    assert events[-1]["name"] == "chat.completed"
    assert events[-1]["payload"]["reply"] == {"segments": []}
    assert pipeline.calls == []
    assert _entries(store) == []
    boundary.close()


def test_intimacy_continue_sends_the_system_signal_and_stops_after_three(tmp_path: Path) -> None:
    runtime, pipeline, boundary, events, store = _ready(tmp_path)
    runtime.configure_intimacy("guide")
    runtime.intimacy_state.enter(by_keyword=True)

    for index in range(4):
        _run(boundary, _initiated(f"op-c{index}", "intimacy_continue"))

    assert len(pipeline.calls) == 3
    tail = pipeline.calls[0][-1]
    assert tail["role"] == "system"
    assert tail["content"] == INTIMACY_CONTINUE_SYSTEM_TEXT
    assert "source" not in tail
    assert events[-1]["payload"]["reply"] == {"segments": []}
    entries = _entries(store)
    assert [(entry.kind, entry.origin) for entry in entries] == [(TimelineKind.ASSISTANT, "proactive")] * 3
    assert runtime.intimacy_state.active is True
    boundary.close()


def test_user_turn_invalidates_in_flight_initiative_and_refreshes_continuations(
    tmp_path: Path,
) -> None:
    runtime, _pipeline, boundary, _events, _store = _ready(tmp_path)
    runtime.configure_intimacy("guide")
    runtime.intimacy_state.enter(by_keyword=True)
    for index in range(3):
        _run(boundary, _initiated(f"op-c{index}", "intimacy_continue"))
    attempt = runtime._initiative.begin_attempt()

    _run(boundary, _request("op-user", {"message": "在吗"}))

    assert runtime._initiative.is_current(attempt) is False
    assert runtime.begin_intimacy_continuation() is True
    boundary.close()
