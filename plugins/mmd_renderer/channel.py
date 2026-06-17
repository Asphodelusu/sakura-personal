"""plugins/mmd_renderer/channel.py — Web 消息通道（队列 + flush）。

把「页面加载完成前的消息缓存、加载完成后按序补发」这一核心逻辑从 Qt 窗口中
剥离为纯 Python 类，便于不依赖 QtWebEngine 进行单元测试。

工作方式：
- 页面就绪前 :meth:`dispatch` 把消息入队；
- :meth:`set_ready` 在 ``loadFinished`` 后调用，置就绪并按入队顺序 flush；
- 实际注入由构造时传入的 ``send_js`` 回调完成（窗口侧用 runJavaScript）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.core.debug_log import debug_log
from .bridge import build_dispatch_js, build_message


class WebMessageChannel:
    """Python→JS 消息通道，带「未就绪入队、就绪后 flush」语义。"""

    def __init__(self, send_js: Callable[[str], None]) -> None:
        self._send_js = send_js
        self._ready = False
        self._pending: list[dict[str, Any]] = []

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def dispatch(self, message_type: str, payload: dict[str, Any] | None = None) -> None:
        """派发消息：未就绪则入队，就绪则立即发送。"""
        message = build_message(message_type, payload)
        if not self._ready:
            self._pending.append(message)
            debug_log("MMDWindow", "页面未就绪，消息入队", {"type": message_type, "queued": len(self._pending)})
            return
        self._emit(message)

    def set_ready(self) -> None:
        """页面加载完成：置就绪并按序 flush 暂存消息。"""
        self._ready = True
        if not self._pending:
            return
        pending = self._pending
        self._pending = []
        debug_log("MMDWindow", "页面就绪，flush 暂存消息", {"count": len(pending)})
        for message in pending:
            self._emit(message)

    def reset(self) -> None:
        """页面重新加载时复位（重新进入未就绪状态）。"""
        self._ready = False
        self._pending = []

    def _emit(self, message: dict[str, Any]) -> None:
        js = build_dispatch_js(message)
        debug_log("MMDWindow", "Python→JS 派发", {"type": message.get("type")})
        self._send_js(js)
