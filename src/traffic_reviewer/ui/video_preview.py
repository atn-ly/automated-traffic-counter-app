from __future__ import annotations

import cv2
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from traffic_reviewer.domain import CountingLine, DetectionZone


class VideoPreview(QWidget):
    line_changed = Signal(object)
    detection_zone_changed = Signal(object)
    drawing_mode_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self._pixmap: QPixmap | None = None
        self._lines: list[CountingLine] = []
        self._active_line: CountingLine | None = None
        self._active_line_id: int | None = None
        self._active_line_name = "Line 1"
        self._active_direction_a_label = "Enter"
        self._active_direction_b_label = "Exit"
        self._draft_start: QPointF | None = None
        self._detection_zone: DetectionZone | None = None
        self._drawing_mode = "line"
        self._zone_draft_points: list[QPointF] = []
        self._cursor_position: QPointF | None = None
        self._frame_margin = 0
        self._frame_aspect_ratio = 16 / 9
        self._fit_height_to_frame = False
        self._fit_minimum_height = self.minimumHeight()

    def set_fit_height_to_frame(self, enabled: bool) -> None:
        """Size the widget's height from its width and the loaded frame ratio."""
        self._fit_height_to_frame = bool(enabled)
        if self._fit_height_to_frame:
            self._fit_minimum_height = self.minimumHeight()
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            self._apply_fitted_height(self.width())
        else:
            self.setMaximumHeight(16_777_215)
        self.updateGeometry()

    def hasHeightForWidth(self) -> bool:
        return self._fit_height_to_frame

    def heightForWidth(self, width: int) -> int:
        if not self._fit_height_to_frame:
            return super().heightForWidth(width)
        margin = self._frame_margin
        frame_width = max(1, int(width) - margin * 2)
        fitted_height = round(frame_width / max(self._frame_aspect_ratio, 0.01))
        return max(self._fit_minimum_height, fitted_height + margin * 2)

    def sizeHint(self) -> QSize:
        if not self._fit_height_to_frame:
            return super().sizeHint()
        width = max(self.minimumWidth(), 640)
        return QSize(width, self.heightForWidth(width))

    def set_frame_margin(self, pixels: int) -> None:
        self._frame_margin = max(0, int(pixels))
        self.updateGeometry()
        self.update()

    def set_bgr_frame(self, frame) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        image = QImage(rgb.data, width, height, channels * width, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(image.copy())
        self._frame_aspect_ratio = width / max(height, 1)
        self._apply_fitted_height(self.width())
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_fitted_height(event.size().width())

    def _apply_fitted_height(self, width: int) -> None:
        """Enforce the fitted height because nested splitter layouts ignore height-for-width."""
        if not self._fit_height_to_frame:
            return
        desired = self.heightForWidth(width)
        if self.minimumHeight() == desired and self.maximumHeight() == desired:
            return
        self.setFixedHeight(desired)

    def clear(self) -> None:
        self._pixmap = None
        self._lines = []
        self._active_line = None
        self._active_line_id = None
        self._active_line_name = "Line 1"
        self._active_direction_a_label = "Enter"
        self._active_direction_b_label = "Exit"
        self._draft_start = None
        self._detection_zone = None
        mode_changed = self._drawing_mode != "line"
        self._drawing_mode = "line"
        self._zone_draft_points = []
        self._cursor_position = None
        if mode_changed:
            self.drawing_mode_changed.emit(self._drawing_mode)
        self.update()

    def set_line(self, line: CountingLine | None) -> None:
        self._lines = [line] if line is not None else []
        self._active_line = line
        self._active_line_id = line.id if line is not None else None
        self._active_line_name = line.name if line is not None else "Line 1"
        self._active_direction_a_label = line.direction_a_label if line is not None else "Enter"
        self._active_direction_b_label = line.direction_b_label if line is not None else "Exit"
        self._draft_start = None
        self._zone_draft_points = []
        self.update()

    def set_lines(self, lines: tuple[CountingLine, ...], active_line_id: int | None) -> None:
        self._lines = list(lines)
        self._active_line_id = active_line_id
        self._active_line = next((line for line in self._lines if line.id == active_line_id), None)
        if self._active_line is not None:
            self._active_line_name = self._active_line.name
            self._active_direction_a_label = self._active_line.direction_a_label
            self._active_direction_b_label = self._active_line.direction_b_label
        self._draft_start = None
        self._zone_draft_points = []
        self.update()

    def start_new_line(self, name: str) -> None:
        self.set_drawing_mode("line")
        self._active_line_id = None
        self._active_line_name = name
        self._active_direction_a_label = "Enter"
        self._active_direction_b_label = "Exit"
        self._active_line = None
        self._draft_start = None
        self.update()

    def start_redraw_active_line(self) -> bool:
        if self._active_line_id is None:
            return False
        self.set_drawing_mode("line")
        self._active_line = None
        self._draft_start = None
        self.update()
        return True

    def set_detection_zone(self, zone: DetectionZone | None) -> None:
        self._detection_zone = zone
        self._zone_draft_points = []
        self.update()

    def detection_zone(self) -> DetectionZone | None:
        return self._detection_zone

    def start_distant_detection_zone(self) -> None:
        self.set_drawing_mode("zone")

    def drawing_mode(self) -> str:
        return self._drawing_mode

    def set_drawing_mode(self, mode: str) -> None:
        if mode not in {"line", "zone"}:
            raise ValueError(f"Unknown drawing mode: {mode}")
        if mode == self._drawing_mode:
            self.update()
            return
        self._drawing_mode = mode
        self._draft_start = None
        self._zone_draft_points = []
        self.drawing_mode_changed.emit(mode)
        self.update()

    def line(self) -> CountingLine | None:
        return self._active_line

    def direction_labels(self) -> tuple[str, str]:
        return self._active_direction_a_label, self._active_direction_b_label

    def swap_direction_labels(self) -> CountingLine | None:
        self._active_direction_a_label, self._active_direction_b_label = (
            self._active_direction_b_label,
            self._active_direction_a_label,
        )
        if self._active_line is not None:
            line = self._active_line
            self._active_line = CountingLine(
                line.x1,
                line.y1,
                line.x2,
                line.y2,
                line.id,
                line.name,
                self._active_direction_a_label,
                self._active_direction_b_label,
            )
        self.update()
        return self._active_line

    def _target_rect(self) -> QRectF:
        if self._pixmap is None:
            return QRectF()
        size = self._pixmap.size()
        margin = self._frame_margin
        size.scale(
            max(1, self.width() - margin * 2),
            max(1, self.height() - margin * 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        left = (self.width() - size.width()) / 2
        top = (self.height() - size.height()) / 2
        return QRectF(left, top, size.width(), size.height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111827"))
        target = self._target_rect()
        if self._pixmap is None:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Select a video to preview")
            return
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        if self._detection_zone is not None:
            zone = self._detection_zone
            zone_polygon = QPolygonF(
                [
                    QPointF(
                        target.left() + x * target.width(),
                        target.top() + y * target.height(),
                    )
                    for x, y in zone.points
                ]
            )
            painter.save()
            painter.setPen(QPen(QColor("#f59e0b"), 3, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(245, 158, 11, 38))
            painter.drawPolygon(zone_polygon)
            painter.setPen(QColor("#f59e0b"))
            painter.drawText(
                zone_polygon.boundingRect().adjusted(6, 4, -6, -4),
                "DISTANT DETECTION ZONE",
            )
            painter.restore()
        inactive_lines = [line for line in self._lines if line.id != self._active_line_id]
        for line in inactive_lines:
            self._paint_line(painter, target, line, QColor("#60a5fa"), 2)
        if self._active_line is not None:
            self._paint_line(painter, target, self._active_line, QColor("#2563eb"), 4)
        if self._draft_start is not None:
            start = QPointF(
                target.left() + self._draft_start.x() * target.width(),
                target.top() + self._draft_start.y() * target.height(),
            )
            painter.setPen(QPen(QColor("#2563eb"), 3))
            painter.setBrush(QColor("#2563eb"))
            painter.drawEllipse(start, 6, 6)
        if self._zone_draft_points:
            points = [
                QPointF(
                    target.left() + point.x() * target.width(),
                    target.top() + point.y() * target.height(),
                )
                for point in self._zone_draft_points
            ]
            painter.setPen(QPen(QColor("#f59e0b"), 3))
            painter.setBrush(QColor("#f59e0b"))
            for draft_point in points:
                painter.drawEllipse(draft_point, 6, 6)
            for start, end in zip(points, points[1:]):
                painter.drawLine(start, end)
        self._paint_cursor_mode_badge(painter, target)

    def _paint_cursor_mode_badge(self, painter: QPainter, target: QRectF) -> None:
        cursor = self._cursor_position
        if cursor is None or not target.contains(cursor):
            return
        if self._drawing_mode == "zone":
            label = f"Zone tool · point {len(self._zone_draft_points) + 1} of 4"
            color = QColor("#d97706")
        else:
            point_number = 2 if self._draft_start is not None else 1
            label = f"Line tool · point {point_number} of 2"
            color = QColor("#2563eb")
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label) + 34
        height = metrics.height() + 10
        left = cursor.x() + 16
        top = cursor.y() + 16
        if left + width > target.right() - 4:
            left = cursor.x() - width - 16
        if top + height > target.bottom() - 4:
            top = cursor.y() - height - 16
        badge = QRectF(left, top, width, height)
        painter.save()
        painter.setPen(QPen(color, 2))
        painter.setBrush(QColor(15, 23, 42, 225))
        painter.drawRoundedRect(badge, 9, 9)
        dot_center = QPointF(badge.left() + 13, badge.center().y())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(dot_center, 4, 4)
        text_rect = badge.adjusted(24, 0, -8, 0)
        painter.setPen(QColor("#f8fafc"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()

    @staticmethod
    def _paint_line(
        painter: QPainter,
        target: QRectF,
        line: CountingLine,
        color: QColor,
        width: int,
    ) -> None:
        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        start = QPointF(
            target.left() + line.x1 * target.width(),
            target.top() + line.y1 * target.height(),
        )
        end = QPointF(
            target.left() + line.x2 * target.width(),
            target.top() + line.y2 * target.height(),
        )
        painter.drawLine(start, end)
        painter.setBrush(color)
        painter.drawEllipse(start, 6, 6)
        painter.drawEllipse(end, 6, 6)
        painter.drawText(start + QPointF(8, -8), line.name)

        direction_a_center, direction_b_center = VideoPreview._side_label_centers(start, end)
        VideoPreview._paint_side_label(
            painter,
            target,
            direction_a_center,
            line.direction_a_label,
            VideoPreview._direction_label_color(line.direction_a_label),
        )
        VideoPreview._paint_side_label(
            painter,
            target,
            direction_b_center,
            line.direction_b_label,
            VideoPreview._direction_label_color(line.direction_b_label),
        )

    @staticmethod
    def _direction_label_color(label: str) -> QColor:
        normalized = label.strip().casefold()
        if normalized == "enter":
            return QColor("#16845b")
        if normalized == "exit":
            return QColor("#dc2626")
        return QColor("#475569")

    @staticmethod
    def _side_label_centers(
        start: QPointF, end: QPointF, offset: float = 34.0
    ) -> tuple[QPointF, QPointF]:
        """Return label centers on the positive (A) and negative (B) line sides."""
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length = max((dx * dx + dy * dy) ** 0.5, 1.0)
        midpoint = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
        normal_x = -dy / length
        normal_y = dx / length
        return (
            midpoint + QPointF(normal_x * offset, normal_y * offset),
            midpoint - QPointF(normal_x * offset, normal_y * offset),
        )

    @staticmethod
    def _paint_side_label(
        painter: QPainter,
        target: QRectF,
        center: QPointF,
        text: str,
        color: QColor,
    ) -> None:
        label = text.upper()
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(label)
        width = text_rect.width() + 18
        height = text_rect.height() + 8
        rect = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
        margin = 4.0
        if rect.left() < target.left() + margin:
            rect.moveLeft(target.left() + margin)
        if rect.right() > target.right() - margin:
            rect.moveRight(target.right() - margin)
        if rect.top() < target.top() + margin:
            rect.moveTop(target.top() + margin)
        if rect.bottom() > target.bottom() - margin:
            rect.moveBottom(target.bottom() - margin)

        painter.save()
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.setBrush(color)
        painter.drawRoundedRect(rect, 5, 5)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        target = self._target_rect()
        point = event.position()
        if self._pixmap is None or not target.contains(point):
            return
        self._cursor_position = point
        normalized = QPointF(
            (point.x() - target.left()) / target.width(),
            (point.y() - target.top()) / target.height(),
        )
        if self._drawing_mode == "zone":
            self._zone_draft_points.append(normalized)
            if len(self._zone_draft_points) == 4:
                zone = DetectionZone.from_points(
                    [(point.x(), point.y()) for point in self._zone_draft_points]
                )
                self._zone_draft_points = []
                if zone.is_valid():
                    self._detection_zone = zone
                    self.detection_zone_changed.emit(zone)
            self.update()
            return
        if self._draft_start is None:
            self._draft_start = normalized
            self._active_line = None
        else:
            line = CountingLine(
                self._draft_start.x(),
                self._draft_start.y(),
                normalized.x(),
                normalized.y(),
                self._active_line_id,
                self._active_line_name,
                self._active_direction_a_label,
                self._active_direction_b_label,
            )
            self._draft_start = None
            if line.is_valid():
                self._active_line = line
                self.line_changed.emit(line)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        target = self._target_rect()
        self._cursor_position = (
            point if self._pixmap is not None and target.contains(point) else None
        )
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._cursor_position = None
        self.update()
        super().leaveEvent(event)
