from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

from traffic_reviewer.analytics import MODES
from traffic_reviewer.domain import (
    CountEvent,
    CountingLine,
    DetectionZone,
    ReviewStatus,
    TimestampSource,
    VideoRecord,
    VideoStatus,
)
from traffic_reviewer.timestamping import VideoClockReading, parse_filename_timestamp
from traffic_reviewer.video import VideoMetadata
from traffic_reviewer.weather import DailyWeather

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    camera TEXT NOT NULL,
    assigned_date TEXT,
    recorded_at TEXT,
    recorded_end_at TEXT,
    timestamp_source TEXT NOT NULL DEFAULT 'missing',
    timestamp_raw TEXT,
    timestamp_confidence REAL NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL,
    fps REAL NOT NULL,
    frame_count INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    status TEXT NOT NULL,
    processing_stride INTEGER,
    processing_model TEXT,
    processing_image_size INTEGER,
    processing_modes TEXT,
    line_x1 REAL,
    line_y1 REAL,
    line_x2 REAL,
    line_y2 REAL,
    detection_zone_x1 REAL,
    detection_zone_y1 REAL,
    detection_zone_x2 REAL,
    detection_zone_y2 REAL,
    detection_zone_x3 REAL,
    detection_zone_y3 REAL,
    detection_zone_x4 REAL,
    detection_zone_y4 REAL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS counting_lines (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    x2 REAL NOT NULL,
    y2 REAL NOT NULL,
    direction_a_label TEXT NOT NULL DEFAULT 'Enter',
    direction_b_label TEXT NOT NULL DEFAULT 'Exit',
    UNIQUE(video_id, name),
    UNIQUE(video_id, position)
);

