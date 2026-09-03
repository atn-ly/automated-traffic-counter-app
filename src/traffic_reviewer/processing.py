from __future__ import annotations

import math
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from traffic_reviewer.database import ProjectRepository
from traffic_reviewer.domain import (
    CountEvent,
    CountingLine,
    Detection,
    DetectionZone,
    ReviewStatus,
    TimestampSource,
    VideoStatus,
)

MODEL_LABEL_TO_MODE = {
    "person": "Pedestrian",
    "pedestrian": "Pedestrian",
    "bicycle": "Bicycle",
    "bike": "Bicycle",
    "car": "Car",
    "truck": "Truck",
    "bus": "Bus",
    "motorcycle": "Motorcycle",
    "motorbike": "Motorcycle",
}

TRACKER_CONFIG_PATH = Path(__file__).with_name("osba_bytetrack.yaml")
DEFAULT_INFERENCE_IMAGE_SIZE = 960
DEFAULT_DETECTION_CONFIDENCE = 0.10
DEFAULT_MAX_DETECTIONS = 1000
UPPER_BODY_ASPECT_RATIO_LIMIT = 1.8
ESTIMATED_FULL_BODY_ASPECT_RATIO = 2.6
MAX_UPPER_BODY_EXTENSION_FACTOR = 1.25


def zone_pixel_points(
    zone: DetectionZone,
    frame_width: int,
    frame_height: int,
) -> tuple[tuple[float, float], ...]:
    """Convert normalized zone corners to clamped full-frame pixel coordinates."""
    return tuple(
        (
            min(max(float(x) * frame_width, 0.0), max(frame_width - 1, 0)),
            min(max(float(y) * frame_height, 0.0), max(frame_height - 1, 0)),
        )
        for x, y in zone.points
    )


def zone_pixel_bounds(
    zone: DetectionZone,
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int]:
    """Return the four-corner zone's safe enclosing pixel bounds."""
    points = zone_pixel_points(zone, frame_width, frame_height)
    x1 = max(0, min(frame_width - 1, math.floor(min(point[0] for point in points))))
    y1 = max(0, min(frame_height - 1, math.floor(min(point[1] for point in points))))
    x2 = max(x1 + 1, min(frame_width, math.ceil(max(point[0] for point in points))))
    y2 = max(y1 + 1, min(frame_height, math.ceil(max(point[1] for point in points))))
    return x1, y1, x2, y2


