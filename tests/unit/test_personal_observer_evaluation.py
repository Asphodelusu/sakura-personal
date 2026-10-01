"""Personal scheduled screens use vision, then a text-only fast decision."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from threading import Event

import pytest

from app.agent.tools import ToolRegistry
from app.core_host.assistant_adapter import AssistantAdapter
from app.llm.api_client import ChatMessage, OpenAICompatibleClient, messages_contain_image

FIXTURE = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"
UIA_BODY = "UIA_BODY_SHOULD_NOT_REACH_VISION_" + ("屏" * 80)
VISION_JSON = json.dumps(
    {
        "visual_summary": "他在看一页说明。",
        "reaction_hint": "先看着。",
        "on_screen_text": "可见短句",
        "suggested_interval": 600,
    },
    ensure_ascii=False,
)
FAST_JSON = json.dumps(
    {
        "should_speak": True,
        "comment": "ちょっと気になるね。",
        "translation": "那段说明有点在意。",
        "tone": "中性",
        "situational_summary": "彼が説明を見ている。",
    },
    ensure_ascii=False,
)


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
    (root / "config" / "system_config.yaml").write_text(
        """
config_version: 1
proactive:
  enabled: true
  eval_temperature: 0.2
  max_tokens: 320
  request_timeout: 17
  timer_seconds: 480
  adaptive_interval_min: 300
  adaptive_interval_max: 1800
