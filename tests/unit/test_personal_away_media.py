"""Explicit goodbye and on-demand media stay on the real request path."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agent.away import message_implies_away
from app.agent.runtime import AgentRuntime
from app.core_host import real_chat
from app.llm.api_client import ChatMessage
from app.perception.media_session import (
    MediaSessionSnapshot,
    clear_media_session_cache,
    read_media_session_snapshot,
)

REPLY = json.dumps({"segments": [{"ja": "ねえ。", "zh": "嘿。", "tone": "中性"}]}, ensure_ascii=False)


def test_sleep_topic_and_questions_are_not_goodbyes() -> None:
    assert message_implies_away("晚安") == "晚安"
    assert message_implies_away("我去睡了") == "我去睡了"
    assert message_implies_away("最近失眠，想聊睡觉的事") is None
    assert message_implies_away("要睡了吗") is None
    assert message_implies_away("别说话") == "别说话"


def test_real_user_boundary_sets_away_after_marking_the_return(monkeypatch) -> None:
    order: list[str] = []
    runtime = AgentRuntime(object(), "system", character_id="sakura")

    def activity() -> None:
        order.append("activity")
        runtime._focus_observer.note_user_activity()

    monkeypatch.setattr(runtime, "note_initiative_user_turn", activity)
    real_chat._note_user_message(runtime, "晚安")
    assert order == ["activity"]
    assert runtime._focus_observer.away_mode is True
    real_chat._note_user_message(runtime, "屏幕观察")
    assert runtime._focus_observer.away_mode is False


def test_goodbye_survives_the_first_focus_poll_and_blocks_both_gates() -> None:
    from app.agent.focus_observer import FocusGate, FocusObserver, FocusSnapshot
    from app.config.relationship_initiative import RelationshipInitiativeSettings

    class Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    runtime = AgentRuntime(object(), "system", character_id="sakura")
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=True).normalized(),
        client=None,
        clock=clock,
        idle_seconds=lambda: 0.0,
    )
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    clock.now += 1000
    runtime.note_user_message("我出去了")
    assert runtime._focus_observer.away_mode is True
    snap = FocusSnapshot(hwnd=1, process="editor.exe", title="", changed_at=0.0)
    assert runtime._focus_observer.advance(snap, scope="generation-a", gate=FocusGate(enabled=True))["reason"] == "away"
    assert runtime._focus_observer.away_mode is True
    assert runtime.screen_gate_reason() == "away"
    assert runtime.initiative_gate_reason() == "away"
    runtime.note_user_message("回来了")
    clock.now += 301
    assert runtime.screen_gate_reason() == "eligible"
    assert runtime.initiative_gate_reason() == "eligible"


def test_media_is_only_read_for_a_related_question_and_reaches_the_request(monkeypatch) -> None:
    from app.agent import local_context
    reads = {"n": 0}

    def reader():
        reads["n"] += 1
        return MediaSessionSnapshot(available=True, playing=True, title="SYNTH_TRACK", artist="SYNTH_ARTIST")

    calls: list[str] = []

    def capture(_system, _messages, **kwargs):
        calls.append(str(kwargs.get("runtime_context") or ""))
        return MagicMock(content=REPLY, tool_calls=[])

    client = MagicMock()
    client.complete_with_tools = capture
    client.settings = MagicMock(model="chat-model", context_window_tokens=20000, context_window_source="user", max_tokens=None)
    client.resolve_dialogue_params.return_value = (0.8, {})
    runtime = AgentRuntime(client, "system", character_id="sakura")
    monkeypatch.setattr(local_context, "read_media_session_snapshot", reader)
    runtime.handle_user_message([
        ChatMessage(role="user", content="你好"),
        ChatMessage(role="assistant", content="在。"),
        ChatMessage(role="user", content="你好呀"),
    ])
    assert reads["n"] == 0
    assert "SYNTH_TRACK" not in calls[-1]
    runtime.handle_user_message([
        ChatMessage(role="user", content="之前"),
        ChatMessage(role="assistant", content="嗯"),
        ChatMessage(role="user", content="现在在听什么"),
    ])
    assert reads["n"] == 1
    assert "SYNTH_TRACK" in calls[-1]
    assert "untrusted" in calls[-1]


def test_automatic_observation_and_events_do_not_read_media(monkeypatch):
    from app.agent import local_context
    from app.llm.prompts.types import ContextRequest

    def forbidden_reader():
        raise AssertionError("automatic source accessed media")

    monkeypatch.setattr(local_context, "read_media_session_snapshot", forbidden_reader)
    runtime = AgentRuntime(object(), "system")
    for request in (
        ContextRequest(current_input="这首歌", source="event"),
        ContextRequest(current_input="这首歌", mode="screen_awareness"),
    ):
        assert not any(fragment.source == "local_media" for fragment in runtime._session_state_fragments(request))


def test_media_reader_uses_a_short_cache_and_does_not_launch_powershell(monkeypatch) -> None:
    import subprocess

    clear_media_session_cache()
    calls = {"n": 0}

    def runner() -> str:
        calls["n"] += 1
        return '{"available":true,"playing":true,"title":"CACHED","artist":"","source":"","playback_status":"Playing"}'

    def fail_run(*_args, **_kwargs):
        raise AssertionError("powershell was launched")

    monkeypatch.setattr(subprocess, "run", fail_run)
    first = read_media_session_snapshot(runner=runner, now=10)
    second = read_media_session_snapshot(runner=runner, now=12)
    assert calls["n"] == 1
    assert first is not None and second is not None
    assert first.title == "CACHED"
    clear_media_session_cache()


@pytest.mark.parametrize("timeout", [False, True])
def test_media_subprocess_is_hidden_bounded_and_utf8_with_failure_empty(monkeypatch, timeout):
    import subprocess
    from types import SimpleNamespace
    from app.perception import media_session

    clear_media_session_cache()
    monkeypatch.setattr(media_session.sys, "platform", "win32")

    def run(args, **kwargs):
        assert args[:2] == ["powershell", "-NoProfile"]
        assert "UTF8Encoding" in args[-1]
        assert kwargs["timeout"] == 2.5
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if timeout:
            raise subprocess.TimeoutExpired(args[0], 2.5)
        return SimpleNamespace(stdout='{"available":true,"title":"测试音轨"}'.encode("utf-8"))

    monkeypatch.setattr(subprocess, "run", run)
    try:
        snapshot = read_media_session_snapshot(force=True)
        if timeout:
            assert snapshot is None
        else:
            assert snapshot.title == "测试音轨"
    finally:
        clear_media_session_cache()
