from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from traffic_reviewer.video import fitted_video_height, probe_video

PLAYBACK_RATES = (0.5, 1.0, 1.5, 2.0, 4.0)
_MAX_WIDGET_SIZE = 16_777_215
_WINDOWED_VIDEO_MAX_HEIGHT = 320


def _media_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class FullScreenVideoWidget(QVideoWidget):
    """Video canvas that stays compact in the page and expands only in full screen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame_aspect_ratio = 16 / 9
        self._windowed_mode = True
        self.setMinimumWidth(640)
        self.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._apply_windowed_height(self.width())

    def set_frame_size(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self._frame_aspect_ratio = width / height
        else:
            self._frame_aspect_ratio = 16 / 9
        self._apply_windowed_height(self.width())
        self.updateGeometry()

    def set_windowed_mode(self, enabled: bool) -> None:
        """Use a compact fixed-height canvas in the page; expand normally in full screen."""
        self._windowed_mode = bool(enabled)
        if self._windowed_mode:
            self.setMinimumWidth(640)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._apply_windowed_height(self.width())
        else:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(_MAX_WIDGET_SIZE, _MAX_WIDGET_SIZE)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.updateGeometry()

    def hasHeightForWidth(self) -> bool:
        return self._windowed_mode

    def heightForWidth(self, width: int) -> int:
        if not self._windowed_mode:
            return super().heightForWidth(width)
        # Keep the embedded player intentionally compact. When there is extra horizontal
        # room QVideoWidget preserves the source aspect ratio and shows black pillar bars,
        # instead of stretching the player downward to fill the whole page.
        fitted_height = fitted_video_height(width, self._frame_aspect_ratio)
        return min(fitted_height, _WINDOWED_VIDEO_MAX_HEIGHT)

    def sizeHint(self) -> QSize:
        if not self._windowed_mode:
            return super().sizeHint()
        width = max(640, self.width())
        return QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_windowed_height(event.size().width())

    def _apply_windowed_height(self, width: int) -> None:
        if not self._windowed_mode:
            return
        desired = self.heightForWidth(max(1, width))
        if self.minimumHeight() == desired and self.maximumHeight() == desired:
            return
        self.setFixedHeight(desired)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.setFullScreen(False)
            event.accept()
            return
        super().keyPressEvent(event)


class PlayerFullScreenWindow(QWidget):
    """Top-level full-screen host that keeps the video controls visible."""

    exitRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self._allow_close = False
        self.setWindowTitle("OSBA Traffic Counter — Video")

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._escape_shortcut.activated.connect(self.exitRequested.emit)

    def close_for_restore(self) -> None:
        self._allow_close = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.exitRequested.emit()


class QcVideoPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(640)
        self._duration = 0
        self._seeking = False
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.video = FullScreenVideoWidget(self)
        self.video.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.player.setVideoOutput(self.video)

        self.play_button = QPushButton("Play")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_playback)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.player.stop)
        self.speed = QComboBox()
        for rate in PLAYBACK_RATES:
            self.speed.addItem(f"{rate:g}×", rate)
        self.speed.setCurrentIndex(PLAYBACK_RATES.index(1.0))
        self.speed.setEnabled(False)
        self.speed.setToolTip("Choose video playback speed")
        self.speed.currentIndexChanged.connect(self._playback_rate_changed)
        self.full_screen_button = QPushButton("Full screen")
        self.full_screen_button.setEnabled(False)
        self.full_screen_button.setToolTip("Show the video and playback controls full screen; press Esc to exit")
        self.full_screen_button.clicked.connect(self._toggle_full_screen)
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.setEnabled(False)
        self.position.sliderPressed.connect(self._start_seek)
        self.position.sliderReleased.connect(self._finish_seek)
        self.position.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00:00 / 00:00:00")

        self.controls_widget = QWidget(self)
        self.controls_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        controls = QHBoxLayout(self.controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(QLabel("Speed"))
        controls.addWidget(self.speed)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(self.full_screen_button)
        self.controls_widget.setFixedHeight(self.controls_widget.sizeHint().height())

        # A real layout keeps the control bar physically attached to the bottom of the
        # video during every resize. The previous manual geometry calculation could leave
        # the video at its old height while moving the controls to a newly calculated
        # height, which created the large blank gap until the window was resized again.
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.video)
        self._layout.addWidget(self.controls_widget)
        self._full_screen_window: PlayerFullScreenWindow | None = None

        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._state_changed)

    def sizeHint(self) -> QSize:
        width = max(640, self.width())
        return QSize(
            width,
            self.video.heightForWidth(width) + self.controls_widget.sizeHint().height(),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self.video.isFullScreen():
            self.video._apply_windowed_height(event.size().width())
            self.updateGeometry()

    def set_video(self, path: Path) -> None:
        resolved_path = Path(path).resolve()
        self.player.stop()
        try:
            metadata = probe_video(resolved_path)
        except (OSError, ValueError):
            self.video.set_frame_size(16, 9)
        else:
            self.video.set_frame_size(metadata.width, metadata.height)
        self.player.setSource(QUrl.fromLocalFile(str(resolved_path)))
        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.speed.setEnabled(True)
        self.full_screen_button.setEnabled(True)
        self.position.setEnabled(True)
        self.play_button.setText("Play")
        self._playback_rate_changed()
        self.updateGeometry()

    def clear(self) -> None:
        if self._full_screen_window is not None:
            self._exit_full_screen()
        self.player.stop()
        self.player.setSource(QUrl())
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.speed.setEnabled(False)
        self.full_screen_button.setEnabled(False)
        self.position.setEnabled(False)
        self.position.setRange(0, 0)
        self.time_label.setText("00:00:00 / 00:00:00")
        self.video.set_frame_size(16, 9)
        self.updateGeometry()

    def _toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _playback_rate_changed(self, _index: int | None = None) -> None:
        rate = self.speed.currentData()
        self.player.setPlaybackRate(float(rate) if rate is not None else 1.0)

    def _toggle_full_screen(self) -> None:
        if self._full_screen_window is None:
            self._enter_full_screen()
        else:
            self._exit_full_screen()

    def _enter_full_screen(self) -> None:
        # Do not use QVideoWidget.setFullScreen() here. That makes only the video
        # surface full screen, so the shared playback controls are left behind on
        # the page. Instead, temporarily move both the video and its control bar
        # into one top-level full-screen window.
        if self._full_screen_window is not None:
            return

        window = PlayerFullScreenWindow(self.window())
        window.exitRequested.connect(self._exit_full_screen)
        self._full_screen_window = window

        self._layout.removeWidget(self.video)
        self._layout.removeWidget(self.controls_widget)
        self.video.set_windowed_mode(False)
        self.video.setParent(window)
        self.controls_widget.setParent(window)
        window.layout.addWidget(self.video, 1)
        window.layout.addWidget(self.controls_widget)

        self.full_screen_button.setText("Exit full screen")
        window.showFullScreen()
        window.raise_()
        window.activateWindow()
        self.video.setFocus()

    def _exit_full_screen(self) -> None:
        window = self._full_screen_window
        if window is None:
            return

        window.layout.removeWidget(self.video)
        window.layout.removeWidget(self.controls_widget)
        self.video.setParent(self)
        self.controls_widget.setParent(self)
        self._layout.addWidget(self.video)
        self._layout.addWidget(self.controls_widget)
        self.video.set_windowed_mode(True)
        self.full_screen_button.setText("Full screen")

        self._full_screen_window = None
        window.close_for_restore()
        window.deleteLater()
        self.updateGeometry()
        self.adjustSize()

    def _state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    def _start_seek(self) -> None:
        self._seeking = True

    def _finish_seek(self) -> None:
        self.player.setPosition(self.position.value())
        self._seeking = False

    def _position_changed(self, position: int) -> None:
        if not self._seeking:
            self.position.setValue(position)
        self.time_label.setText(f"{_media_time(position)} / {_media_time(self._duration)}")

    def _duration_changed(self, duration: int) -> None:
        self._duration = max(0, duration)
        self.position.setRange(0, self._duration)
        self._position_changed(self.player.position())
