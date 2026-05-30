from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.actions import PendingToolAction
from app.debug_log import debug_log


ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    """内部工具定义。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: ToolHandler | None = None
    requires_confirmation: bool = False
    confirmation_risk: str = "normal"
    group: str = "default"
    risk: str = "low"
    capability: str | None = None


@dataclass(frozen=True)
class ToolExecutionResult:
    """工具执行结果，统一交回模型做最终表述。"""

    tool_name: str
    success: bool
    content: dict[str, Any] | str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tool_name": self.tool_name,
            "success": self.success,
            "content": self.content,
        }
        if self.error:
            data["error"] = self.error
        return data


class ToolRegistry:
    """管理 Agent 可用工具，后续 MCP Provider 会挂到这一层。"""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.free_access_enabled = True
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        debug_log(
            "ToolRegistry",
            "注册工具",
            {
                "name": tool.name,
                "group": tool.group,
                "risk": tool.risk,
                "requires_confirmation": tool.requires_confirmation,
            },
        )

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe_tools(self, allowed_capabilities: set[str] | None = None) -> list[dict[str, Any]]:
        """返回可暴露给模型的工具描述；可按能力开关隐藏敏感工具。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "requires_confirmation": tool.requires_confirmation,
                "group": tool.group,
                "risk": tool.risk,
            }
            for tool in self.all()
            if allowed_capabilities is None
            or tool.capability is None
            or tool.capability in allowed_capabilities
        ]

    def set_free_access_enabled(self, enabled: bool) -> None:
        """开启后普通确认工具直接执行，文件删除类高风险工具仍保留确认。"""
        self.free_access_enabled = enabled

    def prepare_or_execute(
        self,
        name: str,
        arguments: dict[str, Any],
        reason: str = "",
    ) -> ToolExecutionResult | PendingToolAction:
        tool = self.get(name)
        debug_log(
            "ToolRegistry",
            "准备工具执行",
            {
                "name": name,
                "known": tool is not None,
                "requires_confirmation": tool.requires_confirmation if tool is not None else False,
                "free_access_enabled": self.free_access_enabled,
                "arguments": arguments,
                "reason": reason,
            },
        )
        if tool is None or not tool.requires_confirmation:
            return self.execute(name, arguments)
        if self.free_access_enabled and not _requires_confirmation_despite_free_access(tool):
            debug_log("ToolRegistry", "自由访问模式直接执行确认工具", {"name": name})
            return self.execute(name, arguments)
        if not isinstance(arguments, dict):
            result = ToolExecutionResult(
                tool_name=name,
                success=False,
                content="",
                error="工具参数必须是 JSON object。",
            )
            debug_log("ToolRegistry", "工具参数无效", result.to_dict())
            return result
        action = PendingToolAction.create(
            tool_name=name,
            arguments=arguments,
            reason=reason,
        )
        debug_log("ToolRegistry", "工具等待用户确认", action.to_dict())
        return action

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        started_at = time.perf_counter()
        tool = self.get(name)
        if tool is None:
            result = ToolExecutionResult(
                tool_name=name,
                success=False,
                content="",
                error=f"未知工具：{name}",
            )
            debug_log("ToolRegistry", "工具执行失败", _result_with_elapsed(result, started_at))
            return result
        if tool.handler is None:
            result = ToolExecutionResult(
                tool_name=name,
                success=False,
                content="",
                error=f"工具未配置处理器：{name}",
            )
            debug_log("ToolRegistry", "工具执行失败", _result_with_elapsed(result, started_at))
            return result
        if not isinstance(arguments, dict):
            result = ToolExecutionResult(
                tool_name=name,
                success=False,
                content="",
                error="工具参数必须是 JSON object。",
            )
            debug_log("ToolRegistry", "工具执行失败", _result_with_elapsed(result, started_at))
            return result

        try:
            debug_log(
                "ToolRegistry",
                "开始执行工具",
                {
                    "name": name,
                    "group": tool.group,
                    "risk": tool.risk,
                    "arguments": arguments,
                },
            )
            content = tool.handler(arguments)
        except Exception as exc:
            result = ToolExecutionResult(
                tool_name=name,
                success=False,
                content="",
                error=str(exc),
            )
            debug_log("ToolRegistry", "工具执行异常", _result_with_elapsed(result, started_at))
            return result
        result = ToolExecutionResult(
            tool_name=name,
            success=True,
            content=content if isinstance(content, (dict, str)) else str(content),
        )
        debug_log("ToolRegistry", "工具执行成功", _result_with_elapsed(result, started_at))
        return result


def _requires_confirmation_despite_free_access(tool: Tool) -> bool:
    """识别自由访问模式也不能直接执行的高风险工具。"""
    if tool.risk == "high":
        return True
    if tool.confirmation_risk in {"delete_file", "file_delete", "destructive_file"}:
        return True
    normalized = tool.name.lower()
    return any(
        marker in normalized
        for marker in (
            "delete_file",
            "remove_file",
            "unlink_file",
            "delete_path",
            "remove_path",
            "delete_local_file",
            "remove_local_file",
        )
    )


def _result_with_elapsed(result: ToolExecutionResult, started_at: float) -> dict[str, Any]:
    data = result.to_dict()
    data["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
    return data
