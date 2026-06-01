"""tests/unit/test_plugin_system.py — 插件系统测试。

覆盖：
- PluginDiscovery 发现/解析
- PluginCapabilityRegistry 贡献收集
- PluginManager 加载/失败隔离/优先级
- PluginLoadResult
- PluginManifest / PluginSpec
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.plugins import (
    PluginCapabilityRegistry,
    PluginDiscovery,
    PluginLoadResult,
    PluginManager,
    PluginManifest,
    PluginSpec,
)
from app.plugins.models import (
    ChatUIWidgetContribution,
    PromptPatchContribution,
    SettingsPanelContribution,
    ToolContribution,
    ToolsTabContribution,
)


class TestPluginSpec:
    """PluginSpec 数据模型"""

    def test_basic_spec(self) -> None:
        spec = PluginSpec(entry="test.module:TestPlugin")
        assert spec.entry == "test.module:TestPlugin"
        assert spec.enabled is True
        assert spec.priority == 100

    def test_spec_with_priority(self) -> None:
        spec = PluginSpec(entry="test:Test", priority=50, enabled=False)
        assert spec.priority == 50
        assert not spec.enabled


class TestPluginManifest:
    """PluginManifest 数据模型"""

    def test_basic_manifest(self) -> None:
        m = PluginManifest(plugin_id="test", version="1.0")
        assert m.plugin_id == "test"
        assert m.version == "1.0"
        assert m.priority == 100
        assert m.enabled is True
        assert m.required is False


class TestPluginCapabilityRegistry:
    """能力注册表"""

    def test_register_tool(self) -> None:
        reg = PluginCapabilityRegistry()
        reg.register_tool(ToolContribution(name="t1", description="d", parameters={}, handler=None))
        assert len(reg.tools) == 1

    def test_register_multiple_types(self) -> None:
        reg = PluginCapabilityRegistry()
        reg.register_tool(ToolContribution(name="t1", description="d", parameters={}, handler=None))
        reg.register_tools_tab(ToolsTabContribution(tab_id="tab", title="T", build=lambda p: None))
        reg.register_settings_panel(SettingsPanelContribution(section_id="s", title="S", build=lambda p: None))
        reg.register_chat_ui_widget(ChatUIWidgetContribution(widget_id="w", build=lambda p: None))
        reg.register_prompt_patch(PromptPatchContribution(patch_id="p", system_prompt_append="append"))
        assert len(reg.tools) == 1
        assert len(reg.tools_tabs) == 1
        assert len(reg.settings_panels) == 1
        assert len(reg.chat_ui_widgets) == 1
        assert len(reg.prompt_patches) == 1

    def test_empty_registry(self) -> None:
        reg = PluginCapabilityRegistry()
        assert len(reg.tools) == 0
        assert len(reg.tools_tabs) == 0


class TestPluginDiscovery:
    """插件发现"""

    def test_empty_discover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "data" / "config").mkdir(parents=True)
            # no plugins.yaml
            discovery = PluginDiscovery(base)
            specs = discovery.discover()
            assert specs == []

    def test_discover_with_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config_dir = base / "data" / "config"
            config_dir.mkdir(parents=True)
            config_dir.joinpath("plugins.yaml").write_text("""
- entry: plugins.a:PluginA
  enabled: true
  priority: 200
- entry: plugins.b:PluginB
  enabled: false
  priority: 50
""")
            discovery = PluginDiscovery(base)
            specs = discovery.discover()
            assert len(specs) == 2
            # 按 priority 降序
            assert specs[0].priority == 200
            assert specs[0].enabled is True

    def test_discover_enabled_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config_dir = base / "data" / "config"
            config_dir.mkdir(parents=True)
            config_dir.joinpath("plugins.yaml").write_text("""
- entry: a:A
  enabled: true
- entry: b:B
  enabled: false
""")
            discovery = PluginDiscovery(base)
            enabled = discovery.discover_enabled()
            assert len(enabled) == 1
            assert enabled[0].entry == "a:A"


class TestPluginManager:
    """插件管理器"""

    def test_load_all_no_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "data" / "config").mkdir(parents=True)
            mgr = PluginManager(base)
            results = mgr.load_all()
            assert results == []
            assert mgr.loaded_count == 0
            assert mgr.failed_count == 0

    def test_collect_tools_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "data" / "config").mkdir(parents=True)
            mgr = PluginManager(base)
            mgr.load_all()
            tools = mgr.collect_tools()
            assert tools == []

    def test_collect_tools_tabs_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "data" / "config").mkdir(parents=True)
            mgr = PluginManager(base)
            mgr.load_all()
            tabs = mgr.collect_tools_tabs()
            assert tabs == []

    def test_plugin_load_result(self) -> None:
        spec = PluginSpec(entry="test:Test")
        result = PluginLoadResult(spec=spec, error="load failed")
        assert not result.loaded
        assert result.error == "load failed"

    def test_plugin_load_result_success(self) -> None:
        spec = PluginSpec(entry="test:Test")
        manifest = PluginManifest(plugin_id="test")
        result = PluginLoadResult(spec=spec, manifest=manifest, loaded=True)
        assert result.loaded
        assert result.manifest is not None


class TestContributionTypes:
    """贡献点数据模型"""

    def test_tool_contribution(self) -> None:
        tc = ToolContribution(name="test", description="desc", parameters={},
                              handler=None, group="memory", risk="medium",
                              requires_confirmation=True, capability="memory")
        assert tc.name == "test"
        assert tc.group == "memory"
        assert tc.risk == "medium"
        assert tc.requires_confirmation

    def test_settings_panel_contribution(self) -> None:
        sp = SettingsPanelContribution(section_id="test", title="Test Panel",
                                       build=lambda p: None, order=50.0)
        assert sp.section_id == "test"
        assert sp.order == 50.0

    def test_prompt_patch_contribution(self) -> None:
        pp = PromptPatchContribution(patch_id="p1", system_prompt_append="extra prompt")
        assert pp.patch_id == "p1"
        assert pp.system_prompt_append == "extra prompt"
