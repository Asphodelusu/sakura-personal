"""plugins/mmd_renderer/window.py — MMD 渲染承载窗口。

:class:`MMDWindow` 直接用 QWebEngineView 承载本地 HTML/WebGL 渲染页。窗口只
负责透明角色层，气泡与输入栏仍由主 PetWindow 绘制并保持在前景。

注意：本模块顶层 import QtWebEngineWidgets，在缺少该组件的环境会 ImportError，
由上层 RendererManager 的 try 包裹捕获并降级，故此处不做额外保护。
"""

from __future__ import annotations

import os
import sys

# QtWebEngine 的 WebGL 后端在不同 Windows GPU/驱动组合下差异很大。默认用
# ANGLE + SwiftShader 稳定提供 WebGL 上下文。必须在 QtWebEngine 初始化（首个
# QWebEngineView 创建）之前设置，故置于模块顶层、WebEngine 导入之前。
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist",
)

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow, QWidget

from app.core.debug_log import debug_log
from .channel import WebMessageChannel

# 渲染窗口默认尺寸（框架阶段；后续随模型/缩放调整）。
_DEFAULT_WIDTH = 400
_DEFAULT_HEIGHT = 600


def web_index_path() -> Path:
    """返回打包内 web/index.html 的绝对路径。"""
    return Path(__file__).resolve().parent / "web" / "index.html"


class _LoggingWebPage(QWebEnginePage):
    """把网页 console.log/warn/error 转发到 Sakura 运行日志。

    QWebEngineView 默认丢弃 JS 控制台输出，开发期无从观测 Python→JS 链路。
    重写本回调转发到 debug_log——debug_log 始终写入 GUI 运行日志（不依赖
    SAKURA_DEBUG 环境变量），因此打开「运行日志」窗口即可看到 [SakuraMMD] 消息。
    """

    # QWebEnginePage.JavaScriptConsoleMessageLevel 取值映射（Info/Warning/Error）。
    _LEVELS = {0: "info", 1: "warning", 2: "error"}

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802 — Qt 命名
        try:
            level_name = self._LEVELS.get(int(level), str(level))
        except Exception:  # noqa: BLE001 — 日志转发不得抛异常
            level_name = str(level)
        debug_log("SakuraMMD", message, {"level": level_name, "line": line_number})


class MMDWindow(QMainWindow):
    """承载 MMD WebGL 渲染页的独立窗口。"""

    _dispatch_requested = Signal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sakura MMD Renderer")

        self.view = QWebEngineView(self)
        self._page = _LoggingWebPage(self.view)
        self.view.setPage(self._page)
        self.setCentralWidget(self.view)

        # 透明角色层：不置顶、不抢输入，层级由宿主同步到 PetWindow 正下方。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setStyleSheet("background: transparent;")
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        try:
            self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        except Exception as exc:  # noqa: BLE001 — 背景设置失败不应阻断
            debug_log("MMDWindow", "设置 WebEngine 背景失败", {"error": str(exc)})

        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)

        self._channel = WebMessageChannel(self._run_js)
        self.view.loadFinished.connect(self._on_load_finished)
        self._dispatch_requested.connect(self._dispatch_on_window_thread)
        self._closed = False

    # ---- 页面加载 ----

    def load_renderer_page(self) -> None:
        """加载本地渲染页；页面缺失抛异常交由上层降级。"""
        index = web_index_path()
        if not index.exists():
            raise FileNotFoundError(f"MMD 渲染页面缺失：{index}")
        url = QUrl.fromLocalFile(str(index))
        debug_log("MMDWindow", "加载渲染页面", {"path": str(index), "url": url.toString()})
        self._channel.reset()
        self.view.load(url)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            debug_log("MMDWindow", "页面加载成功", {})
            self._channel.set_ready()
        else:
            debug_log("MMDWindow", "页面加载失败", {})

    # ---- 消息分发 ----

    def dispatch(self, message_type: str, payload: dict | None = None) -> None:
        """线程安全分发入口；实际 WebEngine 操作统一回到窗口线程执行。"""
        self._dispatch_requested.emit(message_type, payload or {})

    @Slot(str, object)
    def _dispatch_on_window_thread(self, message_type: str, payload: object = None) -> None:
        """窗口线程内分发消息，避免后台 worker 直接触碰 QWebEngine。"""
        if self._closed:
            return
        if not isinstance(payload, dict):
            payload = {}
        self._channel.dispatch(message_type, payload)

    def _run_js(self, js: str) -> None:
        page = self.view.page()
        if page is not None:
            page.runJavaScript(js)

    def stack_below(self, owner: QWidget, *, topmost: bool | None = None) -> None:
        """把 MMD 原生窗口放到宿主窗口正下方，让宿主子控件盖在角色之上。"""
        if owner is None or not self.isVisible():
            return
        if sys.platform == "win32":
            self._stack_below_win32(owner, topmost=topmost)
            return
        self.lower()
        owner.raise_()

    def _stack_below_win32(self, owner: QWidget, *, topmost: bool | None = None) -> None:
        try:
            import ctypes

            hwnd = int(self.winId())
            owner_hwnd = int(owner.winId())
            if not hwnd or not owner_hwnd:
                return
            swp_no_size = 0x0001
            swp_no_move = 0x0002
            swp_no_activate = 0x0010
            flags = swp_no_size | swp_no_move | swp_no_activate
            if topmost is not None:
                hwnd_topmost = -1
                hwnd_notopmost = -2
                insert_after = hwnd_topmost if topmost else hwnd_notopmost
                ctypes.windll.user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)
            # hWndInsertAfter=owner_hwnd 会把角色层放到宿主窗口之后，即视觉上在宿主下方。
            ctypes.windll.user32.SetWindowPos(hwnd, owner_hwnd, 0, 0, 0, 0, flags)
        except Exception as exc:  # noqa: BLE001 — 层级同步失败不应影响角色显示
            debug_log("MMDWindow", "同步窗口层级失败", {"error": str(exc)})

    @property
    def channel(self) -> WebMessageChannel:
        return self._channel

    # ---- 生命周期 ----

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        super().resizeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        self._closed = True
        debug_log("MMDWindow", "窗口关闭", {})
        super().closeEvent(event)
