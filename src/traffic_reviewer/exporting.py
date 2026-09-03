from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from traffic_reviewer.analytics import MODES, available_days, build_hourly_summary
from traffic_reviewer.database import ProjectRepository
from traffic_reviewer.domain import ReviewStatus


@dataclass(frozen=True)
class ExportBundle:
    events: Path
    hourly_counts: Path
    coverage: Path


def export_clean_csvs(repository: ProjectRepository, output_directory: Path) -> ExportBundle:
    output_directory.mkdir(parents=True, exist_ok=True)
    events_path = output_directory / "accepted_count_events.csv"
    hourly_path = output_directory / "accepted_hourly_counts.csv"
    coverage_path = output_directory / "hourly_coverage_qc.csv"
    rows = repository.list_events(ReviewStatus.ACCEPTED)
    videos = repository.list_videos()

    hourly: Counter[tuple[str, int, str, str, str, str]] = Counter()
    with events_path.open("w", newline="", encoding="utf-8-sig") as handle:
        columns = [
            "video_name",
            "camera",
            "event_time",
            "offset_seconds",
            "mode",
            "direction",
            "direction_code",
            "counting_line",
            "confidence",
            "track_id",
            "evidence_frame",
            "review_status",
            "timestamp_source",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            if not row["occurred_at"]:
                continue
            event_time = datetime.fromisoformat(row["occurred_at"])
            writer.writerow(
                {
                    "video_name": Path(row["video_path"]).name,
                    "camera": row["camera"],
                    "event_time": event_time.isoformat(),
                    "offset_seconds": round(row["offset_ms"] / 1000, 3),
                    "mode": row["mode"],
                    "direction": row["direction_label"],
                    "direction_code": row["direction"],
                    "counting_line": row["line_name"],
                    "confidence": round(row["confidence"], 4),
                    "track_id": row["track_id"],
                    "evidence_frame": row["evidence_path"] or "",
                    "review_status": row["review_status"],
                    "timestamp_source": row["timestamp_source"],
                }
            )
            key = (
                event_time.date().isoformat(),
                event_time.hour,
                row["camera"],
                row["line_name"],
                row["mode"],
                row["direction_label"],
            )
            hourly[key] += 1

    with hourly_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "hour", "camera", "counting_line", "mode", "direction", "count"])
        for key, count in sorted(hourly.items()):
            writer.writerow((*key, count))

    cameras = sorted({video.camera for video in videos})
    with coverage_path.open("w", newline="", encoding="utf-8-sig") as handle:
        columns = [
            "date",
            "hour",
            "camera",
            "recorded_minutes",
            "missing_minutes",
            "overlap_minutes",
            "coverage_status",
            *MODES,
            "total",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for day in available_days(videos):
            for camera in cameras:
                for summary in build_hourly_summary(videos, rows, day, camera):
                    record = {
                        "date": day.isoformat(),
                        "hour": summary.hour.hour,
                        "camera": camera,
                        "recorded_minutes": round(summary.recorded_seconds / 60, 2),
                        "missing_minutes": round(max(0, 3600 - summary.recorded_seconds) / 60, 2),
                        "overlap_minutes": round(summary.overlap_seconds / 60, 2),
                        "coverage_status": summary.coverage_status,
                        "total": summary.total,
                    }
                    record.update({mode: summary.counts[mode] for mode in MODES})
                    writer.writerow(record)
    return ExportBundle(events_path, hourly_path, coverage_path)
