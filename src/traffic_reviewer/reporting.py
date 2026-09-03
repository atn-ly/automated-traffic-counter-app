from __future__ import annotations

from collections import Counter
from datetime import date
from html import escape
from math import ceil
from pathlib import Path

from traffic_reviewer.analytics import (
    MODES,
    build_camera_summary,
    build_daily_summary,
    build_hourly_summary,
)
from traffic_reviewer.database import ProjectRepository
from traffic_reviewer.domain import ReviewStatus
from traffic_reviewer.weather_icons import weather_icon_svg

MODE_COLORS = {
    "Pedestrian": "#2f9e72",
    "Bicycle": "#4c78a8",
    "Car": "#f2a65a",
    "Truck": "#9c6ade",
    "Bus": "#e45756",
    "Motorcycle": "#36a3a8",
}

CAMERA_COLORS = (
    "#2f9e72",
    "#4c78a8",
    "#e45756",
    "#9c6ade",
    "#f2a65a",
    "#36a3a8",
    "#d45087",
    "#6b8e23",
    "#8c564b",
    "#1f77b4",
    "#bcbd22",
    "#7f7f7f",
)

DIRECTION_COLORS = {
    "Enter": "#2f9e72",
    "Exit": "#e45756",
}


def _hourly_plot_svg(
    summaries,
    chart_id: str = "hourly-chart",
    chart_title: str = "Hourly traffic counts by class",
) -> str:
    width = 1120
    height = 440
    left = 64
    top = 24
    chart_width = 1030
    chart_height = 310
    bottom = top + chart_height
    totals = [summary.total for summary in summaries]
    maximum = max(totals, default=0)
    tick_size = max(1, ceil(maximum / 5))
    axis_maximum = tick_size * 5
    slot_width = chart_width / 24
    bar_width = slot_width * 0.72
    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="{chart_id}-title {chart_id}-description" '
            'xmlns="http://www.w3.org/2000/svg">'
        ),
        f'<title id="{chart_id}-title">{escape(chart_title)}</title>',
        (
            f'<desc id="{chart_id}-description">Stacked bars for each hour from midnight '
            "through 23:00. Colors identify pedestrian, bicycle, car, truck, bus, and "
            "motorcycle counts.</desc>"
        ),
        '<rect width="1120" height="440" rx="10" fill="#ffffff"/>',
    ]
    for tick_index in range(6):
        value = tick_index * tick_size
        y = bottom - chart_height * value / axis_maximum
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" y2="{y:.1f}" '
            'stroke="#dbe3ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#64748b">{value}</text>'
        )
    for hour, summary in enumerate(summaries):
        x = left + hour * slot_width + (slot_width - bar_width) / 2
        stack_y = float(bottom)
        for mode in MODES:
            count = summary.counts[mode]
            if not count:
                continue
            bar_height = chart_height * count / axis_maximum
            stack_y -= bar_height
            parts.append(
                f'<rect x="{x:.1f}" y="{stack_y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="{MODE_COLORS[mode]}">'
                f"<title>{escape(mode)} · {hour:02d}:00 · {count}</title></rect>"
            )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 19}" text-anchor="middle" '
            f'font-size="11" fill="#475569">{hour:02d}</text>'
        )
    parts.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" '
            'stroke="#475569" stroke-width="1.5"/>',
            (
                f'<text x="{left + chart_width / 2}" y="{bottom + 43}" text-anchor="middle" '
                'font-size="13" fill="#334155">Hour</text>'
            ),
        ]
    )
    if maximum == 0:
        parts.append(
            f'<text x="{left + chart_width / 2}" y="{top + chart_height / 2}" '
            'text-anchor="middle" font-size="16" fill="#64748b">'
            "No accepted detections for this date and camera</text>"
        )
    legend_y = 407
    for index, mode in enumerate(MODES):
        x = left + index * 168
        parts.append(
            f'<rect x="{x}" y="{legend_y - 12}" width="14" height="14" '
            f'rx="2" fill="{MODE_COLORS[mode]}"/>'
        )
        parts.append(
            f'<text x="{x + 20}" y="{legend_y}" font-size="12" fill="#334155">{escape(mode)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _daily_line_svg(summaries, metric: str) -> str:
    class_counts = metric == "class"
    series_names = MODES if class_counts else ("Enter", "Exit")
    colors = MODE_COLORS if class_counts else DIRECTION_COLORS
    chart_id = f"daily-{metric}-chart"
    title = (
        "Daily multimodal traffic counts"
        if class_counts
        else "Daily enter and exit counts"
    )
    width = max(1120, 160 + min(len(summaries), 60) * 44)
    left = 72
    top = 38
    chart_width = width - 105
    chart_height = 296
    bottom = top + chart_height
    height = 445
    maximum = max(
        (
            summary.counts[series_name]
            if class_counts
            else summary.direction_counts[series_name]
            for summary in summaries
            for series_name in series_names
        ),
        default=0,
    )
    tick_size = max(1, ceil(maximum / 5))
    axis_maximum = tick_size * 5
    count = len(summaries)
    label_step = max(1, ceil(count / 10))

    def point_x(index: int) -> float:
        if count <= 1:
            return left + chart_width / 2
        return left + chart_width * index / (count - 1)

    def point_y(value: int) -> float:
        return bottom - chart_height * value / axis_maximum

    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="{chart_id}-title {chart_id}-description" '
            'xmlns="http://www.w3.org/2000/svg">'
        ),
        f'<title id="{chart_id}-title">{title}</title>',
        (
            f'<desc id="{chart_id}-description">Line chart comparing accepted '
            f"{'multimodal traffic' if class_counts else 'Enter and Exit'} counts "
            "over time by date.</desc>"
        ),
        f'<rect width="{width}" height="{height}" rx="10" fill="#ffffff"/>',
    ]

    for tick_index in range(6):
        value = tick_index * tick_size
        y = bottom - chart_height * value / axis_maximum
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" y2="{y:.1f}" '
            'stroke="#dbe3ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#64748b">{value}</text>'
        )

    for series_name in series_names:
        points = []
        for index, summary in enumerate(summaries):
            value = (
                summary.counts[series_name]
                if class_counts
                else summary.direction_counts[series_name]
            )
            x = point_x(index)
            y = point_y(value)
            points.append(f"{x:.1f},{y:.1f}")
        if points:
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" '
                f'stroke="{colors[series_name]}" stroke-width="3" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for index, summary in enumerate(summaries):
            value = (
                summary.counts[series_name]
                if class_counts
                else summary.direction_counts[series_name]
            )
            x = point_x(index)
            y = point_y(value)
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                f'fill="{colors[series_name]}">'
                f'<title>{escape(series_name)} · {summary.day.isoformat()} · {value}</title>'
                '</circle>'
            )

    for index, summary in enumerate(summaries):
        if index % label_step != 0 and index != count - 1:
            continue
        x = point_x(index)
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 21}" text-anchor="middle" '
            f'font-size="11" fill="#475569">{summary.day.isoformat()}</text>'
        )

    parts.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" '
            'stroke="#475569" stroke-width="1.5"/>',
            (
                f'<text x="{left + chart_width / 2}" y="{bottom + 48}" text-anchor="middle" '
                'font-size="13" fill="#334155">Date</text>'
            ),
            (
                f'<text x="18" y="{top + chart_height / 2}" text-anchor="middle" '
                'font-size="13" fill="#334155" '
                f'transform="rotate(-90 18 {top + chart_height / 2})">Counts</text>'
            ),
        ]
    )
    if maximum == 0:
        parts.append(
            f'<text x="{left + chart_width / 2}" y="{top + chart_height / 2}" '
            'text-anchor="middle" font-size="16" fill="#64748b">'
            'No accepted counts are available</text>'
        )

    legend_y = 420
    legend_width = chart_width / max(1, len(series_names))
    for index, series_name in enumerate(series_names):
        x = left + index * legend_width
        parts.append(
            f'<line x1="{x}" y1="{legend_y - 6}" x2="{x + 18}" y2="{legend_y - 6}" '
            f'stroke="{colors[series_name]}" stroke-width="3"/>'
        )
        parts.append(
            f'<circle cx="{x + 9}" cy="{legend_y - 6}" r="4" '
            f'fill="{colors[series_name]}"/>'
        )
        parts.append(
            f'<text x="{x + 26}" y="{legend_y}" font-size="12" fill="#334155">'
            f'{escape(series_name)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _daily_weather_cards_html(summaries, weather_by_day) -> str:
    cards = []
    for summary in summaries:
        weather = weather_by_day.get(summary.day)
        if weather is None:
            continue
        cards.append(
            '<div class="weather-card">'
            f'{weather_icon_svg(weather.weather_code, 28)}'
            '<div>'
            f'<strong>{summary.day.isoformat()}</strong><br>'
            f'<span>{escape(weather.condition)} · '
            f'{weather.high_c:.0f}°/{weather.low_c:.0f}°</span>'
            '</div></div>'
        )
    if not cards:
        return ""
    return '<div class="weather-grid">' + "".join(cards) + '</div>'


def _daily_plot_svg(summaries, metric: str, weather_by_day=None) -> str:
    return _daily_line_svg(summaries, metric)


def _camera_plot_svg(summaries, metric: str = "total") -> str:
    per_recorded_hour = metric == "per_recorded_hour"
    width = 1120
    height = 470
    left = 72
    top = 24
    chart_width = 1015
    chart_height = 310
    bottom = top + chart_height
    maximum = max(
        (
            summary.crossings_per_recorded_hour() if per_recorded_hour else summary.total
            for summary in summaries
        ),
        default=0,
    )
    tick_size = (
        max(0.1, ceil(maximum * 2) / 10)
        if per_recorded_hour
        else max(1, ceil(maximum / 5))
    )
    axis_maximum = tick_size * 5
    slot_width = chart_width / max(1, len(summaries))
    bar_width = min(110, slot_width * 0.62)
    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="camera-chart-{metric}-title camera-chart-{metric}-description" '
            'xmlns="http://www.w3.org/2000/svg">'
        ),
        (
            f'<title id="camera-chart-{metric}-title">'
            f"{'Counts per recorded hour' if per_recorded_hour else 'Total counts'}"
            " by camera</title>"
        ),
        (
            f'<desc id="camera-chart-{metric}-description">Stacked bars compare '
            "pedestrian, "
            "bicycle, car, truck, bus, and motorcycle counts across all cameras with "
            "recordings on this date.</desc>"
        ),
        '<rect width="1120" height="470" rx="10" fill="#ffffff"/>',
    ]
    for tick_index in range(6):
        value = tick_index * tick_size
        y = bottom - chart_height * value / axis_maximum
        tick_label = f"{value:.1f}" if per_recorded_hour else str(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" y2="{y:.1f}" '
            'stroke="#dbe3ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#64748b">{tick_label}</text>'
        )
    for index, summary in enumerate(summaries):
        x = left + index * slot_width + (slot_width - bar_width) / 2
        stack_y = float(bottom)
        for mode in MODES:
            raw_count = summary.counts[mode]
            value = summary.crossings_per_recorded_hour(mode) if per_recorded_hour else raw_count
            if not value:
                continue
            bar_height = chart_height * value / axis_maximum
            stack_y -= bar_height
            tooltip_value = (
                f"{value:.1f} per hour ({raw_count} total over "
                f"{summary.recorded_hours:.2f} recorded hours)"
                if per_recorded_hour
                else str(raw_count)
            )
            parts.append(
                f'<rect x="{x:.1f}" y="{stack_y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="{MODE_COLORS[mode]}">'
                f"<title>{escape(mode)} · {escape(summary.camera)} · "
                f"{tooltip_value}</title></rect>"
            )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 21}" text-anchor="middle" '
            f'font-size="12" fill="#475569">{escape(summary.camera)}</text>'
        )
    parts.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" '
            'stroke="#475569" stroke-width="1.5"/>',
            (
                f'<text x="{left + chart_width / 2}" y="{bottom + 51}" text-anchor="middle" '
                'font-size="13" fill="#334155">Camera</text>'
            ),
            (
                f'<text x="18" y="{top + chart_height / 2}" text-anchor="middle" '
                'font-size="13" fill="#334155" '
                f'transform="rotate(-90 18 {top + chart_height / 2})">'
                f"{'Counts per recorded hour' if per_recorded_hour else 'Total counts'}"
                "</text>"
            ),
        ]
    )
    legend_y = 446
    for index, mode in enumerate(MODES):
        x = left + index * 168
        parts.append(
            f'<rect x="{x}" y="{legend_y - 12}" width="14" height="14" '
            f'rx="2" fill="{MODE_COLORS[mode]}"/>'
        )
        parts.append(
            f'<text x="{x + 20}" y="{legend_y}" font-size="12" fill="#334155">{escape(mode)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _camera_hourly_plot_svg(hourly_by_camera) -> str:
    width = 1120
    left = 72
    top = 24
    chart_width = 1015
    chart_height = 310
    bottom = top + chart_height
    legend_columns = 4
    legend_rows = max(1, ceil(len(hourly_by_camera) / legend_columns))
    height = 390 + legend_rows * 28
    maximum = max(
        (
            summary.total
            for _camera, hourly_summaries in hourly_by_camera
            for summary in hourly_summaries
        ),
        default=0,
    )
    tick_size = max(1, ceil(maximum / 5))
    axis_maximum = tick_size * 5
    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            'aria-labelledby="camera-hourly-title camera-hourly-description" '
            'xmlns="http://www.w3.org/2000/svg">'
        ),
        '<title id="camera-hourly-title">Hourly counts by camera — line chart</title>',
        (
            '<desc id="camera-hourly-description">One line per camera shows its total '
            "counts in each hour from midnight through 23:00.</desc>"
        ),
        f'<rect width="{width}" height="{height}" rx="10" fill="#ffffff"/>',
    ]
    for tick_index in range(6):
        value = tick_index * tick_size
        y = bottom - chart_height * value / axis_maximum
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" y2="{y:.1f}" '
            'stroke="#dbe3ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#64748b">{value}</text>'
        )
    for hour in range(0, 24, 2):
        x = left + chart_width * hour / 23
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 21}" text-anchor="middle" '
            f'font-size="11" fill="#475569">{hour:02d}</text>'
        )
    for index, (camera, hourly_summaries) in enumerate(hourly_by_camera):
        color = CAMERA_COLORS[index % len(CAMERA_COLORS)]
        points = []
        for hour, summary in enumerate(hourly_summaries):
            x = left + chart_width * hour / 23
            y = bottom - chart_height * summary.total / axis_maximum
            points.append(f"{x:.1f},{y:.1f}")
            if summary.total:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}">'
                    f"<title>{escape(camera)} · {hour:02d}:00 · {summary.total}</title>"
                    "</circle>"
                )
        parts.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
            'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
        )
    parts.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" '
            'stroke="#475569" stroke-width="1.5"/>',
            (
                f'<text x="{left + chart_width / 2}" y="{bottom + 47}" text-anchor="middle" '
                'font-size="13" fill="#334155">Hour</text>'
            ),
            (
                f'<text x="18" y="{top + chart_height / 2}" text-anchor="middle" '
                'font-size="13" fill="#334155" '
                f'transform="rotate(-90 18 {top + chart_height / 2})">'
                "Counts</text>"
            ),
        ]
    )
    if maximum == 0:
        parts.append(
            f'<text x="{left + chart_width / 2}" y="{top + chart_height / 2}" '
            'text-anchor="middle" font-size="16" fill="#64748b">'
            "No accepted detections for this date</text>"
        )
    legend_y = bottom + 79
    legend_width = chart_width / legend_columns
    for index, (camera, _hourly_summaries) in enumerate(hourly_by_camera):
        column = index % legend_columns
        row = index // legend_columns
        x = left + column * legend_width
        y = legend_y + row * 28
        color = CAMERA_COLORS[index % len(CAMERA_COLORS)]
        parts.append(
            f'<line x1="{x}" y1="{y - 5}" x2="{x + 22}" y2="{y - 5}" '
            f'stroke="{color}" stroke-width="4"/>'
        )
        parts.append(
            f'<text x="{x + 30}" y="{y}" font-size="12" fill="#334155">'
            f"{escape(camera)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _camera_hourly_stacked_plot_svg(hourly_by_camera) -> str:
    width = 1120
    left = 72
    top = 24
    chart_width = 1015
    chart_height = 310
    bottom = top + chart_height
    legend_columns = 4
    legend_rows = max(1, ceil(len(hourly_by_camera) / legend_columns))
    height = 412 + legend_rows * 28
    maximum = max(
        (
            sum(
                camera_summaries[hour].total
                for _camera, camera_summaries in hourly_by_camera
            )
            for hour in range(24)
        ),
        default=0,
    )
    tick_size = max(1, ceil(maximum / 5))
    axis_maximum = tick_size * 5
    slot_width = chart_width / 24
    bar_width = slot_width * 0.72
    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            'aria-labelledby="camera-hourly-stacked-title '
            'camera-hourly-stacked-description" xmlns="http://www.w3.org/2000/svg">'
        ),
        (
            '<title id="camera-hourly-stacked-title">'
            "Hourly counts by camera — stacked bar chart</title>"
        ),
        (
            '<desc id="camera-hourly-stacked-description">One stacked bar per hour shows '
            "total counts split by camera from midnight through 23:00.</desc>"
        ),
        f'<rect width="{width}" height="{height}" rx="10" fill="#ffffff"/>',
    ]
    for tick_index in range(6):
        value = tick_index * tick_size
        y = bottom - chart_height * value / axis_maximum
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_width}" y2="{y:.1f}" '
            'stroke="#dbe3ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#64748b">{value}</text>'
        )
    for hour in range(24):
        x = left + hour * slot_width + (slot_width - bar_width) / 2
        stack_y = float(bottom)
        for index, (camera, hourly_summaries) in enumerate(hourly_by_camera):
            count = hourly_summaries[hour].total
            if not count:
                continue
            bar_height = chart_height * count / axis_maximum
            stack_y -= bar_height
            color = CAMERA_COLORS[index % len(CAMERA_COLORS)]
            parts.append(
                f'<rect x="{x:.1f}" y="{stack_y:.1f}" width="{bar_width:.1f}" '
                f'height="{bar_height:.1f}" fill="{color}">'
                f"<title>{escape(camera)} · {hour:02d}:00 · {count}</title></rect>"
            )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{bottom + 19}" text-anchor="middle" '
            f'font-size="11" fill="#475569">{hour:02d}</text>'
        )
    parts.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" '
            'stroke="#475569" stroke-width="1.5"/>',
            (
                f'<text x="{left + chart_width / 2}" y="{bottom + 43}" text-anchor="middle" '
                'font-size="13" fill="#334155">Hour</text>'
            ),
            (
                f'<text x="18" y="{top + chart_height / 2}" text-anchor="middle" '
                'font-size="13" fill="#334155" '
                f'transform="rotate(-90 18 {top + chart_height / 2})">Counts</text>'
            ),
        ]
    )
    if maximum == 0:
        parts.append(
            f'<text x="{left + chart_width / 2}" y="{top + chart_height / 2}" '
            'text-anchor="middle" font-size="16" fill="#64748b">'
            "No accepted counts for this date</text>"
        )
    legend_y = 407
    legend_width = chart_width / legend_columns
    for index, (camera, _hourly_summaries) in enumerate(hourly_by_camera):
        column = index % legend_columns
        row = index // legend_columns
        x = left + column * legend_width
        y = legend_y + row * 24
        color = CAMERA_COLORS[index % len(CAMERA_COLORS)]
        parts.append(
            f'<rect x="{x}" y="{y - 12}" width="14" height="14" rx="2" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{x + 20}" y="{y}" font-size="12" fill="#334155">'
            f"{escape(camera)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def generate_html_report(
    repository: ProjectRepository,
    output_path: Path,
    day: date,
    camera: str,
) -> Path:
    videos = repository.list_videos()
    events = repository.list_events(ReviewStatus.ACCEPTED)
    summaries = build_hourly_summary(videos, events, day, camera)
    daily_videos = [
        video for video in videos if video.camera == camera and video.recording_day == day
    ]
    mode_totals: Counter[str] = Counter()
    for summary in summaries:
        mode_totals.update(summary.counts)
    captured_hours = sum(summary.recorded_seconds for summary in summaries) / 3600

    rows = []
    for summary in summaries:
        cells = [
            f"{summary.hour.hour:02d}:00",
            f"{summary.recorded_seconds / 60:.1f}",
            summary.coverage_status,
            *(str(summary.counts[mode]) for mode in MODES),
            str(summary.total),
        ]
        rows.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>")

    mode_cards = "".join(
        f'<div class="card"><span>{escape(mode)}</span><strong>{mode_totals[mode]:,}</strong></div>'
        for mode in MODES
    )
    header_cells = "".join(
        f"<th>{escape(value)}</th>"
        for value in ["Hour", "Recorded min", "Coverage", *MODES, "Total"]
    )
    hourly_plot = _hourly_plot_svg(summaries)
    direction_totals: Counter[tuple[str, str]] = Counter()
    for row in events:
        if (
            row["camera"] == camera
            and row["occurred_at"]
            and date.fromisoformat(row["occurred_at"][:10]) == day
        ):
            direction_totals[(row["mode"], row["direction_label"])] += 1
    direction_rows = "".join(
        "<tr>"
        f"<td>{escape(mode)}</td>"
        f"<td>{direction_totals[(mode, 'Enter')]:,}</td>"
        f"<td>{direction_totals[(mode, 'Exit')]:,}</td>"
        f"<td>{direction_totals[(mode, 'Enter')] + direction_totals[(mode, 'Exit')]:,}</td>"
        "</tr>"
        for mode in MODES
    )
    fragment_count = len(daily_videos)
    fragment_word = "fragment" if fragment_count == 1 else "fragments"
    fragment_verb = "was" if fragment_count == 1 else "were"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Camera report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;color:#172033;margin:40px;max-width:1200px}}
