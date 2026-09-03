from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from traffic_reviewer.domain import TimestampSource
from traffic_reviewer.video import VideoMetadata

FILENAME_TIMESTAMP_PATTERN = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{6})(?:_|$)")


@dataclass(frozen=True)
class VideoClockReading:
    start_at: datetime
    end_at: datetime
    source: TimestampSource
    raw_text: str
    confidence: float


def _raw_filename_timestamp(path: Path | str) -> datetime:
    filename = Path(path).name
    match = FILENAME_TIMESTAMP_PATTERN.match(Path(filename).stem)
    if match is None:
        raise ValueError(
            "Filename must begin with YYYYMMDD_HHMMSS, for example 20260710_100053_tp00002.mp4"
        )
    try:
        value = datetime.strptime(
            match.group("date") + match.group("time"),
            "%Y%m%d%H%M%S",
        )
    except ValueError as exc:
        raise ValueError(f"Filename contains an invalid date or time: {filename}") from exc
    return value


def parse_filename_timestamp(path: Path | str) -> datetime:
    """Read YYYYMMDD_HHMMSS and round seconds 58/59 to the next minute."""
    value = _raw_filename_timestamp(path)
    if value.second >= 58:
        value = value.replace(second=0) + timedelta(minutes=1)
    return value


def filename_rounding_offset_seconds(path: Path | str) -> float:
    """Return how far into the file the rounded recording time begins."""
    value = _raw_filename_timestamp(path)
    if value.second >= 58:
        return float(60 - value.second)
    return 0.0


def read_filename_clock(path: Path, metadata: VideoMetadata) -> VideoClockReading:
    start_at = parse_filename_timestamp(path)
    end_at = start_at + timedelta(seconds=max(0.0, metadata.duration_seconds))
    return VideoClockReading(
        start_at=start_at,
        end_at=end_at,
        source=TimestampSource.FILENAME,
        raw_text=f"filename={path.name}; parsed_start={start_at.isoformat()}",
        confidence=1.0,
    )