def point_in_polygon(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    """Return whether a point lies inside or on a polygon boundary."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) <= 1e-6 and min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6 and min(
            y1, y2
        ) - 1e-6 <= y <= max(y1, y2) + 1e-6:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def merge_full_frame_and_zone_boxes(
    full_boxes: list[list[float]],
    zone_boxes: list[list[float]],
    polygon: tuple[tuple[float, float], ...],
) -> list[list[float]]:
    """Use the enlarged crop inside its zone and full-frame detections elsewhere.

    Both passes can see an object near the crop boundary. Making the crop authoritative
    inside the four-corner zone removes that overlap before one tracker receives boxes.
    """
    def center_is_inside(box: list[float]) -> bool:
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        return point_in_polygon((center_x, center_y), polygon)

    return [box for box in full_boxes if not center_is_inside(box)] + [
        box for box in zone_boxes if center_is_inside(box)
    ]


def rectify_distant_zone(cv2, np, frame, zone: DetectionZone):
    """Straighten a four-corner zone and return its crop and inverse transform."""
    frame_height, frame_width = frame.shape[:2]
    source = np.asarray(
        zone_pixel_points(zone, frame_width, frame_height),
        dtype=np.float32,
    )
    output_width = max(
        2,
        round(
            max(
                np.linalg.norm(source[1] - source[0]),
                np.linalg.norm(source[2] - source[3]),
            )
        ),
    )
    output_height = max(
        2,
        round(
            max(
                np.linalg.norm(source[3] - source[0]),
                np.linalg.norm(source[2] - source[1]),
            )
        ),
    )
    destination = np.asarray(
        [
            (0, 0),
            (output_width - 1, 0),
            (output_width - 1, output_height - 1),
            (0, output_height - 1),
        ],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(source, destination)
    inverse_transform = cv2.getPerspectiveTransform(destination, source)
    crop = cv2.warpPerspective(frame, transform, (output_width, output_height))
    return crop, inverse_transform, tuple((float(x), float(y)) for x, y in source)


def map_rectified_boxes_to_frame(
    cv2,
    np,
    boxes: list[list[float]],
    inverse_transform,
    frame_width: int,
    frame_height: int,
) -> list[list[float]]:
    """Map tracker-ready boxes from the straightened crop into the original frame."""
    mapped_boxes: list[list[float]] = []
    for x1, y1, x2, y2, confidence, class_id in boxes:
        corners = np.asarray(
            [[(x1, y1), (x2, y1), (x2, y2), (x1, y2)]],
            dtype=np.float32,
        )
        mapped = cv2.perspectiveTransform(corners, inverse_transform)[0]
        mapped_x1 = max(0.0, min(float(point[0]) for point in mapped))
        mapped_y1 = max(0.0, min(float(point[1]) for point in mapped))
        mapped_x2 = min(float(frame_width - 1), max(float(point[0]) for point in mapped))
        mapped_y2 = min(float(frame_height - 1), max(float(point[1]) for point in mapped))
        if mapped_x2 > mapped_x1 and mapped_y2 > mapped_y1:
            mapped_boxes.append(
                [mapped_x1, mapped_y1, mapped_x2, mapped_y2, confidence, class_id]
            )
    return mapped_boxes


def side_of_line(x: float, y: float, line: CountingLine) -> float:
    """Signed 2D cross product for a point relative to a directed line."""
    return (line.x2 - line.x1) * (y - line.y1) - (line.y2 - line.y1) * (x - line.x1)


def crossing_direction(
    previous: tuple[float, float],
    current: tuple[float, float],
    line: CountingLine,
    endpoint_margin: float = 0.05,
) -> str | None:
    """Return the crossing direction when motion crosses the drawn segment.

    A small endpoint margin makes angled lines forgiving of box jitter at their visible
    endpoints while still rejecting crossings on a distant invisible extension.
    """
    previous_side = side_of_line(*previous, line)
    current_side = side_of_line(*current, line)
    if previous_side == 0 or current_side == 0 or previous_side * current_side >= 0:
        return None

    # A side change proves that the track crossed the infinite line. Find that
    # crossing point and also require it to fall between the endpoints the user
    # drew; otherwise a separate sidewalk can trigger the line's invisible
    # extension.
    crossing_fraction = previous_side / (previous_side - current_side)
    crossing_x = previous[0] + crossing_fraction * (current[0] - previous[0])
    crossing_y = previous[1] + crossing_fraction * (current[1] - previous[1])
    line_dx = line.x2 - line.x1
    line_dy = line.y2 - line.y1
    line_length_squared = line_dx * line_dx + line_dy * line_dy
    if line_length_squared <= 0:
        return None
    segment_fraction = (
        (crossing_x - line.x1) * line_dx + (crossing_y - line.y1) * line_dy
    ) / line_length_squared
    endpoint_margin = max(0.0, float(endpoint_margin))
    if not -endpoint_margin <= segment_fraction <= 1.0 + endpoint_margin:
        return None
    return "A" if previous_side < current_side else "B"


@dataclass
class _PendingCrossing:
    direction: str
    last_offset_ms: int
    point_indices: set[int] = field(default_factory=set)
    columns: set[int] = field(default_factory=set)


@dataclass
class _TrackGeometry:
    detection: Detection
    point_offset: tuple[float, float]
    stabilized_points: tuple[tuple[float, float], ...]


def _valid_detection_box(detection: Detection) -> bool:
    return detection.box_x2 > detection.box_x1 and detection.box_y2 > detection.box_y1


def partial_body_transition(previous: Detection, current: Detection) -> bool:
    """Return True when one track abruptly changes between body fragments.

    Occlusion can make YOLO alternate between a full person, an upper body, and a lower
    body while ByteTrack keeps the same ID. That changes the box geometry without the
    person making the same movement and must not be treated as a line crossing.
    """
    if (
        previous.mode != "Pedestrian"
        or current.mode != "Pedestrian"
        or not _valid_detection_box(previous)
        or not _valid_detection_box(current)
    ):
        return False

    previous_width = previous.box_x2 - previous.box_x1
    previous_height = previous.box_y2 - previous.box_y1
    current_width = current.box_x2 - current.box_x1
    current_height = current.box_y2 - current.box_y1
    height_ratio = max(previous_height, current_height) / max(
        min(previous_height, current_height), 1e-6
    )
    width_ratio = max(previous_width, current_width) / max(
        min(previous_width, current_width), 1e-6
    )
    previous_center_y = (previous.box_y1 + previous.box_y2) / 2
    current_center_y = (current.box_y1 + current.box_y2) / 2
    vertical_shift = abs(current_center_y - previous_center_y) / max(
        previous_height, current_height, 1e-6
    )
    return height_ratio >= 1.35 or width_ratio >= 1.50 or vertical_shift >= 0.35


def upper_body_ground_extension(detection: Detection) -> float:
    """Estimate the hidden lower-body distance for a short pedestrian box.

    A person box that contains only the upper body is much shorter relative to its width
    than a normal full-body pedestrian box. For those boxes only, project a conservative
    virtual ground position below the visible torso. The estimate is capped by the visible
    box height and the frame boundary so a wide pose cannot create an extreme anchor.
    """
    if detection.mode != "Pedestrian" or not _valid_detection_box(detection):
        return 0.0
    width = detection.box_x2 - detection.box_x1
    height = detection.box_y2 - detection.box_y1
    if height / max(width, 1e-6) >= UPPER_BODY_ASPECT_RATIO_LIMIT:
        return 0.0
    estimated_missing_height = max(
        0.0,
        ESTIMATED_FULL_BODY_ASPECT_RATIO * width - height,
    )
    return min(
        estimated_missing_height,
        MAX_UPPER_BODY_EXTENSION_FACTOR * height,
        max(0.0, 1.0 - detection.box_y2),
    )


@dataclass
class CrossingCounter:
    line: CountingLine
    last_points: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)
    counted_tracks: set[int] = field(default_factory=set)
    pending_crossings: dict[int, _PendingCrossing] = field(default_factory=dict)
    track_geometry: dict[int, _TrackGeometry] = field(default_factory=dict)
    side_deadband: float = 0.003
    support_window_ms: int = 1500

    @staticmethod
    def _point_column(point_index: int, point_count: int) -> int:
        """Group the six lower-body samples into left, centre, and right columns."""
        if point_count <= 1:
            return 0
        return (1, 0, 0, 1, 2, 2)[point_index]

    def _stabilize_counting_points(
        self,
        detection: Detection,
    ) -> tuple[tuple[tuple[float, float], ...], bool]:
        """Keep a continuous virtual body anchor through partial-box changes."""
        raw_points = detection.counting_points
        state = self.track_geometry.get(detection.track_id)
        reset_history = bool(state and partial_body_transition(state.detection, detection))

        if state is None:
            # A track can first appear while its legs are hidden by the frame edge, a
            # crowd, or an obstruction. Give that short pedestrian box a conservative
            # virtual ground point so it can still cross a line below the visible torso.
            point_offset = (0.0, upper_body_ground_extension(detection))
        elif reset_history:
            # Preserve the track's prior ground position when the visible body fragment
            # changes. Later motion of the partial box moves this virtual ground point,
            # but the one-frame resize/jump itself cannot create a crossing.
            previous_primary = state.stabilized_points[0]
            current_primary = raw_points[0]
            point_offset = (
                previous_primary[0] - current_primary[0],
                previous_primary[1] - current_primary[1],
            )
        else:
            point_offset = state.point_offset

        stabilized_points = tuple(
            (point[0] + point_offset[0], point[1] + point_offset[1])
            for point in raw_points
        )
        self.track_geometry[detection.track_id] = _TrackGeometry(
            detection=detection,
            point_offset=point_offset,
            stabilized_points=stabilized_points,
        )
        return stabilized_points, reset_history

    def update(
        self,
        detection: Detection,
        video_id: int,
        offset_ms: int,
        occurred_at,
    ) -> CountEvent | None:
        line_length = math.hypot(self.line.x2 - self.line.x1, self.line.y2 - self.line.y1)
        if line_length <= 0:
            return None
        if detection.track_id in self.counted_tracks:
            return None

        points, reset_history = self._stabilize_counting_points(detection)
        if reset_history:
            self.pending_crossings.pop(detection.track_id, None)
            for point_index, point in enumerate(points):
                self.last_points[(detection.track_id, point_index)] = point
            return None

        candidates: dict[str, list[int]] = {"A": [], "B": []}
        for point_index, current in enumerate(points):
            if abs(side_of_line(*current, self.line)) / line_length <= self.side_deadband:
                # Keep the last point that was clearly on one side. If a sampled
                # point lands directly on the line, the next point can still
                # complete the crossing instead of losing its prior side.
                continue
            key = (detection.track_id, point_index)
            previous = self.last_points.get(key)
            self.last_points[key] = current
            if previous is None:
                continue
            direction = crossing_direction(previous, current, self.line)
            if direction is not None:
                candidates[direction].append(point_index)

        if not candidates["A"] and not candidates["B"]:
            pending = self.pending_crossings.get(detection.track_id)
            if pending and offset_ms - pending.last_offset_ms > self.support_window_ms:
                self.pending_crossings.pop(detection.track_id, None)
            return None

        # A box changing shape at an angled line can make one corner cross even when the
        # person has not. Accept only a strict directional majority; an A/B tie is noise.
        if len(candidates["A"]) == len(candidates["B"]):
            self.pending_crossings.pop(detection.track_id, None)
            return None
        direction = "A" if len(candidates["A"]) > len(candidates["B"]) else "B"

        pending = self.pending_crossings.get(detection.track_id)
        if (
            pending is None
            or pending.direction != direction
            or offset_ms - pending.last_offset_ms > self.support_window_ms
        ):
            pending = _PendingCrossing(direction=direction, last_offset_ms=offset_ms)
            self.pending_crossings[detection.track_id] = pending
        pending.last_offset_ms = offset_ms
        pending.point_indices.update(candidates[direction])
        pending.columns.update(
            self._point_column(point_index, len(points))
            for point_index in candidates[direction]
        )

        # Box-free detections retain the original single ground-point behavior. Normal
        # YOLO boxes need support from two horizontal parts of the lower body. Support can
        # arrive over adjacent sampled frames, which preserves angled-line crossings.
        required_columns = 1 if len(points) == 1 else 2
        if len(pending.columns) < required_columns:
            return None

        self.pending_crossings.pop(detection.track_id, None)
        self.counted_tracks.add(detection.track_id)
        return CountEvent(
            video_id=video_id,
            offset_ms=offset_ms,
            occurred_at=occurred_at,
            track_id=detection.track_id,
            mode=detection.mode,
            direction=direction,
            direction_label=(
                self.line.direction_a_label if direction == "A" else self.line.direction_b_label
            ),
            confidence=detection.confidence,
            line_id=self.line.id,
            line_name=self.line.name,
        )


class ProcessingCancelled(Exception):
    pass


@dataclass
class DecodedFrameBatch:
    frames: list
    metadata: list[tuple[int, int]]


@dataclass
class InferredFrameBatch:
    items: list[tuple[object, int, int, list[Detection]]]


def resolve_inference_batch_size(
    requested_size: int,
    model_path: str = "",
    image_size: int = 640,
) -> int:
    """Resolve Auto using GPU memory, model scale, and inference resolution.

    Large models at 1280 pixels use much more VRAM than the old 640-pixel default. Auto
    therefore favors a stable batch over a large batch that can page memory or fail.
    """
    requested_size = int(requested_size)
    if requested_size > 0:
        return requested_size
    try:
        import torch

        if not torch.cuda.is_available():
            return 1
        total_memory = int(torch.cuda.get_device_properties(0).total_memory)
        memory_gb = total_memory / 1024**3
        model_stem = Path(model_path).stem.lower()
        is_large_model = model_stem.endswith(("l", "x"))
        is_medium_model = model_stem.endswith("m")
        image_size = max(320, int(image_size))

        if image_size >= 1280:
            if is_large_model:
                return 1 if memory_gb < 24 else 2
            if is_medium_model:
                return 2 if memory_gb < 16 else 4
            return 2 if memory_gb < 12 else 4
        if image_size >= 960:
            if is_large_model:
                return 2 if memory_gb < 24 else 4
            if is_medium_model:
                return 4 if memory_gb < 16 else 8
            return 4 if memory_gb < 12 else 8
        if is_large_model:
            return 4 if memory_gb < 24 else 8
        return 16 if total_memory >= 12 * 1024**3 else 8
    except ImportError:
        return 1
    except (AttributeError, RuntimeError):
        return 8


def resolve_inference_device() -> tuple[int | str, bool, str]:
    """Choose CUDA explicitly when available and enable fast FP16 inference."""
    try:
        import torch

        if not torch.cuda.is_available():
            return "cpu", False, "CPU"
        try:
            torch.backends.cudnn.benchmark = True
        except AttributeError:
            pass
        try:
            device_name = str(torch.cuda.get_device_name(0))
        except (AttributeError, RuntimeError):
            device_name = "NVIDIA GPU"
        return 0, True, f"CUDA · {device_name}"
    except ImportError:
        return "cpu", False, "CPU"
    except RuntimeError:
        return "cpu", False, "CPU"


def inference_precision_options(use_fp16: bool) -> dict[str, int]:
    """Return current Ultralytics precision arguments without deprecated warnings."""
    return {"quantize": 16} if use_fp16 else {}


class YoloVideoProcessor:
    """Pipeline decoding, inference, and ordered result output."""

    def __init__(
        self,
        repository: ProjectRepository,
        model_path: str = "yolo26s.pt",
        frame_stride: int = 3,
        batch_size: int = 100,
        inference_batch_size: int = 0,
        inference_image_size: int = DEFAULT_INFERENCE_IMAGE_SIZE,
    ):
        self.repository = repository
        self.model_path = model_path
        self.frame_stride = max(1, frame_stride)
        self.batch_size = max(1, batch_size)
        self.inference_image_size = max(320, int(inference_image_size))
        self.inference_batch_size = resolve_inference_batch_size(
            inference_batch_size,
            self.model_path,
            self.inference_image_size,
        )
        (
            self.inference_device,
            self.use_half_precision,
            self.accelerator_label,
        ) = resolve_inference_device()

    def process(
        self,
        video_id: int,
        progress: Callable[[int, int], None],
        should_cancel: Callable[[], bool],
        annotated_output_path: Path | None = None,
        save_review_evidence: bool = True,
    ) -> int:
        try:
            import cv2
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "The YOLO backend is not installed. Run: pip install -e .[vision]"
            ) from exc

        video = self.repository.get_video(video_id)
        if not video.counting_lines:
            raise ValueError("Draw and save at least one counting line before processing")
        if (
            video.recorded_at is None
            or video.timestamp_source == TimestampSource.MISSING
            or (
                video.timestamp_source == TimestampSource.BURNED_IN_OCR
                and video.timestamp_confidence < 1
            )
        ):
            raise ValueError("Set or verify the recording start time before processing")
        if not video.path.exists():
            raise FileNotFoundError(video.path)

        capture = cv2.VideoCapture(str(video.path))
        if not capture.isOpened():
            raise ValueError("OpenCV could not open this recording")
        self.repository.clear_events(video_id)
        self.repository.set_video_status(video_id, VideoStatus.PROCESSING)
        counters = [CrossingCounter(line) for line in video.counting_lines]
        pending: list[CountEvent] = []
        total_saved = 0
        model = YOLO(self.model_path)
        model_names = (
            model.names.items() if isinstance(model.names, dict) else enumerate(model.names)
        )
        class_modes = {
            int(class_id): MODEL_LABEL_TO_MODE[str(label).strip().lower()]
            for class_id, label in model_names
            if str(label).strip().lower() in MODEL_LABEL_TO_MODE
        }
        selected_modes = set(self.repository.get_selected_modes())
        selected_class_ids = [
            class_id for class_id, mode in class_modes.items() if mode in selected_modes
        ]
        supported_modes = {class_modes[class_id] for class_id in selected_class_ids}
        unsupported_modes = sorted(selected_modes - supported_modes)
        if unsupported_modes:
            raise ValueError(
                "The selected model does not contain these object classes: "
                + ", ".join(unsupported_modes)
                + ". Clear them on Home or choose a custom model with those classes."
            )
        if not selected_class_ids:
            raise ValueError("None of the selected object types exist in this model")
        distant_zone_tracker = None
        tracker_boxes_type = None
        tracker_np = None
        if video.distant_detection_zone is not None:
            import numpy as tracker_numpy
            from ultralytics.engine.results import Boxes

            tracker_np = tracker_numpy
            tracker_boxes_type = Boxes
            distant_zone_tracker = self._create_distant_zone_tracker()
        annotation_writer = None
        annotation_temporary_path: Path | None = None
        annotation_builder = None
        np = None
        decode_queue: queue.Queue[object] = queue.Queue(maxsize=2)
        output_queue: queue.Queue[object] = queue.Queue(maxsize=2)
        pipeline_errors: queue.Queue[Exception] = queue.Queue()
        pipeline_stop = threading.Event()
        decode_complete = object()
        output_complete = object()
        decode_thread: threading.Thread | None = None
        output_thread: threading.Thread | None = None

        try:
            if annotated_output_path is not None:
                import numpy as np_module

                from traffic_reviewer.annotated_video import AnnotatedDateVideoBuilder

                np = np_module
                annotation_builder = AnnotatedDateVideoBuilder(self.repository)
                annotated_output_path = Path(annotated_output_path)
                annotated_output_path.parent.mkdir(parents=True, exist_ok=True)
                annotation_temporary_path = annotated_output_path.with_name(
                    annotated_output_path.stem + ".partial.mp4"
                )
                annotation_temporary_path.unlink(missing_ok=True)
                output_fps = max(
                    0.1,
                    (video.fps if video.fps > 0 else 15.0) / self.frame_stride,
                )
                annotation_writer = cv2.VideoWriter(
                    str(annotation_temporary_path),
                    cv2.VideoWriter_fourcc(*"mp4v"),
                    output_fps,
                    annotation_builder.target_size,
                )
                if not annotation_writer.isOpened():
                    raise RuntimeError(
                        f"Could not create cached annotated video for {video.path.name}"
                    )

            def record_pipeline_error(error: Exception) -> None:
                if pipeline_errors.empty():
                    pipeline_errors.put(error)
                pipeline_stop.set()

            def put_while_running(target: queue.Queue[object], item: object) -> bool:
                while not pipeline_stop.is_set():
                    if should_cancel():
                        record_pipeline_error(ProcessingCancelled())
                        return False
                    try:
                        target.put(item, timeout=0.1)
                        return True
                    except queue.Full:
                        continue
                return False

            def raise_pipeline_error() -> None:
                try:
                    error = pipeline_errors.get_nowait()
                except queue.Empty:
                    return
                raise error

            def decode_frames() -> None:
                frame_index = 0
                frames: list = []
                metadata: list[tuple[int, int]] = []
                try:
                    while not pipeline_stop.is_set():
                        if should_cancel():
                            raise ProcessingCancelled
                        ok, frame = capture.read()
                        if not ok:
                            break
                        source_frame_index = frame_index
                        frame_index += 1
                        if source_frame_index % self.frame_stride:
                            continue

                        position_ms = int(capture.get(cv2.CAP_PROP_POS_MSEC) or 0)
                        if position_ms <= 0 and video.frame_count > 1:
                            position_ms = int(
                                source_frame_index
                                / (video.frame_count - 1)
                                * video.duration_seconds
                                * 1000
                            )
                        frames.append(frame)
                        metadata.append((source_frame_index, position_ms))
                        if len(frames) >= self.inference_batch_size:
                            if not put_while_running(
                                decode_queue,
                                DecodedFrameBatch(frames, metadata),
                            ):
                                return
                            frames = []
                            metadata = []

                    if frames and not put_while_running(
                        decode_queue,
                        DecodedFrameBatch(frames, metadata),
                    ):
                        return
                    put_while_running(decode_queue, decode_complete)
                except Exception as exc:
                    record_pipeline_error(exc)

            def output_results() -> None:
                nonlocal total_saved
                try:
                    while not pipeline_stop.is_set():
                        if should_cancel():
                            raise ProcessingCancelled
                        try:
                            inferred_batch = output_queue.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if inferred_batch is output_complete:
                            return
                        if not isinstance(inferred_batch, InferredFrameBatch):
                            raise RuntimeError("The output pipeline received an invalid batch")

                        for (
                            frame,
                            source_frame_index,
                            position_ms,
                            frame_detections,
                        ) in inferred_batch.items:
                            if should_cancel():
                                raise ProcessingCancelled
                            frame_event_labels: list[str] = []
                            for detection in frame_detections:
                                for counter in counters:
                                    event = counter.update(
                                        detection,
                                        video_id,
                                        position_ms,
                                        video.timestamp_at(position_ms),
                                    )
                                    if event is None:
                                        continue
                                    if save_review_evidence:
                                        evidence_path = self._save_evidence(
                                            frame,
                                            detection,
                                            event,
                                            counter.line,
                                        )
                                        event = replace(
                                            event,
                                            evidence_path=str(evidence_path),
                                        )
                                    else:
                                        event = replace(
                                            event,
                                            status=ReviewStatus.ACCEPTED,
                                        )
                                    pending.append(event)
                                    frame_event_labels.append(
                                        f"{event.direction_label.upper()}: {detection.mode} "
                                        f"ID {detection.track_id} | {event.line_name}"
                                    )

                            if annotation_writer is not None and annotation_builder is not None:
                                annotation_builder._draw_lines(cv2, frame, video)
                                annotation_builder._draw_detection_boxes(
                                    cv2,
                                    frame,
                                    frame_detections,
                                )
                                annotation_builder._draw_header(
                                    cv2,
                                    frame,
                                    video,
                                    position_ms,
                                    self.frame_stride,
                                    0,
                                    0,
                                    frame_event_labels,
                                )
                                annotation_writer.write(
                                    annotation_builder._letterbox(np, cv2, frame)
                                )

                            if len(pending) >= self.batch_size:
                                total_saved += self.repository.add_events(pending)
                                pending.clear()
                            progress(
                                min(source_frame_index, video.frame_count),
                                video.frame_count,
                            )
                except Exception as exc:
                    record_pipeline_error(exc)

            decode_thread = threading.Thread(
                target=decode_frames,
                name="osba-frame-decoder",
                daemon=True,
            )
            output_thread = threading.Thread(
                target=output_results,
                name="osba-result-writer",
                daemon=True,
            )
            decode_thread.start()
            output_thread.start()

            while True:
                if should_cancel():
                    raise ProcessingCancelled
                raise_pipeline_error()
                try:
                    decoded_batch = decode_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if decoded_batch is decode_complete:
                    break
                if not isinstance(decoded_batch, DecodedFrameBatch):
                    raise RuntimeError("The decode pipeline returned an invalid batch")
                try:
                    common_inference_kwargs = {
                        "verbose": False,
                        "classes": selected_class_ids,
                        "batch": len(decoded_batch.frames),
                        "imgsz": self.inference_image_size,
                        "conf": DEFAULT_DETECTION_CONFIDENCE,
                        "max_det": DEFAULT_MAX_DETECTIONS,
                        "device": self.inference_device,
                    }
                    common_inference_kwargs.update(
                        inference_precision_options(self.use_half_precision)
                    )
                    if video.distant_detection_zone is None:
                        results = model.track(
                            list(decoded_batch.frames),
                            persist=True,
                            tracker=str(TRACKER_CONFIG_PATH),
                            **common_inference_kwargs,
                        )
                        zone_results = None
                        zone_transforms = None
                    else:
                        results = model.predict(
                            list(decoded_batch.frames),
                            **common_inference_kwargs,
                        )
                        zone_transforms = [
                            rectify_distant_zone(
                                cv2,
                                tracker_np,
                                frame,
                                video.distant_detection_zone,
                            )
                            for frame in decoded_batch.frames
                        ]
                        crops = [transform[0] for transform in zone_transforms]
                        zone_results = model.predict(
                            crops,
                            **common_inference_kwargs,
                        )
                except RuntimeError as exc:
                    if "out of memory" in str(exc).lower():
                        raise RuntimeError(
                            "The GPU ran out of memory while processing a frame batch. "
                            "Choose a smaller inference batch or resolution on Detection "
                            "and try again."
                        ) from exc
                    raise
                if len(results) != len(decoded_batch.frames):
                    raise RuntimeError(
                        "YOLO returned a different number of results than input frames. "
                        "Choose inference batch 1 and try again."
                    )
                if zone_results is not None and len(zone_results) != len(decoded_batch.frames):
                    raise RuntimeError(
                        "YOLO returned a different number of distant-zone results than input "
                        "frames. Choose inference batch 1 and try again."
                    )

                inferred_items = []
                for index, (frame, (source_frame_index, position_ms), result) in enumerate(
                    zip(
                        decoded_batch.frames,
                        decoded_batch.metadata,
                        results,
                        strict=True,
                    )
                ):
                    frame_height, frame_width = frame.shape[:2]
                    if zone_results is None:
                        frame_detections = self._detections_from_result(
                            result,
                            class_modes,
                            frame_width,
                            frame_height,
                        )
                    else:
                        _crop, inverse_transform, polygon = zone_transforms[index]
                        full_boxes = self._raw_boxes_from_result(result)
                        crop_boxes = map_rectified_boxes_to_frame(
                            cv2,
                            tracker_np,
                            self._raw_boxes_from_result(zone_results[index]),
                            inverse_transform,
                            frame_width,
                            frame_height,
                        )
                        merged_boxes = merge_full_frame_and_zone_boxes(
                            full_boxes,
                            crop_boxes,
                            polygon,
                        )
                        box_array = tracker_np.asarray(merged_boxes, dtype=tracker_np.float32)
                        if box_array.size == 0:
                            box_array = tracker_np.empty((0, 6), dtype=tracker_np.float32)
                        tracked_boxes = distant_zone_tracker.update(
                            tracker_boxes_type(box_array, (frame_height, frame_width)),
                            frame,
                        )
                        frame_detections = self._detections_from_tracked_boxes(
                            tracked_boxes,
                            class_modes,
                            frame_width,
                            frame_height,
                        )
                    inferred_items.append(
                        (
                            frame,
                            source_frame_index,
                            position_ms,
                            frame_detections,
                        )
                    )

                if not put_while_running(
                    output_queue,
                    InferredFrameBatch(inferred_items),
                ):
                    raise_pipeline_error()
                    raise ProcessingCancelled

            if not put_while_running(output_queue, output_complete):
                raise_pipeline_error()
                raise ProcessingCancelled
            decode_thread.join()
            output_thread.join()
            raise_pipeline_error()

            total_saved += self.repository.add_events(pending)
            if annotation_writer is not None and annotation_temporary_path is not None:
                annotation_writer.release()
                annotation_writer = None
                annotation_temporary_path.replace(annotated_output_path)
            self.repository.set_video_status(video_id, VideoStatus.COMPLETE)
            progress(video.frame_count, video.frame_count)
            return total_saved
        except ProcessingCancelled:
            pipeline_stop.set()
            if decode_thread is not None and decode_thread.is_alive():
                decode_thread.join()
            if output_thread is not None and output_thread.is_alive():
                output_thread.join()
            total_saved += self.repository.add_events(pending)
            self.repository.set_video_status(video_id, VideoStatus.CANCELLED)
            if annotation_writer is not None:
                annotation_writer.release()
                annotation_writer = None
            if annotation_temporary_path is not None:
                annotation_temporary_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            pipeline_stop.set()
            if decode_thread is not None and decode_thread.is_alive():
                decode_thread.join()
            if output_thread is not None and output_thread.is_alive():
                output_thread.join()
            self.repository.set_video_status(video_id, VideoStatus.FAILED, str(exc))
            if annotation_writer is not None:
                annotation_writer.release()
                annotation_writer = None
            if annotation_temporary_path is not None:
                annotation_temporary_path.unlink(missing_ok=True)
            raise
        finally:
            pipeline_stop.set()
            if decode_thread is not None and decode_thread.is_alive():
                decode_thread.join()
            if output_thread is not None and output_thread.is_alive():
                output_thread.join()
            if annotation_writer is not None:
                annotation_writer.release()
            capture.release()

    @staticmethod
    def _raw_boxes_from_result(
        result,
        offset_x: int = 0,
        offset_y: int = 0,
    ) -> list[list[float]]:
        """Return tracker-ready xyxy/conf/class rows, optionally mapped from a crop."""
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        classes = boxes.cls.int().cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        corners = boxes.xyxy.cpu().tolist()
        return [
            [
                float(x1) + offset_x,
                float(y1) + offset_y,
                float(x2) + offset_x,
                float(y2) + offset_y,
                float(confidence),
                float(class_id),
            ]
            for class_id, confidence, (x1, y1, x2, y2) in zip(
                classes,
                confidences,
                corners,
                strict=True,
            )
        ]

    @staticmethod
    def _create_distant_zone_tracker():
        from ultralytics.trackers.byte_tracker import BYTETracker
        from ultralytics.utils import IterableSimpleNamespace, YAML

        settings = IterableSimpleNamespace(**YAML.load(TRACKER_CONFIG_PATH))
        return BYTETracker(settings)

    @staticmethod
    def _detections_from_tracked_boxes(
        tracked_boxes,
        class_modes: dict[int, str],
        frame_width: int,
        frame_height: int,
    ) -> list[Detection]:
        detections: list[Detection] = []
        for tracked in tracked_boxes:
            if len(tracked) < 7:
                continue
            x1, y1, x2, y2, track_id, confidence, class_id = tracked[:7]
            mode = class_modes.get(int(class_id))
            if mode is None:
                continue
            detections.append(
                Detection(
                    track_id=int(track_id),
                    mode=mode,
                    confidence=float(confidence),
                    center_x=float(x1 + x2) / 2 / max(frame_width, 1),
                    center_y=float(y1 + y2) / 2 / max(frame_height, 1),
                    box_x1=float(x1) / max(frame_width, 1),
                    box_y1=float(y1) / max(frame_height, 1),
                    box_x2=float(x2) / max(frame_width, 1),
                    box_y2=float(y2) / max(frame_height, 1),
                )
            )
        return detections

    @staticmethod
    def _detections_from_result(
        result,
        class_modes: dict[int, str],
        frame_width: int,
        frame_height: int,
    ) -> list[Detection]:
        detections: list[Detection] = []
        boxes = result.boxes
        if boxes is None or boxes.id is None:
            return detections
        ids = boxes.id.int().cpu().tolist()
        classes = boxes.cls.int().cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        centers = boxes.xywh.cpu().tolist()
        corners = boxes.xyxy.cpu().tolist()
        for (
            track_id,
            class_id,
            confidence,
            (cx, cy, _width, _height),
            (x1, y1, x2, y2),
        ) in zip(ids, classes, confidences, centers, corners, strict=True):
            mode = class_modes.get(class_id)
            if mode is None:
                continue
            detections.append(
                Detection(
                    track_id=int(track_id),
                    mode=mode,
                    confidence=float(confidence),
                    center_x=float(cx) / max(frame_width, 1),
                    center_y=float(cy) / max(frame_height, 1),
                    box_x1=float(x1) / max(frame_width, 1),
                    box_y1=float(y1) / max(frame_height, 1),
                    box_x2=float(x2) / max(frame_width, 1),
                    box_y2=float(y2) / max(frame_height, 1),
                )
            )
        return detections

    def _save_evidence(
        self,
        frame,
        detection: Detection,
        event: CountEvent,
        counting_line: CountingLine,
    ) -> Path:
        import cv2

        height, width = frame.shape[:2]
        scale = min(1.0, 1280 / max(width, 1))
        if scale < 1:
            image = cv2.resize(
                frame,
                (round(width * scale), round(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            image = frame.copy()
        image_height, image_width = image.shape[:2]
        x1 = round(detection.box_x1 * image_width)
        y1 = round(detection.box_y1 * image_height)
        x2 = round(detection.box_x2 * image_width)
        y2 = round(detection.box_y2 * image_height)
        color = (90, 190, 70)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
        label = (
            f"{detection.mode} {detection.confidence:.2f} · {event.line_name} · "
            f"{event.direction_label}"
        )
        label_y = max(28, y1 - 10)
        cv2.putText(
            image,
            label,
            (max(4, x1), label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            label,
            (max(4, x1), label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )
        line_start = (
            round(counting_line.x1 * image_width),
            round(counting_line.y1 * image_height),
        )
        line_end = (
            round(counting_line.x2 * image_width),
            round(counting_line.y2 * image_height),
        )
        # OpenCV uses BGR; keep review evidence consistent with the blue setup/QC line.
        cv2.line(image, line_start, line_end, (235, 99, 37), 3, cv2.LINE_AA)

        directory = self.repository.database_path.parent / "evidence" / f"video_{event.video_id}"
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = event.occurred_at.strftime("%Y%m%d_%H%M%S_%f")
        line_token = event.line_id if event.line_id is not None else "legacy"
        path = directory / (
            f"{timestamp}_line_{line_token}_track_{event.track_id}_{event.mode.lower()}.jpg"
        )
        if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            raise OSError(f"Could not save review evidence to {path}")
        return path
