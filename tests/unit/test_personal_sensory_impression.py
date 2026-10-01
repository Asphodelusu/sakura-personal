"""Short screen impressions stay in the runtime and reach ordinary chat context."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.agent.sensory_impression import CHAT_MAX_CHARS, STORE_MAX_CHARS, SensoryImpressionStore
from app.llm.api_client import ApiSettings, ChatMessage, OpenAICompatibleClient


def test_store_bounds_ttl_and_a_later_user_fact() -> None:
    store = SensoryImpressionStore()
    store.update("画面。" * 300, now=0, wall_unix=10)
    kept = store.get(now=0)
    assert kept is not None
    assert len(kept.text) <= STORE_MAX_CHARS
    assert store.get(now=1201) is None

    stamp = time.monotonic()
    store.update("场景在编辑器。" + ("场景。" * 80) + "対話の既知：不要。", now=stamp, wall_unix=20)
    projected = store.get_for_chat(now=stamp)
    assert "対話の既知" not in projected
    assert len(projected) <= CHAT_MAX_CHARS
    store.note_user_fact(30)
    assert store.get_for_observer(now=stamp) == ""
    evidence = store.chronology_evidence()
    assert "unix=30" in evidence
    assert "unix=20" in evidence
    store.clear()
    assert store.get(now=stamp) is None


def test_normal_chat_consumes_impression_without_reading_media(monkeypatch) -> None:
    def fail_media(*_args, **_kwargs):
        raise AssertionError("screen impression must not read local media")

    monkeypatch.setattr("app.agent.local_context.read_media_session_snapshot", fail_media)
    seen: list[str] = []

    def capture(self, _system, _messages, **kwargs):
        seen.append(str(_system) + "\n" + str(kwargs.get("runtime_context") or ""))
        return MagicMock(content='{"segments":[{"ja":"うん。","zh":"嗯。","tone":"中性"}]}', tool_calls=[], trace_call=None)

    monkeypatch.setattr(OpenAICompatibleClient, "complete_with_tools", capture)
    client = OpenAICompatibleClient(
        ApiSettings(
            base_url="https://fixture.invalid/v1",
            api_key="REDACTED_FIXTURE_API_KEY",
            model="chat-model",
            context_window_tokens=20000,
        ),
        request_attempts=1,
    )
    runtime = AgentRuntime(client, "persona")
    runtime._sensory.update("他正在听什么歌，画面停在编辑器。", now=time.monotonic(), wall_unix=time.time())
    runtime.handle_user_message([ChatMessage(role="user", content="你好")])
    assert any("短时屏幕印象" in item for item in seen)
    runtime.set_screen_away(True)
    assert runtime._sensory.get() is None
    runtime._sensory.update("暂时留下", now=time.monotonic())
    runtime.close()
    assert runtime._sensory.get() is None