CREATE TABLE IF NOT EXISTS count_events (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    offset_ms INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    track_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    direction_label TEXT NOT NULL,
    confidence REAL NOT NULL,
    line_id INTEGER REFERENCES counting_lines(id) ON DELETE SET NULL,
    line_name TEXT NOT NULL DEFAULT 'Line 1',
    evidence_path TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, line_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_events_review ON count_events(review_status);
CREATE INDEX IF NOT EXISTS idx_events_video ON count_events(video_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_weather (
    day TEXT PRIMARY KEY,
    high_c REAL NOT NULL,
    low_c REAL NOT NULL,
    weather_code INTEGER NOT NULL,
    condition TEXT NOT NULL,
    precipitation_mm REAL NOT NULL DEFAULT 0,
    location_name TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class ProjectRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        video_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)").fetchall()
        }
        additions = {
            "assigned_date": "TEXT",
            "recorded_end_at": "TEXT",
            "timestamp_source": "TEXT NOT NULL DEFAULT 'missing'",
            "timestamp_raw": "TEXT",
            "timestamp_confidence": "REAL NOT NULL DEFAULT 0",
            "processing_stride": "INTEGER",
            "processing_model": "TEXT",
            "processing_image_size": "INTEGER",
            "processing_modes": "TEXT",
            "detection_zone_x1": "REAL",
            "detection_zone_y1": "REAL",
            "detection_zone_x2": "REAL",
            "detection_zone_y2": "REAL",
            "detection_zone_x3": "REAL",
            "detection_zone_y3": "REAL",
            "detection_zone_x4": "REAL",
            "detection_zone_y4": "REAL",
        }
        for name, definition in additions.items():
            if name not in video_columns:
                connection.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")

        line_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(counting_lines)").fetchall()
        }
        for name, definition in {
            "direction_a_label": "TEXT NOT NULL DEFAULT 'Enter'",
            "direction_b_label": "TEXT NOT NULL DEFAULT 'Exit'",
        }.items():
            if name not in line_columns:
                connection.execute(f"ALTER TABLE counting_lines ADD COLUMN {name} {definition}")

        connection.execute(
            """
            INSERT OR IGNORE INTO counting_lines (video_id, name, position, x1, y1, x2, y2)
            SELECT id, 'Line 1', 1, line_x1, line_y1, line_x2, line_y2
            FROM videos
            WHERE line_x1 IS NOT NULL
            """
        )

        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(count_events)").fetchall()
        }
        if "occurred_at" not in event_columns:
            connection.execute("ALTER TABLE count_events ADD COLUMN occurred_at TEXT")
            connection.execute(
                """
                UPDATE count_events
                SET occurred_at = datetime(
                    (SELECT recorded_at FROM videos WHERE videos.id = count_events.video_id),
                    printf('+%f seconds', offset_ms / 1000.0)
                )
                """
            )
        if "evidence_path" not in event_columns:
            connection.execute("ALTER TABLE count_events ADD COLUMN evidence_path TEXT")
        if "line_id" not in event_columns:
            connection.execute(
                """
                CREATE TABLE count_events_new (
                    id INTEGER PRIMARY KEY,
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    offset_ms INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    track_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    direction_label TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    line_id INTEGER REFERENCES counting_lines(id) ON DELETE SET NULL,
                    line_name TEXT NOT NULL DEFAULT 'Line 1',
                    evidence_path TEXT,
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(video_id, line_id, track_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO count_events_new (
                    id, video_id, offset_ms, occurred_at, track_id, mode, direction,
                    direction_label, confidence, line_id, line_name, evidence_path,
                    review_status, created_at
                )
                SELECT e.id, e.video_id, e.offset_ms, e.occurred_at, e.track_id, e.mode,
                       e.direction,
                       CASE e.direction WHEN 'A' THEN 'Enter' ELSE 'Exit' END,
                       e.confidence,
                       (
                           SELECT l.id FROM counting_lines l
                           WHERE l.video_id = e.video_id
                           ORDER BY l.position, l.id LIMIT 1
                       ),
                       COALESCE(
                           (
                               SELECT l.name FROM counting_lines l
                               WHERE l.video_id = e.video_id
                               ORDER BY l.position, l.id LIMIT 1
                           ),
                           'Line 1'
                       ),
                       e.evidence_path, e.review_status, e.created_at
                FROM count_events e
                """
            )
            connection.execute("DROP TABLE count_events")
            connection.execute("ALTER TABLE count_events_new RENAME TO count_events")
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(count_events)").fetchall()
        }
        if "direction_label" not in event_columns:
            connection.execute(
                "ALTER TABLE count_events ADD COLUMN direction_label TEXT NOT NULL DEFAULT 'Enter'"
            )
            connection.execute(
                """
                UPDATE count_events
                SET direction_label = CASE direction WHEN 'A' THEN 'Enter' ELSE 'Exit' END
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_time ON count_events(occurred_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_review ON count_events(review_status)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_video ON count_events(video_id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_lines_video ON counting_lines(video_id, position)"
        )

        semantics_key = "visible_frame_timestamps_v1"
        semantics = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (semantics_key,)
        ).fetchone()
        if semantics is None:
            legacy_rows = connection.execute(
                """
                SELECT id, recorded_at, recorded_end_at, duration_seconds, timestamp_raw
                FROM videos
                WHERE timestamp_source = ? AND recorded_at IS NOT NULL
                """,
                (TimestampSource.BURNED_IN_OCR,),
            ).fetchall()
            for row in legacy_rows:
                match = re.search(r" at \+([0-9.]+)s; end=", row["timestamp_raw"] or "")
                if match is None:
                    continue
                offset_seconds = max(0, round(float(match.group(1))))
                if offset_seconds:
                    visible_start = datetime.fromisoformat(row["recorded_at"]) + timedelta(
                        seconds=offset_seconds
                    )
                    connection.execute(
                        "UPDATE videos SET recorded_at = ? WHERE id = ?",
                        (visible_start.isoformat(), row["id"]),
                    )
                    event_rows = connection.execute(
                        "SELECT id, occurred_at, offset_ms FROM count_events WHERE video_id = ?",
                        (row["id"],),
                    ).fetchall()
                    for event_row in event_rows:
                        if row["recorded_end_at"] and row["duration_seconds"] > 0:
                            visible_end = datetime.fromisoformat(row["recorded_end_at"])
                            ratio = min(
                                max(
                                    event_row["offset_ms"] / 1000 / row["duration_seconds"],
                                    0.0,
                                ),
                                1.0,
                            )
                            visible_event_time = (
                                visible_start + (visible_end - visible_start) * ratio
                            )
                        else:
                            visible_event_time = datetime.fromisoformat(
                                event_row["occurred_at"]
                            ) + timedelta(seconds=offset_seconds)
                        connection.execute(
                            "UPDATE count_events SET occurred_at = ? WHERE id = ?",
                            (visible_event_time.isoformat(), event_row["id"]),
                        )
            connection.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, 'enabled')",
                (semantics_key,),
            )

        filename_key = "filename_timestamps_v1"
        filename_semantics = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (filename_key,)
        ).fetchone()
        if filename_semantics is None:
            filename_rows = connection.execute(
                """
                SELECT id, path, duration_seconds
                FROM videos
                WHERE timestamp_source != ?
                """,
                (TimestampSource.MANUAL_OVERLAY,),
            ).fetchall()
            for row in filename_rows:
                try:
                    start_at = parse_filename_timestamp(Path(row["path"]))
                except ValueError:
                    continue
                end_at = start_at + timedelta(seconds=max(0.0, row["duration_seconds"]))
                connection.execute(
                    """
                    UPDATE videos
                    SET assigned_date = ?, recorded_at = ?, recorded_end_at = ?,
                        timestamp_source = ?, timestamp_raw = ?, timestamp_confidence = 1
                    WHERE id = ?
                    """,
                    (
                        start_at.date().isoformat(),
                        start_at.isoformat(),
                        end_at.isoformat(),
                        TimestampSource.FILENAME,
                        f"filename={Path(row['path']).name}; parsed_start={start_at.isoformat()}",
                        row["id"],
                    ),
                )
                event_rows = connection.execute(
                    "SELECT id, offset_ms FROM count_events WHERE video_id = ?",
                    (row["id"],),
                ).fetchall()
                for event_row in event_rows:
                    event_time = start_at + timedelta(milliseconds=event_row["offset_ms"])
                    connection.execute(
                        "UPDATE count_events SET occurred_at = ? WHERE id = ?",
                        (event_time.isoformat(), event_row["id"]),
                    )
            connection.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, 'enabled')",
                (filename_key,),
            )

    def add_video(
        self,
        path: Path,
        metadata: VideoMetadata,
        clock: VideoClockReading | None = None,
    ) -> int:
        camera = path.parent.name or "Unassigned"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO videos (
                    path, camera, recorded_at, recorded_end_at, timestamp_source,
                    timestamp_raw, timestamp_confidence, duration_seconds, fps,
                    frame_count, width, height, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    recorded_at = COALESCE(excluded.recorded_at, videos.recorded_at),
                    recorded_end_at = COALESCE(excluded.recorded_end_at, videos.recorded_end_at),
                    timestamp_source = CASE
                        WHEN excluded.recorded_at IS NULL THEN videos.timestamp_source
                        ELSE excluded.timestamp_source
                    END,
                    timestamp_raw = COALESCE(excluded.timestamp_raw, videos.timestamp_raw),
                    timestamp_confidence = MAX(
                        excluded.timestamp_confidence, videos.timestamp_confidence
                    ),
                    duration_seconds = excluded.duration_seconds,
                    fps = excluded.fps,
                    frame_count = excluded.frame_count,
                    width = excluded.width,
                    height = excluded.height
                RETURNING id
                """,
                (
                    str(path.resolve()),
                    camera,
                    clock.start_at.isoformat() if clock else None,
                    clock.end_at.isoformat() if clock else None,
                    clock.source if clock else TimestampSource.MISSING,
                    clock.raw_text if clock else None,
                    clock.confidence if clock else 0.0,
                    metadata.duration_seconds,
                    metadata.fps,
                    metadata.frame_count,
                    metadata.width,
                    metadata.height,
                    VideoStatus.READY,
                ),
            )
            return int(cursor.fetchone()["id"])

    def list_videos(self) -> list[VideoRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM videos ORDER BY recorded_at IS NULL, recorded_at, path"
            ).fetchall()
            line_rows = connection.execute(
                "SELECT * FROM counting_lines ORDER BY video_id, position, id"
            ).fetchall()
        lines_by_video: dict[int, list[CountingLine]] = {}
        for line_row in line_rows:
            lines_by_video.setdefault(int(line_row["video_id"]), []).append(
                self._line_from_row(line_row)
            )
        return [
            self._video_from_row(row, tuple(lines_by_video.get(int(row["id"]), ()))) for row in rows
        ]

    def get_video(self, video_id: int) -> VideoRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
            line_rows = connection.execute(
                "SELECT * FROM counting_lines WHERE video_id = ? ORDER BY position, id",
                (video_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"Video {video_id} does not exist")
        return self._video_from_row(row, tuple(self._line_from_row(item) for item in line_rows))

    def set_recording_time(
        self,
        video_id: int,
        start_at: datetime,
        source: TimestampSource = TimestampSource.MANUAL_OVERLAY,
        raw_text: str = "Manually edited recording time",
        visible_offset_seconds: float = 0.0,
    ) -> None:
        video = self.get_video(video_id)
        visible_duration = max(0.0, video.duration_seconds - visible_offset_seconds)
        end_at = start_at + timedelta(seconds=visible_duration)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE videos
                SET assigned_date = ?, recorded_at = ?, recorded_end_at = ?, timestamp_source = ?,
                    timestamp_raw = ?, timestamp_confidence = 1
                WHERE id = ?
                """,
                (
                    start_at.date().isoformat(),
                    start_at.isoformat(),
                    end_at.isoformat(),
                    source,
                    raw_text,
                    video_id,
                ),
            )

    def set_camera(self, video_id: int, camera: str) -> None:
        self.set_cameras([video_id], camera)

    def set_cameras(self, video_ids: Iterable[int], camera: str) -> None:
        video_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not video_ids:
            raise ValueError("Select at least one recording")
        camera = camera.strip()
        if not camera:
            raise ValueError("Camera name cannot be empty")
        with self._connect() as connection:
            connection.executemany(
                "UPDATE videos SET camera = ? WHERE id = ?",
                [(camera, video_id) for video_id in video_ids],
            )

    def set_recording_dates(self, video_ids: Iterable[int], recording_day: date) -> None:
        video_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not video_ids:
            raise ValueError("Select at least one recording")
        placeholders = ", ".join("?" for _video_id in video_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, assigned_date, recorded_at, recorded_end_at
                FROM videos
                WHERE id IN ({placeholders})
                """,
                video_ids,
            ).fetchall()
            rows_by_id = {int(row["id"]): row for row in rows}
            missing_ids = [video_id for video_id in video_ids if video_id not in rows_by_id]
            if missing_ids:
                raise KeyError(f"Recordings do not exist: {missing_ids}")

            for video_id in video_ids:
                row = rows_by_id[video_id]
                if row["recorded_at"] is None:
                    connection.execute(
                        "UPDATE videos SET assigned_date = ? WHERE id = ?",
                        (recording_day.isoformat(), video_id),
                    )
                    continue
                old_start = datetime.fromisoformat(row["recorded_at"])
                new_start = old_start.replace(
                    year=recording_day.year,
                    month=recording_day.month,
                    day=recording_day.day,
                )
                shift = new_start - old_start
                old_end = (
                    datetime.fromisoformat(row["recorded_end_at"])
                    if row["recorded_end_at"]
                    else None
                )
                new_end = old_end + shift if old_end else None
                connection.execute(
                    """
                    UPDATE videos
                    SET assigned_date = ?, recorded_at = ?, recorded_end_at = ?
                    WHERE id = ?
                    """,
                    (
                        recording_day.isoformat(),
                        new_start.isoformat(),
                        new_end.isoformat() if new_end else None,
                        video_id,
                    ),
                )
                event_rows = connection.execute(
                    "SELECT id, occurred_at FROM count_events WHERE video_id = ?",
                    (video_id,),
                ).fetchall()
                connection.executemany(
                    "UPDATE count_events SET occurred_at = ? WHERE id = ?",
                    [
                        (
                            (datetime.fromisoformat(event["occurred_at"]) + shift).isoformat(),
                            event["id"],
                        )
                        for event in event_rows
                    ],
                )

    def get_selected_modes(self) -> list[str]:
        default = list(MODES)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'selected_modes'"
            ).fetchone()
        if row is None:
            return default
        try:
            values = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default
        supported = [str(value) for value in values if str(value) in MODES]
        return supported or default

    def set_selected_modes(self, modes: Iterable[str]) -> None:
        values = list(dict.fromkeys(str(mode) for mode in modes if str(mode) in MODES))
        if not values:
            raise ValueError("Select at least one object type")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES ('selected_modes', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (json.dumps(values),),
            )

    def get_ui_zoom(self) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'ui_zoom'"
            ).fetchone()
        if row is None:
            return 1.0
        try:
            return min(1.5, max(0.8, float(row["value"])))
        except (TypeError, ValueError):
            return 1.0

    def set_ui_zoom(self, factor: float) -> None:
        factor = min(1.5, max(0.8, float(factor)))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES ('ui_zoom', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"{factor:.1f}",),
            )

    def get_weather_location(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'weather_location'"
            ).fetchone()
        return row["value"] if row is not None else "Edmonton, Alberta"

    def get_weather_resolved_location(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'weather_resolved_location'"
            ).fetchone()
        return row["value"] if row is not None else ""

    def save_daily_weather(
        self,
        location_query: str,
        resolved_location: str,
        records: Iterable[DailyWeather],
    ) -> int:
        weather_records = tuple(records)
        clean_query = " ".join(location_query.split())
        with self._connect() as connection:
            current = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'weather_location'"
            ).fetchone()
            if current is not None and current["value"] != clean_query:
                connection.execute("DELETE FROM daily_weather")
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES ('weather_location', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (clean_query,),
            )
            connection.execute(
                """
                INSERT INTO app_settings(key, value)
                VALUES ('weather_resolved_location', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (resolved_location,),
            )
            connection.executemany(
                """
                INSERT INTO daily_weather (
                    day, high_c, low_c, weather_code, condition, precipitation_mm,
                    location_name, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    high_c = excluded.high_c,
                    low_c = excluded.low_c,
                    weather_code = excluded.weather_code,
                    condition = excluded.condition,
                    precipitation_mm = excluded.precipitation_mm,
                    location_name = excluded.location_name,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        record.day.isoformat(),
                        record.high_c,
                        record.low_c,
                        record.weather_code,
                        record.condition,
                        record.precipitation_mm,
                        record.location_name,
                        record.source,
                    )
                    for record in weather_records
                ],
            )
        return len(weather_records)

    def list_daily_weather(self) -> dict[date, DailyWeather]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT day, high_c, low_c, weather_code, condition, precipitation_mm,
                       location_name, source
                FROM daily_weather
                ORDER BY day
                """
            ).fetchall()
        return {
            date.fromisoformat(row["day"]): DailyWeather(
                date.fromisoformat(row["day"]),
                float(row["high_c"]),
                float(row["low_c"]),
                int(row["weather_code"]),
                row["condition"],
                float(row["precipitation_mm"]),
                row["location_name"],
                row["source"],
            )
            for row in rows
        }

    def save_counting_line(self, video_id: int, line: CountingLine) -> int:
        if not line.is_valid():
            raise ValueError("Counting line is invalid")
        name = line.name.strip()
        if not name:
            raise ValueError("Counting line name cannot be empty")
        if {line.direction_a_label, line.direction_b_label} != {"Enter", "Exit"}:
            raise ValueError("Counting line directions must map to Enter and Exit")
        with self._connect() as connection:
            if line.id is None:
                position = connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM counting_lines WHERE video_id = ?",
                    (video_id,),
                ).fetchone()[0]
                try:
                    cursor = connection.execute(
                        """
                        INSERT INTO counting_lines (
                            video_id, name, position, x1, y1, x2, y2,
                            direction_a_label, direction_b_label
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            video_id,
                            name,
                            position,
                            line.x1,
                            line.y1,
                            line.x2,
                            line.y2,
                            line.direction_a_label,
                            line.direction_b_label,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f'A line named "{name}" already exists') from exc
                return int(cursor.lastrowid)
            try:
                cursor = connection.execute(
                    """
                    UPDATE counting_lines
                    SET name = ?, x1 = ?, y1 = ?, x2 = ?, y2 = ?,
                        direction_a_label = ?, direction_b_label = ?
                    WHERE id = ? AND video_id = ?
                    """,
                    (
                        name,
                        line.x1,
                        line.y1,
                        line.x2,
                        line.y2,
                        line.direction_a_label,
                        line.direction_b_label,
                        line.id,
                        video_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f'A line named "{name}" already exists') from exc
            if cursor.rowcount != 1:
                raise KeyError(f"Counting line {line.id} does not belong to video {video_id}")
            return int(line.id)

    def set_counting_line(self, video_id: int, line: CountingLine) -> None:
        """Update the first line or create Line 1 for older callers."""
        current = self.get_video(video_id).counting_line
        if current is not None and line.id is None:
            line = CountingLine(
                line.x1,
                line.y1,
                line.x2,
                line.y2,
                current.id,
                current.name,
                current.direction_a_label,
                current.direction_b_label,
            )
        self.save_counting_line(video_id, line)

    def delete_counting_line(self, video_id: int, line_id: int) -> int:
        with self._connect() as connection:
            evidence_rows = connection.execute(
                "SELECT evidence_path FROM count_events WHERE video_id = ? AND line_id = ?",
                (video_id, line_id),
            ).fetchall()
            connection.execute(
                "DELETE FROM count_events WHERE video_id = ? AND line_id = ?",
                (video_id, line_id),
            )
            cursor = connection.execute(
                "DELETE FROM counting_lines WHERE id = ? AND video_id = ?",
                (line_id, video_id),
            )
        self._delete_evidence_files(row["evidence_path"] for row in evidence_rows)
        return cursor.rowcount

    def apply_counting_lines_to_day(self, video_id: int) -> int:
        video = self.get_video(video_id)
        if video.recording_day is None:
            raise ValueError("Verify the recording timestamp before applying daily lines")
        if not video.counting_lines:
            raise ValueError("Save at least one counting line first")
        evidence_paths: list[str | None] = []
        with self._connect() as connection:
            target_rows = connection.execute(
                """
                SELECT id
                FROM videos
                WHERE camera = ?
                  AND COALESCE(assigned_date, date(recorded_at)) = ?
                ORDER BY id
                """,
                (video.camera, video.recording_day.isoformat()),
            ).fetchall()
            target_ids = [int(row["id"]) for row in target_rows if int(row["id"]) != video_id]
            for target_id in target_ids:
                evidence_paths.extend(
                    row["evidence_path"]
                    for row in connection.execute(
                        "SELECT evidence_path FROM count_events WHERE video_id = ?", (target_id,)
                    ).fetchall()
                )
                connection.execute("DELETE FROM count_events WHERE video_id = ?", (target_id,))
                zone = video.distant_detection_zone
                connection.execute(
                    """
                    UPDATE videos
                    SET detection_zone_x1 = ?, detection_zone_y1 = ?,
                        detection_zone_x2 = ?, detection_zone_y2 = ?,
                        detection_zone_x3 = ?, detection_zone_y3 = ?,
                        detection_zone_x4 = ?, detection_zone_y4 = ?,
                        status = ?, error_message = NULL
                    WHERE id = ?
                    """,
                    (
                        zone.x1 if zone else None,
                        zone.y1 if zone else None,
                        zone.x2 if zone else None,
                        zone.y2 if zone else None,
                        zone.x3 if zone else None,
                        zone.y3 if zone else None,
                        zone.x4 if zone else None,
                        zone.y4 if zone else None,
                        VideoStatus.READY,
                        target_id,
                    ),
                )
                connection.execute("DELETE FROM counting_lines WHERE video_id = ?", (target_id,))
                connection.executemany(
                    """
                    INSERT INTO counting_lines (
                        video_id, name, position, x1, y1, x2, y2,
                        direction_a_label, direction_b_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            target_id,
                            line.name,
                            position,
                            line.x1,
                            line.y1,
                            line.x2,
                            line.y2,
                            line.direction_a_label,
                            line.direction_b_label,
                        )
                        for position, line in enumerate(video.counting_lines, start=1)
                    ],
                )
        self._delete_evidence_files(evidence_paths)
        return len(target_ids)

    def set_distant_detection_zone(
        self,
        video_id: int,
        zone: DetectionZone | None,
    ) -> None:
        self.set_distant_detection_zone_for_videos((video_id,), zone)

    def set_distant_detection_zone_for_videos(
        self,
        video_ids: Iterable[int],
        zone: DetectionZone | None,
    ) -> int:
        """Save one optional crop for source recordings and invalidate prior results."""
        target_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not target_ids:
            return 0
        if zone is not None and not zone.is_valid():
            raise ValueError("Distant detection zone is invalid")
        evidence_paths: list[str | None] = []
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in target_ids)
            existing_ids = {
                int(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM videos WHERE id IN ({placeholders})",
                    target_ids,
                ).fetchall()
            }
            if existing_ids != set(target_ids):
                raise KeyError("One or more recordings no longer exist")
            for target_id in target_ids:
                evidence_paths.extend(
                    row["evidence_path"]
                    for row in connection.execute(
                        "SELECT evidence_path FROM count_events WHERE video_id = ?",
                        (target_id,),
                    ).fetchall()
                )
                connection.execute("DELETE FROM count_events WHERE video_id = ?", (target_id,))
                connection.execute(
                    """
                    UPDATE videos
                    SET detection_zone_x1 = ?, detection_zone_y1 = ?,
                        detection_zone_x2 = ?, detection_zone_y2 = ?,
                        detection_zone_x3 = ?, detection_zone_y3 = ?,
                        detection_zone_x4 = ?, detection_zone_y4 = ?,
                        status = ?, error_message = NULL
                    WHERE id = ?
                    """,
                    (
                        zone.x1 if zone else None,
                        zone.y1 if zone else None,
                        zone.x2 if zone else None,
                        zone.y2 if zone else None,
                        zone.x3 if zone else None,
                        zone.y3 if zone else None,
                        zone.x4 if zone else None,
                        zone.y4 if zone else None,
                        VideoStatus.READY,
                        target_id,
                    ),
                )
        self._delete_evidence_files(evidence_paths)
        return len(target_ids)

    def replace_counting_lines_for_videos(
        self,
        video_ids: Iterable[int],
        lines: Iterable[CountingLine],
    ) -> int:
        """Replace one combined video's source line set and invalidate prior detections."""
        target_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        line_set = tuple(lines)
        if not target_ids:
            return 0
        names: set[str] = set()
        for line in line_set:
            if not line.is_valid():
                raise ValueError("Counting line is invalid")
            name = line.name.strip()
            if not name:
                raise ValueError("Counting line name cannot be empty")
            if name in names:
                raise ValueError(f'A line named "{name}" already exists')
            names.add(name)
            if {line.direction_a_label, line.direction_b_label} != {"Enter", "Exit"}:
                raise ValueError("Counting line directions must map to Enter and Exit")

        evidence_paths: list[str | None] = []
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in target_ids)
            existing_ids = {
                int(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM videos WHERE id IN ({placeholders})",
                    target_ids,
                ).fetchall()
            }
            if existing_ids != set(target_ids):
                raise KeyError("One or more combined-video source recordings no longer exist")
            for target_id in target_ids:
                evidence_paths.extend(
                    row["evidence_path"]
                    for row in connection.execute(
                        "SELECT evidence_path FROM count_events WHERE video_id = ?",
                        (target_id,),
                    ).fetchall()
                )
                connection.execute("DELETE FROM count_events WHERE video_id = ?", (target_id,))
                connection.execute("DELETE FROM counting_lines WHERE video_id = ?", (target_id,))
                connection.executemany(
                    """
                    INSERT INTO counting_lines (
                        video_id, name, position, x1, y1, x2, y2,
                        direction_a_label, direction_b_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            target_id,
                            line.name.strip(),
                            position,
                            line.x1,
                            line.y1,
                            line.x2,
                            line.y2,
                            line.direction_a_label,
                            line.direction_b_label,
                        )
                        for position, line in enumerate(line_set, start=1)
                    ],
                )
        self._delete_evidence_files(evidence_paths)
        return len(target_ids)

    def apply_counting_line_to_day(self, video_id: int, line: CountingLine) -> int:
        self.set_counting_line(video_id, line)
        return self.apply_counting_lines_to_day(video_id) + 1

    def set_video_status(
        self, video_id: int, status: VideoStatus, error_message: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE videos SET status = ?, error_message = ? WHERE id = ?",
                (status, error_message, video_id),
            )

    def set_processing_settings(
        self,
        video_id: int,
        stride: int,
        model_path: str,
        modes: Iterable[str],
        image_size: int = 960,
    ) -> None:
        stride = int(stride)
        image_size = int(image_size)
        model_path = model_path.strip()
        if stride < 1:
            raise ValueError("Frame stride must be at least 1")
        if image_size < 320:
            raise ValueError("Inference resolution must be at least 320 pixels")
        if not model_path:
            raise ValueError("Model path cannot be empty")
        selected_modes = tuple(dict.fromkeys(str(mode) for mode in modes if str(mode) in MODES))
        if not selected_modes:
            raise ValueError("Select at least one object type")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE videos
                SET processing_stride = ?, processing_model = ?, processing_image_size = ?,
                    processing_modes = ?
                WHERE id = ?
                """,
                (stride, model_path, image_size, json.dumps(selected_modes), video_id),
            )

    def clear_events(self, video_id: int) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT evidence_path FROM count_events WHERE video_id = ?", (video_id,)
            ).fetchall()
            connection.execute("DELETE FROM count_events WHERE video_id = ?", (video_id,))
        self._delete_evidence_files(row["evidence_path"] for row in rows)

    def add_events(self, events: Iterable[CountEvent]) -> int:
        rows = [
            (
                event.video_id,
                event.offset_ms,
                event.occurred_at.isoformat(),
                event.track_id,
                event.mode,
                event.direction,
                event.direction_label or ("Enter" if event.direction == "A" else "Exit"),
                event.confidence,
                event.line_id,
                event.line_name,
                event.evidence_path,
                event.status,
            )
            for event in events
        ]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO count_events (
                    video_id, offset_ms, occurred_at, track_id, mode,
                    direction, direction_label, confidence, line_id, line_name,
                    evidence_path, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_events(self, status: ReviewStatus | None = None) -> list[sqlite3.Row]:
        query = """
            SELECT e.*, v.path AS video_path, v.camera, v.recorded_at,
                   v.timestamp_source
            FROM count_events e
            JOIN videos v ON v.id = e.video_id
        """
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE e.review_status = ?"
            params = (status,)
        query += " ORDER BY e.occurred_at, e.id"
        with self._connect() as connection:
            return connection.execute(query, params).fetchall()

    def pending_event_ids_at_or_above(self, minimum_confidence: float) -> list[int]:
        threshold = float(minimum_confidence)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("Confidence threshold must be between 0 and 1")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM count_events
                WHERE review_status = ? AND confidence >= ?
                ORDER BY occurred_at, id
                """,
                (ReviewStatus.PENDING, threshold),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def set_review_status(self, event_ids: Iterable[int], status: ReviewStatus) -> None:
        ids = [int(event_id) for event_id in event_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE count_events SET review_status = ? WHERE id IN ({placeholders})",
                (status, *ids),
            )

    def delete_events(self, event_ids: Iterable[int]) -> int:
        ids = [int(event_id) for event_id in event_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT evidence_path FROM count_events WHERE id IN ({placeholders})", ids
            ).fetchall()
            cursor = connection.execute(
                f"DELETE FROM count_events WHERE id IN ({placeholders})", ids
            )
        self._delete_evidence_files(row["evidence_path"] for row in rows)
        return cursor.rowcount

    def delete_videos(self, video_ids: Iterable[int]) -> int:
        ids = [int(video_id) for video_id in video_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT evidence_path FROM count_events WHERE video_id IN ({placeholders})", ids
            ).fetchall()
            cursor = connection.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", ids)
        self._delete_evidence_files(row["evidence_path"] for row in rows)
        return cursor.rowcount

    @staticmethod
    def _delete_evidence_files(paths: Iterable[str | None]) -> None:
        for raw_path in paths:
            if not raw_path:
                continue
            try:
                Path(raw_path).unlink(missing_ok=True)
            except OSError:
                pass

    def accepted_summary(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT v.camera, e.mode, e.direction_label AS direction, COUNT(*) AS count
                FROM count_events e
                JOIN videos v ON v.id = e.video_id
                WHERE e.review_status = ?
                GROUP BY v.camera, e.mode, e.direction_label
                ORDER BY v.camera, e.mode, e.direction_label
                """,
                (ReviewStatus.ACCEPTED,),
            ).fetchall()

    @staticmethod
    def _line_from_row(row: sqlite3.Row) -> CountingLine:
        return CountingLine(
            row["x1"],
            row["y1"],
            row["x2"],
            row["y2"],
            int(row["id"]),
            row["name"],
            row["direction_a_label"],
            row["direction_b_label"],
        )

    @staticmethod
    def _video_from_row(row: sqlite3.Row, counting_lines: tuple[CountingLine, ...]) -> VideoRecord:
        return VideoRecord(
            id=row["id"],
            path=Path(row["path"]),
            camera=row["camera"],
            recorded_at=(
                datetime.fromisoformat(row["recorded_at"]) if row["recorded_at"] else None
            ),
            recorded_end_at=(
                datetime.fromisoformat(row["recorded_end_at"]) if row["recorded_end_at"] else None
            ),
            timestamp_source=TimestampSource(row["timestamp_source"]),
            timestamp_raw=row["timestamp_raw"],
            timestamp_confidence=row["timestamp_confidence"],
            duration_seconds=row["duration_seconds"],
            fps=row["fps"],
            frame_count=row["frame_count"],
            width=row["width"],
            height=row["height"],
            status=VideoStatus(row["status"]),
            assigned_date=(
                date.fromisoformat(row["assigned_date"]) if row["assigned_date"] else None
            ),
            processing_stride=row["processing_stride"],
            processing_model=row["processing_model"],
            processing_image_size=row["processing_image_size"],
            processing_modes=(
                tuple(json.loads(row["processing_modes"])) if row["processing_modes"] else ()
            ),
            counting_lines=counting_lines,
            distant_detection_zone=(
                DetectionZone(
                    row["detection_zone_x1"],
                    row["detection_zone_y1"],
                    row["detection_zone_x2"],
                    row["detection_zone_y2"],
                    row["detection_zone_x3"],
                    row["detection_zone_y3"],
                    row["detection_zone_x4"],
                    row["detection_zone_y4"],
                )
                if row["detection_zone_x1"] is not None
                else None
            ),
            error_message=row["error_message"],
        )
