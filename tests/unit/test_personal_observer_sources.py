"""Ephemeral window text reaches decisions, content gates and no dialogue history."""

import json
from threading import Event

import pytest

from app.agent.focus_observer import FocusGate, FocusObserver, snapshot_from_mapping
from app.agent.screen_observation import ScreenObservation
from app.agent.tools import ToolRegistry
from app.core_host.assistant_adapter import AssistantAdapter
from app.core_host.real_chat import RealChatBoundary
from app.llm.api_client import OpenAICompatibleClient, messages_contain_image
from app.storage.timeline import TimelineKind, TimelineStore
from test_personal_observer_evaluation import VISION_JSON, _root

GENERATION = "00000000-0000-4000-8000-000000004711"
CREDENTIAL = "47" * 16
BODY = "UIA_VISIBLE_MARKER_" + "可见正文" * 350 + "_UIA_TAIL_NOT_FOR_DECISION"
CONTEXT = {"process": "editor.exe", "visibleText": BODY, "visibleTextSource": "uia"}


def _request(name, payload):
    return {"id": name, "kind": "request", "name": name,
            "generationId": GENERATION, "generationCredential": CREDENTIAL, "payload": payload}


@pytest.mark.parametrize("body,source", [(BODY, "uia"), ("too short", "vlm")], ids=["uia", "vlm-fallback"])
def test_scheduled_window_text_reaches_only_fast_and_never_becomes_dialogue(tmp_path, monkeypatch, body, source):
    payloads, authorized, events = [], [], []

    def fake_post(self, payload, *, cancel_checker=None):
        payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        content = VISION_JSON if self.settings.model == "vision-model" else json.dumps({
            "should_speak": False, "situational_summary": "彼は説明を読んでいる。",
        }, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    monkeypatch.setattr("app.core_host.screen_capture.consume_screen_resource", lambda *_args, **_kwargs:
        ScreenObservation("data:image/jpeg;base64,AAAA", 3, 2, "2026-10-01T06:00:00Z", "fixture"))
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    boundary = None
    try:
        session = adapter.initialize(Event()).session
        assert session is not None
        session.runtime._initiative._last_user_at = -1_000_000
        session.runtime._initiative._idle_seconds = lambda: 0
        timeline = TimelineStore(tmp_path / "source-proof.sqlite3")
        timeline.initialize()
        boundary = RealChatBoundary(GENERATION, CREDENTIAL, tmp_path,
            session_provider=lambda: session, timeline_store=timeline,
            event_publisher=events.append, segment_authorizer=lambda **kwargs: authorized.append(kwargs))
        attached = boundary.handle_screen_attach_batch(_request("screen.attachBatch", {
            "resources": [{}], "observerContext": {**CONTEXT, "visibleText": body},
        }))
        send = _request("chat.send", {"operationId": "window-text", "message": "scheduled screen",
                                     "attachmentId": attached["payload"]["attachmentId"]})
        send["id"] = "window-text"
        boundary.reserve_send(send)
        boundary.handle_send(send)
        assert events[-1]["name"] == "chat.completed"
        assert events[-1]["payload"]["reply"]["segments"] == []
        assert [p["model"] for p in payloads] == ["vision-model", "fast-model"]
        assert [messages_contain_image(p["messages"]) for p in payloads] == [True, False]
        vision = json.dumps(payloads[0], ensure_ascii=False)
        fast = json.dumps(payloads[1], ensure_ascii=False)
        assert "UIA_VISIBLE_MARKER" not in vision
        assert ("UIA_VISIBLE_MARKER" in fast) == (source == "uia")
        assert "UIA_TAIL_NOT_FOR_DECISION" not in fast
        assert source in fast.lower()
        if source == "vlm":
            assert "可见短句" in fast
        assert authorized == []
        entries = timeline.read_all("sakura")
        assert entries
        assert all(entry.kind == TimelineKind.OBSERVATION for entry in entries)
        assert "UIA_VISIBLE_MARKER" not in json.dumps([e.payload for e in entries], ensure_ascii=False)
        logs = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.log"))
        assert "UIA_VISIBLE_MARKER" not in logs
    finally:
        if boundary is not None:
            boundary.close()
        adapter.close()


@pytest.mark.parametrize("context", [
    {**CONTEXT, "visibleText": "x" * 2001}, {**CONTEXT, "visibleText": []},
    {**CONTEXT, "visibleTextSource": "user"}, {**CONTEXT, "unknown": True},
])
def test_invalid_source_metadata_is_rejected_before_resource_consumption(tmp_path, monkeypatch, context):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid metadata must precede image reads")

    monkeypatch.setattr("app.core_host.screen_capture.consume_screen_resource", unexpected)
    boundary = RealChatBoundary(GENERATION, CREDENTIAL, tmp_path, session_provider=lambda: None)
    try:
        with pytest.raises(ValueError):
            boundary.handle_screen_attach_batch(_request("screen.attachBatch", {
                "resources": [{}], "observerContext": context,
            }))
    finally:
        boundary.close()


def test_visible_body_change_triggers_after_quiet_without_resetting_focus():
    stamp = [100.0]
    observer = FocusObserver(clock=lambda: stamp[0], timer_seconds=10_000)

    def advance(body, title="notes"):
        return observer.advance(snapshot_from_mapping({
            "hwnd": 1, "pid": 1, "process": "editor.exe", "title": title, "visibleText": body,
        }), scope="a", gate=FocusGate())

    assert advance("A" * 40)["trigger"] == ""
    stamp[0] = 131
    assert advance("B" * 40, title="renamed")["trigger"] == "content"
    assert observer.publish_perception(scope="a", app_key="editor.exe|1", interval=600, content_quiet=600)
    stamp[0] = 162
    assert advance("C" * 40)["trigger"] == ""
    stamp[0] = 735
    assert advance("C" * 40)["trigger"] == "content"
    observer.set_away_mode(True)
    stamp[0] = 800
    assert advance("D" * 40)["trigger"] == ""


@pytest.mark.parametrize("gate", [
    FocusGate(enabled=False), FocusGate(busy=True), FocusGate(continuation=True),
    FocusGate(silence=True), FocusGate(cooldown=True),
])
def test_content_reader_permission_precedes_body_collection(gate):
    observer = FocusObserver(clock=lambda: 100.0)
    observer.advance(snapshot_from_mapping({
        "hwnd": 1, "pid": 1, "process": "editor.exe", "title": "notes",
    }), scope="a", gate=gate)
    assert not observer.content_read_allowed(gate)


def test_content_reader_rejects_private_self_and_away_windows():
    for snapshot, processes, titles in [
        ({"process": "private.exe"}, ("private.exe",), ()),
        ({"title": "password vault"}, (), ("password",)),
        ({"ownProcess": True}, (), ()),
    ]:
        observer = FocusObserver(clock=lambda: 100.0)
        gate = FocusGate()
        observer.advance(snapshot_from_mapping({
            "hwnd": 1, "pid": 1, "process": "editor.exe", "title": "notes", **snapshot,
        }), scope="a", gate=gate, blocked_processes=processes, blocked_titles=titles)
        assert not observer.content_read_allowed(gate, blocked_processes=processes, blocked_titles=titles)
    observer = FocusObserver(clock=lambda: 100.0)
    observer.advance(snapshot_from_mapping({"hwnd": 1, "process": "editor.exe"}), scope="a", gate=FocusGate())
    observer.set_away_mode(True)
    assert not observer.content_read_allowed(FocusGate())


def test_unchanged_visual_summary_skips_fast_but_content_trigger_still_decides(tmp_path, monkeypatch):
    models = []

    def fake_post(self, payload, *, cancel_checker=None):
        models.append(payload["model"])
        content = VISION_JSON if payload["model"] == "vision-model" else '{"should_speak":false}'
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        runtime = adapter.initialize(Event()).session.runtime
        observer = runtime._focus_observer
        stamp = [100.0]
        observer.clock = lambda: stamp[0]
        observer.advance(snapshot_from_mapping({"hwnd": 1, "process": "editor.exe", "title": "notes", "visibleText": "A" * 40}),
                         scope="a", gate=FocusGate())
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
        for _ in range(2):
            runtime.handle_user_message(messages, screen_awareness_mode=True)
        assert models == ["vision-model", "fast-model", "vision-model"]
        stamp[0] = 1000
        assert observer.advance(snapshot_from_mapping({"hwnd": 1, "process": "editor.exe", "title": "notes", "visibleText": "B" * 40}),
                                scope="a", gate=FocusGate())["trigger"] == "content"
        runtime.handle_user_message(messages, screen_awareness_mode=True)
        assert models[-2:] == ["vision-model", "fast-model"]
    finally:
        adapter.close()


@pytest.mark.parametrize("leave_at", ["vision-model", "fast-model"])
def test_leaving_during_evaluation_discards_the_late_reply(tmp_path, monkeypatch, leave_at):
    models = []
    runtime = None

    def fake_post(self, payload, *, cancel_checker=None):
        models.append(payload["model"])
        if payload["model"] == leave_at:
            runtime.set_screen_away(True)
        content = VISION_JSON if payload["model"] == "vision-model" else '{"should_speak":true,"comment":"気になるね。","translation":"有点在意。"}'
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        runtime = adapter.initialize(Event()).session.runtime
        reply = runtime.handle_user_message([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}], screen_awareness_mode=True)
        assert reply.reply.segments == []
        assert reply.visual_observation is None
        if leave_at == "vision-model":
            assert models == ["vision-model"]
    finally:
        adapter.close()


