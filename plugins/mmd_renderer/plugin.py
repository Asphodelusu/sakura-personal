from __future__ import annotations

from app.plugins import (
    PluginBase,
    PluginCapabilityRegistry,
    PluginContext,
    RendererContribution,
    RendererCreateContext,
)


class MMDRendererPlugin(PluginBase):
    """把 MMD 渲染能力作为插件贡献给宿主。"""

    plugin_id = "mmd_renderer"
    plugin_version = "0.1.0"

    def initialize(
        self,
        register: PluginCapabilityRegistry,
        context: PluginContext,
    ) -> None:
        self.context = context
        register.register_renderer(
            RendererContribution(
                renderer_type="mmd",
                display_name="MMD Renderer",
                create=self._create_renderer,
            )
        )

    def _create_renderer(self, context: RendererCreateContext):
        # 延迟导入：只有角色请求 renderer.type=mmd 时才加载 QtWebEngine / three.js 承载层。
        from .renderer import MMDRenderer

        return MMDRenderer(context)
