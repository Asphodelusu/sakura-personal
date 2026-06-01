"""SDK 能力注册表 — 已升级为 Sakura 原生插件系统的新接口。

旧的 register_tools_tab 接口保留兼容。
新增 register_tool / register_settings_panel / register_chat_ui_widget / register_prompt_patch。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sdk.types import (
    ChatUIWidgetContribution,
    PromptPatchContribution,
    SettingsPanelContribution,
    ToolContribution as SDKToolContribution,
    ToolsTabContribution,
)


@dataclass
class PluginCapabilityRegistry:
    """收集插件贡献的能力注册表。"""

    tools: list[SDKToolContribution] = field(default_factory=list)
    settings_panels: list[SettingsPanelContribution] = field(default_factory=list)
    tools_tabs: list[ToolsTabContribution] = field(default_factory=list)
    chat_ui_widgets: list[ChatUIWidgetContribution] = field(default_factory=list)
    prompt_patches: list[PromptPatchContribution] = field(default_factory=list)

    def register_tool(self, contribution: SDKToolContribution) -> None:
        self.tools.append(contribution)

    def register_settings_panel(self, contribution: SettingsPanelContribution) -> None:
        self.settings_panels.append(contribution)

    def register_tools_tab(self, contribution: ToolsTabContribution) -> None:
        self.tools_tabs.append(contribution)

    def register_chat_ui_widget(self, contribution: ChatUIWidgetContribution) -> None:
        self.chat_ui_widgets.append(contribution)

    def register_prompt_patch(self, contribution: PromptPatchContribution) -> None:
        self.prompt_patches.append(contribution)
