"""SDK 共享类型定义 — 已扩展贡献点类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolContribution:
    """插件提供的工具贡献 (SDK风格)。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    group: str = "default"
    risk: str = "low"
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolsTabContribution:
    """插件贡献到设置窗口的工具页。"""

    tab_id: str
    title: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class SettingsPanelContribution:
    """插件贡献的设置面板。"""

    section_id: str
    title: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class ChatUIWidgetContribution:
    """插件贡献的聊天UI组件。"""

    widget_id: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class PromptPatchContribution:
    """插件贡献的提示词补丁。"""

    patch_id: str
    system_prompt_append: str = ""
    reply_protocol_append: str = ""
