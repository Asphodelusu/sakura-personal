"""Reminders are deferred: the prompt must not coach a tool that is not registered."""

from __future__ import annotations

from app.agent.runtime import AgentRuntime
from app.agent.tools import Tool, ToolRegistry


def _reminder_tool() -> Tool:
    return Tool(
        name="add_reminder",
        description="synthetic reminder",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _arguments: {},
    )


def test_prompt_omits_reminder_rules_without_the_tool() -> None:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", tools=ToolRegistry([]))

    assert "add_reminder" not in runtime._build_tool_system_prompt()


def test_prompt_keeps_reminder_rules_when_the_tool_exists() -> None:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", tools=ToolRegistry([_reminder_tool()]))

    assert "add_reminder" in runtime._build_tool_system_prompt()
