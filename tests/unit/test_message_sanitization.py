from __future__ import annotations

from app.llm.api_client import (
    prepare_messages_for_chat_api,
    sanitize_tool_conversation_messages,
    strip_image_parts_from_messages,
)


def test_sanitize_tool_conversation_messages_drops_orphan_tool() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        {"role": "user", "content": "hello"},
    ]
    sanitized = sanitize_tool_conversation_messages(messages)
    assert sanitized == [{"role": "user", "content": "hello"}]


def test_sanitize_tool_conversation_messages_keeps_valid_tool_chain() -> None:
    messages = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web__web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ]
    sanitized = sanitize_tool_conversation_messages(messages)
    assert len(sanitized) == 3
    assert sanitized[-1]["role"] == "tool"


def test_strip_image_parts_from_messages_replaces_image_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看屏幕"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]
    stripped = strip_image_parts_from_messages(messages)
    assert stripped[0]["content"] == "看屏幕\n[1 image(s) omitted for text model]"


def test_prepare_messages_for_chat_api_text_only_strips_and_sanitizes() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "missing", "content": "{}"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "event"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        },
    ]
    prepared = prepare_messages_for_chat_api(messages, text_only=True)
    assert prepared == [{"role": "user", "content": "event\n[1 image(s) omitted for text model]"}]
