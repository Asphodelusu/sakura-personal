"""Bounded inner-thought lifecycle on the real user-turn path."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from app.agent.inner_thought import InnerThoughtSettings
from app.agent.runtime import AgentRuntime
from app.core.chat_pipeline import ChatPipeline
from app.core.relational_drive import RelationalDriveProfile
from app.core_host.real_chat import RealChatBoundary, _ScreenAttachment
from app.llm.api_client import ApiRequestError
from app.llm.chat_reply import ChatReply, ChatSegment
from app.storage.paths import StoragePaths
from app.storage.timeline import TimelineStore


GENERATION_ID = "00000000-0000-4000-8000-000000004108"
GENERATION_CREDENTIAL = "42" * 16
THOUGHT = "雨なら傘を持てばいい。"


def test_cancel_interrupts_join_while_thought_provider_is_blocked() -> None:
    from app.core.cancellation import OperationCancelled

    entered, release, joining, cancelled = Event(), Event(), Event(), Event()
    runtime = _runtime(_BlockingClient([], entered, release), join_timeout=3)
    errors = []

    def check():
        joining.set()
        if cancelled.is_set():
            raise OperationCancelled()

    def join():
        try:
            runtime.join_inner_thought("cancel-wait", cancel_checker=check)
        except OperationCancelled as error:
            errors.append(error)

    waiter = Thread(target=join)
    try:
        assert runtime.start_inner_thought("cancel-wait", [])
        assert entered.wait(2)
        waiter.start()
        assert joining.wait(2)
        cancelled.set()
        waiter.join(0.75)
        assert not waiter.is_alive(), "cancellation waited for the thought join timeout"
        assert len(errors) == 1
        assert runtime._inner_thought.fragment() is None
    finally:
        runtime.close()
        release.set()
        if waiter.ident is not None:
            waiter.join(2)
        assert runtime._inner_thought.wait_until_idle(2)


def test_cancel_arriving_during_join_cannot_publish():
    import pytest
    from app.core.cancellation import OperationCancelled

    runtime = _runtime(_ReadyClient("interest: mid\nprivate thought"), join_timeout=3)
    runtime.start_inner_thought("race", [{"role": "user", "content": "hi"}])
    assert runtime._inner_thought.wait_until_idle(2)
    cancelled = Event()
    work = runtime._inner_thought._active

    class Wake:
        def wait(self, _timeout):
            cancelled.set()
            return True

        def set(self):
            pass

    work.wake = Wake()

    def check():
        if cancelled.is_set():
            raise OperationCancelled()

    with pytest.raises(OperationCancelled):
        runtime.join_inner_thought("race", cancel_checker=check)
    assert runtime._inner_thought.fragment() is None


def test_actual_http_open_remains_owned_until_it_finishes(monkeypatch):
    from app.llm.api_client import ApiSettings, OpenAICompatibleClient
    from io import BytesIO

    release, entered = Event(), Event()

    def opener(*_args, **_kwargs):
        entered.set()
        release.wait(5)
        return BytesIO(b'{"choices":[{"message":{"content":"interest: low\\nlate"}}]}')

    monkeypatch.setattr("app.llm.api_client.urlopen_direct_for_loopback", opener)
    client = OpenAICompatibleClient(ApiSettings(base_url="https://fixture.invalid/v1", api_key="synthetic", model="test", timeout_seconds=1), request_attempts=1)
    runtime = _runtime(client, join_timeout=0)
    try:
        runtime.start_inner_thought("first", [{"role": "user", "content": "hi"}])
        assert entered.wait(2)
        runtime.join_inner_thought("first")
        assert not runtime._inner_thought.wait_until_idle(0.2)
        assert not runtime.start_inner_thought("second", [])
    finally:
        release.set()
        runtime._inner_thought.wait_until_idle(2)
        runtime.close()
    assert runtime._inner_thought.fragment() is None
APPRAISAL = (
    "interest: mid\n"
    "drive_kind: attachment_longing\n"
    "drive_shift: rise\n"
    "drive_strength: mild\n"
    f"{THOUGHT}"
)


def test_blocked_request_timeout_discards_late_result_and_refuses_a_second_call() -> None:
    release = Event()
    entered = Event()
    calls: list[str] = []
    client = _BlockingClient(calls, entered, release)
    runtime = _runtime(client, join_timeout=0)
    assert runtime.start_inner_thought("op-a", [{"role": "user", "content": "在吗"}]) is True
    assert entered.wait(2)
    runtime.join_inner_thought("op-a")
    assert runtime._inner_thought.fragment() is None  # type: ignore[attr-defined]
    assert runtime.start_inner_thought("op-b", [{"role": "user", "content": "还在"}]) is False
    assert calls == ["op-a"]
    client.text = "interest: low\nNOW"
    client.turn_id = "op-c"
    release.set()
    assert runtime._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    assert runtime._inner_thought.fragment() is None  # type: ignore[attr-defined]
    assert runtime.start_inner_thought("op-c", [{"role": "user", "content": "现在"}]) is True
    assert runtime._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    runtime.join_inner_thought("op-c")
    fragment = runtime._inner_thought.fragment()  # type: ignore[attr-defined]
    assert fragment is not None
    assert "NOW" in fragment.content
    assert THOUGHT not in fragment.content
    assert calls == ["op-a", "op-c"]


def test_close_and_replacement_drop_work_without_waiting() -> None:
    release = Event()
    entered = Event()
    calls: list[str] = []
    client = _BlockingClient(calls, entered, release)
    runtime = _runtime(client, join_timeout=3)
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=True,
        profile=RelationalDriveProfile.natural_default(),
        state_path=None,
        character_id="sakura",
    )
    runtime._inner_thought._appraisal_calls = []  # type: ignore[attr-defined]

    def _sink(turn_id: str, _appraisal: object) -> bool:
        runtime._inner_thought._appraisal_calls.append(turn_id)  # type: ignore[attr-defined]
        return True

    runtime._inner_thought._appraisal_sink = _sink  # type: ignore[attr-defined]
    assert runtime.start_inner_thought("op-close", [{"role": "user", "content": "hi"}]) is True
    assert entered.wait(2)
    runtime.close()
    assert not release.is_set()
    release.set()
    assert runtime._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    assert runtime._inner_thought.fragment() is None  # type: ignore[attr-defined]
    assert runtime._inner_thought._appraisal_calls == []  # type: ignore[attr-defined]
    assert runtime.start_inner_thought("op-after-close", [{"role": "user", "content": "x"}]) is False

    release.clear()
    entered.clear()
    calls.clear()
    replacement = _runtime(client, join_timeout=0)
    replacement.start_inner_thought("op-old", [{"role": "user", "content": "old"}])
    assert entered.wait(2)
    replacement.update_character("other", character_id="other", character_name="Other")
    release.set()
    assert replacement._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    replacement.join_inner_thought("op-old")
    assert replacement._inner_thought.fragment() is None  # type: ignore[attr-defined]


def test_duplicate_appraisal_rejected_and_missing_profile_skips_only_appraisal(tmp_path: Path) -> None:
    client = _ReadyClient(APPRAISAL)
    path = StoragePaths(tmp_path).relational_drive_for("sakura")
    runtime = _runtime(client, join_timeout=3)
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=True,
        profile=RelationalDriveProfile.natural_default(),
        state_path=path,
        character_id="sakura",
    )
    runtime.begin_relationship_user_turn("op-dup")
    assert runtime.start_inner_thought("op-dup", [{"role": "user", "content": "在吗"}]) is True
    runtime.join_inner_thought("op-dup")
    assert runtime.start_inner_thought("op-dup", [{"role": "user", "content": "在吗"}]) is True
    runtime.join_inner_thought("op-dup")
    keys = json.loads(path.read_text(encoding="utf-8"))["settled_keys"]
    assert sum(key.endswith(":op-dup:appraisal") for key in keys) == 1

    bare = _runtime(_ReadyClient(APPRAISAL), join_timeout=3)
    bare.configure_relationship_drive(
        enabled=False,
        in_turn_enabled=True,
        profile=None,
        state_path=tmp_path / "missing.json",
        character_id="sakura",
    )
    assert bare.start_inner_thought("op-none", [{"role": "user", "content": "在吗"}]) is True
    bare.join_inner_thought("op-none")
    fragment = bare._inner_thought.fragment()  # type: ignore[attr-defined]
    assert fragment is not None
    assert THOUGHT in fragment.content
    assert not (tmp_path / "missing.json").exists()


def test_disabled_fast_and_proactive_make_zero_calls() -> None:
    calls: list[int] = []
    client = _ReadyClient(APPRAISAL, calls)
    disabled = _runtime(client, join_timeout=0, enabled=False)
    assert disabled.start_inner_thought("op", [{"role": "user", "content": "hi"}]) is False
    fast = _runtime(client, join_timeout=0)
    assert fast.start_inner_thought("op", [{"role": "user", "content": "hi"}], turn_tier="fast") is False
    proactive = _runtime(client, join_timeout=0)
    assert proactive.start_inner_thought(
        "op",
        [{"role": "user", "content": "hi"}],
        proactive_mode=True,
    ) is False
    assert calls == []


def test_user_turn_appraisal_is_private_and_survives_later_chat_failure(tmp_path: Path) -> None:
    contexts: list[str] = []
    thought_kwargs: list[dict[str, object]] = []
    runtime, boundary, events, path = _core(
        tmp_path,
        _ReadyClient(APPRAISAL, kwargs=thought_kwargs),
        _MainClient(contexts),
    )
    request = _request("op-ok")
    boundary.reserve_send(request)
    boundary.handle_send(request)
    assert events[-1]["name"] == "chat.completed"
    assert THOUGHT in contexts[0]
    assert "runtime.inner_thought" not in json.dumps(events[-1]["payload"])
    assert THOUGHT not in json.dumps(events[-1]["payload"])
    assert set(thought_kwargs[0]) <= {"temperature", "max_tokens", "cancel_checker"}
    keys = json.loads(path.read_text(encoding="utf-8"))["settled_keys"]
    assert sum(key.endswith(":op-ok:appraisal") for key in keys) == 1
    assert sum(key.endswith(":op-ok:effect") for key in keys) == 1
    boundary.close()

    failed_contexts: list[str] = []
    failed_runtime, failed_boundary, failed_events, failed_path = _core(
        tmp_path / "failed",
        _ReadyClient(APPRAISAL),
        _MainClient(failed_contexts, fail=True),
    )
    failed = _request("op-fail")
    failed_boundary.reserve_send(failed)
    failed_boundary.handle_send(failed)
    assert failed_events[-1]["name"] == "chat.failed"
    assert THOUGHT in failed_contexts[0]
    failed_keys = json.loads(failed_path.read_text(encoding="utf-8"))["settled_keys"]
    assert sum(key.endswith(":op-fail:appraisal") for key in failed_keys) == 1
    assert not any(key.endswith(":effect") for key in failed_keys)
    failed_boundary.close()
    failed_runtime.close()
    runtime.close()


def test_screen_and_update_events_do_not_start_inner_thought(tmp_path: Path) -> None:
    calls: list[int] = []
    runtime, boundary, events, _path = _core(tmp_path, _ReadyClient(APPRAISAL, calls), _MainClient([]))
    _stage_screen(boundary, "screen-" + "ab" * 16, source="screen_awareness")
    scheduled = _request("op-screen", attachment_id="screen-" + "ab" * 16)
    boundary.reserve_send(scheduled)
    boundary.handle_send(scheduled)
    runtime._inner_thought.wait_until_idle(1)  # type: ignore[attr-defined]
    assert calls == []

    _stage_screen(boundary, "screen-" + "cd" * 16, source="manual")
    manual = _request("op-manual", attachment_id="screen-" + "cd" * 16)
    boundary.reserve_send(manual)
    boundary.handle_send(manual)
    assert runtime._inner_thought.wait_until_idle(2)  # type: ignore[attr-defined]
    assert calls == [1]

    update = _request("op-update")
    update["payload"] = {
        "operationId": "op-update",
        "event": {
            "type": "update_available",
            "payload": {
                "currentVersion": "1.0.0",
                "version": "1.2.0",
                "notes": "notes",
                "pubDate": "2026-08-29T08:00:00Z",
                "mode": "installed",
            },
        },
    }
    before = len(calls)
    boundary.reserve_send(update)
    boundary.handle_send(update)
    runtime._inner_thought.wait_until_idle(1)  # type: ignore[attr-defined]
    assert len(calls) == before
    assert events[-1]["name"] == "chat.completed"
    boundary.close()


class _BlockingClient:
    def __init__(self, calls: list[str], entered: Event, release: Event) -> None:
        self.calls = calls
        self.entered = entered
        self.release = release
        self.text = APPRAISAL
        self.turn_id = ""

    def complete_raw(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append(self.turn_id or "op-a")
        self.entered.set()
        self.release.wait()
        return self.text


class _ReadyClient:
    def __init__(
        self,
        text: str,
        calls: list[int] | None = None,
        kwargs: list[dict[str, object]] | None = None,
    ) -> None:
        self.text = text
        self.calls = calls if calls is not None else []
        self.kwargs = kwargs if kwargs is not None else []

    def complete_raw(self, *_args: object, **kwargs: object) -> str:
        self.calls.append(1)
        self.kwargs.append(dict(kwargs))
        return self.text


class _MainClient:
    def __init__(self, contexts: list[str], *, fail: bool = False) -> None:
        self.contexts = contexts
        self.fail = fail
        self.settings = SimpleNamespace(
            context_window_tokens=32768,
            context_window_source="fallback",
            max_tokens=None,
            model="chat-model",
            timeout_seconds=60,
        )

    def resolve_dialogue_params(self) -> tuple[float, dict[str, object]]:
        return 0.8, {}

    def complete_with_tools(self, *_args: object, **kwargs: object) -> SimpleNamespace:
        self.contexts.append(str(kwargs.get("runtime_context") or ""))
        if self.fail:
            raise ApiRequestError("provider failed")
        return SimpleNamespace(
            content=(
                '{"segments":[{"ja":"うん","zh":"嗯","tone":"中性"}],'
                '"drive_effect":{"event":"mutual_affection","strength":"mild"}}'
            ),
            tool_calls=[],
            runtime_context_role="system",
            trace_call=None,
        )

    def chat(self, *_args: object, **_kwargs: object) -> ChatReply:
        return ChatReply([ChatSegment("うん", "中性", "嗯")])


def _runtime(client: object, *, join_timeout: int, enabled: bool = True) -> AgentRuntime:
    runtime = AgentRuntime(object(), "system prompt", character_id="sakura", character_name="Sakura")
    runtime.configure_inner_thought(
        settings=InnerThoughtSettings(enabled=enabled, join_timeout_seconds=join_timeout),
        client=client,
        source_slot="chat",
    )
    return runtime


def _core(tmp_path: Path, thought_client: object, main_client: _MainClient):
    runtime = AgentRuntime(
        main_client,
        "system prompt",
        character_id="sakura",
        character_name="Sakura",
        reply_tones=["中性"],
        strict_provider_errors=True,
    )
    path = StoragePaths(tmp_path).relational_drive_for("sakura")
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=True,
        profile=RelationalDriveProfile.natural_default(),
        state_path=path,
        character_id="sakura",
    )
    runtime.configure_inner_thought(
        settings=InnerThoughtSettings(join_timeout_seconds=3),
        client=thought_client,
        source_slot="inner_thought",
    )
    events: list[dict[str, object]] = []
    store = TimelineStore(tmp_path / "timeline.sqlite3")
    store.initialize()
    boundary = RealChatBoundary(
        GENERATION_ID,
        GENERATION_CREDENTIAL,
        tmp_path,
        session_provider=lambda: SimpleNamespace(
            character=SimpleNamespace(id="sakura", display_name="Sakura"),
            runtime=runtime,
            pipeline=ChatPipeline(runtime, finalize_trace_operations=False),
        ),
        timeline_store=store,
        event_publisher=events.append,
    )
    return runtime, boundary, events, path


def _request(operation_id: str, *, attachment_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"message": "在吗", "operationId": operation_id}
    if attachment_id is not None:
        payload["attachmentId"] = attachment_id
    return {
        "id": operation_id,
        "kind": "request",
        "name": "chat.send",
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "payload": payload,
    }


def _stage_screen(boundary: RealChatBoundary, attachment_id: str, *, source: str) -> None:
    observation = SimpleNamespace(
        data_url="data:image/jpeg;base64,aa",
        width=2,
        height=2,
        captured_at="2026-09-01T00:00:00+00:00",
        screen_name="desk",
    )
    boundary._pending_screen_attachment = _ScreenAttachment(
        attachment_id=attachment_id,
        observations=(observation,),
        item_ids=("shot-" + "a" * 32,),
        source=source,
        visual_id=None,
    )
