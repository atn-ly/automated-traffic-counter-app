from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class VideoStatus(StrEnum):
    READY = "ready"
    PROCESSING = "processing"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TimestampSource(StrEnum):
    FILENAME = "filename"
    BURNED_IN_OCR = "burned_in_ocr"
    MANUAL_OVERLAY = "manual_overlay"
    MISSING = "missing"


@dataclass(frozen=True)
class CountingLine:
    """A line stored as normalized coordinates so resolution changes are manageable."""

    x1: float
    y1: float
    x2: float
    y2: float
    id: int | None = None
    name: str = "Line 1"
    direction_a_label: str = "Enter"
    direction_b_label: str = "Exit"

    def is_valid(self) -> bool:
        values = (self.x1, self.y1, self.x2, self.y2)
        return all(0.0 <= value <= 1.0 for value in values) and (
            abs(self.x2 - self.x1) + abs(self.y2 - self.y1) > 0.01
        )


@dataclass(frozen=True)
class DetectionZone:
    """An optional normalized four-corner crop for distant-object detection.

    Projects created with the earlier two-corner tool have only the first four values.
    Those values remain a top-left/bottom-right rectangle for backward compatibility.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    x3: float | None = None
    y3: float | None = None
    x4: float | None = None
    y4: float | None = None

    @classmethod
    def from_points(cls, points: list[tuple[float, float]]) -> DetectionZone:
        if len(points) != 4:
            raise ValueError("A distant detection zone requires four corners")
        center_x = sum(point[0] for point in points) / 4
        center_y = sum(point[1] for point in points) / 4
        ordered = sorted(
            ((float(x), float(y)) for x, y in points),
            key=lambda point: math.atan2(
                point[1] - center_y,
                point[0] - center_x,
            ),
        )
        start = min(range(4), key=lambda index: sum(ordered[index]))
        ordered = ordered[start:] + ordered[:start]
        return cls(
            ordered[0][0],
            ordered[0][1],
            ordered[1][0],
            ordered[1][1],
            ordered[2][0],
            ordered[2][1],
            ordered[3][0],
            ordered[3][1],
        )

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        if None in (self.x3, self.y3, self.x4, self.y4):
            return (
                (self.x1, self.y1),
                (self.x2, self.y1),
                (self.x2, self.y2),
                (self.x1, self.y2),
            )
        return (
            (self.x1, self.y1),
            (self.x2, self.y2),
            (float(self.x3), float(self.y3)),
            (float(self.x4), float(self.y4)),
        )

    def is_valid(self) -> bool:
        points = self.points
        if not all(0.0 <= value <= 1.0 for point in points for value in point):
            return False
        area = abs(
            sum(
                x1 * y2 - x2 * y1
                for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
            )
        ) / 2
        return area > 0.0002


@dataclass(frozen=True)
class VideoRecord:
    id: int
    path: Path
    camera: str
    recorded_at: datetime | None
    recorded_end_at: datetime | None
    timestamp_source: TimestampSource
    timestamp_raw: str | None
    timestamp_confidence: float
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int
    status: VideoStatus
    assigned_date: date | None = None
    processing_stride: int | None = None
    processing_model: str | None = None
    processing_image_size: int | None = None
    processing_modes: tuple[str, ...] = ()
    counting_lines: tuple[CountingLine, ...] = ()
    distant_detection_zone: DetectionZone | None = None
    error_message: str | None = None

    @property
    def counting_line(self) -> CountingLine | None:
        """Compatibility helper for projects created before multiple lines."""
        return self.counting_lines[0] if self.counting_lines else None

    @property
    def recording_day(self):
        if self.assigned_date is not None:
            return self.assigned_date
        return self.recorded_at.date() if self.recorded_at else None

    def timestamp_at(self, offset_ms: int) -> datetime:
        if self.recorded_at is None:
            raise ValueError("This recording does not have a start timestamp")
        if self.recorded_end_at and self.duration_seconds > 0:
            ratio = min(max(offset_ms / 1000 / self.duration_seconds, 0.0), 1.0)
            clock_span = self.recorded_end_at - self.recorded_at
            return self.recorded_at + clock_span * ratio
        return self.recorded_at + timedelta(milliseconds=offset_ms)


@dataclass(frozen=True)
class Detection:
    track_id: int
    mode: str
    confidence: float
    center_x: float
    center_y: float
    box_x1: float = 0.0
    box_y1: float = 0.0
    box_x2: float = 0.0
    box_y2: float = 0.0

    @property
    def tracking_point(self) -> tuple[float, float]:
        """Return the bottom-centre ground point used for line crossings."""
        if self.box_x2 > self.box_x1 and self.box_y2 > self.box_y1:
            return (
                (self.box_x1 + self.box_x2) / 2,
                self.box_y2,
            )
        return (self.center_x, self.center_y)

    @property
    def counting_points(self) -> tuple[tuple[float, float], ...]:
        """Return a compact lower-body zone for occlusion-tolerant crossings.

        The bottom-centre remains the primary ground reference shown during review. The
        surrounding points recover a crossing when a crowded or clipped track starts
        after its feet have already passed the line.
        """
        if self.box_x2 <= self.box_x1 or self.box_y2 <= self.box_y1:
            return (self.tracking_point,)
        width = self.box_x2 - self.box_x1
        height = self.box_y2 - self.box_y1
        return (
            self.tracking_point,
            (self.box_x1 + 0.25 * width, self.box_y1 + 0.75 * height),
            (self.box_x1 + 0.25 * width, self.box_y2),
            (self.box_x1 + 0.50 * width, self.box_y1 + 0.75 * height),
            (self.box_x1 + 0.75 * width, self.box_y1 + 0.75 * height),
            (self.box_x1 + 0.75 * width, self.box_y2),
        )


@dataclass(frozen=True)
class CountEvent:
    video_id: int
    offset_ms: int
    occurred_at: datetime
    track_id: int
    mode: str
    direction: str
    confidence: float
    evidence_path: str | None = None
    status: ReviewStatus = ReviewStatus.PENDING
    id: int | None = None
    line_id: int | None = None
    line_name: str = "Line 1"
    direction_label: str | None = None
