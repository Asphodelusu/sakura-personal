from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agent.tool_registry import Tool, ToolRegistry
from app.core.debug_log import debug_log
from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.tool_registry import RegisteredTool, clear_registered_tools, registered_tools
from sdk.types import ToolsTabContribution


@dataclass(frozen=True)
class PluginSpec:
    entry: str
    enabled: bool = True


@dataclass
class SakuraPluginManager:
    """只负责加载本地固定插件的轻量插件管理器。"""

    base_dir: Path
    plugins: list[PluginBase] = field(default_factory=list)
    tools_tabs: list[ToolsTabContribution] = field(default_factory=list)

    def load_from_config(self, tool_registry: ToolRegistry) -> None:
        for spec in load_plugin_specs(self.base_dir / "data" / "config" / "plugins.yaml"):
            if not spec.enabled:
                continue
            self._load_plugin(spec, tool_registry)

    def shutdown_all(self) -> None:
        for plugin in reversed(self.plugins):
            try:
                plugin.shutdown()
            except Exception as exc:
                debug_log(
                    "PluginManager",
                    "插件关闭失败",
                    {"plugin": plugin.plugin_id, "error": str(exc)},
                )

    def _load_plugin(self, spec: PluginSpec, tool_registry: ToolRegistry) -> None:
        plugin_cls = _import_entry(spec.entry)
        plugin = plugin_cls()
        if not isinstance(plugin, PluginBase):
            raise TypeError(f"插件入口不是 PluginBase：{spec.entry}")

        clear_registered_tools()
        capability_registry = PluginCapabilityRegistry()
        plugin_root = _plugin_root_from_entry(self.base_dir, spec.entry)
        plugin.initialize(
            capability_registry,
            plugin_root,
            PluginHostContext(base_dir=self.base_dir),
        )
        converted_tools = registered_tools()
        for registered in converted_tools:
            tool_registry.register(_convert_registered_tool(registered))
        self.tools_tabs.extend(capability_registry.tools_tabs)
        self.plugins.append(plugin)
        debug_log(
            "PluginManager",
            "插件已加载",
            {
                "entry": spec.entry,
                "plugin_id": plugin.plugin_id,
                "tool_count": len(converted_tools),
                "tools_tab_count": len(capability_registry.tools_tabs),
            },
        )


def load_plugin_specs(path: Path) -> list[PluginSpec]:
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    specs: list[PluginSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = item.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            continue
        specs.append(PluginSpec(entry=entry.strip(), enabled=bool(item.get("enabled", True))))
    return specs


def _import_entry(entry: str) -> type[PluginBase]:
    module_name, _, class_name = entry.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"插件入口格式无效：{entry}")
    module = importlib.import_module(module_name)
    plugin_cls = getattr(module, class_name)
    if not isinstance(plugin_cls, type):
        raise TypeError(f"插件入口不是类：{entry}")
    return plugin_cls


def _plugin_root_from_entry(base_dir: Path, entry: str) -> Path:
    module_name = entry.partition(":")[0]
    parts = module_name.split(".")
    if len(parts) >= 2 and parts[0] == "plugins":
        return base_dir / "plugins" / parts[1]
    return base_dir / "plugins"


def _convert_registered_tool(registered: RegisteredTool) -> Tool:
    def handler(arguments: dict[str, Any]) -> Any:
        kwargs = {
            name: arguments.get(name)
            for name in registered.parameters.get("properties", {})
        }
        return registered.func(**kwargs)

    return Tool(
        name=registered.name,
        description=registered.description,
        parameters=registered.parameters,
        handler=handler,
        requires_confirmation=registered.requires_confirmation,
        confirmation_risk="high" if registered.risk == "high" else "normal",
        group=registered.group,
        risk=registered.risk,
    )
