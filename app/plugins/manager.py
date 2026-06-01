"""app/plugins/manager.py — 插件管理器。

负责插件的加载、生命周期管理、贡献收集和失败隔离。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.debug_log import debug_log
from app.plugins.capabilities import PluginCapabilities, PluginCapabilityRegistry
from app.plugins.discovery import PluginDiscovery
from app.plugins.models import (
    PluginManifest,
    PluginSpec,
    ToolContribution,
)
from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext


@dataclass
class PluginLoadResult:
    """单个插件的加载结果。"""

    spec: PluginSpec
    manifest: PluginManifest | None = None
    capabilities: PluginCapabilities | None = None
    error: str | None = None
    loaded: bool = False


@dataclass
class PluginManager:
    """插件管理器。

    职责：
    - 发现插件 (委托给 PluginDiscovery)
    - 按优先级加载插件
    - 收集贡献
    - 失败隔离 (单个插件失败不影响其他)
    - 清理 (shutdown)
    """

    base_dir: Path
    _loaded: list[PluginLoadResult] = field(default_factory=list)
    _plugins: list[PluginBase] = field(default_factory=list)

    @property
    def host_context(self) -> PluginHostContext:
        """返回安全的宿主上下文。"""
        return PluginHostContext(base_dir=self.base_dir)

    def load_all(self) -> list[PluginLoadResult]:
        """加载所有启用的插件。"""
        discovery = PluginDiscovery(self.base_dir)
        specs = discovery.discover_enabled()
        results: list[PluginLoadResult] = []
        for spec in specs:
            result = self._load_one(spec)
            results.append(result)
            if result.error:
                if spec.priority < 0:  # required 插件失败则中止
                    debug_log("PluginManager", "必需插件加载失败，中止", {
                        "entry": spec.entry, "error": result.error,
                    })
                    break
        self._loaded = results
        return results

    def _load_one(self, spec: PluginSpec) -> PluginLoadResult:
        """加载单个插件，失败不影响其他插件。"""
        result = PluginLoadResult(spec=spec)
        try:
            plugin = _import_plugin(spec.entry)
            manifest = _build_manifest(plugin, spec)
            result.manifest = manifest

            capability_registry = PluginCapabilityRegistry()
            plugin_root = _plugin_root_from_entry(self.base_dir, spec.entry)
            plugin.initialize(capability_registry, plugin_root, self.host_context)

            result.capabilities = PluginCapabilities(
                plugin_id=manifest.plugin_id,
                tools=list(capability_registry.tools),
                settings_panels=list(capability_registry.settings_panels),
                tools_tabs=list(capability_registry.tools_tabs),
                chat_ui_widgets=list(capability_registry.chat_ui_widgets),
                prompt_patches=list(capability_registry.prompt_patches),
            )
            result.loaded = True
            self._plugins.append(plugin)
            debug_log("PluginManager", "插件已加载", {
                "plugin_id": manifest.plugin_id,
                "tools": len(result.capabilities.tools),
                "tabs": len(result.capabilities.tools_tabs),
            })
        except Exception as exc:
            result.error = str(exc)
            debug_log("PluginManager", "插件加载失败", {
                "entry": spec.entry, "error": str(exc),
            })
        return result

    def collect_tools(self) -> list[ToolContribution]:
        """收集所有已加载插件的工具贡献。"""
        tools: list[ToolContribution] = []
        for result in self._loaded:
            if result.capabilities:
                tools.extend(result.capabilities.tools)
        return tools

    def collect_settings_panels(self) -> list:
        """收集所有已加载插件的设置面板贡献。"""
        panels: list = []
        for result in self._loaded:
            if result.capabilities:
                panels.extend(result.capabilities.settings_panels)
        return panels

    def collect_tools_tabs(self) -> list:
        """收集所有已加载插件的工具页贡献。"""
        tabs: list = []
        for result in self._loaded:
            if result.capabilities:
                tabs.extend(result.capabilities.tools_tabs)
        return tabs

    def collect_prompt_patches(self) -> list:
        """收集所有已加载插件的提示词补丁。"""
        patches: list = []
        for result in self._loaded:
            if result.capabilities:
                patches.extend(result.capabilities.prompt_patches)
        return patches

    def shutdown_all(self) -> None:
        """逆序关闭所有已加载插件。"""
        for plugin in reversed(self._plugins):
            try:
                plugin.shutdown()
            except Exception as exc:
                debug_log("PluginManager", "插件关闭失败", {
                    "plugin": getattr(plugin, "plugin_id", "unknown"),
                    "error": str(exc),
                })

    @property
    def loaded_count(self) -> int:
        """已成功加载的插件数。"""
        return sum(1 for r in self._loaded if r.loaded)

    @property
    def failed_count(self) -> int:
        """加载失败的插件数。"""
        return sum(1 for r in self._loaded if r.error)

    @property
    def results(self) -> list[PluginLoadResult]:
        """所有加载结果。"""
        return list(self._loaded)


# ---- 内部辅助 ----

def _import_plugin(entry: str) -> PluginBase:
    module_name, _, class_name = entry.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"插件入口格式无效：{entry}")
    module = importlib.import_module(module_name)
    plugin_cls = getattr(module, class_name)
    plugin = plugin_cls()
    if not isinstance(plugin, PluginBase):
        raise TypeError(f"插件入口不是 PluginBase：{entry}")
    return plugin


def _build_manifest(plugin: PluginBase, spec: PluginSpec) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin.plugin_id,
        version=plugin.plugin_version,
        priority=spec.priority,
        enabled=spec.enabled,
        entry=spec.entry,
    )


def _plugin_root_from_entry(base_dir: Path, entry: str) -> Path:
    module_name = entry.partition(":")[0]
    parts = module_name.split(".")
    if len(parts) >= 2 and parts[0] == "plugins":
        return base_dir / "plugins" / parts[1]
    return base_dir / "plugins"
