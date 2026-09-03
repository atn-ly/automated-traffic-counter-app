from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from traffic_reviewer.domain import Detection, TimestampSource, VideoRecord, VideoStatus
from traffic_reviewer.processing import (
    DEFAULT_DETECTION_CONFIDENCE,
    DEFAULT_MAX_DETECTIONS,
    CrossingCounter,
    MODEL_LABEL_TO_MODE,
    YoloVideoProcessor,
    map_rectified_boxes_to_frame,
    merge_full_frame_and_zone_boxes,
    rectify_distant_zone,
)


class AnnotatedVideoCancelled(Exception):
    pass


ANNOTATION_FRAGMENT_VERSION = 11
ANNOTATED_VIDEO_VERSION = 16


def try_remove_annotated_video(path: Path) -> bool:
    """Remove a stale QC video, returning False while Windows still locks it."""
    try:
        Path(path).unlink(missing_ok=True)
    except PermissionError:
        return False
    return True


def recordings_for_date(videos: list[VideoRecord], day: date, camera: str) -> list[VideoRecord]:
    day_start = datetime.combine(day, time.min)
    day_end = day_start + timedelta(days=1)
    recordings = []
    for video in videos:
        if video.camera != camera or video.recorded_at is None:
            continue
        if video.timestamp_source == TimestampSource.MISSING or (
            video.timestamp_source == TimestampSource.BURNED_IN_OCR
            and video.timestamp_confidence < 1
        ):
            continue
        end = video.recorded_end_at or video.recorded_at + timedelta(seconds=video.duration_seconds)
        if video.recorded_at < day_end and end > day_start:
            recordings.append(video)
    return sorted(recordings, key=lambda video: (video.recorded_at, video.path.name))


def resolve_detection_settings(recordings: list[VideoRecord]) -> tuple[int, str, tuple[str, ...]]:
    if not recordings:
        raise ValueError("No timestamped recordings are available for this date and camera.")
    incomplete = [video.path.name for video in recordings if video.status != VideoStatus.COMPLETE]
    if incomplete:
        raise ValueError(
            "Process every recording before Annotated Video QC. Not complete: "
            + ", ".join(incomplete)
        )
    missing = [
        video.path.name
        for video in recordings
        if video.processing_stride is None
        or not video.processing_model
        or not video.processing_modes
    ]
    if missing:
        raise ValueError(
            "Reprocess these recordings so their detection settings are recorded: "
            + ", ".join(missing)
        )
    settings = {
        (
            int(video.processing_stride),
            str(video.processing_model),
            tuple(video.processing_modes),
        )
        for video in recordings
    }
    if len(settings) != 1:
        details = ", ".join(
            f"{video.path.name} (stride {video.processing_stride}, "
            f"model {video.processing_model}, classes {', '.join(video.processing_modes)})"
            for video in recordings
        )
        raise ValueError(
            "All recordings must use the same detection model, frame stride, and classes. "
            + details
        )
    return next(iter(settings))


def internal_recording_gaps(
    recordings: list[VideoRecord], minimum_seconds: float = 0.5
) -> list[tuple[datetime, datetime]]:
    if not recordings:
        return []
    start = recordings[0].recorded_at
    end = max(
        video.recorded_end_at or video.recorded_at + timedelta(seconds=video.duration_seconds)
        for video in recordings
    )
    return recording_gaps_in_window(recordings, start, end, minimum_seconds)


def recording_gaps_in_window(
    recordings: list[VideoRecord],
    window_start: datetime,
    window_end: datetime,
    minimum_seconds: float = 0.5,
) -> list[tuple[datetime, datetime]]:
    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for video in recordings:
        if video.recorded_at > cursor + timedelta(seconds=minimum_seconds):
            gaps.append((cursor, min(video.recorded_at, window_end)))
        end = video.recorded_end_at or video.recorded_at + timedelta(seconds=video.duration_seconds)
        cursor = max(cursor, end)
        if cursor >= window_end:
            break
    if cursor < window_end - timedelta(seconds=minimum_seconds):
        gaps.append((cursor, window_end))
    return gaps


