# SAKURA_PLUGIN_SDK.md — Sakura 插件开发指南

> **重要安全声明：Sakura 插件在宿主进程中运行，不是安全沙箱。**
> 插件可以访问文件系统、网络和宿主应用的内部状态。只安装来自可信来源的插件。

---

## 快速开始

一个最小插件结构：

```
my_plugin/
  plugin.py    # 插件入口
```

`plugin.py`:

```python
from pathlib import Path
from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.types import ToolContribution

class MyPlugin(PluginBase):
    @property
    def plugin_id(self) -> str:
        return "my_plugin"

    @property
    def plugin_version(self) -> str:
        return "1.0.0"

    def initialize(self, register: PluginCapabilityRegistry,
                   plugin_root: Path, host: PluginHostContext) -> None:
        register.register_tool(ToolContribution(
            name="my_tool", description="my tool",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self._handle_my_tool, group="default", risk="low",
        ))

    def _handle_my_tool(self, **kwargs):
        return {"result": "hello from plugin"}

    def shutdown(self) -> None:
        pass
```

在 `data/config/plugins.yaml` 中注册：

```yaml
- entry: plugins.my_plugin.plugin:MyPlugin
  enabled: true
  priority: 100
```

---

## 贡献点

| 方法 | 贡献类型 | 说明 |
|------|----------|------|
| `register_tool()` | ToolContribution | 注册 Agent 工具 |
| `register_tools_tab()` | ToolsTabContribution | 设置窗口工具页 |
| `register_settings_panel()` | SettingsPanelContribution | 设置面板 |
| `register_chat_ui_widget()` | ChatUIWidgetContribution | 聊天 UI 组件 |
| `register_prompt_patch()` | PromptPatchContribution | 提示词补丁 |

---

## PluginBase 接口

| 属性/方法 | 说明 |
|----------|------|
| `plugin_id` (property) | 唯一标识符 |
| `plugin_version` (property) | 版本号，默认 "0.0.0" |
| `initialize(register, plugin_root, host)` | 初始化注册贡献 |
| `shutdown()` | 清理资源 |

---

## PluginHostContext

只暴露安全信息 (base_dir)，不包含 API Key 等敏感数据。

---

## 插件配置 (plugins.yaml)

```yaml
- entry: plugins.my_plugin.plugin:MyPlugin  # module:ClassName
  enabled: true
  priority: 100     # 越大越先加载
```

---

## 从旧 SDK 迁移

旧版使用 `sdk/tool_registry.py` 的 `@tool()` 装饰器和全局状态，已废弃。
新代码使用 `PluginCapabilityRegistry.register_tool()` 直接注册 ToolContribution。
