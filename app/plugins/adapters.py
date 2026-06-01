"""app/plugins/adapters.py — SDK 兼容适配层。

将旧的 Shinsekai SDK 风格转换为新的 Sakura 原生插件接口。
"""

from __future__ import annotations

from app.agent.tools.registry import Tool
from app.plugins.models import ToolContribution


def sdk_tool_to_contribution(name: str, description: str, parameters: dict,
                              handler, group: str = "default",
                              risk: str = "low", requires_confirmation: bool = False,
                              capability: str | None = None) -> ToolContribution:
    """将 SDK 风格工具参数转换为统一贡献。"""
    return ToolContribution(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        group=group,
        risk=risk,
        requires_confirmation=requires_confirmation,
        capability=capability,
    )


def contribution_to_app_tool(contribution: ToolContribution) -> Tool:
    """将工具贡献转换为 app 可用的 Tool 实例。"""
    return Tool(
        name=contribution.name,
        description=contribution.description,
        parameters=contribution.parameters,
        handler=contribution.handler,
        requires_confirmation=contribution.requires_confirmation,
        group=contribution.group,
        risk=contribution.risk,
        capability=contribution.capability,
        source="plugin",
    )