def annotated_video_path(database_path: Path, day: date, camera: str, stride: int) -> Path:
    safe_camera = re.sub(r"[^A-Za-z0-9_-]+", "_", camera).strip("_") or "camera"
    return (
        Path(database_path).parent
        / "final_qc"
        / (
            f"annotated_{day.isoformat()}_{safe_camera}_stride_{stride}"
            f"_v{ANNOTATED_VIDEO_VERSION}.mp4"
        )
    )


def annotated_fragment_path(database_path: Path, video_id: int, stride: int) -> Path:
    return (
        Path(database_path).parent
        / "final_qc"
        / "fragments"
        / (
            f"video_{int(video_id)}_stride_{max(1, int(stride))}"
            f"_v{ANNOTATION_FRAGMENT_VERSION}.mp4"
        )
    )


def cached_annotated_fragments_ready(
    database_path: Path, recordings: list[VideoRecord], stride: int
) -> bool:
    return bool(recordings) and all(
        (path := annotated_fragment_path(database_path, video.id, stride)).is_file()
        and path.stat().st_size > 0
        for video in recordings
    )


def remove_cached_annotated_fragments(database_path: Path, video_id: int) -> None:
    fragment_directory = Path(database_path).parent / "final_qc" / "fragments"
    for path in fragment_directory.glob(f"video_{int(video_id)}_stride_*_v*.mp4"):
        path.unlink(missing_ok=True)
    for path in fragment_directory.glob(f"video_{int(video_id)}_stride_*_v*.partial.mp4"):
        path.unlink(missing_ok=True)


