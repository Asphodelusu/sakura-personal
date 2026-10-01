"""Visual, fast and inner-thought slots reach the clients that actually request them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

from app.agent.tools import ToolRegistry
from app.config.core_config_reader import CoreConfigReader
from app.core_host.assistant_adapter import AssistantAdapter
from app.llm.api_client import ChatMessage, OpenAICompatibleClient, messages_contain_image

FIXTURE = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"
REPLY = json.dumps({"segments": [{"ja": "ねえ。", "zh": "嘿。", "tone": "中性"}]}, ensure_ascii=False)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    shutil.copytree(FIXTURE, root)
    (root / "config" / "api.yaml").write_text(
        """
api_profiles:
  - id: fixture
    alias: Fixture
    base_url: https://fixture.invalid/v1
    api_key: REDACTED_FIXTURE_API_KEY
    models:
      - name: chat-model
      - name: vision-model
      - name: fast-model
      - name: thought-model
      - name: memory-model
model_slots:
  chat:
    profile_id: fixture
    model: chat-model
    context_window_tokens: 20000
  vision_chat:
    profile_id: fixture
    model: vision-model
    context_window_tokens: 16000
  chat_fast:
    profile_id: fixture
    model: fast-model
    context_window_tokens: 8192
  inner_thought:
    profile_id: fixture
    model: thought-model
    context_window_tokens: 12288
  memory_curation:
    profile_id: fixture
    model: memory-model
    context_window_tokens: 7000
llm:
  temperature: 0.4
  timeout_seconds: 30
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return root


def test_reader_keeps_chat_identity_and_binds_an_explicit_vision_slot(tmp_path: Path) -> None:
    result = CoreConfigReader().read(_root(tmp_path))
    assert result.config_problem is None
    selection = result.provider_selection
    assert selection is not None
    assert selection.api_settings.model == "chat-model"
    assert selection.api_settings.context_window_tokens == 20000
    assert selection.api_settings.temperature == 0.4
    assert selection.vision_api_settings is not None
    assert selection.vision_api_settings.model == "vision-model"
    assert selection.vision_api_settings.context_window_tokens == 16000
    assert selection.vision_api_settings.temperature is None


def test_adapter_routes_text_and_images_and_closes_new_clients_once(tmp_path: Path, monkeypatch) -> None:
    import app.core_host.assistant_adapter as adapter_module

    calls: list[tuple[str, int, int, bool]] = []
    closed: list[object] = []

    class Client(OpenAICompatibleClient):
        def close(self):
            closed.append(self)

    monkeypatch.setattr(adapter_module, "OpenAICompatibleClient", Client)

    def capture(self, _system, messages, **kwargs):
        window = int(kwargs["trace_metadata"].snapshot.context_window_tokens)
        calls.append((
            self.settings.model,
            int(self.settings.context_window_tokens),
            window,
            messages_contain_image(messages),
        ))
        return MagicMock(content=REPLY, tool_calls=[])

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    readiness = adapter.initialize(Event())
    runtime = readiness.session.runtime
    runtime.handle_user_message([ChatMessage(role="user", content="普通文本")])
    runtime.handle_user_message([
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        )
    ])
    assert calls[0] == ("chat-model", 20000, 20000, False)
    assert calls[1] == ("vision-model", 16000, 16000, True)
    assert runtime._initiative_client.settings.model == "fast-model"
    assert runtime._initiative_client.settings.context_window_tokens == 8192
    thought = runtime._inner_thought._client
    assert thought is not None
    assert thought.settings.model == "thought-model"
    assert thought.settings.context_window_tokens == 12288

    vision = runtime.vision_api_client
    fast = runtime._initiative_client
    adapter.retire_session()
    assert closed.count(vision) == 1
    assert closed.count(fast) == 1
    adapter.close()
    assert closed.count(vision) == 1
    assert closed.count(fast) == 1


