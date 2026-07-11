"""麦克风按钮：全自绘，clipPath 精确裁剪绿色填充

回到之前效果好的版本，像素坐标，无 translate/scale 歧义。
- 手动绘制粉色圆角背景（匹配其他按钮）
- 麦克风图标用 QPainterPath 绘制
- 绿色填充通过 clipPath 限制在麦克风主体内
- EMA 平滑，快升慢降
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton


# 按钮色（匹配 theme.py）
BG = QColor(190, 120, 200, 232)
BG_HOVER = QColor(180, 100, 200, 242)
BORDER = QColor(255, 255, 255, 150)


class MicLevelButton(QPushButton):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._raw_level: float = 0.0
        self._smooth_level: float = 0.0
        self._active: bool = False
        self._processing: bool = False
        self._pulse_phase: float = 0.0
        self._hover: bool = False

        self.setFixedSize(36, 36)
        self.setText("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._idle_pen = QPen(QColor(210, 210, 210), 1.6)
        self._active_pen = QPen(QColor(255, 80, 80), 1.6)
        self._processing_pen = QPen(QColor(255, 180, 60), 1.6)
        self._fill_color = QColor(76, 210, 100)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.setInterval(50)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    # ── 公共接口 ──────────────────────────────────────────────

    def set_mic_level(self, level: float) -> None:
        self._raw_level = max(0.0, min(1.0, level))
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._processing = False
        self._raw_level = 0.0
        self._smooth_level = 0.0
        self._pulse_timer.stop()
        self.update()

    def set_processing(self) -> None:
        self._active = False
        self._processing = True
        self._pulse_phase = 0.0
        self._pulse_timer.start()
        self.update()

    def set_idle(self) -> None:
        self._active = False
        self._processing = False
        self._pulse_timer.stop()
        self.update()

    # ── 路径（像素坐标，基准 36×36） ───────────────────────────

    @staticmethod
    def _body_path() -> QPainterPath:
        """麦克风主体路径（用于裁剪填充）。"""
        p = QPainterPath()
        p.addRoundedRect(QRectF(12, 5, 12, 20), 4, 4)
        return p

    @staticmethod
    def _outline_path() -> QPainterPath:
        """完整麦克风轮廓。"""
        p = QPainterPath()
        # 主体圆角矩形
        p.addRoundedRect(QRectF(12, 5, 12, 18), 5, 5)
        # 底部弧线
        p.moveTo(9, 22)
        p.arcTo(QRectF(9, 17, 18, 10), 180, 180)
        p.lineTo(18, 26)
        # 底座
        p.moveTo(9, 27)
        p.lineTo(27, 27)
        p.moveTo(18, 27)
        p.lineTo(18, 31)
        return p

    # ── 内部 ──────────────────────────────────────────────────

    def _tick_pulse(self) -> None:
        if not self._processing:
            self._pulse_timer.stop()
            return
        self._pulse_phase += 0.12
        self.update()

    def _smooth(self) -> float:
        target = self._raw_level
        alpha = 0.35 if target > self._smooth_level else 0.12
        self._smooth_level += alpha * (target - self._smooth_level)
        return self._smooth_level

    def paintEvent(self, event) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── 1. 圆角粉色背景 ──
        bg = BG_HOVER if self._hover else BG
        p.setPen(QPen(BORDER, 1))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 18, 18)

        # ── 2. 麦克风图标 ──
        if self._processing:
            p.setPen(self._processing_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._outline_path())
            # 脉冲光晕
            alpha = int(80 + 60 * abs((self._pulse_phase % 6.283) / 3.1415 - 1))
            p.save()
            p.setClipPath(self._body_path())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 180, 60, alpha))
            p.drawRoundedRect(QRectF(12, 5, 12, 20), 4, 4)
            p.restore()

        elif self._active:
            level = self._smooth()
            p.setPen(self._active_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)

            if level > 0.015:
                # 从底部向上填充
                body_h = 20  # 与 _body_path 一致
                fill_h = body_h * level
                fill_rect = QRectF(12, 5 + body_h - fill_h, 12, fill_h)
                p.save()
                p.setClipPath(self._body_path())
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._fill_color)
                p.drawRoundedRect(fill_rect, 3, 3)
                p.restore()

            p.drawPath(self._outline_path())

        else:
            p.setPen(self._idle_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._outline_path())

        p.end()