""".strip()
        + "\n",
        encoding="utf-8",
    )
    package = root / "characters" / "sakura"
    (package / "system_guards.md").write_text(
        "## 身份与人称\nSYNTHETIC_IDENTITY_TOKEN\n",
        encoding="utf-8",
    )
    card = package / "card.md"
    card.write_text(card.read_text(encoding="utf-8") + "\n\n## 她怎样存在\nSYNTHETIC_BEHAVIOR_TOKEN\n", encoding="utf-8")
    return root


def test_personal_screen_calls_vision_then_text_only_fast(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def capture(self, system_prompt, messages, **kwargs):
        window = int(kwargs["trace_metadata"].snapshot.context_window_tokens)
        blob = json.dumps(
            {"system": system_prompt, "messages": messages},
            ensure_ascii=False,
            default=str,
        )
        calls.append(
            {
                "client": id(self),
                "model": self.settings.model,
                "window": int(self.settings.context_window_tokens),
                "snapshot_window": window,
                "timeout": int(self.settings.timeout_seconds),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
                "thinking": kwargs.get("thinking"),
                "image": messages_contain_image(messages),
                "blob": blob,
            }
        )
        content = VISION_JSON if messages_contain_image(messages) else FAST_JSON
        from unittest.mock import MagicMock

        return MagicMock(content=content, tool_calls=[], trace_call=None)

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    readiness = adapter.initialize(Event())
    runtime = readiness.session.runtime
    from app.plugins.models import ContextProviderContribution
    recall_calls = []
    runtime.set_context_providers([ContextProviderContribution(
        provider_id="memory", description="memory", build_context=lambda request: recall_calls.append(request) or [],
    )])
    shared_vision_timeout = int(runtime.vision_api_client.settings.timeout_seconds)
    shared_fast_timeout = int(runtime._initiative_client.settings.timeout_seconds)
    try:
        result = runtime.handle_user_message(
            [
                ChatMessage(role="user", content="刚才我说先别提那件事。"),
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": f"定时观察\n{UIA_BODY}"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                ),
            ],
            screen_awareness_mode=True,
        )
    finally:
        adapter.close()

    assert recall_calls == [], "trace metadata must not run memory retrieval"
    assert [item["model"] for item in calls] == ["vision-model", "fast-model"]
    assert [item["image"] for item in calls] == [True, False]
    assert calls[0]["window"] == 16000
    assert calls[0]["snapshot_window"] == 16000
    assert calls[1]["window"] == 8192
    assert calls[1]["snapshot_window"] == 8192
    assert calls[0]["client"] != id(runtime.vision_api_client)
    assert calls[1]["client"] != id(runtime._initiative_client)
    assert calls[1]["timeout"] == 17
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["max_tokens"] == 320
    assert calls[0]["timeout"] == 17
    assert calls[1]["temperature"] == 0.5
    assert calls[1]["max_tokens"] == 1024
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert calls[1]["thinking"] == {"type": "disabled"}
    assert "SYNTHETIC_IDENTITY_TOKEN" in str(calls[0]["blob"])
    assert "SYNTHETIC_BEHAVIOR_TOKEN" in str(calls[1]["blob"])
    assert UIA_BODY not in str(calls[0]["blob"])
    assert "data:image" not in str(calls[1]["blob"])
    assert shared_vision_timeout == 30
    assert shared_fast_timeout == 30
    assert int(runtime.vision_api_client.settings.timeout_seconds) == 30
    assert result.reply.segments
    assert result.reply.segments[0].translation


def _settings(model: str, window: int) -> object:
    from app.llm.api_client import ApiSettings

    return ApiSettings(
        base_url="https://fixture.invalid/v1",
        api_key="REDACTED_FIXTURE_API_KEY",
        model=model,
        timeout_seconds=30,
        context_window_tokens=window,
    )


def _runtime(monkeypatch, replies: list[str]):
    from app.agent.runtime import AgentRuntime
    from app.config.relationship_initiative import RelationshipInitiativeSettings

    calls: list[dict[str, object]] = []

    def capture(self, _system, messages, **kwargs):
        calls.append(
            {
                "model": self.settings.model,
                "image": messages_contain_image(messages),
                "blob": json.dumps({"messages": messages, "kwargs": {k: kwargs.get(k) for k in ("temperature", "max_tokens", "thinking")}}, ensure_ascii=False, default=str),
            }
        )
        from unittest.mock import MagicMock

        content = replies[len(calls) - 1] if len(calls) <= len(replies) else "nope"
        return MagicMock(content=content, tool_calls=[], trace_call=None)

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    chat = OpenAICompatibleClient(_settings("chat-model", 20000), request_attempts=1)
    vision = OpenAICompatibleClient(_settings("vision-model", 16000), request_attempts=1)
    fast = OpenAICompatibleClient(_settings("fast-model", 8192), request_attempts=1)
    runtime = AgentRuntime(chat, "【身份锚】\nSYNTHETIC_IDENTITY_TOKEN", vision_api_client=vision, strict_provider_errors=True)
    runtime.configure_personal_reply(personal_style=True)
    runtime.configure_initiative(RelationshipInitiativeSettings(proactive_enabled=False), client=fast)
    runtime.configure_screen_initiative(
        loader=lambda: (
            True,
            600.0,
            10.0,
            {
                "enabled": True,
                "eval_temperature": 0.2,
                "max_tokens": 320,
                "request_timeout": 17,
                "timer_seconds": 480,
                "adaptive_interval_min": 300,
                "adaptive_interval_max": 1800,
                "content_quiet_seconds": 180,
            },
        )
    )
    return runtime, calls


def _screen_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="刚才我说的是另一件事。"),
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": UIA_BODY},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        ),
    ]


def test_invalid_vision_makes_no_fast_call(monkeypatch) -> None:
    runtime, calls = _runtime(monkeypatch, ["这不是观测 JSON"])
    result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
    assert [item["model"] for item in calls] == ["vision-model"]
    assert result.reply.segments == []
    assert runtime._focus_observer.next_timer_at == 0


def test_silent_decision_has_no_speech_and_still_arms_timing(monkeypatch) -> None:
    runtime, calls = _runtime(
        monkeypatch,
        [
            VISION_JSON,
            json.dumps({"should_speak": False, "comment": "", "situational_summary": "彼は作業中。"}, ensure_ascii=False),
        ],
    )
    clock = runtime._focus_observer.clock
    runtime._focus_observer.clock = lambda: 50.0
    result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
    assert [item["image"] for item in calls] == [True, False]
    assert result.reply.segments == []
    assert result.visual_observation is not None
    assert runtime._focus_observer.next_timer_at == 650
    assert runtime._focus_observer.content_quiet_until == 650
    assert clock is not None


def test_untranslated_repair_does_not_yield_speech(monkeypatch) -> None:
    runtime, calls = _runtime(
        monkeypatch,
        [
            VISION_JSON,
            json.dumps(
                {"should_speak": True, "comment": "ちょっと気になるね。", "translation": "", "tone": "中性"},
                ensure_ascii=False,
            ),
            "まだ修復できない",
        ],
    )
    result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
    assert [item["image"] for item in calls] == [True, False, False]
    assert calls[2]["model"] == "chat-model"
    assert "base64" not in calls[2]["blob"]
    assert result.reply.segments == []


def test_cancel_and_stale_focus_publish_nothing(monkeypatch) -> None:
    from app.core.cancellation import OperationCancelled

    runtime, calls = _runtime(monkeypatch, [VISION_JSON, FAST_JSON])

    def cancel():
        raise OperationCancelled()

    try:
        runtime.handle_user_message(_screen_messages(), cancel_checker=cancel, screen_awareness_mode=True)
    except OperationCancelled:
        pass
    else:
        raise AssertionError("cancel must abort the evaluation")
    assert calls == []
    assert runtime._focus_observer.next_timer_at == 0

    runtime.advance_focus(
        {"hwnd": 2, "process": "editor.exe", "title": "SECRET_TITLE", "changedAt": 1, "pid": 2},
        scope="scope-a",
        busy=False,
        timer_seconds=480,
    )

    def capture(self, _system, messages, **_kwargs):
        if messages_contain_image(messages):
            runtime.advance_focus(
                {"hwnd": 9, "process": "other.exe", "title": "SECRET_TITLE", "changedAt": 5, "pid": 9},
                scope="scope-b",
                busy=False,
                timer_seconds=480,
            )
        calls.append({"model": self.settings.model, "image": messages_contain_image(messages)})
        from unittest.mock import MagicMock

        return MagicMock(content=VISION_JSON, tool_calls=[], trace_call=None)

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
    assert [item["image"] for item in calls] == [True]
    assert result.reply.segments == []
    assert runtime._focus_observer.next_timer_at == 0
    rendered = json.dumps(runtime._focus_observer.diagnostics(), ensure_ascii=False)
    assert "SECRET_TITLE" not in rendered
    assert "base64" not in rendered


def test_later_user_correction_suppresses_the_old_impression(monkeypatch) -> None:
    import time

    runtime, calls = _runtime(monkeypatch, [VISION_JSON, FAST_JSON])
    runtime._sensory.update("OLD_IMPRESSION_TEXT", now=time.monotonic(), wall_unix=10)
    runtime.note_user_message("不是那样，是另一件事。")
    runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
    fast = calls[1]["blob"]
    assert "OLD_IMPRESSION_TEXT" not in str(fast)
    assert "时序" in str(fast)
    assert UIA_BODY not in str(fast)


def test_personal_evaluation_parameters_reach_http(tmp_path: Path, monkeypatch) -> None:
    payloads = []

    def capture(self, payload, *, cancel_checker=None):
        payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        content = VISION_JSON if self.settings.model == "vision-model" else FAST_JSON
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat_completions", capture)
    adapter = AssistantAdapter(_root(tmp_path), tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        session = adapter.initialize(Event()).session
        assert session is not None
        result = session.runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
        assert result.reply.segments[0].translation
        assert [payload["model"] for payload in payloads] == ["vision-model", "fast-model"]
        assert [messages_contain_image(payload["messages"]) for payload in payloads] == [True, False]
        assert [payload["temperature"] for payload in payloads] == [0.2, 0.5]
        assert [payload["max_tokens"] for payload in payloads] == [320, 1024]
        assert [payload.get("thinking") for payload in payloads] == [
            {"type": "disabled"}, {"type": "disabled"},
        ]
    finally:
        adapter.close()


def test_focus_change_during_decision_discards_the_observation(monkeypatch) -> None:
    runtime, calls = _runtime(monkeypatch, [])
    runtime.advance_focus(
        {"hwnd": 2, "process": "editor.exe", "title": "before", "pid": 2},
        scope="scope-a", busy=False, timer_seconds=480,
    )

    def capture(self, _system, messages, **_kwargs):
        from app.llm.api_client import ChatCompletionTurn

        calls.append(self.settings.model)
        if self.settings.model == "fast-model":
            runtime.advance_focus(
                {"hwnd": 9, "process": "other.exe", "title": "after", "pid": 9},
                scope="scope-b", busy=False, timer_seconds=480,
            )
        content = VISION_JSON if messages_contain_image(messages) else FAST_JSON
        return ChatCompletionTurn(content=content, tool_calls=[], message={})

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    try:
        result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
        assert calls == ["vision-model", "fast-model"]
        assert result.reply.segments == []
        assert result.visual_observation is None
        assert runtime._sensory.get() is None
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["visual_summary", "reaction_hint", "on_screen_text"])
def test_non_text_perception_is_not_a_valid_observation(monkeypatch, field) -> None:
    runtime, calls = _runtime(monkeypatch, [json.dumps({field: {"unexpected": "value"}})])
    try:
        result = runtime.handle_user_message(_screen_messages(), screen_awareness_mode=True)
        assert [call["model"] for call in calls] == ["vision-model"]
        assert result.reply.segments == []
        assert result.visual_observation is None
        assert runtime._focus_observer.next_timer_at == 0
    finally:
        runtime.close()
