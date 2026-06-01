"""app/plugins/models.py — 插件数据模型。

定义插件清单(manifest)、发现规格(spec)、贡献点(contribution)的统一模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class PluginManifest:
    """插件的完整清单信息。

    可从 plugin.yaml 或 PluginBase 属性中解析。
    """

    plugin_id: str
    version: str = "0.0.0"
    priority: int = 100
    enabled: bool = True
    required: bool = False
    entry: str = ""


@dataclass(frozen=True)
class PluginSpec:
    """插件发现规格。

    从 plugins.yaml 配置文件解析。
    """

    entry: str
    enabled: bool = True
    priority: int = 100


# ---- 贡献点类型 ----

@dataclass(frozen=True)
class ToolContribution:
    """插件提供的工具贡献。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., Any] | None = None
    group: str = "default"
    risk: str = "low"
    requires_confirmation: bool = False
    capability: str | None = None


@dataclass(frozen=True)
class SettingsPanelContribution:
    """插件贡献到设置窗口的面板/区段。"""

    section_id: str
    title: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class ToolsTabContribution:
    """插件贡献到设置窗口的工具页。"""

    tab_id: str
    title: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class ChatUIWidgetContribution:
    """插件贡献到聊天 UI 的组件。"""

    widget_id: str
    build: Callable[[Any], Any]
    order: float = 100.0


@dataclass(frozen=True)
class PromptPatchContribution:
    """插件贡献的提示词/输出合约补丁。"""

    patch_id: str
    system_prompt_append: str = ""
    reply_protocol_append: str = ""
