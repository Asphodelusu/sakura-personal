"""麦克风按钮：主题样式表画背景，自绘图标与电平填充。

- 背景/悬停走 #voiceButton QSS，随主题色变化
- 麦克风轮廓用固定设计坐标，按控件尺寸等比缩放
- 绿色填充通过 clipPath 限制在麦克风主体内
- EMA 平滑，快升慢降
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton

# 路径设计基准（绘制时按实际宽高等比缩放）
_DESIGN = 36.0
_BODY_RECT = QRectF(12, 4, 12, 17)
_BODY_RADIUS = 6.0


class MicLevelButton(QPushButton):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._raw_level: float = 0.0
        self._smooth_level: float = 0.0
        self._active: bool = False
        self._processing: bool = False
        self._pulse_phase: float = 0.0

        self.setFixedSize(38, 38)
        self.setText("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("语音输入（再点一次结束）；快捷键 Alt+T")
        self.setAccessibleName("语音输入")

        # 浅色底上用近白描边，保证在主题色按钮上可读
        self._idle_pen = QPen(QColor(255, 255, 255, 235), 1.7)
        self._idle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._idle_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self._active_pen = QPen(QColor(255, 90, 90), 1.7)
        self._active_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._active_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self._processing_pen = QPen(QColor(255, 190, 70), 1.7)
        self._processing_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._processing_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self._fill_color = QColor(76, 210, 100)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.setInterval(50)

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

    # ── 路径（设计坐标，基准 36×36） ───────────────────────────

    @staticmethod
    def _body_path() -> QPainterPath:
        """麦克风主体路径（用于裁剪填充，与轮廓主体一致）。"""
        p = QPainterPath()
        p.addRoundedRect(_BODY_RECT, _BODY_RADIUS, _BODY_RADIUS)
        return p

    @staticmethod
    def _outline_path() -> QPainterPath:
        """完整麦克风轮廓：胶囊 + U 形支架 + 竖杆 + 底座（各为独立子路径）。"""
        p = QPainterPath()
        p.addRoundedRect(_BODY_RECT, _BODY_RADIUS, _BODY_RADIUS)
        # U 形支架：结束后勿 lineTo 到中心，否则会从右臂拉出斜线
        p.moveTo(9, 19)
        p.arcTo(QRectF(9, 14, 18, 12), 180, 180)
        # 竖杆
        p.moveTo(18, 26)
        p.lineTo(18, 30)
        # 底座
        p.moveTo(12, 30)
        p.lineTo(24, 30)
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

    def _prepare_design_painter(self, painter: QPainter) -> None:
        w, h = self.width(), self.height()
        scale = min(w, h) / _DESIGN
        painter.translate((w - _DESIGN * scale) * 0.5, (h - _DESIGN * scale) * 0.5)
        painter.scale(scale, scale)

    def paintEvent(self, event) -> None:
        # 背景/悬停/禁用态交给主题 QSS（#voiceButton）
        super().paintEvent(event)

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._prepare_design_painter(p)

        body = self._body_path()
        outline = self._outline_path()

        if self._processing:
            p.setPen(self._processing_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(outline)
            alpha = int(80 + 60 * abs((self._pulse_phase % 6.283) / 3.1415 - 1))
            p.save()
            p.setClipPath(body)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 180, 60, alpha))
            p.drawRoundedRect(_BODY_RECT, _BODY_RADIUS, _BODY_RADIUS)
            p.restore()

        elif self._active:
            level = self._smooth()
            p.setPen(self._active_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)

            if level > 0.015:
                body_h = _BODY_RECT.height()
                fill_h = body_h * level
                fill_rect = QRectF(
                    _BODY_RECT.x(),
                    _BODY_RECT.y() + body_h - fill_h,
                    _BODY_RECT.width(),
                    fill_h,
                )
                p.save()
                p.setClipPath(body)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._fill_color)
                p.drawRect(fill_rect)
                p.restore()

            p.drawPath(outline)

        else:
            p.setPen(self._idle_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(outline)

        p.end()
