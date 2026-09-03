from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from traffic_reviewer.domain import TimestampSource, VideoRecord

MODES = ("Pedestrian", "Bicycle", "Car", "Truck", "Bus", "Motorcycle")


@dataclass
class HourlySummary:
    hour: datetime
    recorded_seconds: float = 0.0
    overlap_seconds: float = 0.0
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def coverage_status(self) -> str:
        if self.recorded_seconds >= 3599:
            return "Complete"
        if self.recorded_seconds > 0:
            return "Partial"
        return "Missing"


@dataclass
class CameraSummary:
    camera: str
    counts: Counter[str] = field(default_factory=Counter)
    direction_counts: Counter[str] = field(default_factory=Counter)
    recorded_seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def recorded_hours(self) -> float:
        return self.recorded_seconds / 3600

    def crossings_per_recorded_hour(self, mode: str | None = None) -> float:
        if self.recorded_hours <= 0:
            return 0.0
        count = self.total if mode is None else self.counts[mode]
        return count / self.recorded_hours


@dataclass
class DailySummary:
    day: date
    counts: Counter[str] = field(default_factory=Counter)
    direction_counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class RecordingGap:
    start: datetime
    end: datetime
    previous_recording: str | None
    next_recording: str | None

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def kind(self) -> str:
        if self.previous_recording is None and self.next_recording is None:
            return "No recordings"
        if self.previous_recording is None:
            return "Before first recording"
        if self.next_recording is None:
            return "After last recording"
        return "Between recordings"


