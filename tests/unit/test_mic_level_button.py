from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication

from app.ui.mic_level_button import MicLevelButton, _BODY_RADIUS, _BODY_RECT


def test_mic_outline_keeps_yoke_and_stem_separate() -> None:
    """U 形支架与竖杆必须是独立子路径，避免从右臂拉斜线到中心。"""
    outline = MicLevelButton._outline_path()
    move_to = QPainterPath.ElementType.MoveToElement
    line_to = QPainterPath.ElementType.LineToElement

    moves: list[tuple[float, float]] = []
    for i in range(outline.elementCount()):
        el = outline.elementAt(i)
        if el.type == move_to:
            moves.append((float(el.x), float(el.y)))

    # 至少：弧起点、竖杆、底座
    assert (9.0, 19.0) in moves
    assert (18.0, 26.0) in moves
    assert (12.0, 30.0) in moves

    # 竖杆子路径：MoveTo(18,26) 后应是 LineTo(18,30)，而非从弧终点斜接
    for i in range(outline.elementCount() - 1):
        el = outline.elementAt(i)
        nxt = outline.elementAt(i + 1)
        if el.type == move_to and float(el.x) == 18.0 and float(el.y) == 26.0:
            assert nxt.type == line_to
            assert float(nxt.x) == 18.0 and float(nxt.y) == 30.0
            break
    else:
        raise AssertionError("未找到竖杆 MoveTo(18, 26)")


def test_mic_body_matches_fill_capsule() -> None:
    body = MicLevelButton._body_path().boundingRect()
    assert abs(body.x() - _BODY_RECT.x()) < 0.01
    assert abs(body.y() - _BODY_RECT.y()) < 0.01
    assert abs(body.width() - _BODY_RECT.width()) < 0.01
    assert abs(body.height() - _BODY_RECT.height()) < 0.01
    assert _BODY_RADIUS == 6.0


def test_mic_button_defaults() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    button = MicLevelButton()
    assert button.width() == 38
    assert button.height() == 38
    assert "Alt+T" in button.toolTip()
    button.set_active(True)
    button.set_mic_level(0.5)
    button.set_processing()
    button.set_idle()
    button.deleteLater()
