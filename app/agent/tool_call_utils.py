"""工具调用归一化与去重 — 从 runtime.py 拆分的叶子模块。

纯函数 + 一个不可变 dataclass；负责把模型原生 tool_call 归一化为策略调用、
移除规划层 reason 字段、识别重复工具调用，以及自动补写错过的记忆工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import app.agent.tool_routing as tool_routing
from app.agent.tools import ToolExecutionResult, ToolRegistry
from app.core.debug_log import debug_log
from app.llm.api_client import ChatCompletionTurn, ChatMessage, NativeToolCall


def _native_tool_call_to_policy_call(
    call: NativeToolCall,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": call.id,
        "name": call.name,
        "arguments": arguments if arguments is not None else call.arguments,
        "reason": _tool_call_reason(call),
    }


def _tool_call_reason(call: NativeToolCall) -> str:
    reason = call.arguments.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


def _tool_arguments_for_execution(call: NativeToolCall, tools: ToolRegistry) -> dict[str, Any]:
    """移除规划层的 reason 字段，避免它污染真实工具参数。"""

    arguments = dict(call.arguments)
    if "reason" not in arguments:
        return arguments
    tool = tools.get(call.name)
    properties = {}
    if tool is not None and isinstance(tool.parameters, dict):
        raw_properties = tool.parameters.get("properties", {})
        if isinstance(raw_properties, dict):
            properties = raw_properties
    if "reason" not in properties:
        arguments.pop("reason", None)
    return arguments


def _groups_from_search_tools_result(result: ToolExecutionResult) -> set[str]:
    if not result.success:
        return set()
    content = result.content
    if isinstance(content, dict):
        raw_tools = content.get("tools") or content.get("results") or content.get("content")
    else:
        raw_tools = content
    if not isinstance(raw_tools, list):
        return set()
    groups: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        group = item.get("group")
        if isinstance(group, str) and group.strip():
            groups.add(group.strip())
    return groups


_DEDUP_SEARCH_TOOL_NAMES = frozenset({"web__web_search", "web_search"})
_DEDUP_FETCH_TOOL_NAMES = frozenset({"web__fetch_url", "fetch_url"})


@dataclass(frozen=True)
class _MemoryToolSupplement:
    continue_loop: bool
    results: list[ToolExecutionResult]
    appended_messages: list[ChatMessage]


def _assistant_turn_message(turn: "ChatCompletionTurn") -> ChatMessage:
    """从 ChatCompletionTurn 构建可追加到 working_messages 的 assistant 消息。"""
    return cast(ChatMessage, dict(turn.message))


def _try_supplement_missed_memory_tools(
    *,
    tools: ToolRegistry,
    working_messages: list[ChatMessage],
    turn: ChatCompletionTurn,
    execution_results: list[ToolExecutionResult],
    step_index: int,
    model_vision_enabled: bool,
) -> _MemoryToolSupplement | None:
    """模型只回了文本、没调记忆工具时，按用户意图补写或补搜长期记忆。"""
    if step_index != 0 or turn.tool_calls:
        return None

    if tool_routing.user_requests_memory_remember(working_messages):
        if any(result.tool_name == "memory_remember" for result in execution_results):
            return None
        if tools.get("memory_remember") is None:
            return None
        content = tool_routing.extract_memory_remember_content(working_messages)
        if not content:
            return None
        result = tools.execute("memory_remember", {"content": content})
        debug_log(
            "AgentRuntime",
            "自动补写长期记忆",
            {
                "content_chars": len(content),
                "success": result.success,
                "error": result.error or "",
            },
        )
        return _MemoryToolSupplement(continue_loop=False, results=[result], appended_messages=[])

    return None


def _normalize_fetch_url_for_dedup(url: object) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _is_duplicate_tool_call(
    call: NativeToolCall,
    execution_results: list[ToolExecutionResult],
) -> bool:
    if call.name in _DEDUP_SEARCH_TOOL_NAMES:
        return any(
            result.tool_name in _DEDUP_SEARCH_TOOL_NAMES
            and result.success
            and not (isinstance(result.content, dict) and result.content.get("skipped"))
            for result in execution_results
        )
    if call.name in _DEDUP_FETCH_TOOL_NAMES:
        target = _normalize_fetch_url_for_dedup(call.arguments.get("url"))
        if not target:
            return False
        for result in execution_results:
            if result.tool_name not in _DEDUP_FETCH_TOOL_NAMES or not result.success:
                continue
            if isinstance(result.content, dict) and result.content.get("skipped"):
                continue
            existing = ""
            if isinstance(result.content, dict):
                existing = _normalize_fetch_url_for_dedup(result.content.get("url"))
            if existing and existing == target:
                return True
        return False
    return False


def _build_duplicate_tool_call_result(call: NativeToolCall) -> ToolExecutionResult:
    if call.name in _DEDUP_FETCH_TOOL_NAMES:
        message = "本轮已读取过该网页，请直接根据之前的工具结果作答，不要重复抓取同一 URL。"
    else:
        message = "本轮已执行过同名工具，请直接根据之前的工具结果作答，不要重复调用。"
    return ToolExecutionResult(
        tool_name=call.name,
        success=True,
        content={
            "skipped": True,
            "reason": "duplicate_tool_call",
            "message": message,
        },
        error="",
    )