@dataclass(frozen=True)
class CoverageSegment:
    start: datetime
    end: datetime
    is_recorded: bool

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    merged_seconds = 0.0
    current_start, current_end = sorted(intervals)[0]
    for start, end in sorted(intervals)[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    return merged_seconds + (current_end - current_start).total_seconds()


def build_hourly_summary(
    videos: list[VideoRecord], event_rows, day: date, camera: str
) -> list[HourlySummary]:
    day_start = datetime.combine(day, time.min)
    summaries = [HourlySummary(day_start + timedelta(hours=hour)) for hour in range(24)]
    intervals_by_hour: list[list[tuple[datetime, datetime]]] = [[] for _ in range(24)]
    raw_seconds = [0.0] * 24

    for video in videos:
        if video.camera != camera or video.recorded_at is None:
            continue
        if (
            video.timestamp_source == TimestampSource.BURNED_IN_OCR
            and video.timestamp_confidence < 1
        ):
            continue
        start = video.recorded_at
        end = video.recorded_end_at or start + timedelta(seconds=video.duration_seconds)
        for hour, summary in enumerate(summaries):
            hour_end = summary.hour + timedelta(hours=1)
            clipped_start = max(start, summary.hour)
            clipped_end = min(end, hour_end)
            if clipped_start < clipped_end:
                intervals_by_hour[hour].append((clipped_start, clipped_end))
                raw_seconds[hour] += (clipped_end - clipped_start).total_seconds()

    for hour, summary in enumerate(summaries):
        summary.recorded_seconds = _merge_intervals(intervals_by_hour[hour])
        summary.overlap_seconds = max(0.0, raw_seconds[hour] - summary.recorded_seconds)

    for row in event_rows:
        if row["camera"] != camera or not row["occurred_at"]:
            continue
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        if occurred_at.date() == day:
            summaries[occurred_at.hour].counts[row["mode"]] += 1
    return summaries


def build_camera_summary(videos: list[VideoRecord], event_rows, day: date) -> list[CameraSummary]:
    """Return accepted class counts for every camera with coverage on a date."""
    day_start = datetime.combine(day, time.min)
    day_end = day_start + timedelta(days=1)
    summaries: dict[str, CameraSummary] = {}
    intervals_by_camera: dict[str, list[tuple[datetime, datetime]]] = {}
    for video in videos:
        if video.recorded_at is None:
            continue
        if (
            video.timestamp_source == TimestampSource.BURNED_IN_OCR
            and video.timestamp_confidence < 1
        ):
            continue
        video_end = video.recorded_end_at or video.recorded_at + timedelta(
            seconds=video.duration_seconds
        )
        if video.recorded_at < day_end and video_end > day_start:
            summaries.setdefault(video.camera, CameraSummary(video.camera))
            intervals_by_camera.setdefault(video.camera, []).append(
                (max(video.recorded_at, day_start), min(video_end, day_end))
            )

    for camera, intervals in intervals_by_camera.items():
        summaries[camera].recorded_seconds = _merge_intervals(intervals)

    for row in event_rows:
        if not row["camera"] or not row["occurred_at"]:
            continue
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        if occurred_at.date() == day:
            summary = summaries.setdefault(row["camera"], CameraSummary(row["camera"]))
            summary.counts[row["mode"]] += 1
            try:
                direction_label = row["direction_label"]
            except (KeyError, IndexError):
                direction_label = None
            if direction_label in {"Enter", "Exit"}:
                summary.direction_counts[direction_label] += 1

    def natural_camera_key(summary: CameraSummary):
        return tuple(
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", summary.camera)
        )

    return sorted(summaries.values(), key=natural_camera_key)


def build_daily_summary(videos: list[VideoRecord], event_rows) -> list[DailySummary]:
    """Return accepted class and direction counts for every available date."""
    days = set(available_days(videos))
    summaries: dict[date, DailySummary] = {
        recording_day: DailySummary(recording_day) for recording_day in days
    }
    for row in event_rows:
        if not row["occurred_at"]:
            continue
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        summary = summaries.setdefault(occurred_at.date(), DailySummary(occurred_at.date()))
        summary.counts[row["mode"]] += 1
        try:
            direction_label = row["direction_label"]
        except (KeyError, IndexError):
            direction_label = None
        if direction_label in {"Enter", "Exit"}:
            summary.direction_counts[direction_label] += 1
    return [summaries[recording_day] for recording_day in sorted(summaries)]


def available_days(videos: list[VideoRecord]) -> list[date]:
    days: set[date] = set()
    for video in videos:
        if video.recording_day is not None:
            days.add(video.recording_day)
        if video.recorded_end_at:
            days.add(video.recorded_end_at.date())
    return sorted(days)


def find_recording_gaps(
    videos: list[VideoRecord],
    day: date,
    camera: str,
    start_hour: int = 0,
    end_hour: int = 24,
) -> list[RecordingGap]:
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("Expected hours must satisfy 0 <= start < end <= 24")
    day_start = datetime.combine(day, time.min)
    window_start = day_start + timedelta(hours=start_hour)
    window_end = day_start + timedelta(hours=end_hour)
    intervals: list[tuple[datetime, datetime, str]] = []
    for video in videos:
        if video.camera != camera or video.recorded_at is None:
            continue
        if (
            video.timestamp_source == TimestampSource.BURNED_IN_OCR
            and video.timestamp_confidence < 1
        ):
            continue
        video_end = video.recorded_end_at or video.recorded_at + timedelta(
            seconds=video.duration_seconds
        )
        start = max(window_start, video.recorded_at)
        end = min(window_end, video_end)
        if start < end:
            intervals.append((start, end, video.path.name))
    intervals.sort(key=lambda item: (item[0], item[1]))

    gaps: list[RecordingGap] = []
    cursor = window_start
    previous_recording = None
    for start, end, recording in intervals:
        if start > cursor:
            gaps.append(RecordingGap(cursor, start, previous_recording, recording))
        if end > cursor:
            cursor = end
            previous_recording = recording
        if cursor >= window_end:
            break
    if cursor < window_end:
        gaps.append(RecordingGap(cursor, window_end, previous_recording, None))
    return gaps


def build_coverage_segments(
    day: date,
    start_hour: int,
    end_hour: int,
    gaps: list[RecordingGap],
) -> list[CoverageSegment]:
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("Expected hours must satisfy 0 <= start < end <= 24")
    day_start = datetime.combine(day, time.min)
    window_start = day_start + timedelta(hours=start_hour)
    window_end = day_start + timedelta(hours=end_hour)
    cursor = window_start
    segments: list[CoverageSegment] = []
    for gap in sorted(gaps, key=lambda item: item.start):
        gap_start = max(window_start, gap.start)
        gap_end = min(window_end, gap.end)
        if gap_start >= gap_end:
            continue
        if cursor < gap_start:
            segments.append(CoverageSegment(cursor, gap_start, True))
        segments.append(CoverageSegment(gap_start, gap_end, False))
        cursor = max(cursor, gap_end)
    if cursor < window_end:
        segments.append(CoverageSegment(cursor, window_end, True))
    return segments