h1{{color:#102a43;margin-bottom:4px}} .subtitle{{color:#64748b;margin-top:0}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}}
.card{{border:1px solid #dbe3ec;border-radius:10px;padding:16px;background:#f8fafc}}
.card span{{display:block;color:#64748b}} .card strong{{font-size:26px;color:#16845b}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{background:#edf3f7;text-align:left}}
th,td{{padding:8px;border-bottom:1px solid #dbe3ec}}
.note{{background:#fff8e6;padding:14px;border-radius:8px}}
.chart-wrap{{border:1px solid #dbe3ec;border-radius:10px;padding:12px;overflow-x:auto;
margin:18px 0 28px}}
.chart-wrap svg{{display:block;width:100%;min-width:850px;height:auto}}
</style></head><body>
<h1>Camera report</h1>
<p class="subtitle">{escape(camera)} · {day.isoformat()}</p>
<h2>Daily summary</h2>
<p><strong>{fragment_count} recording {fragment_word}</strong> {fragment_verb} combined
for this date, providing <strong>{captured_hours * 60:.1f} minutes
({captured_hours:.2f} hours)</strong> of video.</p>
<h2>Multimodal traffic counts</h2>
<div class="cards">{mode_cards}</div>
<h2>Enter and exit summary</h2>
<table><thead><tr><th>Class</th><th>Enter</th><th>Exit</th><th>Total</th></tr></thead>
<tbody>{direction_rows}</tbody></table>
<h2>Hourly counts and coverage</h2><table><thead><tr>{header_cells}</tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<h2>Hourly counts by class</h2>
<div class="chart-wrap">{hourly_plot}</div>
</body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def generate_camera_comparison_html_report(
    repository: ProjectRepository,
    output_path: Path,
    day: date,
) -> Path:
    videos = repository.list_videos()
    events = repository.list_events(ReviewStatus.ACCEPTED)
    summaries = build_camera_summary(videos, events, day)
    total_enter = sum(summary.direction_counts["Enter"] for summary in summaries)
    total_exit = sum(summary.direction_counts["Exit"] for summary in summaries)
    accepted_total = sum(summary.total for summary in summaries)
    recorded_hours = [summary.recorded_hours for summary in summaries]
    coverage_text = (
        f"Recorded coverage ranges from {min(recorded_hours):.2f} to "
        f"{max(recorded_hours):.2f} hours per camera."
        if recorded_hours
        else "No recorded camera coverage is available for this date."
    )
    mode_totals: Counter[str] = Counter()
    for summary in summaries:
        mode_totals.update(summary.counts)
    mode_cards = "".join(
        f'<div class="card"><span>{escape(mode)}</span><strong>{mode_totals[mode]:,}</strong></div>'
        for mode in MODES
    )
    direction_rows = "".join(
        "<tr>"
        f"<td>{escape(summary.camera)}</td>"
        f"<td>{summary.direction_counts['Enter']:,}</td>"
        f"<td>{summary.direction_counts['Exit']:,}</td>"
        f"<td>{summary.total:,}</td>"
        f"<td>{summary.recorded_hours:.2f}</td>"
        "</tr>"
        for summary in summaries
    )
    if summaries:
        direction_rows += (
            '<tr class="total-row"><td>Overall total</td>'
            f"<td>{total_enter:,}</td><td>{total_exit:,}</td>"
            f"<td>{accepted_total:,}</td><td>—</td></tr>"
        )
    camera_total_plot = _camera_plot_svg(summaries)
    camera_rate_plot = _camera_plot_svg(summaries, "per_recorded_hour")
    hourly_by_camera = [
        (summary.camera, build_hourly_summary(videos, events, day, summary.camera))
        for summary in summaries
    ]
    camera_hourly_line_plot = _camera_hourly_plot_svg(hourly_by_camera)
    camera_hourly_stacked_plot = _camera_hourly_stacked_plot_svg(hourly_by_camera)
    hourly_panels = "".join(
        '<section class="panel">'
        f"<h3>{escape(camera)}</h3>"
        '<div class="chart-wrap">'
        f"{_hourly_plot_svg(hourly, f'hourly-camera-{index}', f'Hourly counts · {camera}')}"
        "</div></section>"
        for index, (camera, hourly) in enumerate(hourly_by_camera)
    )
    if not hourly_panels:
        hourly_panels = '<p class="note">No camera hourly plots are available for this date.</p>'
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Camera comparison report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;color:#172033;margin:40px;max-width:1200px}}
h1{{color:#102a43;margin-bottom:4px}} .subtitle{{color:#64748b;margin-top:0}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}}
.card{{border:1px solid #dbe3ec;border-radius:10px;padding:16px;background:#f8fafc}}
.card span{{display:block;color:#64748b}} .card strong{{font-size:26px;color:#16845b}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th{{background:#edf3f7;text-align:left}}
th,td{{padding:8px;border-bottom:1px solid #dbe3ec}}
.total-row{{font-weight:700;background:#f1f5f9}}
.note{{background:#eaf7f0;padding:14px;border-radius:8px}}
.chart-wrap{{border:1px solid #dbe3ec;border-radius:10px;padding:12px;overflow-x:auto;
margin:18px 0 28px}}
.chart-wrap svg{{display:block;width:100%;min-width:850px;height:auto}}
.panel-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}}
.panel{{border:1px solid #dbe3ec;border-radius:10px;padding:12px;background:#f8fafc}}
.panel h3{{margin:4px 4px 8px}} .panel .chart-wrap{{margin:0;background:white}}
.panel .chart-wrap svg{{min-width:0}}
@media (max-width:900px){{.panel-grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Camera comparison report</h1>
<p class="subtitle">All cameras · {day.isoformat()}</p>
<p class="note"><strong>{len(summaries)} cameras</strong> ·
<strong>{accepted_total:,} accepted counts</strong> ({total_enter:,} Enter and
{total_exit:,} Exit). {coverage_text}</p>
<h2>Multimodal traffic counts</h2>
<div class="cards">{mode_cards}</div>
<h2>Enter and exit summary</h2>
<table><thead><tr><th>Camera</th><th>Enter</th><th>Exit</th><th>Total</th>
<th>Recorded hours</th></tr></thead><tbody>{direction_rows}</tbody></table>
<h2>Total counts by camera</h2>
<div class="chart-wrap">{camera_total_plot}</div>
<h2>Counts per recorded hour by camera</h2>
<p>This normalized view accounts for cameras with different amounts of recorded coverage.
Overlapping recording fragments are counted once.</p>
<div class="chart-wrap">{camera_rate_plot}</div>
<h2>Hourly counts by camera</h2>
<p>The line and stacked bar charts show each camera's total counts for every hour.</p>
<h3>Line chart</h3>
<div class="chart-wrap">{camera_hourly_line_plot}</div>
<h3>Stacked bar chart</h3>
<div class="chart-wrap">{camera_hourly_stacked_plot}</div>
<h2>Hourly class plots for every camera</h2>
<p>Each panel repeats the hourly stacked class plot available in Camera Reports.</p>
<div class="panel-grid">{hourly_panels}</div>
</body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def generate_daily_html_report(
    repository: ProjectRepository,
    output_path: Path,
) -> Path:
    if output_path.name.casefold() in {"daily_report", "daily_report.html"}:
        output_path = output_path.with_name("daily_trends.html")
    videos = repository.list_videos()
    events = repository.list_events(ReviewStatus.ACCEPTED)
    summaries = build_daily_summary(videos, events)
    weather_by_day = repository.list_daily_weather()
    total_counts: Counter[str] = Counter()
    total_directions: Counter[str] = Counter()
    for summary in summaries:
        total_counts.update(summary.counts)
        total_directions.update(summary.direction_counts)
    overall_total = sum(total_counts.values())

    def daily_table_row(summary) -> str:
        weather = weather_by_day.get(summary.day)
        if weather is None:
            weather_cells = "<td>—</td><td>—</td><td>—</td>"
        else:
            condition = (
                '<span class="weather-condition">'
                f"{weather_icon_svg(weather.weather_code, 24)}"
                f"<span>{escape(weather.condition)}</span></span>"
            )
            weather_cells = (
                f"<td>{weather.high_c:.1f}</td>"
                f"<td>{weather.low_c:.1f}</td>"
                f"<td>{condition}</td>"
            )
        return (
            "<tr>"
            f"<td>{summary.day.isoformat()}</td>"
            + "".join(f"<td>{summary.counts[mode]:,}</td>" for mode in MODES)
            + f"<td>{summary.direction_counts['Enter']:,}</td>"
            f"<td>{summary.direction_counts['Exit']:,}</td>"
            f"<td>{summary.total:,}</td>"
            f"{weather_cells}</tr>"
        )

    table_rows = "".join(daily_table_row(summary) for summary in summaries)
    if summaries:
        table_rows += (
            '<tr class="total-row"><td>Overall total</td>'
            + "".join(f"<td>{total_counts[mode]:,}</td>" for mode in MODES)
            + f"<td>{total_directions['Enter']:,}</td>"
            f"<td>{total_directions['Exit']:,}</td>"
            f"<td>{overall_total:,}</td><td>—</td><td>—</td><td>—</td></tr>"
        )
    header_cells = "".join(
        f"<th>{escape(value)}</th>"
        for value in [
            "Date",
            *MODES,
            "Enter",
            "Exit",
            "Total",
            "High °C",
            "Low °C",
            "Conditions",
        ]
    )
    weather_cards = _daily_weather_cards_html(summaries, weather_by_day)
    class_plot = _daily_plot_svg(summaries, "class", weather_by_day)
    direction_plot = _daily_plot_svg(summaries, "direction", weather_by_day)
    attribution = (
        '<p class="attribution">Weather data: '
        '<a href="https://open-meteo.com/">Open-Meteo</a>, '
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.</p>'
        if weather_by_day
        else ""
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Daily Trends</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;color:#172033;margin:40px;max-width:1400px}}
h1{{color:#102a43;margin-bottom:4px}} .subtitle{{color:#64748b;margin-top:0}}
.note{{background:#eaf7f0;padding:14px;border-radius:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{background:#edf3f7;text-align:left;position:sticky;top:0}}
th,td{{padding:8px;border-bottom:1px solid #dbe3ec}}
.total-row{{font-weight:700;background:#f1f5f9}}
.attribution{{font-size:12px;color:#64748b}}
.weather-condition{{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}}
.weather-icon{{display:inline-block;vertical-align:middle;flex:0 0 auto}}
.weather-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;
margin:12px 0 24px}}
.weather-card{{display:flex;align-items:center;gap:9px;background:#f8fafc;border:1px solid #dbe3ec;
border-radius:9px;padding:9px 11px;min-width:0}}
.weather-card span{{font-size:12px;color:#475569}}
.table-wrap,.chart-wrap{{border:1px solid #dbe3ec;border-radius:10px;padding:12px;
overflow-x:auto;margin:18px 0 28px}}
.chart-wrap svg{{display:block;width:100%;min-width:850px;height:auto}}
</style></head><body>
<h1>Daily Trends</h1>
<p class="subtitle">All available dates and cameras</p>
<p class="note"><strong>{len(summaries)} dates</strong> ·
<strong>{overall_total:,} accepted counts</strong> ({total_directions['Enter']:,} Enter and
{total_directions['Exit']:,} Exit).</p>
<h2>Counts by day</h2>
<div class="table-wrap"><table><thead><tr>{header_cells}</tr></thead>
<tbody>{table_rows}</tbody></table></div>
{weather_cards}
<h2>Daily multimodal traffic counts</h2>
<div class="chart-wrap">{class_plot}</div>
<h2>Daily enter and exit counts</h2>
<div class="chart-wrap">{direction_plot}</div>
{attribution}
</body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
