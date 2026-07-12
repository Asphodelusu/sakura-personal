from __future__ import annotations

import importlib
from typing import Any

from app.agent.actions import AgentAction, AgentEvent, AgentProgress, AgentResult, PendingToolAction
from app.agent.reminders import ReminderStore, ScheduledReminder
from app.agent.tools import Tool, ToolExecutionResult, ToolMetadata, ToolPermissionPolicy, ToolRegistry
from app.agent.runtime_limits import (
    MAX_AGENT_STEPS_PER_TURN,
    MAX_TOOL_CALLS_PER_STEP,
    MAX_TOOL_CALLS_PER_TURN,
    ProgressCallback,
    RuntimeLoopSettings,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentRuntime": ("app.agent.runtime", "AgentRuntime"),
    "MemoryStore": ("app.agent.memory", "MemoryStore"),
    "create_builtin_tool_registry": ("app.agent.builtin_tools", "create_builtin_tool_registry"),
    "MCPToolProvider": ("app.agent.mcp.provider", "MCPToolProvider"),
    "register_mcp_tools_from_config": ("app.agent.mcp.provider", "register_mcp_tools_from_config"),
}

__all__ = [
    "AgentAction",
    "AgentEvent",
    "AgentProgress",
    "AgentResult",
    "AgentRuntime",
    "MAX_AGENT_STEPS_PER_TURN",
    "MAX_TOOL_CALLS_PER_STEP",
    "MAX_TOOL_CALLS_PER_TURN",
    "MCPToolProvider",
    "MemoryStore",
    "PendingToolAction",
    "ProgressCallback",
    "ReminderStore",
    "RuntimeLoopSettings",
    "ScheduledReminder",
    "Tool",
    "ToolExecutionResult",
    "ToolMetadata",
    "ToolPermissionPolicy",
    "ToolRegistry",
    "create_builtin_tool_registry",
    "register_mcp_tools_from_config",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    return getattr(importlib.import_module(module_name), attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
