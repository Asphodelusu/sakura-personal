"""Phase M：initial / tool_step / semantic_compose 不得串位。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.llm.api_client import ChatCompletionTurn, OpenAICompatibleClient


def _client() -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def _turn(content: str, tool_calls: list[object] | None = None) -> ChatCompletionTurn:
    return ChatCompletionTurn(
        content=content,
        tool_calls=tool_calls or [],
        message={"role": "assistant", "content": content},
    )


def _purposes(client: MagicMock) -> list[str]:
    return [
        str(call.kwargs.get("request_purpose") or "")
        for call in client.complete_with_tools.call_args_list
    ]


def test_qualified_segments_use_single_initial_request() -> None:
    client = _client()
    raw = json.dumps(
        {
            "segments": [
                {"ja": "おはよう。", "zh": "早安。", "tone": "开心", "portrait": "站立待机"}
            ]
        },
        ensure_ascii=False,
    )
    client.complete_with_tools.return_value = _turn(raw)
    runtime = AgentRuntime(client, "synthetic-system")

    runtime.handle_user_message([{"role": "user", "content": "synthetic-user-hi"}])

    assert client.complete_with_tools.call_count == 1
    assert _purposes(client) == ["initial"]


def test_plain_japanese_compose_is_not_labeled_initial() -> None:
    client = _client()
    original = "……開いたよ。"
    composed = json.dumps(
        {
            "segments": [
                {"ja": original, "zh": "", "tone": "中性", "portrait": "站立待机"}
            ]
        },
        ensure_ascii=False,
    )
    client.complete_with_tools.side_effect = [
        _turn(original),
        _turn(composed),
    ]
    runtime = AgentRuntime(client, "synthetic-system")

    runtime.handle_user_message([{"role": "user", "content": "synthetic-user-hi"}])

    purposes = _purposes(client)
    assert purposes[0] == "initial"
    assert purposes[1] == "structural_repair"
    assert purposes.count("initial") == 1
    assert "semantic_compose" not in purposes
