from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from traffic_reviewer.annotated_video import (
    AnnotatedDateVideoBuilder,
    AnnotatedVideoCancelled,
    annotated_fragment_path,
    annotated_video_path,
    cached_annotated_fragments_ready,
    recordings_for_date,
    remove_cached_annotated_fragments,
    resolve_detection_settings,
)
from traffic_reviewer.combined_video import CombinedDateVideoBuilder, CombinedVideoCancelled
from traffic_reviewer.database import ProjectRepository
from traffic_reviewer.processing import ProcessingCancelled, YoloVideoProcessor
from traffic_reviewer.timestamping import read_filename_clock
from traffic_reviewer.video import probe_video
from traffic_reviewer.weather import fetch_daily_weather


class VideoIntakeWorker(QObject):
    progress = Signal(int, int, str, str)
    completed = Signal(int, object, object, bool)
    finished = Signal()

    def __init__(self, database_path, paths: list[str]):
        super().__init__()
        self.database_path = database_path
        self.paths = [Path(path) for path in paths]
        self._cancel_requested = False

    @Slot()
    def run(self) -> None:
        failures: list[str] = []
        time_failures: list[str] = []
        added = 0
        total = len(self.paths)
        total_steps = total * 3
        try:
            repository = ProjectRepository(self.database_path)
            for index, path in enumerate(self.paths, start=1):
                if self._cancel_requested:
                    break
                first_step = (index - 1) * 3
                self.progress.emit(first_step, total_steps, path.name, "Reading video metadata")
                try:
                    metadata = probe_video(path)
                    if self._cancel_requested:
                        break
                    self.progress.emit(
                        first_step + 1,
                        total_steps,
                        path.name,
                        "Reading date and time from filename",
                    )
                    clock = None
                    try:
                        clock = read_filename_clock(path, metadata)
                    except Exception as exc:
                        time_failures.append(f"{path.name}: {exc}")
                    if self._cancel_requested:
                        break
                    self.progress.emit(first_step + 2, total_steps, path.name, "Saving recording")
                    repository.add_video(path, metadata, clock)
                    added += 1
                    stage = "Added"
                    if clock is None:
                        stage = "Added — timestamp needs verification"
                    self.progress.emit(index * 3, total_steps, path.name, stage)
                except Exception as exc:
                    failures.append(f"{path.name}: {exc}")
                    self.progress.emit(index * 3, total_steps, path.name, "Could not add")
        except Exception as exc:
            failures.append(f"Video intake stopped: {exc}")
        finally:
            self.completed.emit(added, failures, time_failures, self._cancel_requested)
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True


class WeatherWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, location: str, days: list[str]):
        super().__init__()
        self.location = location
        self.days = [datetime.fromisoformat(day).date() for day in days]
        self._cancel_requested = False

    @Slot()
    def run(self) -> None:
        try:
            result = fetch_daily_weather(self.location, self.days)
            if not self._cancel_requested:
                self.completed.emit(result)
        except Exception as exc:
            if not self._cancel_requested:
                self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True