class AnnotatedDateVideoBuilder:
    target_size = (1280, 720)
    gap_card_seconds = 2.0
    output_box_thickness = 3
    output_line_thickness = 3
    mode_order = ("Pedestrian", "Bicycle", "Car", "Truck", "Bus", "Motorcycle")

    def __init__(self, repository):
        self.repository = repository

    def build(
        self,
        recordings: list[VideoRecord],
        output_path: Path,
        model_path: str,
        stride: int,
        coverage_start: datetime,
        coverage_end: datetime,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> Path:
        try:
            import cv2
            import numpy as np
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Annotated video export requires OpenCV, NumPy, and Ultralytics YOLO."
            ) from exc

        if not recordings:
            raise ValueError("No recordings were provided for Annotated Video QC")
        stride = max(1, int(stride))
        source_fps = recordings[0].fps if recordings[0].fps > 0 else 15.0
        output_fps = max(0.1, source_fps / stride)
        gap_frames = max(1, round(output_fps * self.gap_card_seconds))
        gaps = recording_gaps_in_window(recordings, coverage_start, coverage_end)
        total_frames = (
            sum(max(1, (video.frame_count + stride - 1) // stride) for video in recordings)
            + len(gaps) * gap_frames
        )
        selected_modes = set(recordings[0].processing_modes)
        display_modes = [mode for mode in self.mode_order if mode in selected_modes]
        running_counts: Counter[tuple[str, str]] = Counter()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(output_path.stem + ".partial.mp4")
        temporary_path.unlink(missing_ok=True)
        writer = cv2.VideoWriter(
            str(temporary_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            output_fps,
            self.target_size,
        )
        if not writer.isOpened():
            raise RuntimeError("Could not create the annotated MP4 video")

        completed = 0
        previous_end: datetime | None = coverage_start
        capture = None
        try:
            progress(0, total_frames, "Preparing annotated video")
            for file_index, video in enumerate(recordings, start=1):
                if should_cancel():
                    raise AnnotatedVideoCancelled
                gap_end = min(video.recorded_at, coverage_end)
                if previous_end is not None and gap_end > previous_end + timedelta(seconds=0.5):
                    gap_start = previous_end
                    card = self._gap_card(np, cv2, gap_start, gap_end)
                    self._draw_running_counts(cv2, card, display_modes, running_counts)
                    for _ in range(gap_frames):
                        writer.write(card)
                        completed += 1
                        progress(completed, total_frames, "Adding recording gap notice")

                capture = cv2.VideoCapture(str(video.path))
                if not capture.isOpened():
                    raise ValueError(f"OpenCV could not open {video.path.name}")
                model = YOLO(model_path)
                class_modes = self._class_modes(model)
                selected_class_ids = [
                    class_id for class_id, mode in class_modes.items() if mode in selected_modes
                ]
                unsupported = sorted(
                    selected_modes - {class_modes[class_id] for class_id in selected_class_ids}
                )
                if unsupported:
                    raise ValueError(
                        "The selected model does not contain: " + ", ".join(unsupported)
                    )
                counters = [CrossingCounter(line) for line in video.counting_lines]
                zone_tracker = (
                    YoloVideoProcessor._create_distant_zone_tracker()
                    if video.distant_detection_zone is not None
                    else None
                )
                frame_index = 0
                while True:
                    if should_cancel():
                        raise AnnotatedVideoCancelled
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if frame_index % stride:
                        frame_index += 1
                        continue
                    position_ms = int(capture.get(cv2.CAP_PROP_POS_MSEC) or 0)
                    if position_ms <= 0 and video.frame_count > 1:
                        position_ms = int(
                            frame_index / (video.frame_count - 1) * video.duration_seconds * 1000
                        )
                    if video.distant_detection_zone is None:
                        result = model.track(
                            frame,
                            persist=True,
                            verbose=False,
                            classes=selected_class_ids,
                        )[0]
                        detections = YoloVideoProcessor._detections_from_result(
                            result,
                            class_modes,
                            frame.shape[1],
                            frame.shape[0],
                        )
                    else:
                        from ultralytics.engine.results import Boxes

                        crop, inverse_transform, polygon = rectify_distant_zone(
                            cv2,
                            np,
                            frame,
                            video.distant_detection_zone,
                        )
                        prediction_options = {
                            "verbose": False,
                            "classes": selected_class_ids,
                            "imgsz": video.processing_image_size or 960,
                            "conf": DEFAULT_DETECTION_CONFIDENCE,
                            "max_det": DEFAULT_MAX_DETECTIONS,
                        }
                        full_result = model.predict(frame, **prediction_options)[0]
                        zone_result = model.predict(crop, **prediction_options)[0]
                        mapped_zone_boxes = map_rectified_boxes_to_frame(
                            cv2,
                            np,
                            YoloVideoProcessor._raw_boxes_from_result(zone_result),
                            inverse_transform,
                            frame.shape[1],
                            frame.shape[0],
                        )
                        merged = merge_full_frame_and_zone_boxes(
                            YoloVideoProcessor._raw_boxes_from_result(full_result),
                            mapped_zone_boxes,
                            polygon,
                        )
                        box_array = np.asarray(merged, dtype=np.float32)
                        if box_array.size == 0:
                            box_array = np.empty((0, 6), dtype=np.float32)
                        tracked = zone_tracker.update(
                            Boxes(box_array, frame.shape[:2]),
                            frame,
                        )
                        detections = YoloVideoProcessor._detections_from_tracked_boxes(
                            tracked,
                            class_modes,
                            frame.shape[1],
                            frame.shape[0],
                        )
                    self._draw_lines(cv2, frame, video)
                    events = self._draw_detection_list(
                        cv2,
                        frame,
                        detections,
                        counters,
                        video,
                        position_ms,
                        running_counts,
                    )
                    self._draw_header(
                        cv2,
                        frame,
                        video,
                        position_ms,
                        stride,
                        file_index,
                        len(recordings),
                        events,
                    )
                    output_frame = self._letterbox(np, cv2, frame)
                    self._draw_running_counts(
                        cv2,
                        output_frame,
                        display_modes,
                        running_counts,
                    )
                    writer.write(output_frame)
                    completed += 1
                    progress(
                        min(completed, total_frames),
                        total_frames,
                        f"Annotating {file_index}/{len(recordings)}: {video.path.name}",
                    )
                    frame_index += 1
                capture.release()
                capture = None
                video_end = video.recorded_end_at or video.recorded_at + timedelta(
                    seconds=video.duration_seconds
                )
                previous_end = max(previous_end, video_end) if previous_end else video_end
            if previous_end is not None and coverage_end > previous_end + timedelta(seconds=0.5):
                card = self._gap_card(np, cv2, previous_end, coverage_end)
                self._draw_running_counts(cv2, card, display_modes, running_counts)
                for _ in range(gap_frames):
                    writer.write(card)
                    completed += 1
                    progress(completed, total_frames, "Adding final recording gap notice")
            writer.release()
            writer = None
            temporary_path.replace(output_path)
            progress(total_frames, total_frames, "Annotated video complete")
            return output_path
        except Exception:
            if capture is not None:
                capture.release()
            if writer is not None:
                writer.release()
            temporary_path.unlink(missing_ok=True)
            raise

    def build_from_cached_fragments(
        self,
        recordings: list[VideoRecord],
        output_path: Path,
        stride: int,
        coverage_start: datetime,
        coverage_end: datetime,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> Path:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Annotated video assembly requires OpenCV and NumPy.") from exc

        if not recordings:
            raise ValueError("No recordings were provided for Annotated Video QC")
        stride = max(1, int(stride))
        fragment_paths = [
            annotated_fragment_path(self.repository.database_path, video.id, stride)
            for video in recordings
        ]
        missing = [
            video.path.name
            for video, fragment in zip(recordings, fragment_paths, strict=True)
            if not fragment.exists()
        ]
        if missing:
            raise ValueError(
                "Cached annotated video is missing for: "
                + ", ".join(missing)
                + ". Reprocess with Create annotated video during detection checked."
            )

        fragment_metadata = []
        for video, path in zip(recordings, fragment_paths, strict=True):
            capture = cv2.VideoCapture(str(path))
            if not capture.isOpened():
                raise ValueError(f"OpenCV could not open {path.name}")
            frame_count = max(1, round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            fragment_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            capture.release()
            if fragment_fps <= 0:
                source_fps = video.fps if video.fps > 0 else 15.0
                fragment_fps = max(0.1, source_fps / stride)
            fragment_metadata.append(
                {
                    "frame_count": frame_count,
                    "fps": fragment_fps,
                }
            )

        output_fps = max(metadata["fps"] for metadata in fragment_metadata)
        for metadata in fragment_metadata:
            metadata["output_frames"] = self._resampled_frame_count(
                metadata["frame_count"],
                metadata["fps"],
                output_fps,
            )
        gap_frames = max(1, round(output_fps * self.gap_card_seconds))
        gaps = recording_gaps_in_window(recordings, coverage_start, coverage_end)
        total_frames = (
            sum(metadata["output_frames"] for metadata in fragment_metadata)
            + len(gaps) * gap_frames
        )

        display_modes = [
            mode
            for mode in self.mode_order
            if any(mode in video.processing_modes for video in recordings)
        ]
        video_ids = {video.id for video in recordings}
        event_timeline = sorted(
            (
                datetime.fromisoformat(row["occurred_at"]),
                str(row["mode"]),
                str(
                    row["direction_label"]
                    or ("Enter" if row["direction"] == "A" else "Exit")
                ).title(),
            )
            for row in self.repository.list_events()
            if row["video_id"] in video_ids and row["occurred_at"]
        )
        running_counts: Counter[tuple[str, str]] = Counter()
        event_index = 0

        def advance_counts(timestamp: datetime) -> None:
            nonlocal event_index
            while (
                event_index < len(event_timeline)
                and event_timeline[event_index][0] <= timestamp
            ):
                _, mode, direction = event_timeline[event_index]
                running_counts[(mode, direction)] += 1
                event_index += 1

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(output_path.stem + ".partial.mp4")
        temporary_path.unlink(missing_ok=True)
        writer = cv2.VideoWriter(
            str(temporary_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            output_fps,
            self.target_size,
        )
        if not writer.isOpened():
            raise RuntimeError("Could not create the assembled annotated MP4 video")

        completed = 0
        previous_end: datetime | None = coverage_start
        capture = None
        try:
            progress(0, total_frames, "Preparing cached annotated video")
            for file_index, (video, fragment_path, metadata) in enumerate(
                zip(recordings, fragment_paths, fragment_metadata, strict=True), start=1
            ):
                if should_cancel():
                    raise AnnotatedVideoCancelled
                gap_end = min(video.recorded_at, coverage_end)
                if previous_end is not None and gap_end > previous_end + timedelta(seconds=0.5):
                    advance_counts(previous_end)
                    card = self._gap_card(np, cv2, previous_end, gap_end)
                    self._draw_running_counts(cv2, card, display_modes, running_counts)
                    for _ in range(gap_frames):
                        writer.write(card)
                        completed += 1
                        progress(completed, total_frames, "Adding recording gap notice")

                capture = cv2.VideoCapture(str(fragment_path))
                if not capture.isOpened():
                    raise ValueError(f"OpenCV could not open {fragment_path.name}")
                source_index = 0
                written_for_fragment = 0
                source_fps = metadata["fps"]
                output_frames = metadata["output_frames"]
                while True:
                    if should_cancel():
                        raise AnnotatedVideoCancelled
                    ok, frame = capture.read()
                    if not ok:
                        break
                    source_index += 1
                    if frame.shape[1::-1] != self.target_size:
                        frame = cv2.resize(frame, self.target_size, interpolation=cv2.INTER_AREA)
                    expected_writes = min(
                        output_frames,
                        round(source_index * output_fps / source_fps),
                    )
                    while written_for_fragment < expected_writes:
                        position_ms = round(written_for_fragment / output_fps * 1000)
                        advance_counts(video.timestamp_at(position_ms))
                        output_frame = frame.copy()
                        self._draw_running_counts(
                            cv2,
                            output_frame,
                            display_modes,
                            running_counts,
                        )
                        writer.write(output_frame)
                        written_for_fragment += 1
                        completed += 1
                        progress(
                            min(completed, total_frames),
                            total_frames,
                            f"Assembling {file_index}/{len(recordings)}: {video.path.name}",
                        )
                capture.release()
                capture = None
                video_end = video.recorded_end_at or video.recorded_at + timedelta(
                    seconds=video.duration_seconds
                )
                previous_end = max(previous_end, video_end) if previous_end else video_end

            if previous_end is not None and coverage_end > previous_end + timedelta(seconds=0.5):
                advance_counts(previous_end)
                card = self._gap_card(np, cv2, previous_end, coverage_end)
                self._draw_running_counts(cv2, card, display_modes, running_counts)
                for _ in range(gap_frames):
                    writer.write(card)
                    completed += 1
                    progress(completed, total_frames, "Adding final recording gap notice")
            writer.release()
            writer = None
            temporary_path.replace(output_path)
            progress(total_frames, total_frames, "Annotated video complete")
            return output_path
        except Exception:
            if capture is not None:
                capture.release()
            if writer is not None:
                writer.release()
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _class_modes(model) -> dict[int, str]:
        names = model.names.items() if isinstance(model.names, dict) else enumerate(model.names)
        return {
            int(class_id): MODEL_LABEL_TO_MODE[str(label).strip().lower()]
            for class_id, label in names
            if str(label).strip().lower() in MODEL_LABEL_TO_MODE
        }

    @staticmethod
    def _resampled_frame_count(frame_count: int, source_fps: float, output_fps: float) -> int:
        return max(1, round(frame_count * output_fps / max(source_fps, 0.1)))

    @staticmethod
    def _draw_running_counts(
        cv2,
        frame,
        modes: list[str] | tuple[str, ...],
        counts: Counter[tuple[str, str]],
    ) -> None:
        if not modes:
            return
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.58
        thickness = 2
        padding = 12
        row_height = 27
        title = "DETECTED CROSSINGS"
        labels = [
            f"{mode}: Enter {counts[(mode, 'Enter')]:,} | "
            f"Exit {counts[(mode, 'Exit')]:,}"
            for mode in modes
        ]
        text_widths = [
            cv2.getTextSize(text, font, scale, thickness)[0][0]
            for text in [title, *labels]
        ]
        box_width = max(text_widths) + padding * 2
        box_height = padding * 2 + row_height * (len(labels) + 1)
        right = frame.shape[1] - 12
        left = max(12, right - box_width)
        top = 50
        bottom = min(frame.shape[0] - 12, top + box_height)
        cv2.rectangle(frame, (left, top), (right, bottom), (15, 23, 42), -1)
        cv2.rectangle(frame, (left, top), (right, bottom), (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(
            frame,
            title,
            (left + padding, top + padding + 18),
            font,
            scale,
            (40, 220, 255),
            thickness,
            cv2.LINE_AA,
        )
        for index, label in enumerate(labels, start=1):
            cv2.putText(
                frame,
                label,
                (left + padding, top + padding + 18 + index * row_height),
                font,
                scale,
                (255, 255, 255),
                thickness,
                cv2.LINE_AA,
            )

    @classmethod
    def _annotation_style(cls, width: int, height: int) -> tuple[int, float, int]:
        target_width, target_height = cls.target_size
        resize_scale = min(
            target_width / max(width, 1),
            target_height / max(height, 1),
        )
        box_thickness = max(2, round(cls.output_box_thickness / max(resize_scale, 0.01)))
        text_scale = max(0.5, 0.75 / max(resize_scale, 0.01))
        text_thickness = max(1, round(2 / max(resize_scale, 0.01)))
        return box_thickness, text_scale, text_thickness

    @classmethod
    def _resize_scale(cls, width: int, height: int) -> float:
        target_width, target_height = cls.target_size
        return min(
            target_width / max(width, 1),
            target_height / max(height, 1),
        )

    @staticmethod
    def _draw_detections(
        cv2,
        frame,
        result,
        class_modes,
        counters,
        video,
        position_ms,
        running_counts: Counter[tuple[str, str]] | None = None,
    ) -> list[str]:
        detections: list[Detection] = []
        boxes = result.boxes
        if boxes is None or boxes.id is None:
            return []
        ids = boxes.id.int().cpu().tolist()
        classes = boxes.cls.int().cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        centers = boxes.xywh.cpu().tolist()
        corners = boxes.xyxy.cpu().tolist()
        height, width = frame.shape[:2]
        for track_id, class_id, confidence, center, corner in zip(
            ids, classes, confidences, centers, corners, strict=True
        ):
            mode = class_modes.get(class_id)
            if mode is None:
                continue
            cx, cy = center[:2]
            x1, y1, x2, y2 = corner
            detection = Detection(
                track_id=int(track_id),
                mode=mode,
                confidence=float(confidence),
                center_x=float(cx) / max(width, 1),
                center_y=float(cy) / max(height, 1),
                box_x1=float(x1) / max(width, 1),
                box_y1=float(y1) / max(height, 1),
                box_x2=float(x2) / max(width, 1),
                box_y2=float(y2) / max(height, 1),
            )
            detections.append(detection)
        return AnnotatedDateVideoBuilder._draw_detection_list(
            cv2,
            frame,
            detections,
            counters,
            video,
            position_ms,
            running_counts,
        )

    @staticmethod
    def _draw_detection_list(
        cv2,
        frame,
        detections: list[Detection],
        counters,
        video,
        position_ms,
        running_counts: Counter[tuple[str, str]] | None = None,
    ) -> list[str]:
        events: list[str] = []
        for detection in detections:
            mode = detection.mode
            for counter in counters:
                event = counter.update(
                    detection,
                    video.id,
                    position_ms,
                    video.timestamp_at(position_ms),
                )
                if event is not None:
                    if running_counts is not None:
                        direction_label = event.direction_label or (
                            "Enter" if event.direction == "A" else "Exit"
                        )
                        running_counts[(mode, direction_label)] += 1
                    events.append(
                        f"{event.direction_label.upper()}: {mode} ID {detection.track_id} | "
                        f"{event.line_name}"
                    )
        AnnotatedDateVideoBuilder._draw_detection_boxes(cv2, frame, detections)
        return events

    @staticmethod
    def _draw_detection_boxes(cv2, frame, detections: list[Detection]) -> None:
        height, width = frame.shape[:2]
        box_thickness, text_scale, text_thickness = AnnotatedDateVideoBuilder._annotation_style(
            width, height
        )
        for detection in detections:
            x1 = round(detection.box_x1 * width)
            y1 = round(detection.box_y1 * height)
            x2 = round(detection.box_x2 * width)
            y2 = round(detection.box_y2 * height)
            color = (70, 210, 100)
            cv2.rectangle(
                frame,
                (x1, y1),
                (x2, y2),
                (0, 0, 0),
                box_thickness + max(1, box_thickness // 2),
                cv2.LINE_AA,
            )
            cv2.rectangle(
                frame,
                (x1, y1),
                (x2, y2),
                color,
                box_thickness,
                cv2.LINE_AA,
            )
            AnnotatedDateVideoBuilder._detection_label(
                cv2,
                frame,
                f"{detection.mode} {detection.confidence:.2f} | ID {detection.track_id}",
                x1,
                y1,
                text_scale,
                text_thickness,
                color,
            )

    @staticmethod
    def _draw_lines(cv2, frame, video) -> None:
        height, width = frame.shape[:2]
        resize_scale = AnnotatedDateVideoBuilder._resize_scale(width, height)
        _box_thickness, text_scale, text_thickness = AnnotatedDateVideoBuilder._annotation_style(
            width, height
        )
        line_thickness = max(
            2,
            round(AnnotatedDateVideoBuilder.output_line_thickness / max(resize_scale, 0.01)),
        )
        # OpenCV uses BGR; this is the same #2563eb blue used in Line Setup.
        line_color = (235, 99, 37)
        for line in video.counting_lines:
            start = (round(line.x1 * width), round(line.y1 * height))
            end = (round(line.x2 * width), round(line.y2 * height))
            cv2.line(
                frame,
                start,
                end,
                (0, 0, 0),
                line_thickness + max(1, line_thickness // 2),
                cv2.LINE_AA,
            )
            cv2.line(
                frame,
                start,
                end,
                line_color,
                line_thickness,
                cv2.LINE_AA,
            )
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length = max((dx * dx + dy * dy) ** 0.5, 1.0)
            midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            normal = (-dy / length, dx / length)
            label_offset = round(52 / max(resize_scale, 0.01))
            for label, sign in (
                (line.direction_a_label.upper(), 1),
                (line.direction_b_label.upper(), -1),
            ):
                color = (
                    (91, 132, 22)
                    if label.casefold() == "enter"
                    else (38, 38, 220) if label.casefold() == "exit" else (105, 86, 71)
                )
                position = (
                    round(midpoint[0] + normal[0] * label_offset * sign),
                    round(midpoint[1] + normal[1] * label_offset * sign),
                )
                AnnotatedDateVideoBuilder._boxed_text(
                    cv2,
                    frame,
                    label,
                    position,
                    text_scale,
                    color,
                    text_thickness,
                    (255, 255, 255),
                )
            name_position = (
                round(start[0] + dx * 0.14),
                round(start[1] + dy * 0.14),
            )
            AnnotatedDateVideoBuilder._boxed_text(
                cv2,
                frame,
                line.name,
                name_position,
                text_scale,
                (15, 23, 42),
                text_thickness,
                line_color,
            )

    @staticmethod
    def _boxed_text(
        cv2,
        frame,
        text: str,
        center,
        scale: float,
        background_color,
        thickness: int,
        border_color,
    ) -> None:
        font = cv2.FONT_HERSHEY_SIMPLEX
        (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
        padding_x = max(7, thickness * 3)
        padding_y = max(5, thickness * 2)
        box_width = text_width + padding_x * 2
        box_height = text_height + baseline + padding_y * 2
        frame_height, frame_width = frame.shape[:2]
        left = round(center[0] - box_width / 2)
        top = round(center[1] - box_height / 2)
        left = max(0, min(left, frame_width - box_width - 1))
        top = max(0, min(top, frame_height - box_height - 1))
        right = min(frame_width - 1, left + box_width)
        bottom = min(frame_height - 1, top + box_height)
        cv2.rectangle(frame, (left, top), (right, bottom), background_color, -1)
        cv2.rectangle(
            frame,
            (left, top),
            (right, bottom),
            border_color,
            max(1, round(thickness / 2)),
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            text,
            (left + padding_x, top + padding_y + text_height),
            font,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

    @staticmethod
    def _detection_label(
        cv2,
        frame,
        text: str,
        box_x: int,
        box_y: int,
        scale: float,
        thickness: int,
        color,
    ) -> None:
        font = cv2.FONT_HERSHEY_SIMPLEX
        (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
        padding = max(5, thickness * 2)
        frame_height, frame_width = frame.shape[:2]
        left = max(0, min(box_x, frame_width - text_width - padding * 2))
        text_bottom = box_y - padding
        if text_bottom - text_height - baseline - padding < 0:
            text_bottom = min(frame_height - padding, box_y + text_height + padding * 2)
        top = max(0, text_bottom - text_height - baseline - padding)
        right = min(frame_width - 1, left + text_width + padding * 2)
        bottom = min(frame_height - 1, text_bottom + baseline + padding)
        cv2.rectangle(frame, (left, top), (right, bottom), (15, 23, 42), -1)
        cv2.rectangle(
            frame,
            (left, top),
            (right, bottom),
            color,
            max(1, thickness),
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            text,
            (left + padding, text_bottom),
            font,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

    @staticmethod
    def _draw_header(
        cv2,
        frame,
        video,
        position_ms,
        stride,
        file_index,
        file_count,
        events,
    ) -> None:
        timestamp = video.timestamp_at(position_ms).strftime("%Y-%m-%d %H:%M:%S")
        file_context = (
            f"file {file_index}/{file_count}" if file_count > 0 else "saved during detection"
        )
        header = f"FINAL QC | {timestamp} | {video.path.name} | {file_context} | stride {stride}"
        cv2.rectangle(frame, (0, 0), (frame.shape[1], 38), (15, 23, 42), -1)
        cv2.putText(
            frame,
            header,
            (12, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        for index, event in enumerate(events[:3]):
            y = 68 + index * 30
            AnnotatedDateVideoBuilder._outlined_text(
                cv2, frame, event, (12, y), 0.72, (40, 220, 255)
            )

    def _letterbox(self, np, cv2, frame):
        target_width, target_height = self.target_size
        height, width = frame.shape[:2]
        scale = min(target_width / max(width, 1), target_height / max(height, 1))
        resized = cv2.resize(
            frame,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        canvas = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        y = (target_height - resized.shape[0]) // 2
        x = (target_width - resized.shape[1]) // 2
        canvas[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
        return canvas

    def _gap_card(self, np, cv2, start: datetime, end: datetime):
        width, height = self.target_size
        card = np.zeros((height, width, 3), dtype=np.uint8)
        card[:] = (24, 30, 42)
        duration = str(end - start).split(".")[0]
        lines = [
            "NO RECORDING",
            f"Gap: {start:%Y-%m-%d %H:%M:%S} to {end:%Y-%m-%d %H:%M:%S}",
            f"Missing duration: {duration}",
        ]
        for index, text in enumerate(lines):
            scale = 1.3 if index == 0 else 0.8
            color = (80, 120, 240) if index == 0 else (235, 235, 235)
            size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)[0]
            position = ((width - size[0]) // 2, 290 + index * 70)
            cv2.putText(
                card,
                text,
                position,
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                color,
                2,
                cv2.LINE_AA,
            )
        return card

    @staticmethod
    def _outlined_text(cv2, frame, text, position, scale, color, thickness: int = 2) -> None:
        cv2.putText(
            frame,
            text,
            position,
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (0, 0, 0),
            thickness + 3,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            text,
            position,
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            thickness,
            cv2.LINE_AA,
        )
