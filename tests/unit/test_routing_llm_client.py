from __future__ import annotations

from unittest.mock import MagicMock

from app.llm.api_client import ChatMessage
from app.llm.chat_reply import ChatReply, ChatSegment
from app.llm.local_client import RoutingLlmClient


def test_routing_llm_client_chat_forwards_reply_tones_and_portraits() -> None:
    cloud = MagicMock()
    cloud.chat.return_value = ChatReply(
        segments=[ChatSegment("おはよう", "早安", "中性", "站立待机")]
    )
    client = RoutingLlmClient.__new__(RoutingLlmClient)
    client._cloud = cloud

    client.chat(
        "system",
        [ChatMessage(role="user", content="hi")],
        ["中性", "开心"],
        ["站立待机", "开心脸红"],
    )

    cloud.chat.assert_called_once_with(
        "system",
        [ChatMessage(role="user", content="hi")],
        ["中性", "开心"],
        ["站立待机", "开心脸红"],
        cancel_checker=None,
        runtime_context="",
        on_chunk=None,
    )
