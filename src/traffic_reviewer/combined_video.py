from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from traffic_reviewer.domain import VideoRecord


class CombinedVideoCancelled(Exception):
    pass


COMBINED_VIDEO_VERSION = 3


@dataclass(frozen=True)
class CombinedVideoRecord:
    path: Path
    day: date
    camera: str
    coverage_start: datetime
    coverage_end: datetime
    source_video_ids: tuple[int, ...]

    @property
    def duration_seconds(self) -> float:
        return (self.coverage_end - self.coverage_start).total_seconds()


def combined_video_path(
    database_path: Path,
    day: date,
    camera: str,
    start_hour: int,
    end_hour: int,
) -> Path:
    safe_camera = re.sub(r"[^A-Za-z0-9_-]+", "_", camera).strip("_") or "camera"
    return (
        Path(database_path).parent
        / "preprocessed"
        / (
            f"combined_{day.isoformat()}_{safe_camera}_{start_hour:02d}-{end_hour:02d}"
            f"_v{COMBINED_VIDEO_VERSION}.mp4"
        )
    )


def combined_video_manifest_path(output_path: Path) -> Path:
    return Path(output_path).with_suffix(".json")


def delete_combined_video_files(output_path: Path) -> tuple[Path, ...]:
    """Delete one generated combined MP4 and its manifest, never its source recordings."""
    output_path = Path(output_path)
    if (
        output_path.suffix.lower() != ".mp4"
        or not output_path.name.startswith("combined_")
        or output_path.parent.name != "preprocessed"
    ):
        raise ValueError("Only generated combined videos in the preprocessed folder can be deleted.")
    deleted = []
    for target in (output_path, combined_video_manifest_path(output_path)):
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        deleted.append(target)
    return tuple(deleted)


def combined_video_signature(
    recordings: list[VideoRecord],
    coverage_start: datetime,
    coverage_end: datetime,
) -> dict:
    sources = []
    for video in recordings:
        try:
            stat = video.path.stat()
            file_size = stat.st_size
            modified_ns = stat.st_mtime_ns
        except OSError:
            file_size = None
            modified_ns = None
        sources.append(
            {
                "id": video.id,
                "path": str(video.path.resolve()),
                "start": video.recorded_at.isoformat() if video.recorded_at else None,
                "end": video.recorded_end_at.isoformat() if video.recorded_end_at else None,
                "duration_seconds": video.duration_seconds,
                "fps": video.fps,
                "frame_count": video.frame_count,
                "file_size": file_size,
                "modified_ns": modified_ns,
            }
        )
    return {
        "version": COMBINED_VIDEO_VERSION,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "sources": sources,
    }


def combined_video_is_current(
    output_path: Path,
    recordings: list[VideoRecord],
    coverage_start: datetime,
    coverage_end: datetime,
) -> bool:
    output_path = Path(output_path)
    manifest_path = combined_video_manifest_path(output_path)
    if not output_path.is_file() or output_path.stat().st_size <= 0 or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return manifest == combined_video_signature(recordings, coverage_start, coverage_end)


def discover_combined_videos(
    database_path: Path,
    videos: list[VideoRecord],
) -> list[CombinedVideoRecord]:
    """Return current combined outputs whose source recordings still match the manifest."""
    video_by_id = {video.id: video for video in videos}
    directory = Path(database_path).parent / "preprocessed"
    combined: list[CombinedVideoRecord] = []
    for manifest_path in sorted(directory.glob("combined_*.json")):
        output_path = manifest_path.with_suffix(".mp4")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            coverage_start = datetime.fromisoformat(manifest["coverage_start"])
            coverage_end = datetime.fromisoformat(manifest["coverage_end"])
            source_ids = tuple(int(source["id"]) for source in manifest["sources"])
            recordings = [video_by_id[video_id] for video_id in source_ids]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not recordings or not combined_video_is_current(
            output_path, recordings, coverage_start, coverage_end
        ):
            continue
        cameras = {video.camera for video in recordings}
        if len(cameras) != 1:
            continue
        combined.append(
            CombinedVideoRecord(
                path=output_path,
                day=coverage_start.date(),
                camera=next(iter(cameras)),
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                source_video_ids=source_ids,
            )
        )
    return sorted(
        combined,
        key=lambda item: (item.day, item.camera.casefold(), item.coverage_start, item.path.name),
    )


