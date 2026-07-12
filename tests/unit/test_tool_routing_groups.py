from __future__ import annotations

from app.agent.tool_routing import (
    DEFAULT_ACTIVE_TOOL_GROUPS,
    _should_fast_forward_after_web_search,
    extract_memory_recall_query,
    extract_memory_remember_content,
    infer_active_tool_groups_from_messages,
    messages_contain_recent_web_search,
    user_requests_mcp_followup,
    user_requests_memory_recall,
    user_requests_memory_remember,
)
from app.agent.tools import ToolExecutionResult
from app.llm.api_client import ChatMessage


def test_default_active_groups_is_core_only() -> None:
    assert DEFAULT_ACTIVE_TOOL_GROUPS == frozenset({"core"})


def test_infer_active_groups_stays_core_for_casual_chat() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天天气真好呀"}]
    assert infer_active_tool_groups_from_messages(messages) == {"core"}


def test_infer_active_groups_adds_productivity_for_todo_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我记一条待办"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "core" in groups
    assert "productivity" in groups


def test_infer_active_groups_adds_mcp_for_search_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我搜一下今天的新闻"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_infer_active_groups_adds_memory_write_for_remember_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "记住我喜欢喝乌龙茶"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "memory-write" in groups


def test_infer_active_groups_adds_mcp_for_repeat_search_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "再查一遍天津天气"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_infer_active_groups_adds_mcp_when_recent_web_search_and_followup() -> None:
    messages: list[ChatMessage] = [
        {"role": "user", "content": "查一下天津天气"},
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
        {"role": "tool", "tool_call_id": "call_1", "name": "web__web_search", "content": "{}"},
        {"role": "user", "content": "再查一遍，还是刚才那个"},
    ]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_messages_contain_recent_web_search_from_tool_message() -> None:
    messages: list[ChatMessage] = [
        {"role": "tool", "tool_call_id": "call_1", "name": "web__web_search", "content": "{}"},
    ]
    assert messages_contain_recent_web_search(messages)


def test_user_requests_mcp_followup_detects_repeat_phrases() -> None:
    assert user_requests_mcp_followup("再查一遍天气")
    assert user_requests_mcp_followup("还是刚才那个")


def test_extract_memory_remember_content() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "记住：我最喜欢喝乌龙茶"}]
    assert extract_memory_remember_content(messages) == "我最喜欢喝乌龙茶"


def test_user_requests_memory_recall_not_confused_with_remember() -> None:
    remember_messages: list[ChatMessage] = [{"role": "user", "content": "记住我喜欢喝乌龙茶"}]
    recall_messages: list[ChatMessage] = [{"role": "user", "content": "我刚才让你记住我喜欢喝什么？"}]
    assert user_requests_memory_remember(remember_messages)
    assert not user_requests_memory_recall(remember_messages)
    assert user_requests_memory_recall(recall_messages)
    assert extract_memory_recall_query(recall_messages) == "我刚才让你记住我喜欢喝什么？"


def test_should_fast_forward_after_web_search_for_lookup_intent() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我查一下天津天气"}]
    results = [
        ToolExecutionResult(
            tool_name="web__web_search",
            success=True,
            content={"results": [{"title": "天津天气"}]},
            error="",
        )
    ]
    assert _should_fast_forward_after_web_search(messages, results)


def test_should_not_fast_forward_after_web_search_for_casual_chat() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天心情不错"}]
    results = [
        ToolExecutionResult(
            tool_name="web__web_search",
            success=True,
            content={"results": [{"title": "x"}]},
            error="",
        )
    ]
    assert not _should_fast_forward_after_web_search(messages, results)
