"""app/perception — 主动屏幕感知 & 桌面上下文观察。

基于 desktop-kanojo / OpenMeido 的 ProactiveObserver 模式，
为 Sakura 提供"看懂屏幕、判断时机、主动搭话"的能力。
"""

from app.perception.win32 import get_active_window_title, get_active_window_process_name, get_idle_seconds
from app.perception.privacy import PrivacyGuard
from app.perception.screen_capture import ScreenCapture, ScreenObservation
from app.perception.observer import ProactiveObserver, ProactiveConfig

__all__ = [
    "get_active_window_title",
    "get_active_window_process_name",
    "get_idle_seconds",
    "PrivacyGuard",
    "ScreenCapture",
    "ScreenObservation",
    "ProactiveObserver",
    "ProactiveConfig",
]
