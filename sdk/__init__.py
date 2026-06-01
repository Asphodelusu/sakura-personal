"""Sakura 的 Shinsekai 插件兼容层。

注意：sdk/tool_registry.py 的全局变量设计已废弃。
新插件请使用 app/plugins/ 中的原生接口 (PluginCapabilityRegistry)。
"""

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry

__all__ = [
    "PluginBase",
    "PluginCapabilityRegistry",
    "PluginHostContext",
]