@pytest.mark.parametrize("change", [{"pid": 9}, {"title": "a different document"}])
def test_reused_window_or_changed_document_cannot_publish_the_old_observation(tmp_path, monkeypatch, change):
    snapshot = {"hwnd": 1, "pid": 1, "process": "editor.exe", "title": "notes"}
    runtime = None
    models = []

    def fake_post(self, payload, *, cancel_checker=None):
        models.append(payload["model"])
        if payload["model"] == "vision-model":
            runtime._focus_observer.advance(snapshot_from_mapping({**snapshot, **change}), scope="a", gate=FocusGate())
        return {"choices": [{"message": {"content": VISION_JSON if payload["model"] == "vision-model" else '{"should_speak":false}'}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        runtime = adapter.initialize(Event()).session.runtime
        runtime._focus_observer.advance(snapshot_from_mapping(snapshot), scope="a", gate=FocusGate())
        reply = runtime.handle_user_message([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}], screen_awareness_mode=True)
        assert reply.visual_observation is None
        assert models == ["vision-model"]
    finally:
        adapter.close()


def test_translation_repair_does_not_receive_or_log_the_window_body(tmp_path, monkeypatch):
    payloads = []

    def fake_post(self, payload, *, cancel_checker=None):
        payloads.append(payload)
        content = {
            "vision-model": VISION_JSON,
            "fast-model": '{"should_speak":true,"comment":"気になるね。","translation":""}',
            "chat-model": '{"segments":[{"ja":"気になるね。","zh":"有点在意。","tone":"中性"}]}',
        }[payload["model"]]
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        runtime = adapter.initialize(Event()).session.runtime
        with runtime.trace_operation("source-repair-proof"):
            result = runtime.handle_user_message([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
                                                 screen_awareness_mode=True, observer_context=CONTEXT)
        assert [p["model"] for p in payloads] == ["vision-model", "fast-model", "chat-model"]
        assert result.reply.segments[0].translation == "有点在意。"
        assert "UIA_VISIBLE_MARKER" in json.dumps(payloads[1], ensure_ascii=False)
        assert "UIA_VISIBLE_MARKER" not in json.dumps(payloads[2], ensure_ascii=False)
        assert "UIA_VISIBLE_MARKER" not in "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.log"))
    finally:
        adapter.close()
