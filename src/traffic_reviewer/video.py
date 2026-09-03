from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int


def fitted_video_height(width: int, aspect_ratio: float) -> int:
    """Return the exact canvas height needed to display a video without letterboxing."""
    return max(1, round(max(1, int(width)) / max(float(aspect_ratio), 0.01)))


def fitted_video_player_height(
    video_height: int,
    controls_height: int,
    spacing: int = 0,
    margins: int = 0,
) -> int:
    """Return the player height that keeps its controls directly under the video."""
    return (
        max(1, int(video_height))
        + max(0, int(controls_height))
        + max(0, int(spacing))
        + max(0, int(margins))
    )


def _probe_with_ffprobe(path: Path) -> VideoMetadata | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=width,height,avg_frame_rate,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = float(data["format"]["duration"])
        rate_text = stream.get("avg_frame_rate", "0/1")
        fps = float(Fraction(rate_text)) if rate_text != "0/0" else 0.0
        frame_count = int(stream.get("nb_frames") or round(duration * fps))
        return VideoMetadata(
            duration_seconds=duration,
            fps=fps,
            frame_count=frame_count,
            width=int(stream["width"]),
            height=int(stream["height"]),
        )
    except (KeyError, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def probe_video(path: Path) -> VideoMetadata:
    """Read container timing without using filesystem created/modified timestamps."""
    ffprobe_metadata = _probe_with_ffprobe(path)
    if ffprobe_metadata is not None:
        return ffprobe_metadata

    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("OpenCV could not open this video")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        capture.release()
    duration = frame_count / fps if fps > 0 else 0.0
    return VideoMetadata(duration, fps, frame_count, width, height)


def read_frame_at(path: Path, seconds: float):
    """Decode one BGR frame at a container-relative time."""
    import cv2

    executable = shutil.which("ffmpeg")
    if executable is not None:
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(max(0.0, seconds)),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        result = subprocess.run(command, capture_output=True, check=False, timeout=45)
        if result.returncode == 0 and result.stdout:
            import numpy as np

            decoded = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                return decoded

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("OpenCV could not open this video")
    try:
        if seconds <= 10:
            frame = None
            ok = False
            target_ms = max(0.0, seconds) * 1000
            while True:
                ok, frame = capture.read()
                if not ok or capture.get(cv2.CAP_PROP_POS_MSEC) >= target_ms:
                    break
        else:
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            duration = frame_count / fps if fps > 0 else 0
            ratio = min(seconds / duration, 1.0) if duration > 0 else 0
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_count * ratio) - 1))
            ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise ValueError("Could not decode a video frame")
    return frame


def _timestamp_overlay_score(frame) -> float:
    """Estimate whether the camera timestamp is visible in its top-left overlay band."""
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    crop = frame[: max(1, int(height * 0.08)), : max(1, int(width * 0.65))]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    bright = gray >= 210
    dark = (gray <= 70).astype(np.uint8)
    near_dark = cv2.dilate(dark, np.ones((3, 3), dtype=np.uint8)) > 0
    return float(np.mean(bright & near_dark))


def read_timestamp_preview_frame(path: Path, second_start: float):
    """Decode the earliest frame with a visible timestamp inside the requested second."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("OpenCV could not open this video")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        capture.release()
        raise ValueError("Video frame rate is unavailable")

    first_frame = max(0, int(round(max(0.0, second_start) * fps)))
    last_frame = max(first_frame, int((max(0.0, second_start) + 0.95) * fps))
    best_frame = None
    best_score = -1.0
    frame_index = 0
    try:
        while frame_index <= last_frame:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if frame_index >= first_frame:
                score = _timestamp_overlay_score(frame)
                if score > best_score:
                    best_frame = frame.copy()
                    best_score = score
                if score >= 0.03:
                    return frame
            frame_index += 1
    finally:
        capture.release()

    if best_frame is None:
        raise ValueError("Could not decode a timestamp preview frame")
    return best_frame


def read_preview(path: Path, position: float = 0.0):
    metadata = probe_video(path)
    return read_frame_at(path, metadata.duration_seconds * min(max(position, 0.0), 1.0))