class ProcessingWorker(QObject):
    progress = Signal(int, int)
    stage = Signal(str)
    assembly_progress = Signal(int, int, str)
    annotated_ready = Signal(object)
    annotated_warning = Signal(str)
    completed = Signal(int)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        database_path,
        video_ids: int | list[int],
        model_path: str,
        frame_stride: int,
        create_annotated_video: bool = False,
        save_review_snapshots: bool = True,
        inference_batch_size: int = 0,
        inference_image_size: int = 960,
    ):
        super().__init__()
        self.database_path = database_path
        self.video_ids = [video_ids] if isinstance(video_ids, int) else list(video_ids)
        self.model_path = model_path
        self.frame_stride = frame_stride
        self.create_annotated_video = bool(create_annotated_video)
        self.save_review_snapshots = bool(save_review_snapshots)
        self.inference_batch_size = int(inference_batch_size)
        self.inference_image_size = int(inference_image_size)
        self._cancel_requested = False

    @Slot()
    def run(self) -> None:
        try:
            repository = ProjectRepository(self.database_path)
            processor = YoloVideoProcessor(
                repository,
                model_path=self.model_path,
                frame_stride=self.frame_stride,
                inference_batch_size=self.inference_batch_size,
                inference_image_size=self.inference_image_size,
            )
            self.stage.emit(
                f"Starting {self.model_path} on "
                f"{getattr(processor, 'accelerator_label', 'CPU')}"
            )
            videos = [repository.get_video(video_id) for video_id in self.video_ids]
            selected_modes = repository.get_selected_modes()
            total_frames = sum(video.frame_count for video in videos)
            finished_frames = 0
            total_count = 0
            self.progress.emit(0, total_frames)
            for video in videos:
                if self._cancel_requested:
                    raise ProcessingCancelled
                remove_cached_annotated_fragments(self.database_path, video.id)
                repository.set_processing_settings(
                    video.id,
                    self.frame_stride,
                    self.model_path,
                    selected_modes,
                    self.inference_image_size,
                )
                base_frames = finished_frames
                process_args = (
                    video.id,
                    lambda current, _total, base=base_frames: self.progress.emit(
                        min(base + current, total_frames), total_frames
                    ),
                    lambda: self._cancel_requested,
                )
                if self.create_annotated_video:
                    total_count += processor.process(
                        *process_args,
                        annotated_output_path=annotated_fragment_path(
                            self.database_path, video.id, self.frame_stride
                        ),
                        save_review_evidence=self.save_review_snapshots,
                    )
                else:
                    total_count += processor.process(
                        *process_args,
                        save_review_evidence=self.save_review_snapshots,
                    )
                finished_frames += video.frame_count
                self.progress.emit(min(finished_frames, total_frames), total_frames)
            if self.create_annotated_video:
                outputs = self._assemble_ready_dates(repository, videos)
                if outputs:
                    self.annotated_ready.emit([str(path) for path in outputs])
            self.completed.emit(total_count)
        except (ProcessingCancelled, AnnotatedVideoCancelled):
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True

    def _assemble_ready_dates(self, repository, processed_videos) -> list[Path]:
        outputs: list[Path] = []
        affected = {
            (video.recording_day, video.camera)
            for video in processed_videos
            if video.recording_day is not None
        }
        all_videos = repository.list_videos()
        builder = AnnotatedDateVideoBuilder(repository)
        jobs = []
        for day, camera in sorted(affected, key=lambda value: (value[0], value[1])):
            recordings = recordings_for_date(all_videos, day, camera)
            try:
                stride, _model_path, _modes = resolve_detection_settings(recordings)
            except ValueError:
                continue
            if not cached_annotated_fragments_ready(self.database_path, recordings, stride):
                continue
            estimated_frames = sum(
                max(1, (video.frame_count + stride - 1) // stride)
                for video in recordings
            )
            jobs.append((day, camera, recordings, stride, max(1, estimated_frames)))

        total_assembly_frames = sum(job[4] for job in jobs)
        completed_assembly_frames = 0
        for job_index, (day, camera, recordings, stride, estimated_frames) in enumerate(
            jobs, start=1
        ):
            if self._cancel_requested:
                raise AnnotatedVideoCancelled
            job_label = f"annotated video {job_index}/{len(jobs)}"
            self.stage.emit(
                f"Assembling {job_label} for {day.isoformat()} · {camera}…"
            )
            day_start = datetime.combine(day, datetime.min.time())
            output = annotated_video_path(self.database_path, day, camera, stride)

            def report_assembly_progress(
                current,
                total,
                stage,
                base=completed_assembly_frames,
                weight=estimated_frames,
                label=job_label,
                combined_total=total_assembly_frames,
            ):
                ratio = min(max(current / total, 0.0), 1.0) if total else 0.0
                combined_current = base + round(ratio * weight)
                self.assembly_progress.emit(
                    min(combined_current, combined_total),
                    combined_total,
                    f"{stage} · {label}",
                )

            try:
                builder.build_from_cached_fragments(
                    recordings,
                    output,
                    stride,
                    day_start,
                    day_start + timedelta(days=1),
                    report_assembly_progress,
                    lambda: self._cancel_requested,
                )
            except AnnotatedVideoCancelled:
                raise
            except Exception as exc:
                self.annotated_warning.emit(
                    f"Detections completed, but the annotated video for "
                    f"{day.isoformat()} · {camera} could not be assembled: {exc}"
                )
                completed_assembly_frames += estimated_frames
                self.assembly_progress.emit(
                    completed_assembly_frames,
                    total_assembly_frames,
                    f"Skipped {job_label} after an assembly error",
                )
                continue
            outputs.append(output)
            completed_assembly_frames += estimated_frames
        return outputs


class AnnotatedVideoWorker(QObject):
    progress = Signal(int, int, str)
    completed = Signal(str)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        database_path,
        video_ids: list[int],
        output_path: str,
        model_path: str,
        frame_stride: int,
        coverage_start: str,
        coverage_end: str,
    ):
        super().__init__()
        self.database_path = database_path
        self.video_ids = list(video_ids)
        self.output_path = Path(output_path)
        self.model_path = model_path
        self.frame_stride = frame_stride
        self.coverage_start = datetime.fromisoformat(coverage_start)
        self.coverage_end = datetime.fromisoformat(coverage_end)
        self._cancel_requested = False

    @Slot()
    def run(self) -> None:
        try:
            repository = ProjectRepository(self.database_path)
            recordings = [repository.get_video(video_id) for video_id in self.video_ids]
            builder = AnnotatedDateVideoBuilder(repository)
            if cached_annotated_fragments_ready(self.database_path, recordings, self.frame_stride):
                output = builder.build_from_cached_fragments(
                    recordings,
                    self.output_path,
                    self.frame_stride,
                    self.coverage_start,
                    self.coverage_end,
                    lambda current, total, stage: self.progress.emit(current, total, stage),
                    lambda: self._cancel_requested,
                )
            else:
                output = builder.build(
                    recordings,
                    self.output_path,
                    self.model_path,
                    self.frame_stride,
                    self.coverage_start,
                    self.coverage_end,
                    lambda current, total, stage: self.progress.emit(current, total, stage),
                    lambda: self._cancel_requested,
                )
            self.completed.emit(str(output))
        except AnnotatedVideoCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True


class CombinedVideoWorker(QObject):
    progress = Signal(int, int, str)
    completed = Signal(str)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        database_path,
        video_ids: list[int],
        output_path: str,
        coverage_start: str,
        coverage_end: str,
    ):
        super().__init__()
        self.database_path = database_path
        self.video_ids = list(video_ids)
        self.output_path = Path(output_path)
        self.coverage_start = datetime.fromisoformat(coverage_start)
        self.coverage_end = datetime.fromisoformat(coverage_end)
        self._cancel_requested = False

    @Slot()
    def run(self) -> None:
        try:
            repository = ProjectRepository(self.database_path)
            recordings = [repository.get_video(video_id) for video_id in self.video_ids]
            output = CombinedDateVideoBuilder().build(
                recordings,
                self.output_path,
                self.coverage_start,
                self.coverage_end,
                lambda current, total, stage: self.progress.emit(current, total, stage),
                lambda: self._cancel_requested,
            )
            self.completed.emit(str(output))
        except CombinedVideoCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested = True
