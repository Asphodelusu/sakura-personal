"""Sakura 角色工作室（Tauri）独立入口。

与桌宠内「打开角色工坊」、设置页 ``studio.launch`` 使用同一 ``sakura-studio``
二进制与 ``CharacterStudioService`` RPC 后端。

启动方式::

    python -m tools.studio_tauri.main
    start_studio.bat

构建前置::

    cd tools/studio-tauri/src-tauri && cargo build --release

或设置环境变量 ``SAKURA_TAURI_STUDIO_BIN`` 指向已构建的可执行文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt, QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from app.ui.tauri_studio import (
    TAURI_STUDIO_BIN_ENV,
    resolve_tauri_studio_binary,
    run_tauri_studio_host,
)


def _qt_message_handler(msg_type: QtMsgType, context: object, msg: str) -> None:
    if "setDarkBorderToWindow" in msg:
        return
    sys.stderr.write(f"{msg}\n")
    if msg_type == QtMsgType.QtFatalMsg:
        sys.exit(1)


def _force_light_palette(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#fff6fa"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#3d2b35"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#3d2b35"))
    app.setPalette(palette)


def _configure_windows_high_dpi() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    for attempt in (
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ):
        try:
            if attempt():
                break
        except Exception:  # noqa: BLE001
            continue


def main() -> int:
    _configure_windows_high_dpi()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough,
    )
    app = QApplication(sys.argv)
    qInstallMessageHandler(_qt_message_handler)
    _force_light_palette(app)

    if resolve_tauri_studio_binary(PROJECT_ROOT) is None:
        sys.stderr.write(
            "未找到角色工作室（sakura-studio）。\n"
            "请先构建 tools/studio-tauri，"
            f"或设置环境变量 {TAURI_STUDIO_BIN_ENV}。\n"
        )
        return 1

    try:
        result = run_tauri_studio_host(PROJECT_ROOT, parent=app)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    if result is False:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
