"""Core completion settles one drive effect only after a committed user turn."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.agent.runtime import AgentRuntime
from app.core.interaction import interaction_context
from app.core.relational_drive import DriveEffect, RelationalDriveProfile
from app.core_host.real_chat import RealChatBoundary, _ScreenAttachment
from app.llm.chat_reply import ChatReply, ChatSegment
from app.storage.paths import StoragePaths
from app.storage.timeline import TimelineKind, TimelineStore


GENERATION_ID = "00000000-0000-4000-8000-000000004107"
GENERATION_CREDENTIAL = "41" * 16


def test_completed_user_turn_settles_rebuilt_reply_once(tmp_path: Path) -> None:
    effect = DriveEffect(event="mutual_affection", strength="mild")
    original = ChatReply(
        [ChatSegment("……好き。", "中性", "……喜欢。")],
        drive_effect=effect,
    )
    delivered = ChatReply([replace(segment, control=None) for segment in original.segments])
    runtime, boundary, events, path = _ready(tmp_path, delivered)
    request = _request("op-complete")

    boundary.reserve_send(request)
    boundary.handle_send(request)

    assert events[-1]["name"] == "chat.completed"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert sum(key.endswith(":op-complete:effect") for key in payload["settled_keys"]) == 1
    assert payload["last_affectionate_contact_at"]
    before = path.read_text(encoding="utf-8")
    assert runtime.settle_relationship_reply("op-complete", delivered, character_id="sakura") is False
    assert path.read_text(encoding="utf-8") == before
    boundary.close()


def test_manual_screenshot_contacts_and_screen_awareness_does_not(tmp_path: Path) -> None:
    effect = DriveEffect(event="aftercare", strength="subtle")
    reply = ChatReply([ChatSegment("見てる。", "中性", "我看着。")], drive_effect=effect)
    runtime, boundary, _events, path = _ready(tmp_path, reply)
    _stage_screen(boundary, "screen-" + "ab" * 16, source="manual")
    manual = _request("op-manual", attachment_id="screen-" + "ab" * 16)
    boundary.reserve_send(manual)
    boundary.handle_send(manual)
    manual_keys = json.loads(path.read_text(encoding="utf-8"))["settled_keys"]
    assert any(key.endswith(":op-manual:contact") for key in manual_keys)
    assert any(key.endswith(":op-manual:effect") for key in manual_keys)

    _stage_screen(boundary, "screen-" + "cd" * 16, source="screen_awareness")
    scheduled = _request("op-screen", attachment_id="screen-" + "cd" * 16)
    boundary.reserve_send(scheduled)
    boundary.handle_send(scheduled)
    keys = json.loads(path.read_text(encoding="utf-8"))["settled_keys"]
    assert not any("op-screen" in key for key in keys)
    assert runtime.settle_relationship_reply("op-screen", reply, character_id="sakura") is False
    boundary.close()


def test_update_cancel_empty_write_failure_and_wrong_id_do_not_settle(tmp_path: Path) -> None:
    effect = DriveEffect(event="fulfilled", strength="mild")
    reply = ChatReply([ChatSegment("できた。", "中性", "好了。")], drive_effect=effect)
    runtime, boundary, events, path = _ready(tmp_path, reply)

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
    boundary.reserve_send(update)
    boundary.handle_send(update)
    assert events[-1]["name"] == "chat.completed"
    assert not path.exists()

    cancelled = _request("op-cancel")
    boundary.reserve_send(cancelled)
    boundary._executions["op-cancel"].cancel.cancel()
    boundary.handle_send(cancelled)
    assert events[-1]["name"] == "chat.cancelled"
    assert not path.exists()

    empty_runtime, empty_boundary, empty_events, empty_path = _ready(
        tmp_path / "empty",
        ChatReply([ChatSegment("   ")]),
    )
    empty_request = _request("op-empty")
    empty_boundary.reserve_send(empty_request)
    empty_boundary.handle_send(empty_request)
    assert empty_events[-1]["name"] == "chat.completed"
    assert not empty_path.exists() or _effect_keys(empty_path) == []
    empty_boundary.close()

    failing = _request("op-write")
    store = boundary._timeline
    original_append = store.append

    def _fail_assistant(entry):  # type: ignore[no-untyped-def]
        if entry.kind is TimelineKind.ASSISTANT:
            raise OSError("disk")
        return original_append(entry)

    store.append = _fail_assistant  # type: ignore[method-assign]
    boundary.reserve_send(failing)
    boundary.handle_send(failing)
    store.append = original_append  # type: ignore[method-assign]
    assert events[-1]["name"] == "chat.failed"
    assert _effect_keys(path) == []

    mismatched = _request("op-mismatch")
    boundary.reserve_send(mismatched)
    with interaction_context("other-turn"):
        boundary.handle_send(mismatched)
    assert events[-1]["name"] == "chat.completed"
    assert not any("op-mismatch:effect" in key or "other-turn:effect" in key for key in _keys(path))
    assert runtime.settle_relationship_reply("op-mismatch", reply, character_id="sakura") is False
    boundary.close()


def test_closed_role_and_settlement_error_do_not_fail_completed_chat(tmp_path: Path) -> None:
    effect = DriveEffect(event="hesitation", strength="mild")
    reply = ChatReply([ChatSegment("待って。", "中性", "等一下。")], drive_effect=effect)

    def _close(runtime: AgentRuntime, _messages, **_kwargs):  # type: ignore[no-untyped-def]
        runtime.close()
        return SimpleNamespace(reply=reply, actions=[])

    _runtime, boundary, events, path = _ready(tmp_path / "closed", reply, pipeline_call=_close)
    request = _request("op-closed")
    boundary.reserve_send(request)
    boundary.handle_send(request)
    assert events[-1]["name"] == "chat.completed"
    assert _effect_keys(path) == []
    boundary.close()

    def _replace(runtime: AgentRuntime, _messages, **_kwargs):  # type: ignore[no-untyped-def]
        runtime.update_character("other", character_id="other")
        return SimpleNamespace(reply=reply, actions=[])

    _runtime, replaced, replaced_events, replaced_path = _ready(
        tmp_path / "replaced",
        reply,
        pipeline_call=_replace,
    )
    replaced_request = _request("op-replaced")
    replaced.reserve_send(replaced_request)
    replaced.handle_send(replaced_request)
    assert replaced_events[-1]["name"] == "chat.completed"
    assert _effect_keys(replaced_path) == []
    replaced.close()

    def _boom(runtime: AgentRuntime, _messages, **_kwargs):  # type: ignore[no-untyped-def]
        original = runtime.settle_relationship_reply

        def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret dialogue")

        runtime.settle_relationship_reply = _raise  # type: ignore[method-assign]
        runtime._original_settle = original  # type: ignore[attr-defined]
        return SimpleNamespace(reply=reply, actions=[])

    _runtime, broken, broken_events, broken_path = _ready(
        tmp_path / "broken",
        reply,
        pipeline_call=_boom,
    )
    broken_request = _request("op-broken")
    broken.reserve_send(broken_request)
    broken.handle_send(broken_request)
    assert broken_events[-1]["name"] == "chat.completed"
    assert broken_events[-1]["payload"]["reply"]["segments"][0]["text"] == "待って。"
    assert "drive_effect" not in json.dumps(broken_events[-1]["payload"])
    assert _effect_keys(broken_path) == []
    broken.close()


def _ready(
    tmp_path: Path,
    reply: ChatReply,
    *,
    pipeline_call=None,
):
    runtime = AgentRuntime(SimpleNamespace(), "system", character_id="sakura", character_name="Sakura")
    path = StoragePaths(tmp_path).relational_drive_for("sakura")
    runtime.configure_relationship_drive(
        enabled=True,
        in_turn_enabled=True,
        profile=RelationalDriveProfile.natural_default(),
        state_path=path,
        character_id="sakura",
    )
    events: list[dict[str, object]] = []

    class Pipeline:
        def run_user_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            if pipeline_call is not None:
                return pipeline_call(runtime, messages, **kwargs)
            return SimpleNamespace(reply=reply, actions=[])

        def run_event(self, _event, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(reply=reply, actions=[SimpleNamespace(type="event")])

    session = SimpleNamespace(
        character=SimpleNamespace(id="sakura", display_name="Sakura"),
        runtime=runtime,
        pipeline=Pipeline(),
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


def _keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8"))["settled_keys"])


def _effect_keys(path: Path) -> list[str]:
    return [key for key in _keys(path) if key.endswith(":effect")]
