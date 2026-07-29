"""桌宠主气泡文本视口：限高后内滚。

规范：
- 粉气泡是唯一底板；文本区全透明
- 短句垂直居中；长文顶对齐
- 开播一次出全文：溢出时先看前半；本段语音结束后再滚到后半
- 标签高度只跟文案走，禁止钉成视口高（否则会误撑气泡）
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QScrollArea, QSizePolicy, QWidget


def _clear_widget_fill(widget: QWidget) -> None:
    """去掉默认底板，让粉色气泡底透出来。

    不要设 WA_TranslucentBackground：气泡上挂了 QGraphicsOpacityEffect，
    子控件再声明半透明会在 Windows 上「挖洞」露出立绘。
    """
    widget.setAutoFillBackground(False)
    widget.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
    widget.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
    palette = widget.palette()
    transparent = QColor(0, 0, 0, 0)
    palette.setColor(QPalette.ColorRole.Window, transparent)
    palette.setColor(QPalette.ColorRole.Base, transparent)
    widget.setPalette(palette)


def build_speech_text_scroll(parent: QWidget, *, initial_text: str = "") -> tuple[QScrollArea, QLabel]:
    """创建透明滚动视口 + 可换行 speech QLabel。"""
    scroll = QScrollArea(parent)
    scroll.setObjectName("speechTextScroll")
    scroll.setWidgetResizable(False)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    scroll.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    scroll.setStyleSheet(
        "QScrollArea#speechTextScroll,"
        "QScrollArea#speechTextScroll QWidget {"
        " background-color: rgba(0, 0, 0, 0);"
        " border: none;"
        "}"
    )
    _clear_widget_fill(scroll)

    viewport = scroll.viewport()
    viewport.setStyleSheet("background-color: rgba(0, 0, 0, 0); border: none;")
    _clear_widget_fill(viewport)

    label = QLabel(initial_text, scroll)
    label.setObjectName("speechText")
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    scroll.setWidget(label)
    _clear_widget_fill(label)
    return scroll, label


def measure_speech_label_height(label: QLabel, viewport_width: int) -> int:
    """按当前视口宽度测算文本所需高度。"""
    width = max(1, int(viewport_width))
    label.setMinimumHeight(0)
    label.setMaximumHeight(16777215)
    label.setFixedWidth(width)
    h = label.heightForWidth(width)
    if h <= 0:
        h = label.sizeHint().height()
    return max(1, int(h))


def layout_speech_label_in_scroll(scroll: QScrollArea, label: QLabel) -> tuple[int, int]:
    """按视口宽度排版标签，返回 (content_h, viewport_h)。"""
    viewport = scroll.viewport()
    vw = max(1, viewport.width())
    vh = max(1, viewport.height())
    content_h = measure_speech_label_height(label, vw)
    label.setFixedSize(vw, content_h)
    return content_h, vh


def sync_speech_scroll(
    scroll: QScrollArea,
    label: QLabel,
    *,
    reveal_tail: bool = False,
) -> None:
    """排版并同步滚动位置。

    - 未溢出：垂直居中，停在顶部偏移 0
    - 溢出且说话中：顶对齐，先显示前半（value=0）
    - 溢出且本段语音结束：滚到后半（value=maximum）
    """
    content_h, vh = layout_speech_label_in_scroll(scroll, label)
    overflows = content_h > vh + 1
    if overflows:
        scroll.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    else:
        scroll.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

    bar = scroll.verticalScrollBar()
    if bar is None:
        return
    if reveal_tail and overflows:
        bar.setValue(bar.maximum())
    else:
        bar.setValue(0)


# 兼容旧名
def sync_speech_scroll_for_typing(
    scroll: QScrollArea,
    label: QLabel,
    *,
    bubble_at_max: bool = False,
    reveal_tail: bool = False,
) -> None:
    _ = bubble_at_max
    sync_speech_scroll(scroll, label, reveal_tail=reveal_tail)
