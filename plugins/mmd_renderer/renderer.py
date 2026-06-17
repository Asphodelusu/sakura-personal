"""plugins/mmd_renderer/renderer.py — MMD 渲染器。

实现 :class:`CharacterRenderer` 接口，把 Python 侧命令转换成 Python→JS 消息，
交由 :class:`MMDWindow` 注入网页执行。本类**不解析 PMX、不实现渲染**，只负责
命令转发、窗口生命周期与事件映射；真正的 three.js/MMDLoader 渲染在 web 侧。

事件→动作映射：优先读取角色配置的 ``event_motions``（事件名→动作名），
再叠加内置默认（TTS 口型、LLM 思考/中性表情），不把全部事件逻辑写死。
"""

from __future__ import annotations

from typing import Any

from app.core.debug_log import debug_log
from app.plugins.events import (
    EVENT_LLM_REQUEST_FAILED,
    EVENT_LLM_REQUEST_FINISHED,
    EVENT_LLM_REQUEST_STARTED,
    EVENT_PET_HIDDEN,
    EVENT_PET_REOPENED,
    EVENT_TTS_FAILED,
    EVENT_TTS_FINISHED,
    EVENT_TTS_STARTED,
)
from app.plugins.models import RendererCreateContext
from app.renderers.base import CharacterRenderer
from .bridge import (
    MSG_EVENT,
    MSG_LOAD_CHARACTER,
    MSG_LOOK_AT,
    MSG_PING,
    MSG_PLAY_MOTION,
    MSG_SET_EXPRESSION,
    MSG_SET_LIP_SYNC,
    MSG_SET_SCALE,
    MSG_STOP_MOTION,
)
from .config import MMDRendererConfig
from .window import MMDWindow, web_index_path


class MMDRenderer(CharacterRenderer):
    """基于 QWebEngineView + three.js 的 MMD 渲染后端（骨架）。"""

    renderer_name = "mmd"
    replaces_default_portrait = True

    def __init__(self, context: RendererCreateContext) -> None:
        self._context = context
        self._parent_window = context.owner_window
        self.window: MMDWindow | None = None
        self.config: MMDRendererConfig | None = None

    # ---- 生命周期 ----

    def is_available(self) -> bool:
        # 能 import 到本模块说明 QtWebEngine 可用；再确认渲染页面存在。
        available = web_index_path().exists()
        if not available:
            debug_log("MMDRenderer", "渲染页面不存在，判定不可用", {"path": str(web_index_path())})
        return available

    def initialize(self, app_context: dict[str, Any] | None = None) -> None:
        debug_log("MMDRenderer", "初始化", {"character_id": self._context.character_id})
        package_dir = self._context.package_dir
        renderer_cfg = self._context.renderer_config
        self.config = MMDRendererConfig.from_mapping(renderer_cfg, package_dir)
        # 不把 MMDWindow 设为宿主 Tool 子窗口，否则平台会强制它压在宿主之上。
        # 它的生命周期由本 renderer 持有，层级由 _stack_below_parent 同步。
        self.window = MMDWindow(parent=None)
        self.window.load_renderer_page()
        # 探活消息，验证 Python→JS 链路（会先入队，页面就绪后 flush）。
        self.window.dispatch(MSG_PING)

    def load_character(self, character_config: dict[str, Any]) -> None:
        package_dir = self._context.package_dir
        self.config = MMDRendererConfig.from_mapping(character_config, package_dir)
        if self.config.missing_paths:
            debug_log("MMDRenderer", "部分模型/动作资源缺失（框架阶段忽略）", {"missing": self.config.missing_paths})
        debug_log(
            "MMDRenderer",
            "加载角色",
            {"model": self.config.model_url, "motions": list(self.config.motions)},
        )
        self._dispatch(MSG_LOAD_CHARACTER, self.config.to_payload())
        if self.config.scale and self.config.scale != 1.0:
            self.set_scale(self.config.scale)

    def show(self) -> None:
        if self.window is not None:
            self.window.show()
            self._stack_below_parent()

    def hide(self) -> None:
        if self.window is not None:
            self.window.hide()

    def close(self) -> None:
        if self.window is not None:
            self.window.close()
            self.window = None

    # ---- 变换 ----

    def set_position(self, x: int, y: int) -> None:
        if self.window is not None:
            self.window.move(int(x), int(y))

    def set_geometry(self, x: int, y: int, width: int, height: int) -> None:
        if self.window is not None:
            self.window.setGeometry(int(x), int(y), max(1, int(width)), max(1, int(height)))
            if self.window.isVisible():
                self._stack_below_parent()

    def stack_below(self, owner_window: Any, *, topmost: bool | None = None) -> None:
        if self.window is not None:
            self.window.stack_below(owner_window, topmost=topmost)

    def set_scale(self, scale: float) -> None:
        self._dispatch(MSG_SET_SCALE, {"scale": float(scale)})

    # ---- 动作 / 表情 / 口型 ----

    def play_motion(self, motion_name: str, loop: bool = False) -> None:
        self._dispatch(MSG_PLAY_MOTION, {"name": motion_name, "loop": bool(loop)})

    def stop_motion(self, motion_name: str | None = None) -> None:
        self._dispatch(MSG_STOP_MOTION, {"name": motion_name})

    def set_expression(self, expression_name: str, weight: float = 1.0) -> None:
        self._dispatch(MSG_SET_EXPRESSION, {"name": expression_name, "weight": float(weight)})

    def set_lip_sync(self, value: float) -> None:
        self._dispatch(MSG_SET_LIP_SYNC, {"value": float(value)})

    def look_at(self, x: float, y: float) -> None:
        self._dispatch(MSG_LOOK_AT, {"x": float(x), "y": float(y)})

    # ---- 事件映射 ----

    def handle_event(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        # 先把原始事件透传给 JS，便于 web 侧自定义响应。
        self._dispatch(MSG_EVENT, {"name": event_name, "payload": payload or {}})

        # 配置驱动：事件 → 角色配置中映射的动作。
        if self.config is not None:
            motion = self.config.event_motions.get(event_name)
            if motion:
                self.play_motion(motion)

        # 内置默认：TTS 口型与 LLM 思考/中性表情（后续可被配置覆盖）。
        if event_name == EVENT_TTS_STARTED:
            self.set_lip_sync(0.5)
        elif event_name in (EVENT_TTS_FINISHED, EVENT_TTS_FAILED):
            self.set_lip_sync(0.0)
        elif event_name == EVENT_LLM_REQUEST_STARTED:
            self.set_expression("thinking", 1.0)
        elif event_name in (EVENT_LLM_REQUEST_FINISHED, EVENT_LLM_REQUEST_FAILED):
            self.set_expression("neutral", 1.0)
        elif event_name == EVENT_PET_HIDDEN:
            self.hide()
        elif event_name == EVENT_PET_REOPENED:
            self.show()

    # ---- 内部 ----

    def _dispatch(self, message_type: str, payload: dict[str, Any] | None = None) -> None:
        if self.window is None:
            debug_log("MMDRenderer", "窗口未就绪，丢弃消息", {"type": message_type})
            return
        self.window.dispatch(message_type, payload)

    def _stack_below_parent(self) -> None:
        if self.window is None or self._parent_window is None:
            return
        topmost = bool(getattr(self._parent_window, "always_on_top_enabled", False))
        self.window.stack_below(self._parent_window, topmost=topmost)
