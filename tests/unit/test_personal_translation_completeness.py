"""Every spoken or displayed segment needs its translation before the reply is adopted."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.llm.api_client import ChatMessage, OpenAICompatibleClient

PARTIAL = json.dumps({"segments": [
    {"ja": "\u304a\u306f\u3088\u3046\u3002", "zh": "\u65e9\u4e0a\u597d\u3002", "tone": "\u4e2d\u6027"},
    {"ja": "\u4eca\u65e5\u306f\u96e8\u3060\u306d\u3002", "tone": "\u4e2d\u6027"},
    {"ja": "\u5098\u3092\u6301\u3063\u3066\u3044\u3063\u3066\u306d\u3002", "tone": "\u4e2d\u6027"},
]}, ensure_ascii=False)
COMPLETE = json.dumps({"segments": [
    {"ja": "\u304a\u306f\u3088\u3046\u3002", "zh": "\u65e9\u4e0a\u597d\u3002", "tone": "\u4e2d\u6027"},
    {"ja": "\u4eca\u65e5\u306f\u96e8\u3060\u306d\u3002", "zh": "\u4eca\u5929\u4e0b\u96e8\u5462\u3002", "tone": "\u4e2d\u6027"},
    {"ja": "\u5098\u3092\u6301\u3063\u3066\u3044\u3063\u3066\u306d\u3002", "zh": "\u8bb0\u5f97\u5e26\u4f1e\u3002", "tone": "\u4e2d\u6027"},
]}, ensure_ascii=False)


def _client(*contents: str) -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.complete_with_tools.side_effect = [MagicMock(content=content, tool_calls=[]) for content in contents]
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def _run(client: MagicMock):
    runtime = AgentRuntime(client, "SYNTHETIC_PERSONA")
    return runtime.handle_user_message([ChatMessage(role="user", content="\u65e9")])


def test_partially_translated_reply_is_repaired() -> None:
    client = _client(PARTIAL, COMPLETE)

    result = _run(client)

    assert client.complete_with_tools.call_count == 2
    assert all(segment.translation for segment in result.reply.segments)


def test_repair_that_still_misses_a_translation_keeps_the_original_reply() -> None:
    client = _client(PARTIAL, PARTIAL)

    result = _run(client)

    assert client.complete_with_tools.call_count == 2
    assert [segment.text for segment in result.reply.segments][0] == "\u304a\u306f\u3088\u3046\u3002"


def test_fully_translated_reply_is_adopted_without_repair() -> None:
    client = _client(COMPLETE)

    _run(client)

    assert client.complete_with_tools.call_count == 1