@pytest.mark.parametrize("cancelled", [False, True])
def test_adapter_cleans_created_clients_when_initialization_does_not_publish(tmp_path, monkeypatch, cancelled):
    import app.core_host.assistant_adapter as adapter_module
    from app.core.cancellation import OperationCancelled

    created, closed = [], []
    cancel = Event()

    class Client(OpenAICompatibleClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
            if self.settings.model == "vision-model" and cancelled:
                cancel.set()

        def close(self):
            closed.append(self)

    def fail_prompt(_profile):
        raise ValueError("synthetic prompt failure")

    monkeypatch.setattr(adapter_module, "OpenAICompatibleClient", Client)
    if not cancelled:
        monkeypatch.setattr(adapter_module, "load_character_system_prompt", fail_prompt)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    if cancelled:
        with pytest.raises(OperationCancelled):
            adapter.initialize(cancel)
    else:
        assert adapter.initialize(cancel).code == "ASSISTANT_INITIALIZATION_FAILED"
    adapter.close()
    assert [client.settings.model for client in created] == ["chat-model", "vision-model"]
    assert closed == list(reversed(created))


def test_malformed_fast_slot_does_not_use_chat_fallback(tmp_path):
    from app.core_host.inner_thought_settings import load_fast_slot_settings

    root = _root(tmp_path)
    path = root / "config" / "api.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("context_window_tokens: 8192", "context_window_tokens: true"), encoding="utf-8")
    assert load_fast_slot_settings(root) is None
    adapter = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        readiness = adapter.initialize(Event())
        assert readiness.session.runtime._initiative_client is None
    finally:
        adapter.close()


def test_user_turn_inner_thought_reaches_http_and_main_prompt(tmp_path, monkeypatch):
    from app.core import runtime_log
    from app.core_host.real_chat import RealChatBoundary
    from app.storage.timeline import TimelineStore

    root = _root(tmp_path)
    marker = "雨なら傘を持てばいい。INNER_HTTP_CAPTURE"
    calls, events = [], []
    monkeypatch.setattr(runtime_log, "_EXTERNAL_SINK", None)
    monkeypatch.setattr(runtime_log, "_load_debug_values", lambda: {"enabled": False})

    def fake_post(self, payload, *, cancel_checker=None):
        body = json.loads(json.dumps(payload, ensure_ascii=False))
        calls.append({"body": body, "window": self.settings.context_window_tokens})
        if body["model"] == "thought-model":
            content = "interest: mid\n" + marker
        elif body["model"] == "chat-model":
            content = REPLY
        else:
            raise AssertionError(f"unexpected model: {body['model']}")
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", fake_post)
    adapter = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None)
    boundary = None
    try:
        session = adapter.initialize(Event()).session
        assert session is not None
        timeline = TimelineStore(tmp_path / "inner-evidence.sqlite3")
        timeline.initialize()
        generation_id, credential = "00000000-0000-4000-8000-000000004108", "42" * 16
        boundary = RealChatBoundary(generation_id, credential, root,
            session_provider=lambda: session, timeline_store=timeline, event_publisher=events.append)
        request = {"id": "inner-http", "kind": "request", "name": "chat.send",
            "generationId": generation_id, "generationCredential": credential,
            "payload": {"operationId": "inner-http", "message": "在吗"}}
        boundary.reserve_send(request)
        boundary.handle_send(request)
        assert events[-1]["name"] == "chat.completed"
        assert session.runtime._inner_thought.wait_until_idle(2)
        assert [call["body"]["model"] for call in calls] == ["thought-model", "chat-model"]
        thought, main = calls
        assert thought["body"]["temperature"] == 0.9
        assert thought["body"]["max_tokens"] == 180
        assert "在吗" in thought["body"]["messages"][1]["content"]
        assert thought["window"] == 12288  # Binding evidence, not prompt budget consumption.
        assert marker in json.dumps(main["body"]["messages"], ensure_ascii=False)
        assert marker not in json.dumps(events[-1]["payload"])
    finally:
        if boundary is not None:
            boundary.close()
        adapter.close()