class CombinedDateVideoBuilder:
    """Join verified camera-day fragments and insert short cards for missing intervals."""

    target_size = (1280, 720)
    gap_card_seconds = 2.0
    minimum_gap_for_card_seconds = 3.0
    progress_unit_bytes = 1024
    estimated_gap_card_bytes = 1024 * 1024

    def build(
        self,
        recordings: list[VideoRecord],
        output_path: Path,
        coverage_start: datetime,
        coverage_end: datetime,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> Path:
        if coverage_start >= coverage_end:
            raise ValueError("Expected end hour must be later than the start hour.")
        recordings = sorted(
            (video for video in recordings if video.recorded_at is not None),
            key=lambda video: (video.recorded_at, video.path.name),
        )
        if not recordings:
            raise ValueError("No timestamped recordings are available for this date and camera.")
        missing = [video.path.name for video in recordings if not video.path.is_file()]
        if missing:
            raise ValueError("Source video files could not be found: " + ", ".join(missing))

        output_fps = next((video.fps for video in recordings if video.fps > 0), 15.0)
        output_fps = min(max(float(output_fps), 1.0), 60.0)
        clips, _gap_intervals = self._build_timeline(
            recordings, coverage_start, coverage_end, output_fps
        )
        if not clips:
            raise ValueError("No recording footage overlaps the selected expected hours.")

        ffmpeg = self._ffmpeg_executable()
        fast_settings = self._fast_join_settings(clips)
        if ffmpeg is not None and fast_settings is not None:
            return self._build_fast_join(
                ffmpeg,
                recordings,
                clips,
                output_path,
                coverage_start,
                coverage_end,
                fast_settings,
                progress,
                should_cancel,
            )

        progress(
            0,
            1,
            "Fast joining is unavailable for these files; using compatibility mode",
        )
        return self._build_legacy(
            recordings,
            output_path,
            coverage_start,
            coverage_end,
            progress,
            should_cancel,
        )

    def _build_legacy(
        self,
        recordings: list[VideoRecord],
        output_path: Path,
        coverage_start: datetime,
        coverage_end: datetime,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> Path:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Video preprocessing requires OpenCV and NumPy.") from exc

        if coverage_start >= coverage_end:
            raise ValueError("Expected end hour must be later than the start hour.")
        recordings = sorted(
            (video for video in recordings if video.recorded_at is not None),
            key=lambda video: (video.recorded_at, video.path.name),
        )
        if not recordings:
            raise ValueError("No timestamped recordings are available for this date and camera.")
        missing = [video.path.name for video in recordings if not video.path.is_file()]
        if missing:
            raise ValueError("Source video files could not be found: " + ", ".join(missing))

        output_fps = next((video.fps for video in recordings if video.fps > 0), 15.0)
        output_fps = min(max(float(output_fps), 1.0), 60.0)
        gap_frames = max(1, round(output_fps * self.gap_card_seconds))
        clips, gap_intervals = self._build_timeline(
            recordings, coverage_start, coverage_end, output_fps
        )
        if not clips:
            raise ValueError("No recording footage overlaps the selected expected hours.")
        total_frames = sum(clip["output_frames"] for clip in clips) + (
            len(gap_intervals) * gap_frames
        )

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
            raise RuntimeError("Could not create the combined MP4 video.")

        completed = 0
        capture = None
        cursor = coverage_start
        try:
            progress(0, total_frames, "Preparing combined video")
            for clip_index, clip in enumerate(clips, start=1):
                if should_cancel():
                    raise CombinedVideoCancelled
                clip_start = clip["timeline_start"]
                if self._needs_gap_card(cursor, clip_start):
                    card = self._gap_card(np, cv2, cursor, clip_start)
                    for _ in range(gap_frames):
                        if should_cancel():
                            raise CombinedVideoCancelled
                        writer.write(card)
                        completed += 1
                        progress(completed, total_frames, "Adding recording gap notice")

                video = clip["video"]
                capture = cv2.VideoCapture(str(video.path))
                if not capture.isOpened():
                    raise ValueError(f"OpenCV could not open {video.path.name}")
                capture.set(cv2.CAP_PROP_POS_FRAMES, clip["start_frame"])
                source_fps = clip["source_fps"]
                source_index = 0
                written_for_clip = 0
                while source_index < clip["source_frames"]:
                    if should_cancel():
                        raise CombinedVideoCancelled
                    ok, frame = capture.read()
                    if not ok:
                        break
                    source_index += 1
                    expected_writes = min(
                        clip["output_frames"],
                        round(source_index * output_fps / source_fps),
                    )
                    resized = None
                    while written_for_clip < expected_writes:
                        if resized is None:
                            resized = self._letterbox(np, cv2, frame)
                        writer.write(resized)
                        written_for_clip += 1
                        completed += 1
                        progress(
                            min(completed, total_frames),
                            total_frames,
                            (f"Combining {clip_index}/{len(clips)}: {video.path.name}"),
                        )
                capture.release()
                capture = None
                cursor = max(cursor, clip["timeline_end"])

            if self._needs_gap_card(cursor, coverage_end):
                card = self._gap_card(np, cv2, cursor, coverage_end)
                for _ in range(gap_frames):
                    if should_cancel():
                        raise CombinedVideoCancelled
                    writer.write(card)
                    completed += 1
                    progress(completed, total_frames, "Adding final recording gap notice")

            writer.release()
            writer = None
            temporary_path.replace(output_path)
            manifest = combined_video_signature(recordings, coverage_start, coverage_end)
            combined_video_manifest_path(output_path).write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            progress(total_frames, total_frames, "Combined video complete")
            return output_path
        except Exception:
            if capture is not None:
                capture.release()
            if writer is not None:
                writer.release()
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _ffmpeg_executable() -> str | None:
        executable = shutil.which("ffmpeg")
        if executable is not None:
            return executable
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            return None

    @staticmethod
    def _fast_join_settings(clips: list[dict]) -> dict | None:
        """Return stream-copy settings when every source has compatible video parameters."""
        try:
            import cv2
        except ImportError:
            return None

        codec_aliases = {
            "hevc": ("hevc", "hevc_mp4toannexb", "libx265", "hvc1"),
            "hev1": ("hevc", "hevc_mp4toannexb", "libx265", "hvc1"),
            "hvc1": ("hevc", "hevc_mp4toannexb", "libx265", "hvc1"),
            "h264": ("h264", "h264_mp4toannexb", "libx264", "avc1"),
            "avc1": ("h264", "h264_mp4toannexb", "libx264", "avc1"),
        }
        expected_codec = None
        expected_size = None
        expected_fps = None
        settings = None
        for clip in clips:
            video = clip["video"]
            capture = cv2.VideoCapture(str(video.path))
            if not capture.isOpened():
                return None
            try:
                fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
            finally:
                capture.release()
            fourcc = "".join(chr((fourcc_value >> (8 * index)) & 0xFF) for index in range(4))
            fourcc = fourcc.rstrip("\x00").lower()
            current_settings = codec_aliases.get(fourcc)
            if current_settings is None:
                return None

            current_codec = current_settings[0]
            current_size = (int(video.width), int(video.height))
            current_fps = float(video.fps)
            if current_size[0] <= 0 or current_size[1] <= 0 or current_fps <= 0:
                return None
            if expected_codec is None:
                expected_codec = current_codec
                expected_size = current_size
                expected_fps = current_fps
                settings = current_settings
                continue
            if current_codec != expected_codec or current_size != expected_size:
                return None
        if settings is None:
            return None
        codec, bitstream_filter, card_encoder, mp4_tag = settings
        return {
            "codec": codec,
            "bitstream_filter": bitstream_filter,
            "card_encoder": card_encoder,
            "mp4_tag": mp4_tag,
            "width": expected_size[0],
            "height": expected_size[1],
            "fps": expected_fps,
        }

    def _build_fast_join(
        self,
        ffmpeg: str,
        recordings: list[VideoRecord],
        clips: list[dict],
        output_path: Path,
        coverage_start: datetime,
        coverage_end: datetime,
        settings: dict,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> Path:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Fast video preprocessing requires OpenCV and NumPy.") from exc

        sequence: list[dict] = []
        cursor = coverage_start
        for clip in clips:
            video = clip["video"]
            video_start = video.recorded_at
            video_end = video.recorded_end_at or video_start + timedelta(
                seconds=video.duration_seconds
            )
            if self._needs_gap_card(cursor, video_start):
                sequence.append({"kind": "gap", "start": cursor, "end": video_start})
            sequence.append(
                {
                    "kind": "source",
                    "video": video,
                    "duration": video.duration_seconds,
                    "work_units": self._work_units_for_bytes(video.path.stat().st_size),
                }
            )
            cursor = max(cursor, video_end)
        if self._needs_gap_card(cursor, coverage_end):
            sequence.append({"kind": "gap", "start": cursor, "end": coverage_end})

        gap_work_units = self._work_units_for_bytes(self.estimated_gap_card_bytes)
        sequence_work = sum(
            item["work_units"] if item["kind"] == "source" else gap_work_units
            for item in sequence
        )
        # FFmpeg copies the source bytes once into temporary transport-stream
        # segments, then copies those segments once more into the final MP4.
        total_work = max(1, sequence_work * 2)
        completed_work = 0

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(output_path.stem + ".partial.mp4")
        temporary_path.unlink(missing_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"{output_path.stem}_segments_",
                dir=output_path.parent,
            ) as temporary_directory:
                temporary_directory = Path(temporary_directory)
                segment_paths: list[Path] = []
                for index, item in enumerate(sequence, start=1):
                    if should_cancel():
                        raise CombinedVideoCancelled
                    segment_path = temporary_directory / f"segment_{index:04d}.ts"
                    segment_paths.append(segment_path)
                    if item["kind"] == "source":
                        video = item["video"]
                        work_units = int(item["work_units"])
                        stage = (
                            f"Fast joining {index}/{len(sequence)}: {video.path.name}"
                        )
                        self._run_ffmpeg(
                            ffmpeg,
                            [
                                "-i",
                                str(video.path),
                                "-map",
                                "0:v:0",
                                "-an",
                                "-c:v",
                                "copy",
                                "-bsf:v",
                                settings["bitstream_filter"],
                                "-muxdelay",
                                "0",
                                "-muxpreload",
                                "0",
                                "-f",
                                "mpegts",
                                "-y",
                                str(segment_path),
                            ],
                            work_units,
                            completed_work,
                            total_work,
                            stage,
                            progress,
                            should_cancel,
                        )
                        completed_work += work_units
                    else:
                        duration = self.gap_card_seconds
                        work_units = gap_work_units
                        card_path = temporary_directory / f"gap_{index:04d}.png"
                        card = self._gap_card(
                            np,
                            cv2,
                            item["start"],
                            item["end"],
                            size=(settings["width"], settings["height"]),
                        )
                        if not cv2.imwrite(str(card_path), card):
                            raise RuntimeError("Could not create a recording-gap card.")
                        stage = f"Creating gap card {index}/{len(sequence)}"
                        encoder_args = [
                            "-loop",
                            "1",
                            "-framerate",
                            f"{settings['fps']:.8f}",
                            "-i",
                            str(card_path),
                            "-t",
                            f"{duration:.3f}",
                            "-an",
                            "-c:v",
                            settings["card_encoder"],
                            "-preset",
                            "ultrafast",
                        ]
                        if settings["codec"] == "hevc":
                            encoder_args.extend(
                                [
                                    "-x265-params",
                                    "log-level=error:repeat-headers=1",
                                ]
                            )
                        else:
                            encoder_args.extend(
                                [
                                    "-x264-params",
                                    "repeat-headers=1",
                                ]
                            )
                        encoder_args.extend(
                            [
                                "-pix_fmt",
                                "yuv420p",
                                "-muxdelay",
                                "0",
                                "-muxpreload",
                                "0",
                                "-f",
                                "mpegts",
                                "-y",
                                str(segment_path),
                            ]
                        )
                        self._run_ffmpeg(
                            ffmpeg,
                            encoder_args,
                            work_units,
                            completed_work,
                            total_work,
                            stage,
                            progress,
                            should_cancel,
                        )
                        completed_work += work_units

                concat_path = temporary_directory / "segments.ffconcat"
                concat_lines = ["ffconcat version 1.0"]
                concat_lines.extend(
                    f"file '{self._escape_concat_path(path)}'" for path in segment_paths
                )
                concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
                self._run_ffmpeg(
                    ffmpeg,
                    [
                        "-fflags",
                        "+genpts",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat_path),
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        "-tag:v",
                        settings["mp4_tag"],
                        "-movflags",
                        "+faststart",
                        "-y",
                        str(temporary_path),
                    ],
                    sequence_work,
                    completed_work,
                    total_work,
                    "Finalizing combined video",
                    progress,
                    should_cancel,
                )

            if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
                raise RuntimeError("Fast joining did not create a valid combined video.")
            temporary_path.replace(output_path)
            manifest = combined_video_signature(recordings, coverage_start, coverage_end)
            combined_video_manifest_path(output_path).write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            progress(total_work, total_work, "Combined video complete")
            return output_path
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _run_ffmpeg(
        ffmpeg: str,
        arguments: list[str],
        expected_work_units: int,
        completed_work: int,
        total_work: int,
        stage: str,
        progress: Callable[[int, int, str], None],
        should_cancel: Callable[[], bool],
    ) -> None:
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            *arguments,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_tail: deque[str] = deque(maxlen=30)
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if line:
                        output_tail.append(line)
                    if should_cancel():
                        process.terminate()
                        raise CombinedVideoCancelled
                    if line.startswith("total_size="):
                        try:
                            current_bytes = int(line.split("=", 1)[1])
                        except ValueError:
                            continue
                        current_work = min(
                            CombinedDateVideoBuilder._work_units_for_bytes(
                                max(current_bytes, 0)
                            ),
                            expected_work_units,
                        )
                        progress(
                            min(
                                completed_work + current_work,
                                total_work,
                            ),
                            total_work,
                            stage,
                        )
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        if return_code != 0:
            details = "\n".join(output_tail)
            raise RuntimeError(
                "FFmpeg could not complete fast video joining."
                + (f"\n{details}" if details else "")
            )
        progress(
            min(completed_work + expected_work_units, total_work),
            total_work,
            stage,
        )

    @classmethod
    def _work_units_for_bytes(cls, byte_count: int) -> int:
        return max(1, math.ceil(byte_count / cls.progress_unit_bytes))

    @staticmethod
    def _escape_concat_path(path: Path) -> str:
        return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")

    @classmethod
    def _needs_gap_card(cls, start: datetime, end: datetime) -> bool:
        return (end - start).total_seconds() > cls.minimum_gap_for_card_seconds

    @staticmethod
    def _build_timeline(
        recordings: list[VideoRecord],
        coverage_start: datetime,
        coverage_end: datetime,
        output_fps: float,
    ) -> tuple[list[dict], list[tuple[datetime, datetime]]]:
        clips: list[dict] = []
        gaps: list[tuple[datetime, datetime]] = []
        cursor = coverage_start
        for video in recordings:
            video_start = video.recorded_at
            video_end = video.recorded_end_at or video_start + timedelta(
                seconds=video.duration_seconds
            )
            timeline_start = max(video_start, coverage_start, cursor)
            timeline_end = min(video_end, coverage_end)
            if timeline_start >= timeline_end:
                continue
            if CombinedDateVideoBuilder._needs_gap_card(cursor, timeline_start):
                gaps.append((cursor, timeline_start))
            source_fps = video.fps if video.fps > 0 else output_fps
            start_seconds = max(0.0, (timeline_start - video_start).total_seconds())
            clip_seconds = (timeline_end - timeline_start).total_seconds()
            start_frame = max(0, math.floor(start_seconds * source_fps))
            source_frames = max(1, math.ceil(clip_seconds * source_fps))
            if video.frame_count > 0:
                source_frames = min(source_frames, max(0, video.frame_count - start_frame))
            if source_frames <= 0:
                continue
            output_frames = max(1, round(source_frames * output_fps / source_fps))
            clips.append(
                {
                    "video": video,
                    "timeline_start": timeline_start,
                    "timeline_end": timeline_end,
                    "source_fps": source_fps,
                    "start_frame": start_frame,
                    "source_frames": source_frames,
                    "output_frames": output_frames,
                }
            )
            cursor = max(cursor, timeline_end)
        if CombinedDateVideoBuilder._needs_gap_card(cursor, coverage_end):
            gaps.append((cursor, coverage_end))
        return clips, gaps

    @classmethod
    def _letterbox(cls, np, cv2, frame):
        target_width, target_height = cls.target_size
        height, width = frame.shape[:2]
        scale = min(target_width / max(width, 1), target_height / max(height, 1))
        resized_width = max(1, round(width * scale))
        resized_height = max(1, round(height * scale))
        resized = cv2.resize(
            frame,
            (resized_width, resized_height),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
        canvas = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        x = (target_width - resized_width) // 2
        y = (target_height - resized_height) // 2
        canvas[y : y + resized_height, x : x + resized_width] = resized
        return canvas

    @classmethod
    def _gap_card(
        cls,
        np,
        cv2,
        start: datetime,
        end: datetime,
        size: tuple[int, int] | None = None,
    ):
        width, height = size or cls.target_size
        frame = np.full((height, width, 3), (28, 31, 38), dtype=np.uint8)
        duration = end - start
        lines = (
            "NO RECORDING",
            f"{start:%Y-%m-%d %H:%M:%S}  to  {end:%Y-%m-%d %H:%M:%S}",
            f"Gap duration: {duration}",
        )
        display_scale = min(width / cls.target_size[0], height / cls.target_size[1])
        sizes = (1.7 * display_scale, 0.85 * display_scale, 0.85 * display_scale)
        colors = ((80, 210, 255), (235, 235, 235), (190, 190, 190))
        y_positions = (round(height * 0.42), round(height * 0.52), round(height * 0.59))
        thickness = max(2, round(2 * display_scale))
        for text, scale, color, y in zip(lines, sizes, colors, y_positions, strict=True):
            (text_width, _), _ = cv2.getTextSize(
                text,
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                thickness,
            )
            cv2.putText(
                frame,
                text,
                ((width - text_width) // 2, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                color,
                thickness,
                cv2.LINE_AA,
            )
        return frame
