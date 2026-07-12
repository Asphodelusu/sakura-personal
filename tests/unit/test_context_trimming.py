from __future__ import annotations

from app.llm.context_trimming import MAX_MODEL_CONTEXT_MESSAGES, trim_messages_for_model


def test_trim_messages_for_model_keeps_recent_window() -> None:
    messages = [{"role": "user", "content": f"msg-{index}"} for index in range(MAX_MODEL_CONTEXT_MESSAGES + 5)]
    trimmed = trim_messages_for_model(messages)
    assert len(trimmed) == MAX_MODEL_CONTEXT_MESSAGES
    assert trimmed[-1]["content"] == f"msg-{MAX_MODEL_CONTEXT_MESSAGES + 4}"


def test_trim_messages_for_model_respects_token_budget() -> None:
    messages = [{"role": "user", "content": "a" * 5000} for _ in range(30)]
    trimmed = trim_messages_for_model(messages)
    assert len(trimmed) <= MAX_MODEL_CONTEXT_MESSAGES


def test_trim_messages_for_model_drops_leading_orphan_tool_after_trim() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "big event"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("a" * 20000)}},
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_current_time", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]
    trimmed = trim_messages_for_model(messages)
    assert trimmed
    assert trimmed[0]["role"] != "tool"
