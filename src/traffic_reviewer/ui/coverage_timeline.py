from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from traffic_reviewer.analytics import CoverageSegment


def _duration_text(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class CoverageTimeline(QWidget):
    """Paint a proportional recorded-versus-gap timeline with hover details."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window_start: datetime | None = None
        self._window_end: datetime | None = None
        self._segments: list[CoverageSegment] = []
        self._hit_regions: list[tuple[QRectF, CoverageSegment]] = []
        self.setMinimumHeight(160)
        self.setMouseTracking(True)
        self.setAccessibleName("Recording coverage timeline")

    def set_timeline(
        self,
        window_start: datetime,
        window_end: datetime,
        segments: list[CoverageSegment],
    ) -> None:
        self._window_start = window_start
        self._window_end = window_end
        self._segments = segments
        self.update()

    def clear(self) -> None:
        self._window_start = None
        self._window_end = None
        self._segments = []
        self._hit_regions = []
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if self._window_start is None or self._window_end is None:
            painter.setPen(QColor("#64748b"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Choose a date and camera to display recording coverage",
            )
            return

        total_seconds = (self._window_end - self._window_start).total_seconds()
        if total_seconds <= 0:
            return
        left = 34.0
        right = max(left + 1, self.width() - 24.0)
        width = right - left
        bar = QRectF(left, 38.0, width, 52.0)
        painter.setPen(QPen(QColor("#b8c4d1"), 1))
        painter.setBrush(QColor("#eef2f6"))
        painter.drawRoundedRect(bar, 6, 6)

        self._hit_regions = []
        for segment in self._segments:
            start_ratio = (segment.start - self._window_start).total_seconds() / total_seconds
            end_ratio = (segment.end - self._window_start).total_seconds() / total_seconds
            segment_rect = QRectF(
                left + width * start_ratio,
                bar.top(),
                max(1.0, width * (end_ratio - start_ratio)),
                bar.height(),
            )
            color = QColor("#4caf78") if segment.is_recorded else QColor("#e46464")
            painter.fillRect(segment_rect, color)
            self._hit_regions.append((segment_rect, segment))
            if segment_rect.width() >= 72:
                painter.setPen(QColor("#ffffff"))
                painter.drawText(
                    segment_rect,
                    Qt.AlignmentFlag.AlignCenter,
                    "Recorded" if segment.is_recorded else "Gap",
                )

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawRoundedRect(bar, 6, 6)
        total_hours = total_seconds / 3600
        tick_step = 1 if total_hours <= 12 else 2
        hour_count = round(total_hours)
        ticks = list(range(0, hour_count + 1, tick_step))
        if ticks[-1] != hour_count:
            ticks.append(hour_count)
        for tick in ticks:
            ratio = tick / total_hours if total_hours else 0
            x = left + width * ratio
            painter.setPen(QPen(QColor("#64748b"), 1))
            painter.drawLine(round(x), round(bar.bottom()), round(x), round(bar.bottom() + 6))
            tick_time = self._window_start + (self._window_end - self._window_start) * ratio
            label = tick_time.strftime("%H:%M")
            if tick == round(total_hours) and tick_time.date() > self._window_start.date():
                label = "24:00"
            painter.drawText(
                QRectF(x - 28, bar.bottom() + 8, 56, 22),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )
        legend_y = self.height() - 28
        painter.fillRect(QRectF(left, legend_y, 15, 15), QColor("#4caf78"))
        painter.setPen(QColor("#334155"))
        painter.drawText(round(left + 21), round(legend_y + 13), "Recorded")
        painter.fillRect(QRectF(left + 112, legend_y, 15, 15), QColor("#e46464"))
        painter.drawText(round(left + 133), round(legend_y + 13), "Gap")

    def mouseMoveEvent(self, event) -> None:
        for rect, segment in self._hit_regions:
            if rect.contains(event.position()):
                state = "Recorded" if segment.is_recorded else "Gap"
                text = (
                    f"{state}\n{segment.start:%Y-%m-%d %H:%M:%S} to "
                    f"{segment.end:%Y-%m-%d %H:%M:%S}\n"
                    f"Duration: {_duration_text(segment.duration_seconds)}"
                )
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QToolTip.hideText()

    def leaveEvent(self, event) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)
