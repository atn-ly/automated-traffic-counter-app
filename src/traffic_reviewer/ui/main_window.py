from __future__ import annotations

import shutil
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QDateTimeAxis,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, QSize, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from traffic_reviewer import __version__
from traffic_reviewer.analytics import (
    MODES,
    available_days,
    build_camera_summary,
    build_coverage_segments,
    build_daily_summary,
    build_hourly_summary,
    find_recording_gaps,
)
from traffic_reviewer.annotated_video import (
    annotated_video_path,
    cached_annotated_fragments_ready,
    recordings_for_date,
    resolve_detection_settings,
    try_remove_annotated_video,
)
from traffic_reviewer.combined_video import (
    CombinedVideoRecord,
    combined_video_is_current,
    combined_video_path,
    delete_combined_video_files,
    discover_combined_videos,
)
from traffic_reviewer.database import ProjectRepository
from traffic_reviewer.domain import CountingLine, ReviewStatus, TimestampSource, VideoRecord
from traffic_reviewer.exporting import export_clean_csvs
from traffic_reviewer.progress_timing import format_clock, progress_time_text
from traffic_reviewer.project_management import ProjectCatalog, ProjectInfo
from traffic_reviewer.reporting import (
    generate_camera_comparison_html_report,
    generate_daily_html_report,
    generate_html_report,
)
from traffic_reviewer.timestamping import filename_rounding_offset_seconds
from traffic_reviewer.ui.coverage_timeline import CoverageTimeline
from traffic_reviewer.ui.qc_video_player import QcVideoPlayer
from traffic_reviewer.ui.video_preview import VideoPreview
from traffic_reviewer.video import read_preview, read_timestamp_preview_frame
from traffic_reviewer.weather_icons import weather_icon_svg
from traffic_reviewer.workers import (
    AnnotatedVideoWorker,
    CombinedVideoWorker,
    ProcessingWorker,
    VideoIntakeWorker,
    WeatherWorker,
)


def format_duration(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1

DETECTION_PRESETS = (
    ("Fast (Efficient)", "yolo26n.pt", 3, 640),
    ("Recommended (Balanced)", "yolo26s.pt", 3, 960),
    ("Slow (Maximum accuracy)", "yolo26x.pt", 3, 1280),
)


class EvidenceFrameLabel(QLabel):
    """Display a review snapshot without stretching its canvas vertically."""

    _PLACEHOLDER_HEIGHT = 300
    _FRAME_INSET = 2

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._source_pixmap = QPixmap()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(440)
        self.setFixedHeight(self._PLACEHOLDER_HEIGHT)

    def set_evidence_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = QPixmap(pixmap)
        self.setText("")
        self._fit_source_pixmap()

    def clear_evidence_pixmap(self) -> None:
        self._source_pixmap = QPixmap()
        self.setPixmap(QPixmap())
        self.setFixedHeight(self._PLACEHOLDER_HEIGHT)
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_source_pixmap()

    def _fit_source_pixmap(self) -> None:
        if self._source_pixmap.isNull() or self.width() <= self._FRAME_INSET:
            return
        content_width = max(1, self.width() - self._FRAME_INSET)
        content_height = round(
            content_width
            * self._source_pixmap.height()
            / max(self._source_pixmap.width(), 1)
        )
        desired_height = max(1, content_height + self._FRAME_INSET)
        if self.height() != desired_height:
            self.setFixedHeight(desired_height)
            self.updateGeometry()
        self.setPixmap(
            self._source_pixmap.scaled(
                content_width,
                content_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class SortableTableWidgetItem(QTableWidgetItem):
    """Table item that can sort by a typed value instead of its displayed text."""

    def __init__(self, text: str = "", sort_value=None):
        super().__init__(text)
        self.setData(SORT_ROLE, text.casefold() if sort_value is None else sort_value)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(SORT_ROLE)
        right = other.data(SORT_ROLE)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


def configure_sortable_table(table: QTableWidget, default_column: int) -> None:
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    table.setSortingEnabled(True)
    table.sortItems(default_column, Qt.SortOrder.AscendingOrder)


def begin_table_refresh(table: QTableWidget) -> tuple[int, Qt.SortOrder]:
    header = table.horizontalHeader()
    sort_state = (header.sortIndicatorSection(), header.sortIndicatorOrder())
    table.setSortingEnabled(False)
    return sort_state


def finish_table_refresh(table: QTableWidget, sort_state: tuple[int, Qt.SortOrder]) -> None:
    column, order = sort_state
    table.setSortingEnabled(True)
    if 0 <= column < table.columnCount():
        table.sortItems(column, order)


def table_row_identity(table: QTableWidget, row: int) -> int | None:
    item = table.item(row, 0) if 0 <= row < table.rowCount() else None
    value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
    return int(value) if value is not None else None


def table_row_key(table: QTableWidget, row: int):
    item = table.item(row, 0) if 0 <= row < table.rowCount() else None
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


def find_table_row_by_key(table: QTableWidget, identity) -> int | None:
    for row in range(table.rowCount()):
        if table_row_key(table, row) == identity:
            return row
    return None


def combined_row_key(video: CombinedVideoRecord) -> str:
    return f"combined:{video.path.resolve()}"


def find_table_row(table: QTableWidget, identity: int) -> int | None:
    for row in range(table.rowCount()):
        if table_row_identity(table, row) == identity:
            return row
    return None


class TimestampDialog(QDialog):
    def __init__(self, video: VideoRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit recording time")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        heading = QLabel(video.path.name)
        heading.setObjectName("sectionTitle")
        instructions = QLabel(
            "The filename supplies the recording time. Change it here only when the "
            "filename is incorrect."
        )
        instructions.setWordWrap(True)
        self.preview = VideoPreview()
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.preview.set_frame_margin(10)
        try:
            self.visible_offset_seconds = filename_rounding_offset_seconds(video.path)
        except ValueError:
            self.visible_offset_seconds = 0.0
        self.preview.set_bgr_frame(
            read_timestamp_preview_frame(video.path, self.visible_offset_seconds)
        )
        visible_time = video.recorded_at or datetime.now().replace(microsecond=0)
        if video.recorded_at is None and video.assigned_date is not None:
            visible_time = visible_time.replace(
                year=video.assigned_date.year,
                month=video.assigned_date.month,
                day=video.assigned_date.day,
            )
        self.timestamp = QDateTimeEdit(QDateTime(visible_time))
        self.timestamp.setDisplayFormat("yyyy-MM-dd hh:mm:ss AP")
        self.timestamp.setCalendarPopup(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(instructions)
        layout.addWidget(self.preview, 1)
        layout.addWidget(QLabel("Start"))
        layout.addWidget(self.timestamp)
        layout.addWidget(buttons)

    def recording_start(self) -> datetime:
        return self.timestamp.dateTime().toPython()


class MainWindow(QMainWindow):
    def __init__(
        self,
        project_path: Path,
        project_catalog: ProjectCatalog | None = None,
    ):
        super().__init__()
        self.project_path = Path(project_path)
        self.project_catalog = project_catalog or ProjectCatalog(
            self.project_path.parent,
            self.project_path,
        )
        self.repository = ProjectRepository(self.project_path)
        self._current_project = self.project_catalog.project_for_database(self.project_path)
        self.project_catalog.repair_project_video_paths(self._current_project)
        self._zoom_factor = self.repository.get_ui_zoom()
        self._videos: list[VideoRecord] = []
        self._detection_checked_video_ids: set[int | str] = set()
        self._active_video_id: int | None = None
        self._active_combined_video: CombinedVideoRecord | None = None
        self._combined_videos: list[CombinedVideoRecord] = []
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None
        self._intake_thread: QThread | None = None
        self._intake_worker: VideoIntakeWorker | None = None
        self._close_requested = False
        self._intake_started_at: float | None = None
        self._intake_current = 0
        self._intake_total = 0
        self._processing_started_at: float | None = None
        self._processing_phase = "idle"
        self._processing_current = 0
        self._processing_total = 0
        self._processing_detection_share = 1000
        self._processing_video_count = 0
        self._processing_save_review_snapshots = False
        self._processing_annotated_outputs: list[Path] = []
        self._processing_annotation_warnings: list[str] = []
        self._qc_video_thread: QThread | None = None
        self._qc_video_worker: AnnotatedVideoWorker | None = None
        self._qc_video_started_at: float | None = None
        self._qc_video_current = 0
        self._qc_video_total = 0
        self._qc_video_output: Path | None = None
        self._combined_video_thread: QThread | None = None
        self._combined_video_worker: CombinedVideoWorker | None = None
        self._combined_video_started_at: float | None = None
        self._combined_video_current = 0
        self._combined_video_total = 0
        self._combined_video_output: Path | None = None
        self._weather_thread: QThread | None = None
        self._weather_worker: WeatherWorker | None = None
        self._detection_check_anchor: int | None = None
        self._event_check_anchor: int | None = None
        self._updating_detection_checks = False
        self._updating_event_checks = False
        self._populating_video_table = False
        self._populating_line_video_table = False
        self._populating_detection_table = False
        self._populating_event_table = False

        self._update_project_title()
        self.resize(1320, 840)
        self._build_ui()
        self._progress_time_timer = QTimer(self)
        self._progress_time_timer.timeout.connect(self._refresh_progress_times)
        self._progress_time_timer.start(1000)
        self._apply_zoom()
        self._refresh_all()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(14)
        self.brand_logo = QLabel()
        self.brand_logo.setObjectName("brandLogo")
        self.brand_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_logo.setAccessibleName("District Whyte logo")
        logo_path = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "district_whyte_logo.png"
        )
        self._brand_logo_pixmap = QPixmap(str(logo_path))
        self._resize_brand_logo()
        header.addWidget(self.brand_logo, 0, Qt.AlignmentFlag.AlignVCenter)
        heading = QVBoxLayout()
        title = QLabel("OSBA Traffic Counter")
        title.setObjectName("title")
        subtitle = QLabel(
            "Multimodal traffic counting from video to report."
        )
        subtitle.setObjectName("subtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.menu_toggle_button = QPushButton("Hide menu")
        self.menu_toggle_button.setToolTip("Show or hide the left-side workflow menu (Ctrl+M)")
        self.menu_toggle_button.clicked.connect(self._toggle_side_menu)
        header.addWidget(self.menu_toggle_button)
        zoom_out = QPushButton("Zoom out")
        zoom_out.setToolTip("Make app text and controls smaller (Ctrl+-)")
        zoom_out.clicked.connect(lambda: self._change_zoom(-0.1))
        self.zoom_reset = QPushButton("100%")
        self.zoom_reset.setToolTip("Reset app zoom (Ctrl+0)")
        self.zoom_reset.clicked.connect(lambda: self._set_zoom(1.0))
        zoom_in = QPushButton("Zoom in")
        zoom_in.setToolTip("Make app text and controls larger (Ctrl+=)")
        zoom_in.clicked.connect(lambda: self._change_zoom(0.1))
        header.addWidget(zoom_out)
        header.addWidget(self.zoom_reset)
        header.addWidget(zoom_in)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(18)
        self.side_panel = QWidget()
        self.side_panel.setObjectName("sidePanel")
        self.side_panel.setMinimumWidth(240)
        self.side_panel.setMaximumWidth(290)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(8, 10, 8, 10)
        side_layout.setSpacing(8)
        side_shadow = QGraphicsDropShadowEffect(self.side_panel)
        side_shadow.setBlurRadius(18)
        side_shadow.setOffset(0, 3)
        side_shadow.setColor(QColor(15, 42, 67, 35))
        self.side_panel.setGraphicsEffect(side_shadow)

        self.navigation = QTreeWidget()
        self.navigation.setHeaderHidden(True)
        self.navigation.setRootIsDecorated(False)
        self.navigation.setExpandsOnDoubleClick(False)
        self.navigation.setIndentation(24)
        self.navigation.setIconSize(QSize(24, 24))
        self.navigation.setAnimated(True)
        self.navigation.setObjectName("sideNavigation")
        self.navigation.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.navigation.setVisible(True)
        side_layout.addWidget(self.navigation, 1)

        self.pages = QStackedWidget()
        self._page_indices: dict[str, int] = {}
        for key, page in (
            ("home", self._build_home_tab()),
            ("videos", self._build_videos_tab()),
            ("preprocessing", self._build_preprocessing_tab()),
            ("lines", self._build_line_tab()),
            ("counting", self._build_counting_landing_page()),
            ("detection", self._build_detection_tab()),
            ("review", self._build_review_tab()),
            ("quality", self._build_final_qc_tab()),
            ("reports", self._build_reports_tab()),
            ("camera_comparison", self._build_camera_comparison_tab()),
            ("daily_report", self._build_daily_report_tab()),
        ):
            self._page_indices[key] = self.pages.addWidget(page)

        self._navigation_items: dict[str, QTreeWidgetItem] = {}
        self._add_navigation_item("home", "Home", icon_glyph="⌂")
        self._add_navigation_item("videos", "1  Videos", icon_glyph="▶")
        self._add_navigation_item("preprocessing", "2  Preprocessing", icon_glyph="↻")
        self._add_navigation_item("lines", "3  Line Setup", icon_glyph="╱")
        counting_item = self._add_navigation_group("counting", "4  Counting", "#")
        self._add_navigation_item("detection", "Detection", counting_item, icon_glyph="●")
        self._add_navigation_item("review", "Review Results", counting_item, icon_glyph="✓")
        self._add_navigation_item("quality", "5  Quality Control", icon_glyph="◆")
        self._add_navigation_item("reports", "6  Camera Report", icon_glyph="▤")
        self._add_navigation_item(
            "camera_comparison", "7  Camera Comparison", icon_glyph="⇄"
        )
        self._add_navigation_item("daily_report", "8  Daily Trends", icon_glyph="↗")
        counting_item.setExpanded(False)
        self._set_navigation_group_arrow(counting_item)
        self.navigation.currentItemChanged.connect(self._navigation_changed)
        self.navigation.itemClicked.connect(self._navigation_item_clicked)
        self.navigation.itemExpanded.connect(self._set_navigation_group_arrow)
        self.navigation.itemCollapsed.connect(self._set_navigation_group_arrow)
        self.navigation.setCurrentItem(self._navigation_items["home"])

        self.sidebar_version_label = QLabel(f"OSBA Traffic Counter  ·  v{__version__}")
        self.sidebar_version_label.setObjectName("sidebarVersion")
        self.sidebar_version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(self.sidebar_version_label)

        page_container = QWidget()
        page_layout = QVBoxLayout(page_container)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(self.pages)
        self._content_container = page_container
        page_container.setMinimumSize(1000, 720)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page_container)
        self.content_scroll = scroll
        body.addWidget(self.side_panel)
        body.addWidget(scroll, 1)
        layout.addLayout(body, 1)
        self.setCentralWidget(root)

        open_action = QAction("Add videos", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._add_videos)
        self.addAction(open_action)

        zoom_in_action = QAction("Zoom in", self)
        zoom_in_action.setShortcut("Ctrl+=")
        zoom_in_action.triggered.connect(lambda: self._change_zoom(0.1))
        self.addAction(zoom_in_action)
        zoom_out_action = QAction("Zoom out", self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(lambda: self._change_zoom(-0.1))
        self.addAction(zoom_out_action)
        zoom_reset_action = QAction("Reset zoom", self)
        zoom_reset_action.setShortcut("Ctrl+0")
        zoom_reset_action.triggered.connect(lambda: self._set_zoom(1.0))
        self.addAction(zoom_reset_action)
        menu_toggle_action = QAction("Show or hide side menu", self)
        menu_toggle_action.setShortcut("Ctrl+M")
        menu_toggle_action.triggered.connect(self._toggle_side_menu)
        self.addAction(menu_toggle_action)

    def _add_navigation_item(
        self,
        key: str,
        label: str,
        parent: QTreeWidgetItem | None = None,
        page_key: str | None = None,
        icon_glyph: str | None = None,
    ) -> QTreeWidgetItem:
        owner = parent if parent is not None else self.navigation
        item = QTreeWidgetItem(owner, [label])
        item.setData(0, Qt.ItemDataRole.UserRole, page_key or key)
        if icon_glyph:
            item.setIcon(0, self._navigation_icon(icon_glyph))
        item.setSizeHint(0, QSize(0, 44 if parent is not None else 50))
        self._navigation_items[key] = item
        return item

    def _add_navigation_group(
        self, key: str, label: str, icon_glyph: str | None = None
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.navigation, [label])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, label)
        if icon_glyph:
            item.setIcon(0, self._navigation_icon(icon_glyph))
        item.setSizeHint(0, QSize(0, 50))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._navigation_items[key] = item
        return item

    @staticmethod
    def _navigation_icon(glyph: str) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e6eefb"))
        painter.drawRoundedRect(1, 1, 22, 22, 6, 6)
        painter.setPen(QColor("#2457a6"))
        font = QFont("Segoe UI Symbol", 10)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _set_navigation_group_arrow(item: QTreeWidgetItem) -> None:
        base_label = item.data(0, Qt.ItemDataRole.UserRole + 1) or item.text(0)
        arrow = "▾" if item.isExpanded() else "▸"
        item.setText(0, f"{arrow}  {base_label}")

    def _navigation_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())
            self._set_navigation_group_arrow(item)
        self._scroll_content_to_top()

    def _navigation_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        page_key = current.data(0, Qt.ItemDataRole.UserRole)
        if page_key in self._page_indices:
            self.pages.setCurrentIndex(self._page_indices[page_key])
            self._scroll_content_to_top()
        if page_key == "home" and hasattr(self, "project_table"):
            self._refresh_projects()

    def _navigate(self, key: str) -> None:
        item = self._navigation_items[key]
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)
            self._set_navigation_group_arrow(parent)
        self.navigation.setCurrentItem(item)
        page_key = item.data(0, Qt.ItemDataRole.UserRole)
        self.pages.setCurrentIndex(self._page_indices[page_key])
        self._scroll_content_to_top()

    def _scroll_content_to_top(self) -> None:
        scroll = getattr(self, "content_scroll", None)
        if scroll is None:
            return
        QTimer.singleShot(0, lambda: scroll.verticalScrollBar().setValue(0))

    def _build_home_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Project home")
        heading.setObjectName("sectionTitle")
        welcome = QLabel("Welcome to the OSBA Traffic Counter!")
        welcome.setWordWrap(True)
        intro = QLabel(
            "Choose an event project first, then move through Videos → Preprocessing → "
            "Line Setup → Counting → Quality Control → Camera Report → Camera Comparison → "
            "Daily Trends."
        )
        intro.setWordWrap(True)
        projects_group = QGroupBox("Projects")
        projects_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        projects_layout = QVBoxLayout(projects_group)
        projects_layout.setSpacing(8)
        self.current_project_label = QLabel()
        self.current_project_label.setWordWrap(False)
        self.current_project_label.setStyleSheet("background: transparent;")
        self.current_project_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        projects_layout.addWidget(self.current_project_label)
        project_actions = QHBoxLayout()
        create_project = QPushButton("Create project")
        create_project.setObjectName("primaryButton")
        create_project.clicked.connect(self._create_project)
        open_project = QPushButton("Open selected")
        open_project.clicked.connect(self._open_selected_project)
        rename_project = QPushButton("Rename selected")
        rename_project.setToolTip("Rename the project and its folder")
        rename_project.clicked.connect(self._rename_selected_project)
        show_project_folder = QPushButton("Show project folder")
        show_project_folder.clicked.connect(self._show_selected_project_folder)
        refresh_projects = QPushButton("Refresh list")
        refresh_projects.setToolTip(
            "Scan data/projects again after adding a project folder in File Explorer"
        )
        refresh_projects.clicked.connect(self._refresh_projects)
        self.move_default_project_button = QPushButton("Move Default Project…")
        self.move_default_project_button.setToolTip(
            "One-time migration of the old database and generated files into a project folder"
        )
        self.move_default_project_button.clicked.connect(self._move_default_project)
        project_actions.addWidget(create_project)
        project_actions.addWidget(open_project)
        project_actions.addWidget(rename_project)
        project_actions.addWidget(show_project_folder)
        project_actions.addWidget(refresh_projects)
        project_actions.addWidget(self.move_default_project_button)
        project_actions.addStretch()
        projects_layout.addLayout(project_actions)
        self.project_table = QTableWidget(0, 3)
        self.project_table.setHorizontalHeaderLabels(["Project", "Folder", "Status"])
        self.project_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.project_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.project_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.project_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.project_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.project_table.cellDoubleClicked.connect(
            lambda _row, _column: self._open_selected_project()
        )
        configure_sortable_table(self.project_table, 0)
        projects_layout.addWidget(self.project_table)
        project_note = QLabel(
            "Each project keeps its videos, counts, quality control files, and reports "
            "organized separately."
        )
        project_note.setWordWrap(True)
        project_note.setStyleSheet("background: transparent;")
        project_note.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        projects_layout.addWidget(project_note)
        self.home_overview = QLabel()
        self.home_overview.setObjectName("infoBanner")
        self.home_overview.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(welcome)
        layout.addWidget(intro)
        layout.addWidget(projects_group, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.home_overview)

        group = QGroupBox("Objects to detect")
        grid = QGridLayout(group)
        selected_modes = set(self.repository.get_selected_modes())
        self.mode_checkboxes: dict[str, QCheckBox] = {}
        for index, mode in enumerate(MODES):
            checkbox = QCheckBox(mode)
            checkbox.setChecked(mode in selected_modes)
            self.mode_checkboxes[mode] = checkbox
            grid.addWidget(checkbox, index // 3, index % 3)
        object_note = QLabel(
            "The standard YOLO model supports pedestrian, bicycle, car, truck, bus, and motorcycle."
        )
        object_note.setWordWrap(True)
        grid.addWidget(object_note, 2, 0, 1, 3)
        save_modes = QPushButton("Save detection choices")
        save_modes.setObjectName("primaryButton")
        save_modes.clicked.connect(self._save_detection_modes)
        grid.addWidget(save_modes, 3, 0, 1, 3)
        layout.addWidget(group)
        layout.addStretch()
        return page

    def _build_counting_landing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        heading = QLabel("Counting")
        heading.setObjectName("sectionTitle")
        intro = QLabel(
            "Turn selected traffic videos into multimodal crossing counts and review the "
            "results when needed."
        )
        intro.setWordWrap(True)

        cards = QHBoxLayout()
        cards.setSpacing(24)
        detection_card = self._build_counting_card(
            number="1",
            title="DETECTION",
            description=(
                "Select recordings and run\n"
                "multimodal traffic counting.\n\n"
                "Choose automatic acceptance or\n"
                "save snapshots for review."
            ),
            background="#16845b",
            hover="#0f6b49",
        )
        detection_card.setAccessibleName("Open Detection")
        detection_card.clicked.connect(lambda: self._navigate("detection"))
        review_card = self._build_counting_card(
            number="2",
            title="REVIEW RESULTS",
            description=(
                "Inspect saved snapshots.\n"
                "Accept, reject, or delete\n"
                "crossing results.\n\n"
                "Used when manual review\n"
                "was enabled."
            ),
            background="#2f62ad",
            hover="#245295",
        )
        review_card.setAccessibleName("Open Review Results")
        review_card.clicked.connect(lambda: self._navigate("review"))
        cards.addWidget(detection_card, 1)
        cards.addWidget(review_card, 1)

        layout.addWidget(heading)
        layout.addWidget(intro)
        layout.addSpacing(12)
        layout.addLayout(cards)
        layout.addStretch()
        return page

    @staticmethod
    def _build_counting_card(
        *,
        number: str,
        title: str,
        description: str,
        background: str,
        hover: str,
    ) -> QPushButton:
        card = QPushButton()
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setFixedHeight(350)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card.setStyleSheet(
            f"""
            QPushButton {{
                background: {background};
                border: 0;
                border-radius: 18px;
                padding: 0;
            }}
            QPushButton:hover {{
                background: {hover};
            }}
            QPushButton:pressed {{
                background: {hover};
                padding-top: 2px;
            }}
            """
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(34, 32, 34, 32)
        card_layout.setSpacing(16)

        number_label = QLabel(number)
        number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number_label.setFixedSize(58, 58)
        number_label.setStyleSheet(
            f"background: white; color: {background}; border-radius: 29px; "
            "font-size: 24px; font-weight: 750;"
        )
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(
            "background: transparent; color: white; font-size: 22px; font-weight: 750;"
        )
        description_label = QLabel(description)
        description_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        description_label.setWordWrap(True)
        description_label.setStyleSheet(
            "background: transparent; color: white; font-size: 16px;"
        )
        for label in (number_label, title_label, description_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        card_layout.addWidget(number_label, 0, Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(title_label)
        card_layout.addWidget(description_label, 1)
        return card

    def _toggle_side_menu(self) -> None:
        hide_menu = not self.side_panel.isHidden()
        self.side_panel.setHidden(hide_menu)
        self.menu_toggle_button.setText("Show menu" if hide_menu else "Hide menu")

    def _build_videos_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Videos")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        selection_note = QLabel(
            "Select one or more recordings to set the camera or date, edit a recording time, "
            "or remove recordings from the project. Click a column heading to sort."
        )
        selection_note.setWordWrap(True)
        layout.addWidget(selection_note)
        toolbar = QHBoxLayout()
        self.add_video_button = QPushButton("Add videos")
        self.add_video_button.setObjectName("primaryButton")
        self.add_video_button.clicked.connect(self._add_videos)
        time_button = QPushButton("Edit recording time")
        time_button.clicked.connect(self._edit_video_time)
        camera_button = QPushButton("Set camera for selected")
        camera_button.clicked.connect(self._set_camera)
        date_button = QPushButton("Set date for selected")
        date_button.clicked.connect(self._set_recording_date)
        delete_button = QPushButton("Delete selected")
        delete_button.setObjectName("dangerButton")
        delete_button.clicked.connect(self._delete_selected_videos)
        self.day_filter = QComboBox()
        self.day_filter.currentIndexChanged.connect(self._refresh_videos)
        toolbar.addWidget(self.add_video_button)
        toolbar.addWidget(time_button)
        toolbar.addWidget(camera_button)
        toolbar.addWidget(date_button)
        toolbar.addWidget(delete_button)
        toolbar.addStretch()
        toolbar.addWidget(QLabel("Date"))
        toolbar.addWidget(self.day_filter)
        layout.addLayout(toolbar)

        intake_status = QHBoxLayout()
        self.intake_progress = QProgressBar()
        self.intake_progress.setRange(0, 1)
        self.intake_progress.setValue(0)
        self.intake_progress.setFormat("Ready to add recordings")
        self.intake_time_label = QLabel("Elapsed 00:00:00 · Remaining —")
        self.intake_time_label.setMinimumWidth(245)
        self.intake_cancel_button = QPushButton("Cancel loading")
        self.intake_cancel_button.clicked.connect(self._cancel_video_intake)
        self.intake_cancel_button.setVisible(False)
        intake_status.addWidget(self.intake_progress, 1)
        intake_status.addWidget(self.intake_time_label)
        intake_status.addWidget(self.intake_cancel_button)
        layout.addLayout(intake_status)

        self.day_summary = QLabel()
        self.day_summary.setObjectName("infoBanner")
        self.day_summary.setWordWrap(True)
        layout.addWidget(self.day_summary)

        self.video_table = QTableWidget(0, 9)
        self.video_table.setHorizontalHeaderLabels(
            [
                "Date",
                "Recording",
                "Camera",
                "Start",
                "End",
                "Duration",
                "Time source",
                "Line",
                "Status",
            ]
        )
        self.video_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.video_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.video_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.video_table.verticalHeader().setVisible(False)
        self.video_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5, 6, 7, 8):
            self.video_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        configure_sortable_table(self.video_table, 0)
        layout.addWidget(self.video_table, 1)

        return page

    def _build_preprocessing_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Preprocessing")
        heading.setObjectName("sectionTitle")
        explanation = QLabel(
            "Choose one date and camera to review its source recordings and coverage. Build one "
            "clean daily video in timestamp order; short NO RECORDING cards mark gaps without "
            "creating hours of blank footage. Overlapping time is included once."
        )
        explanation.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(explanation)

        filters = QHBoxLayout()
        self.qc_day = QComboBox()
        self.qc_camera = QComboBox()
        self.qc_start_hour = QSpinBox()
        self.qc_start_hour.setRange(0, 23)
        self.qc_start_hour.setValue(0)
        self.qc_end_hour = QSpinBox()
        self.qc_end_hour.setRange(1, 24)
        self.qc_end_hour.setValue(24)
        self.qc_day.currentIndexChanged.connect(self._preprocessing_filter_changed)
        self.qc_camera.currentIndexChanged.connect(self._preprocessing_filter_changed)
        self.qc_start_hour.valueChanged.connect(self._preprocessing_filter_changed)
        self.qc_end_hour.valueChanged.connect(self._preprocessing_filter_changed)
        filters.addWidget(QLabel("Date"))
        filters.addWidget(self.qc_day)
        filters.addWidget(QLabel("Camera"))
        filters.addWidget(self.qc_camera)
        filters.addWidget(QLabel("Expected start hour"))
        filters.addWidget(self.qc_start_hour)
        filters.addWidget(QLabel("Expected end hour"))
        filters.addWidget(self.qc_end_hour)
        filters.addStretch()
        layout.addLayout(filters)

        source_group = QGroupBox("Source recordings for this date and camera")
        source_layout = QVBoxLayout(source_group)
        self.qc_video_table = QTableWidget(0, 6)
        self.qc_video_table.setHorizontalHeaderLabels(
            ["Recording", "Start", "End", "Duration", "FPS", "Frames"]
        )
        self.qc_video_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.qc_video_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.qc_video_table.verticalHeader().setVisible(False)
        self.qc_video_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, 6):
            self.qc_video_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.qc_video_table.setMinimumHeight(170)
        configure_sortable_table(self.qc_video_table, 1)
        source_layout.addWidget(self.qc_video_table)
        # Let the recordings table use any spare page height instead of leaving a
        # large empty white area below the compact video player.
        layout.addWidget(source_group, 1)

        gaps_heading = QLabel("Recording gaps")
        gaps_heading.setObjectName("subsectionTitle")
        layout.addWidget(gaps_heading)
        self.qc_summary = QLabel()
        self.qc_summary.setObjectName("infoBanner")
        self.qc_summary.setWordWrap(True)
        layout.addWidget(self.qc_summary)
        timeline_group = QGroupBox("Visual coverage timeline — hover for exact times")
        timeline_layout = QVBoxLayout(timeline_group)
        self.qc_timeline = CoverageTimeline()
        timeline_layout.addWidget(self.qc_timeline)
        layout.addWidget(timeline_group)
        self.qc_table = QTableWidget(0, 6)
        self.qc_table.setHorizontalHeaderLabels(
            ["Issue", "Gap start", "Gap end", "Duration", "Previous file", "Next file"]
        )
        self.qc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.qc_table.verticalHeader().setVisible(False)
        self.qc_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.qc_table.setMinimumHeight(150)
        # Share spare vertical room with the useful table content. The video player
        # itself remains compact and its controls stay attached directly underneath.
        layout.addWidget(self.qc_table, 1)

        combined_heading = QLabel("Combined daily video")
        combined_heading.setObjectName("subsectionTitle")
        layout.addWidget(combined_heading)
        combined_note = QLabel(
            "The combined video uses the recording times read from the filenames. It does "
            "not add detection boxes or counting lines."
        )
        combined_note.setWordWrap(True)
        layout.addWidget(combined_note)
        actions = QHBoxLayout()
        self.build_combined_video_button = QPushButton("Build combined video")
        self.build_combined_video_button.setObjectName("primaryButton")
        self.build_combined_video_button.clicked.connect(self._build_combined_video)
        self.cancel_combined_video_button = QPushButton("Cancel")
        self.cancel_combined_video_button.setEnabled(False)
        self.cancel_combined_video_button.clicked.connect(self._cancel_combined_video)
        self.download_combined_video_button = QPushButton("Download / save copy")
        self.download_combined_video_button.setEnabled(False)
        self.download_combined_video_button.clicked.connect(self._save_combined_video_copy)
        actions.addWidget(self.build_combined_video_button)
        actions.addWidget(self.cancel_combined_video_button)
        actions.addWidget(self.download_combined_video_button)
        actions.addStretch()
        layout.addLayout(actions)

        progress_row = QHBoxLayout()
        self.combined_video_progress = QProgressBar()
        self.combined_video_progress.setRange(0, 1000)
        self.combined_video_progress.setValue(0)
        self.combined_video_progress.setFormat("Ready")
        self.combined_video_time_label = QLabel("Elapsed 00:00:00 · Remaining —")
        self.combined_video_time_label.setMinimumWidth(245)
        progress_row.addWidget(self.combined_video_progress, 1)
        progress_row.addWidget(self.combined_video_time_label)
        layout.addLayout(progress_row)

        self.combined_video_player = QcVideoPlayer()
        layout.addWidget(self.combined_video_player, 0)
        return page

    def _build_line_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Line Setup")
        heading.setObjectName("sectionTitle")
        instructions = QLabel(
            "Select a recording from the list to load its preview here. Add a line for each "
            "counting location, then click its start and end points on the preview. The selected "
            "line is green and other saved lines are blue."
        )
        instructions.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(instructions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        recording_group = QGroupBox("Recordings")
        recording_layout = QVBoxLayout(recording_group)
        source_controls = QHBoxLayout()
        source_controls.addWidget(QLabel("Video list"))
        self.line_source_mode = QComboBox()
        self.line_source_mode.addItem("Original recordings", "original")
        self.line_source_mode.addItem("Combined daily videos", "combined")
        self.line_source_mode.currentIndexChanged.connect(self._line_source_changed)
        source_controls.addWidget(self.line_source_mode, 1)
        recording_layout.addLayout(source_controls)
        recording_note = QLabel(
            "Choose original recordings or combined daily videos, then click a row to preview "
            "and edit its counting lines. Lines saved on a combined video are applied to every "
            "source recording it contains."
        )
        recording_note.setWordWrap(True)
        recording_layout.addWidget(recording_note)
        self.line_video_table = QTableWidget(0, 5)
        self.line_video_table.setHorizontalHeaderLabels(
            ["Date", "Start", "Recording", "Camera", "Lines"]
        )
        self.line_video_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.line_video_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.line_video_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.line_video_table.itemSelectionChanged.connect(self._line_video_selection_changed)
        self.line_video_table.verticalHeader().setVisible(False)
        self.line_video_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        for column in (0, 1, 3, 4):
            self.line_video_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        configure_sortable_table(self.line_video_table, 0)
        recording_layout.addWidget(self.line_video_table, 1)
        recording_group.setMinimumWidth(360)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.line_heading = QLabel("Select a recording from the list")
        self.line_heading.setObjectName("sectionTitle")
        self.line_heading.setWordWrap(True)
        editor_layout.addWidget(self.line_heading)
        drawing_mode_controls = QHBoxLayout()
        drawing_mode_controls.addWidget(QLabel("Drawing mode"))
        self.draw_line_mode_button = QPushButton("Draw Line")
        self.draw_line_mode_button.setObjectName("lineModeButton")
        self.draw_line_mode_button.setCheckable(True)
        self.draw_line_mode_button.setChecked(True)
        self.draw_line_mode_button.setEnabled(False)
        self.draw_line_mode_button.clicked.connect(self._select_line_drawing_mode)
        self.draw_detection_zone_button = QPushButton("Draw Zone")
        self.draw_detection_zone_button.setObjectName("zoneModeButton")
        self.draw_detection_zone_button.setCheckable(True)
        self.draw_detection_zone_button.setEnabled(False)
        self.draw_detection_zone_button.clicked.connect(self._draw_distant_detection_zone)
        self.drawing_mode_group = QButtonGroup(self)
        self.drawing_mode_group.setExclusive(True)
        self.drawing_mode_group.addButton(self.draw_line_mode_button)
        self.drawing_mode_group.addButton(self.draw_detection_zone_button)
        self.drawing_mode_status = QLabel("Active mode: LINE")
        drawing_mode_controls.addWidget(self.draw_line_mode_button)
        drawing_mode_controls.addWidget(self.draw_detection_zone_button)
        drawing_mode_controls.addWidget(self.drawing_mode_status)
        drawing_mode_controls.addStretch()
        editor_layout.addLayout(drawing_mode_controls)
        line_controls = QHBoxLayout()
        line_controls.addWidget(QLabel("Counting line"))
        self.line_selector = QComboBox()
        self.line_selector.setMinimumWidth(180)
        self.line_selector.setEnabled(False)
        self.line_selector.currentIndexChanged.connect(self._line_selection_changed)
        self.add_line_button = QPushButton("Add line")
        self.add_line_button.setEnabled(False)
        self.add_line_button.clicked.connect(self._add_line)
        self.redraw_line_button = QPushButton("Redraw selected line")
        self.redraw_line_button.setEnabled(False)
        self.redraw_line_button.clicked.connect(self._redraw_selected_line)
        self.delete_line_button = QPushButton("Delete line")
        self.delete_line_button.setObjectName("dangerButton")
        self.delete_line_button.setEnabled(False)
        self.delete_line_button.clicked.connect(self._delete_line)
        line_controls.addWidget(self.line_selector)
        line_controls.addWidget(self.add_line_button)
        line_controls.addWidget(self.redraw_line_button)
        line_controls.addWidget(self.delete_line_button)
        line_controls.addStretch()
        editor_layout.addLayout(line_controls)
        zone_controls = QHBoxLayout()
        zone_controls.addWidget(QLabel("Distant detection zone"))
        self.clear_detection_zone_button = QPushButton("Clear zone")
        self.clear_detection_zone_button.setEnabled(False)
        self.clear_detection_zone_button.clicked.connect(self._clear_distant_detection_zone)
        self.detection_zone_status = QLabel("No zone")
        zone_controls.addWidget(self.clear_detection_zone_button)
        zone_controls.addWidget(self.detection_zone_status)
        zone_controls.addStretch()
        editor_layout.addLayout(zone_controls)
        zone_note = QLabel(
            "Optional: click four corners around an angled distant area where people are too "
            "small for normal detection. Include the counting line and approach space on both "
            "sides. The app straightens and enlarges that area, then uses one tracker."
        )
        zone_note.setWordWrap(True)
        editor_layout.addWidget(zone_note)
        self.swap_directions_button = QPushButton("Swap Enter / Exit")
        self.swap_directions_button.setEnabled(False)
        self.swap_directions_button.clicked.connect(self._swap_line_directions)
        direction_note = QLabel(
            "The ENTER and EXIT markers show which side an object finishes on. Use Swap Enter / "
            "Exit when those sides are reversed."
        )
        direction_note.setWordWrap(True)
        editor_layout.addWidget(direction_note)
        self.preview = VideoPreview()
        self.preview.setMinimumSize(520, 180)
        self.preview.set_fit_height_to_frame(True)
        self.preview.line_changed.connect(self._line_changed)
        self.preview.detection_zone_changed.connect(self._distant_detection_zone_changed)
        self.preview.drawing_mode_changed.connect(self._sync_drawing_mode_ui)
        editor_layout.addWidget(self.preview)
        actions = QHBoxLayout()
        self.line_status = QLabel("No line drawn")
        self.save_line_button = QPushButton("Save for this recording")
        self.save_line_button.setObjectName("primaryButton")
        self.save_line_button.setEnabled(False)
        self.save_line_button.clicked.connect(self._save_line)
        self.apply_day_line_button = QPushButton("Apply lines and zone to same camera and date")
        self.apply_day_line_button.setEnabled(False)
        self.apply_day_line_button.clicked.connect(self._apply_line_to_day)
        actions.addWidget(self.line_status)
        actions.addStretch()
        actions.addWidget(self.swap_directions_button)
        actions.addWidget(self.save_line_button)
        actions.addWidget(self.apply_day_line_button)
        editor_layout.addLayout(actions)
        editor_layout.addStretch()

        splitter.addWidget(recording_group)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([380, 820])
        layout.addWidget(splitter, 1)
        return page

    def _build_detection_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Detection")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        selection_group = QGroupBox("Choose recordings to process")
        selection_layout = QVBoxLayout(selection_group)
        source_controls = QHBoxLayout()
        source_controls.addWidget(QLabel("Video list"))
        self.detection_source_mode = QComboBox()
        self.detection_source_mode.addItem("Original recordings", "original")
        self.detection_source_mode.addItem("Combined daily videos", "combined")
        self.detection_source_mode.setToolTip(
            "Shows one camera-day row per combined video. Detection reads the original source "
            "frames behind that row so filename-based event times remain exact and gap cards "
            "are ignored."
        )
        self.detection_source_mode.currentIndexChanged.connect(self._detection_source_changed)
        source_controls.addWidget(self.detection_source_mode, 1)
        selection_layout.addLayout(source_controls)
        self.detection_selection_note = QLabel(
            "The checkboxes below are the complete processing list. Double-click any recording "
            "row to toggle it. To check a range, click the first row and Shift-click the last; "
            "the highlighted range and checkboxes stay synchronized. Click a column heading "
            "to change the sort order."
        )
        self.detection_selection_note.setWordWrap(True)
        selection_layout.addWidget(self.detection_selection_note)
        selection_controls = QHBoxLayout()
        check_selected = QPushButton("Check selected rows")
        check_selected.clicked.connect(self._check_selected_detection_rows)
        check_all = QPushButton("Check all")
        check_all.clicked.connect(lambda: self._set_all_detection_checks(True))
        clear_checks = QPushButton("Clear checks")
        clear_checks.clicked.connect(lambda: self._set_all_detection_checks(False))
        self.delete_selected_combined_button = QPushButton("Delete selected")
        self.delete_selected_combined_button.setObjectName("dangerButton")
        self.delete_selected_combined_button.setToolTip(
            "Delete highlighted generated combined daily videos. Original recordings and "
            "detection results are not deleted."
        )
        self.delete_selected_combined_button.clicked.connect(
            self._delete_selected_detection_combined_videos
        )
        self.delete_selected_combined_button.setVisible(False)
        self.video_check_summary = QLabel("0 recordings checked")
        selection_controls.addWidget(check_selected)
        selection_controls.addWidget(check_all)
        selection_controls.addWidget(clear_checks)
        selection_controls.addWidget(self.delete_selected_combined_button)
        selection_controls.addWidget(self.video_check_summary)
        selection_controls.addStretch()
        selection_layout.addLayout(selection_controls)

        self.detection_video_table = QTableWidget(0, 9)
        self.detection_video_table.setHorizontalHeaderLabels(
            [
                "Process",
                "Date",
                "Recording",
                "Camera",
                "Start",
                "End",
                "Duration",
                "Lines",
                "Readiness",
            ]
        )
        self.detection_video_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.detection_video_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.detection_video_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detection_video_table.itemChanged.connect(self._detection_video_check_item_changed)
        self.detection_video_table.itemSelectionChanged.connect(
            self._detection_video_selection_changed
        )
        self.detection_video_table.cellDoubleClicked.connect(
            self._toggle_detection_video_from_double_click
        )
        self.detection_video_table.verticalHeader().setVisible(False)
        self.detection_video_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        for column in (0, 1, 3, 4, 5, 6, 7, 8):
            self.detection_video_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        configure_sortable_table(self.detection_video_table, 1)
        self.detection_video_table.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_: setattr(self, "_detection_check_anchor", None)
        )
        selection_layout.addWidget(self.detection_video_table, 1)
        layout.addWidget(selection_group, 2)

        processing_group = QGroupBox("Run object detection")
        processing_layout = QVBoxLayout(processing_group)
        processing_note = QLabel(
            "Choose a processing mode and run the checked recordings. Recommended provides "
            "the best balance of speed and image detail for normal counting. Fast processes "
            "recordings more quickly using a smaller model and lower image resolution. Slow "
            "analyzes more image detail but takes much longer and may not improve counts in "
            "crowded scenes. Leave review snapshots off for faster automatic counting; turn "
            "them on only when you want to manually review detections."
        )
        processing_note.setWordWrap(True)
        processing_layout.addWidget(processing_note)
        self.save_review_snapshots = QCheckBox(
            "Save detection snapshots for manual review"
        )
        self.save_review_snapshots.setChecked(False)
        self.save_review_snapshots.setToolTip(
            "Checked: save boxed snapshots and leave detections Pending for Review Results. "
            "Unchecked: save no snapshots and automatically Accept every detected crossing."
        )
        processing_layout.addWidget(self.save_review_snapshots)
        self.create_annotated_during_detection = QCheckBox(
            "Create annotated video during detection"
        )
        self.create_annotated_during_detection.setChecked(True)
        self.create_annotated_during_detection.setToolTip(
            "Saves annotated frames while YOLO is already running. This uses extra disk space "
            "and a small amount of encoding time, but makes Final QC much faster."
        )
        processing_layout.addWidget(self.create_annotated_during_detection)
        processing = QHBoxLayout()
        self.processing_mode = QComboBox()
        for label, model_path, frame_stride, image_size in DETECTION_PRESETS:
            self.processing_mode.addItem(
                label,
                (model_path, frame_stride, image_size),
            )
        self.processing_mode.setCurrentIndex(1)
        self.processing_mode.setToolTip(
            "Fast: YOLO26n at 640. Recommended: YOLO26s at 960. Slow: YOLO26x at 1280. "
            "Every mode uses frame stride 3 and automatic batching."
        )
        self.process_button = QPushButton("Process checked")
        self.process_button.setObjectName("primaryButton")
        self.process_button.clicked.connect(self._process_selected)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_processing)
        processing.addWidget(QLabel("Processing mode"))
        processing.addWidget(self.processing_mode, 1)
        processing.addWidget(self.process_button)
        processing.addWidget(self.cancel_button)
        processing_layout.addLayout(processing)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        processing_status = QHBoxLayout()
        self.processing_time_label = QLabel("Elapsed 00:00:00 · Remaining —")
        self.processing_time_label.setMinimumWidth(245)
        processing_status.addWidget(self.progress, 1)
        processing_status.addWidget(self.processing_time_label)
        processing_layout.addLayout(processing_status)
        layout.addWidget(processing_group)

        next_step = QPushButton("Open Review Results")
        next_step.clicked.connect(lambda: self._navigate("review"))
        layout.addWidget(next_step, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Review Results")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        explanation = QLabel(
            "Select a crossing to inspect the saved evidence frame. Accept it when the box, "
            "object class, direction, and line crossing are correct; reject false detections. "
            "Bulk acceptance uses model confidence only, so spot-check the evidence first. "
            "Click any column heading to sort the results."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("infoBanner")
        layout.addWidget(explanation)
        self.review_filter_tabs = QTabBar()
        self.review_filter_tabs.setExpanding(False)
        for label, status in (
            ("Pending", ReviewStatus.PENDING),
            ("Accepted", ReviewStatus.ACCEPTED),
            ("Rejected", ReviewStatus.REJECTED),
            ("All", None),
        ):
            index = self.review_filter_tabs.addTab(label)
            self.review_filter_tabs.setTabData(index, status.value if status else None)
        self.review_filter_tabs.currentChanged.connect(self._refresh_events)
        layout.addWidget(self.review_filter_tabs)
        controls = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_events)
        accept = QPushButton("Accept checked")
        accept.setObjectName("primaryButton")
        accept.clicked.connect(lambda: self._review_selected(ReviewStatus.ACCEPTED))
        reject = QPushButton("Reject checked")
        reject.clicked.connect(lambda: self._review_selected(ReviewStatus.REJECTED))
        select_all = QPushButton("Check all")
        clear_checks = QPushButton("Clear checks")
        self.event_check_summary = QLabel("0 detections checked")
        delete_selected = QPushButton("Delete checked")
        delete_selected.setObjectName("dangerButton")
        delete_selected.clicked.connect(self._delete_selected_events)
        controls.addWidget(refresh)
        controls.addWidget(select_all)
        controls.addWidget(clear_checks)
        controls.addWidget(self.event_check_summary)
        controls.addWidget(accept)
        controls.addWidget(reject)
        controls.addWidget(delete_selected)
        controls.addStretch()
        layout.addLayout(controls)

        threshold_controls = QHBoxLayout()
        threshold_controls.addWidget(QLabel("Bulk accept pending detections at/above"))
        self.review_confidence_threshold = QSpinBox()
        self.review_confidence_threshold.setRange(0, 100)
        self.review_confidence_threshold.setValue(90)
        self.review_confidence_threshold.setSuffix("%")
        self.review_confidence_threshold.setToolTip(
            "Only pending detections at or above this model confidence will be accepted"
        )
        accept_above = QPushButton("Accept matching pending")
        accept_above.clicked.connect(self._accept_pending_at_or_above_threshold)
        threshold_controls.addWidget(self.review_confidence_threshold)
        threshold_controls.addWidget(accept_above)
        threshold_controls.addStretch()
        layout.addLayout(threshold_controls)

        self.event_table = QTableWidget(0, 10)
        self.event_table.setHorizontalHeaderLabels(
            [
                "Select",
                "Status",
                "Event time",
                "Recording",
                "Mode",
                "Direction",
                "Line",
                "Confidence",
                "Track",
                "Camera",
            ]
        )
        self.event_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.event_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.event_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.event_table.itemSelectionChanged.connect(self._event_selection_changed)
        self.event_table.itemChanged.connect(self._event_check_item_changed)
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 2, 4, 5, 6, 7, 8, 9):
            self.event_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        configure_sortable_table(self.event_table, 2)
        self.event_table.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_: setattr(self, "_event_check_anchor", None)
        )
        select_all.clicked.connect(lambda: self._set_all_event_checks(True))
        clear_checks.clicked.connect(lambda: self._set_all_event_checks(False))

        evidence_panel = QWidget()
        evidence_layout = QVBoxLayout(evidence_panel)
        evidence_heading = QLabel("Detection evidence")
        evidence_heading.setObjectName("sectionTitle")
        self.evidence_label = EvidenceFrameLabel(
            "Select a detection to view its boxed frame"
        )
        self.evidence_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.evidence_label.setObjectName("evidenceFrame")
        self.evidence_details = QLabel()
        self.evidence_details.setWordWrap(True)
        evidence_layout.addWidget(evidence_heading)
        evidence_layout.addWidget(self.evidence_label)
        evidence_layout.addWidget(self.evidence_details)
        evidence_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.event_table)
        splitter.addWidget(evidence_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return page

    def _build_final_qc_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Quality Control")
        heading.setObjectName("sectionTitle")
        explanation = QLabel(
            "Use Completed to view finished annotated videos. Use Not generated to create "
            "an annotated video."
        )
        explanation.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(explanation)

        self.final_qc_video_tabs = QTabBar()
        self.final_qc_video_tabs.setExpanding(False)
        self.final_qc_video_tabs.addTab("Completed (0)")
        self.final_qc_video_tabs.addTab("Not generated (0)")
        layout.addWidget(self.final_qc_video_tabs)

        self.final_qc_video_stack = QStackedWidget()
        self.final_qc_completed_table = QTableWidget(0, 5)
        self.final_qc_pending_table = QTableWidget(0, 5)
        for table in (self.final_qc_completed_table, self.final_qc_pending_table):
            table.setHorizontalHeaderLabels(
                ["Date", "Camera", "Recordings", "Recorded time", "Action"]
            )
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            for column in (0, 2, 3, 4):
                table.horizontalHeader().setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents
                )
        self.final_qc_video_stack.addWidget(self.final_qc_completed_table)
        self.final_qc_video_stack.addWidget(self.final_qc_pending_table)
        self.final_qc_video_stack.setMinimumHeight(170)
        # This table is the useful place for spare vertical room on the QC page.
        # Do not cap it at 220 px and then leave the rest of the page blank below
        # the compact video player.
        self.final_qc_video_tabs.currentChanged.connect(
            self.final_qc_video_stack.setCurrentIndex
        )
        layout.addWidget(self.final_qc_video_stack, 1)

        self.final_qc_day = QComboBox()
        self.final_qc_camera = QComboBox()
        self.final_qc_day.currentIndexChanged.connect(self._final_qc_filter_changed)
        self.final_qc_camera.currentIndexChanged.connect(self._final_qc_filter_changed)

        self.final_qc_summary = QLabel()
        self.final_qc_summary.setObjectName("infoBanner")
        self.final_qc_summary.setWordWrap(True)
        layout.addWidget(self.final_qc_summary)

        actions = QHBoxLayout()
        self.generate_qc_video_button = QPushButton("Generate full annotated video")
        self.generate_qc_video_button.setObjectName("primaryButton")
        self.generate_qc_video_button.clicked.connect(self._generate_final_qc_video)
        self.generate_qc_video_button.setVisible(False)
        self.cancel_qc_video_button = QPushButton("Cancel generation")
        self.cancel_qc_video_button.setEnabled(False)
        self.cancel_qc_video_button.clicked.connect(self._cancel_final_qc_video)
        self.download_qc_video_button = QPushButton("Download / save copy")
        self.download_qc_video_button.setEnabled(False)
        self.download_qc_video_button.clicked.connect(self._save_final_qc_video_copy)
        actions.addWidget(self.generate_qc_video_button)
        actions.addWidget(self.cancel_qc_video_button)
        actions.addWidget(self.download_qc_video_button)
        actions.addStretch()
        layout.addLayout(actions)

        progress_row = QHBoxLayout()
        self.final_qc_progress = QProgressBar()
        self.final_qc_progress.setRange(0, 1000)
        self.final_qc_progress.setValue(0)
        self.final_qc_progress.setFormat("Ready")
        self.final_qc_time_label = QLabel("Elapsed 00:00:00 · Remaining —")
        self.final_qc_time_label.setMinimumWidth(245)
        progress_row.addWidget(self.final_qc_progress, 1)
        progress_row.addWidget(self.final_qc_time_label)
        layout.addLayout(progress_row)

        self.final_qc_player = QcVideoPlayer()
        layout.addWidget(self.final_qc_player, 0)
        self.qc_start_hour.valueChanged.connect(self._final_qc_coverage_window_changed)
        self.qc_end_hour.valueChanged.connect(self._final_qc_coverage_window_changed)
        return page

    def _build_reports_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Camera Report")
        heading.setObjectName("sectionTitle")
        description = QLabel(
            "View and export multimodal traffic counts for one camera on one date."
        )
        description.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(description)
        filters = QHBoxLayout()
        self.report_day = QComboBox()
        self.report_camera = QComboBox()
        self.report_view = QComboBox()
        self.report_view.addItem("Enter/exit summary", "totals")
        self.report_view.addItem("Hourly counts", "hourly")
        self.report_view.addItem("Hourly plot", "plots")
        self.report_day.currentIndexChanged.connect(self._refresh_report)
        self.report_camera.currentIndexChanged.connect(self._refresh_report)
        self.report_view.currentIndexChanged.connect(self._refresh_report)
        export = QPushButton("Export clean CSVs")
        export.clicked.connect(self._export)
        html = QPushButton("Create HTML report")
        html.setObjectName("primaryButton")
        html.clicked.connect(self._create_report)
        filters.addWidget(QLabel("Date"))
        filters.addWidget(self.report_day)
        filters.addWidget(QLabel("Camera"))
        filters.addWidget(self.report_camera)
        filters.addWidget(QLabel("View"))
        filters.addWidget(self.report_view)
        filters.addStretch()
        filters.addWidget(export)
        filters.addWidget(html)
        layout.addLayout(filters)
        self.report_quality = QLabel()
        self.report_quality.setObjectName("infoBanner")
        self.report_quality.setWordWrap(True)
        layout.addWidget(self.report_quality)
        self.report_table = QTableWidget(0, 4)
        self.report_table.setHorizontalHeaderLabels(["Mode", "Enter", "Exit", "Total"])
        self.report_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.report_table.verticalHeader().setVisible(False)
        self.report_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.report_table, 1)
        self.report_chart = QChartView()
        self.report_chart.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.report_chart.setMinimumHeight(420)
        self.report_chart.setVisible(False)
        layout.addWidget(self.report_chart, 1)
        return page

    def _build_camera_comparison_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Camera Comparison")
        heading.setObjectName("sectionTitle")
        description = QLabel(
            "Compare multimodal traffic counts across all cameras on one date."
        )
        description.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(description)
        filters = QHBoxLayout()
        self.camera_comparison_day = QComboBox()
        self.camera_comparison_metric = QComboBox()
        self.camera_comparison_metric.addItem("Enter/Exit summary", "direction_summary")
        self.camera_comparison_metric.addItem("Total counts", "total")
        self.camera_comparison_metric.addItem("Counts per recorded hour", "per_recorded_hour")
        self.camera_comparison_metric.addItem("Hourly counts by camera", "hourly_by_camera")
        self.camera_comparison_metric.addItem("All camera hourly plots", "hourly_panels")
        self.camera_comparison_day.currentIndexChanged.connect(self._refresh_camera_comparison)
        self.camera_comparison_metric.currentIndexChanged.connect(self._refresh_camera_comparison)
        export = QPushButton("Export clean CSVs")
        export.clicked.connect(self._export)
        html = QPushButton("Create HTML report")
        html.setObjectName("primaryButton")
        html.clicked.connect(self._create_camera_comparison_report)
        filters.addWidget(QLabel("Date"))
        filters.addWidget(self.camera_comparison_day)
        filters.addWidget(QLabel("View"))
        filters.addWidget(self.camera_comparison_metric)
        filters.addStretch()
        filters.addWidget(export)
        filters.addWidget(html)
        layout.addLayout(filters)

        self.camera_comparison_quality = QLabel()
        self.camera_comparison_quality.setObjectName("infoBanner")
        self.camera_comparison_quality.setWordWrap(True)
        layout.addWidget(self.camera_comparison_quality)
        self.camera_comparison_table = QTableWidget(0, 4)
        self.camera_comparison_table.setHorizontalHeaderLabels(
            ["Camera", "Enter", "Exit", "Total"]
        )
        self.camera_comparison_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.camera_comparison_table.verticalHeader().setVisible(False)
        self.camera_comparison_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.camera_comparison_table, 1)
        self.camera_comparison_chart = QChartView()
        self.camera_comparison_chart.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.camera_comparison_chart.setMinimumHeight(420)
        self.camera_comparison_chart.setVisible(False)
        layout.addWidget(self.camera_comparison_chart, 1)
        self.camera_comparison_hourly_charts = QWidget()
        hourly_charts_layout = QVBoxLayout(self.camera_comparison_hourly_charts)
        hourly_charts_layout.setContentsMargins(0, 0, 0, 0)
        self.camera_comparison_hourly_line_chart = QChartView()
        self.camera_comparison_hourly_line_chart.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )
        self.camera_comparison_hourly_line_chart.setMinimumHeight(420)
        self.camera_comparison_hourly_stacked_chart = QChartView()
        self.camera_comparison_hourly_stacked_chart.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )
        self.camera_comparison_hourly_stacked_chart.setMinimumHeight(420)
        hourly_charts_layout.addWidget(self.camera_comparison_hourly_line_chart)
        hourly_charts_layout.addWidget(self.camera_comparison_hourly_stacked_chart)
        self.camera_comparison_hourly_charts.setVisible(False)
        layout.addWidget(self.camera_comparison_hourly_charts)
        self.camera_comparison_panels = QScrollArea()
        self.camera_comparison_panels.setWidgetResizable(True)
        self.camera_comparison_panels.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.camera_comparison_panels.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.camera_comparison_panels_container = QWidget()
        self.camera_comparison_panels_layout = QGridLayout(
            self.camera_comparison_panels_container
        )
        self.camera_comparison_panels_layout.setContentsMargins(4, 4, 4, 4)
        self.camera_comparison_panels_layout.setSpacing(12)
        self.camera_comparison_panels.setWidget(self.camera_comparison_panels_container)
        self.camera_comparison_panels.setMinimumHeight(480)
        self.camera_comparison_panels.setVisible(False)
        layout.addWidget(self.camera_comparison_panels, 1)
        return page

    def _build_daily_report_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Daily Trends")
        heading.setObjectName("sectionTitle")
        actions = QHBoxLayout()
        description = QLabel(
            "Compare multimodal traffic counts across multiple dates alongside daily weather."
        )
        description.setWordWrap(True)
        description.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        layout.addWidget(heading)
        actions.addWidget(description, 1, Qt.AlignmentFlag.AlignTop)
        export = QPushButton("Export clean CSVs")
        export.clicked.connect(self._export)
        html = QPushButton("Create HTML report")
        html.setObjectName("primaryButton")
        html.clicked.connect(self._create_daily_report)
        actions.addWidget(export)
        actions.addWidget(html)
        layout.addLayout(actions)

        weather_controls = QHBoxLayout()
        weather_controls.addWidget(QLabel("Weather location"))
        self.daily_weather_location = QLineEdit()
        self.daily_weather_location.setPlaceholderText("Edmonton, Alberta")
        self.daily_weather_location.setMinimumWidth(260)
        weather_controls.addWidget(self.daily_weather_location)
        self.load_daily_weather_button = QPushButton("Load weather")
        self.load_daily_weather_button.clicked.connect(self._load_daily_weather)
        weather_controls.addWidget(self.load_daily_weather_button)
        self.daily_weather_status = QLabel("Weather has not been loaded")
        self.daily_weather_status.setWordWrap(True)
        weather_controls.addWidget(self.daily_weather_status, 1)
        layout.addLayout(weather_controls)

        self.daily_report_quality = QLabel()
        self.daily_report_quality.setObjectName("infoBanner")
        self.daily_report_quality.setWordWrap(True)
        layout.addWidget(self.daily_report_quality)

        counts_heading = QLabel("Counts by day")
        counts_heading.setObjectName("sectionTitle")
        layout.addWidget(counts_heading)
        self.daily_report_table = QTableWidget(0, len(MODES) + 7)
        self.daily_report_table.setHorizontalHeaderLabels(
            [
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
        self.daily_report_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.daily_report_table.verticalHeader().setVisible(False)
        self.daily_report_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.daily_report_table.setMinimumHeight(260)
        configure_sortable_table(self.daily_report_table, 0)
        layout.addWidget(self.daily_report_table)

        class_heading = QLabel("Daily multimodal traffic counts")
        class_heading.setObjectName("sectionTitle")
        layout.addWidget(class_heading)
        (
            self.daily_class_weather_strip,
            self.daily_class_weather_strip_layout,
        ) = self._create_daily_weather_strip()
        layout.addWidget(self.daily_class_weather_strip)
        self.daily_class_chart = QChartView()
        self.daily_class_chart.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.daily_class_chart.setMinimumHeight(420)
        layout.addWidget(self.daily_class_chart)

        direction_heading = QLabel("Daily enter and exit counts")
        direction_heading.setObjectName("sectionTitle")
        layout.addWidget(direction_heading)
        (
            self.daily_direction_weather_strip,
            self.daily_direction_weather_strip_layout,
        ) = self._create_daily_weather_strip()
        layout.addWidget(self.daily_direction_weather_strip)
        self.daily_direction_chart = QChartView()
        self.daily_direction_chart.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.daily_direction_chart.setMinimumHeight(420)
        layout.addWidget(self.daily_direction_chart)
        self.daily_weather_attribution = QLabel(
            'Weather data: <a href="https://open-meteo.com/">Open-Meteo</a> '
            '(<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>)'
        )
        self.daily_weather_attribution.setOpenExternalLinks(True)
        self.daily_weather_attribution.setVisible(False)
        layout.addWidget(self.daily_weather_attribution)
        return page

    def _add_videos(self) -> None:
        if self._intake_thread is not None:
            return
        if self._combined_video_thread is not None:
            QMessageBox.information(
                self,
                "Combined video build in progress",
                "Finish or cancel Preprocessing before adding recordings.",
            )
            return
        if self._qc_video_thread is not None:
            QMessageBox.information(
                self,
                "Annotated video generation in progress",
                "Finish or cancel the annotated video before adding recordings.",
            )
            return
        if self._thread is not None:
            QMessageBox.information(
                self,
                "Processing in progress",
                "Finish or cancel detection processing before adding more videos.",
            )
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add traffic videos",
            "",
            "Videos (*.mp4 *.mov *.avi *.mkv *.m4v);;All files (*)",
        )
        if not paths:
            return

        self._intake_thread = QThread(self)
        self._intake_worker = VideoIntakeWorker(self.project_path, paths)
        self._intake_worker.moveToThread(self._intake_thread)
        self._intake_thread.started.connect(self._intake_worker.run)
        self._intake_worker.progress.connect(self._video_intake_progress)
        self._intake_worker.completed.connect(self._video_intake_complete)
        self._intake_worker.finished.connect(self._intake_thread.quit)
        self._intake_worker.finished.connect(self._intake_worker.deleteLater)
        self._intake_thread.finished.connect(self._video_intake_finished)
        self._intake_thread.finished.connect(self._intake_thread.deleteLater)
        self.add_video_button.setEnabled(False)
        self.process_button.setEnabled(False)
        self.intake_cancel_button.setVisible(True)
        self.intake_cancel_button.setEnabled(True)
        self.intake_progress.setRange(0, len(paths) * 3)
        self.intake_progress.setValue(0)
        self.intake_progress.setFormat(f"Starting intake for {len(paths)} recordings…")
        self._intake_started_at = monotonic()
        self._intake_current = 0
        self._intake_total = len(paths) * 3
        self._refresh_progress_times()
        self._intake_thread.start()

    def _cancel_video_intake(self) -> None:
        if self._intake_worker is None:
            return
        self._intake_worker.cancel()
        self.intake_cancel_button.setEnabled(False)
        self.intake_progress.setFormat("Cancelling after the current file step…")

    def _video_intake_progress(
        self, current_step: int, total_steps: int, filename: str, stage: str
    ) -> None:
        self.intake_progress.setRange(0, total_steps)
        self.intake_progress.setValue(current_step)
        self._intake_current = current_step
        self._intake_total = total_steps
        self._refresh_progress_times()
        files_complete = current_step // 3
        files_total = total_steps // 3
        self.intake_progress.setFormat(
            f"{stage}: {filename} — {files_complete} of {files_total} files complete"
        )
        self.statusBar().showMessage(f"{stage}: {filename}")

    def _video_intake_complete(
        self, added: int, failures: list[str], time_failures: list[str], cancelled: bool
    ) -> None:
        self._refresh_all()
        elapsed = (
            monotonic() - self._intake_started_at if self._intake_started_at is not None else 0.0
        )
        if cancelled:
            self.intake_progress.setFormat(f"Loading cancelled — {added} recordings added")
            self.intake_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining —")
            self.statusBar().showMessage("Video loading cancelled", 5000)
        else:
            self.intake_progress.setValue(self.intake_progress.maximum())
            self.intake_progress.setFormat(f"Complete — {added} recordings added")
            self.intake_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining 00:00:00")
            self.statusBar().showMessage(f"Added {added} recordings", 5000)
        self._intake_started_at = None
        messages = []
        if failures:
            messages.append("Could not add:\n" + "\n".join(failures))
        if time_failures:
            messages.append(
                "Added but the filename date/time could not be read:\n" + "\n".join(time_failures)
            )
        if messages and not self._close_requested:
            QMessageBox.warning(self, "Video intake needs attention", "\n\n".join(messages))

    def _video_intake_finished(self) -> None:
        self.add_video_button.setEnabled(True)
        self.process_button.setEnabled(self._worker is None)
        self.intake_cancel_button.setVisible(False)
        self._intake_worker = None
        self._intake_thread = None
        if (
            self._close_requested
            and self._worker is None
            and self._qc_video_worker is None
            and self._combined_video_worker is None
            and self._weather_worker is None
        ):
            QTimer.singleShot(0, self.close)

    def _selected_video_id(self) -> int | None:
        selected = self._selected_video_ids()
        return selected[0] if len(selected) == 1 else None

    def _selected_video_ids(self) -> list[int]:
        selection_model = self.video_table.selectionModel()
        if selection_model is None:
            return []
        selected_ids = []
        for index in sorted(selection_model.selectedRows(), key=lambda value: value.row()):
            video_id = table_row_identity(self.video_table, index.row())
            if video_id is not None:
                selected_ids.append(video_id)
        return selected_ids

    def _checked_video_ids(self) -> list[int]:
        if self.detection_source_mode.currentData() == "combined":
            selected_ids: list[int] = []
            for combined in self._combined_videos:
                if combined_row_key(combined) in self._detection_checked_video_ids:
                    selected_ids.extend(combined.source_video_ids)
            return list(dict.fromkeys(selected_ids))
        return [video.id for video in self._videos if video.id in self._detection_checked_video_ids]

    def _detection_source_changed(self, _index: int | None = None) -> None:
        self._detection_checked_video_ids.clear()
        combined_mode = self.detection_source_mode.currentData() == "combined"
        self.delete_selected_combined_button.setVisible(combined_mode)
        if combined_mode:
            self.detection_selection_note.setText(
                "Each row is one combined camera-day video from Preprocessing. Detection "
                "processes its original source frames so filename-based times remain exact "
                "and NO RECORDING cards are ignored. Double-click a row to toggle its checkbox. "
                "Delete selected removes only highlighted generated combined videos; original "
                "recordings and count data remain unchanged."
            )
        else:
            self.detection_selection_note.setText(
                "The checkboxes below are the complete processing list. Double-click any "
                "recording row to toggle it. To check a range, click the first row and "
                "Shift-click the last; the highlighted range and checkboxes stay synchronized. "
                "Click a column heading to change the sort order."
            )
        self._refresh_detection_videos()

    def _set_all_detection_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._updating_detection_checks = True
        try:
            for row in range(self.detection_video_table.rowCount()):
                item = self.detection_video_table.item(row, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self._updating_detection_checks = False
        self._detection_check_anchor = None
        self._sync_detection_checked_ids_from_table()
        self._update_video_check_summary()

    def _detection_video_check_item_changed(self, item: QTableWidgetItem) -> None:
        if (
            self._populating_detection_table
            or self._updating_detection_checks
            or item.column() != 0
        ):
            return
        row = item.row()
        if (
            QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            and self._detection_check_anchor is not None
        ):
            self._set_check_range(
                self.detection_video_table,
                self._detection_check_anchor,
                row,
                item.checkState(),
                "_updating_detection_checks",
            )
        self._detection_check_anchor = row
        self._sync_detection_checked_ids_from_table()
        self._update_video_check_summary()

    def _detection_video_selection_changed(self) -> None:
        if self._populating_detection_table or not (
            QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            return
        if (
            self.detection_video_table.currentColumn() == 0
            and QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton
        ):
            return
        self._check_selected_detection_rows()

    def _check_selected_detection_rows(self) -> None:
        self._check_highlighted_rows(self.detection_video_table, "_updating_detection_checks")
        self._sync_detection_checked_ids_from_table()
        self._update_video_check_summary()

    def _toggle_detection_video_from_double_click(self, row: int, column: int) -> None:
        if column == 0 or not 0 <= row < self.detection_video_table.rowCount():
            return
        item = self.detection_video_table.item(row, 0)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def _sync_detection_checked_ids_from_table(self) -> None:
        visible_ids = {
            identity
            for row in range(self.detection_video_table.rowCount())
            if (identity := table_row_key(self.detection_video_table, row)) is not None
        }
        self._detection_checked_video_ids.difference_update(visible_ids)
        for row in range(self.detection_video_table.rowCount()):
            identity = table_row_key(self.detection_video_table, row)
            item = self.detection_video_table.item(row, 0)
            if (
                identity is not None
                and item is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                self._detection_checked_video_ids.add(identity)

    def _update_video_check_summary(self) -> None:
        count = len(self._detection_checked_video_ids)
        if self.detection_source_mode.currentData() == "combined":
            noun = "combined video" if count == 1 else "combined videos"
        else:
            noun = "recording" if count == 1 else "recordings"
        self.video_check_summary.setText(f"{count} {noun} checked")
        self.process_button.setText(f"Process checked ({count})" if count else "Process checked")

    def _selected_detection_combined_videos(self) -> list[CombinedVideoRecord]:
        if self.detection_source_mode.currentData() != "combined":
            return []
        selection_model = self.detection_video_table.selectionModel()
        if selection_model is None:
            return []
        combined_by_key = {
            combined_row_key(video): video for video in self._combined_videos
        }
        selected = []
        for index in sorted(selection_model.selectedRows(), key=lambda value: value.row()):
            key = table_row_key(self.detection_video_table, index.row())
            video = combined_by_key.get(key)
            if video is not None:
                selected.append(video)
        return selected

    def _delete_selected_detection_combined_videos(self) -> None:
        if self._worker is not None or self._combined_video_worker is not None:
            QMessageBox.information(
                self,
                "Video operation in progress",
                "Finish or cancel detection and Preprocessing before deleting combined videos.",
            )
            return
        selected = self._selected_detection_combined_videos()
        if not selected:
            QMessageBox.information(
                self,
                "Select combined videos",
                "Highlight one or more combined daily video rows to delete.",
            )
            return
        count = len(selected)
        answer = QMessageBox.question(
            self,
            "Delete selected combined videos?",
            f"Permanently delete {count} generated combined daily "
            f"video{'s' if count != 1 else ''}?\n\n"
            "Original recordings, counting lines, detections, and review decisions will not "
            "be deleted. A combined video can be rebuilt in Preprocessing.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        deleted = []
        failures = []
        for video in selected:
            try:
                delete_combined_video_files(video.path)
            except (OSError, ValueError) as exc:
                failures.append(f"{video.path.name}: {exc}")
            else:
                deleted.append(video)
        deleted_paths = {video.path.resolve() for video in deleted}
        if (
            self._combined_video_output is not None
            and self._combined_video_output.resolve() in deleted_paths
        ):
            self._combined_video_output = None
            self.combined_video_player.clear()
            self.download_combined_video_button.setEnabled(False)
        self._detection_checked_video_ids.difference_update(
            combined_row_key(video) for video in deleted
        )
        self._refresh_all()
        if deleted:
            deleted_count = len(deleted)
            self.statusBar().showMessage(
                f"Deleted {deleted_count} combined daily "
                f"video{'s' if deleted_count != 1 else ''}; original recordings were kept",
                7000,
            )
        if failures:
            QMessageBox.critical(
                self,
                "Some combined videos could not be deleted",
                "\n".join(failures),
            )

    def _delete_selected_videos(self) -> None:
        video_ids = self._selected_video_ids()
        if not video_ids:
            QMessageBox.information(self, "Select recordings", "Select recordings to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Remove selected recordings?",
            f"Remove {len(video_ids)} recordings and their detection evidence from this project? "
            "The original video files will not be deleted.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repository.delete_videos(video_ids)
            self._refresh_all()

    def _edit_video_time(self) -> None:
        video_id = self._selected_video_id()
        if video_id is None:
            QMessageBox.information(self, "Select a recording", "Select one recording first.")
            return
        video = self.repository.get_video(video_id)
        try:
            dialog = TimestampDialog(video, self)
        except Exception as exc:
            QMessageBox.critical(self, "Preview failed", str(exc))
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.set_recording_time(
                video_id,
                dialog.recording_start(),
                visible_offset_seconds=dialog.visible_offset_seconds,
            )
            self._refresh_all()

    def _set_camera(self) -> None:
        video_ids = self._selected_video_ids()
        if not video_ids:
            QMessageBox.information(
                self,
                "Select recordings",
                "Select one or more recordings first.",
            )
            return
        cameras = {self.repository.get_video(video_id).camera for video_id in video_ids}
        current_camera = next(iter(cameras)) if len(cameras) == 1 else ""
        count = len(video_ids)
        value, ok = QInputDialog.getText(
            self,
            "Set camera for selected recordings",
            f"Camera/location for {count} selected recording{'s' if count != 1 else ''}",
            text=current_camera,
        )
        if ok and value.strip():
            camera = value.strip()
            self.repository.set_cameras(video_ids, camera)
            self._refresh_all()
            self.statusBar().showMessage(
                f'Set {count} recording{"s" if count != 1 else ""} to "{camera}"',
                5000,
            )

    def _set_recording_date(self) -> None:
        video_ids = self._selected_video_ids()
        if not video_ids:
            QMessageBox.information(
                self,
                "Select recordings",
                "Select one or more recordings first.",
            )
            return
        videos = [self.repository.get_video(video_id) for video_id in video_ids]
        selected_days = {video.recording_day for video in videos}
        initial_date = (
            next(iter(selected_days)).isoformat()
            if len(selected_days) == 1 and None not in selected_days
            else ""
        )
        count = len(video_ids)
        while True:
            value, ok = QInputDialog.getText(
                self,
                "Set date for selected recordings",
                f"Date for {count} selected recording{'s' if count != 1 else ''} (YYYY-MM-DD)",
                text=initial_date,
            )
            if not ok:
                return
            try:
                recording_day = date.fromisoformat(value.strip())
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Invalid date",
                    "Enter a valid date in YYYY-MM-DD format, such as 2026-07-10.",
                )
                initial_date = value.strip()
                continue
            break
        self.repository.set_recording_dates(video_ids, recording_day)
        self._refresh_all()
        self.statusBar().showMessage(
            f"Set {count} recording{'s' if count != 1 else ''} to {recording_day.isoformat()}",
            5000,
        )

    def _line_source_changed(self) -> None:
        self._active_video_id = None
        self._active_combined_video = None
        self._clear_line_editor()
        self._refresh_line_videos()

    def _clear_line_editor(self) -> None:
        self.preview.clear()
        self.line_heading.setText("Select a recording from the list")
        self.line_selector.blockSignals(True)
        self.line_selector.clear()
        self.line_selector.addItem("No recording selected", None)
        self.line_selector.blockSignals(False)
        self.line_selector.setEnabled(False)
        self.add_line_button.setEnabled(False)
        self.line_status.setText("No recording selected")
        self.save_line_button.setText("Save for this recording")
        self.save_line_button.setEnabled(False)
        self.apply_day_line_button.setEnabled(False)
        self.delete_line_button.setEnabled(False)
        self.redraw_line_button.setEnabled(False)
        self.swap_directions_button.setEnabled(False)
        self.draw_line_mode_button.setEnabled(False)
        self.draw_detection_zone_button.setEnabled(False)
        self.clear_detection_zone_button.setEnabled(False)
        self.detection_zone_status.setText("No zone")
        self._sync_drawing_mode_ui("line")

    def _line_video_selection_changed(self) -> None:
        if self._populating_line_video_table:
            return
        row = self.line_video_table.currentRow()
        selected_key = table_row_key(self.line_video_table, row)
        if selected_key is not None:
            active_key = (
                combined_row_key(self._active_combined_video)
                if self._active_combined_video is not None
                else self._active_video_id
            )
            if (
                active_key is not None
                and selected_key != active_key
                and self.save_line_button.isEnabled()
            ):
                answer = QMessageBox.question(
                    self,
                    "Discard unsaved line changes?",
                    "This recording has line changes that have not been saved. "
                    "Discard them and open the selected recording?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self._select_line_video_in_table(active_key)
                    return
            if isinstance(selected_key, int):
                self._load_line_video(selected_key)
                return
            combined = next(
                (
                    video
                    for video in self._combined_videos
                    if combined_row_key(video) == selected_key
                ),
                None,
            )
            if combined is not None:
                self._load_combined_line_video(combined)

    def _select_line_video_in_table(self, identity) -> None:
        row = find_table_row_by_key(self.line_video_table, identity)
        if row is None:
            return
        self._populating_line_video_table = True
        try:
            self.line_video_table.selectRow(row)
            self.line_video_table.scrollToItem(self.line_video_table.item(row, 2))
        finally:
            self._populating_line_video_table = False

    def _load_line_video(self, video_id: int, selected_line_id: int | None = None) -> None:
        video = self.repository.get_video(video_id)
        try:
            self.preview.set_bgr_frame(read_preview(video.path, 0.05))
        except Exception as exc:
            detail = f"{exc}\n\nRecording: {video.path}"
            if not video.path.is_file():
                detail += (
                    "\n\nThe recording path no longer exists. Keep the source recording "
                    "available, or remove and add it again from its current location."
                )
            QMessageBox.critical(self, "Preview failed", detail)
            return
        self._active_video_id = video_id
        self._active_combined_video = None
        self._select_line_video_in_table(video_id)
        self.line_selector.setEnabled(True)
        self.add_line_button.setEnabled(True)
        self.draw_line_mode_button.setEnabled(True)
        self.draw_detection_zone_button.setEnabled(True)
        self.save_line_button.setText("Save for this recording")
        self.apply_day_line_button.setText("Apply lines and zone to same camera and date")
        date_text = video.recording_day.isoformat() if video.recording_day else "timestamp missing"
        self.line_heading.setText(f"{video.path.name} · {video.camera} · {date_text}")
        self._populate_line_selector(video, selected_line_id)
        self.save_line_button.setEnabled(False)

    def _load_combined_line_video(
        self,
        combined: CombinedVideoRecord,
        selected_line_name: str | None = None,
    ) -> None:
        source_videos = [
            self.repository.get_video(video_id) for video_id in combined.source_video_ids
        ]
        if not source_videos:
            return
        representative = source_videos[0]
        try:
            self.preview.set_bgr_frame(read_preview(combined.path, 0.05))
        except Exception as exc:
            QMessageBox.critical(self, "Preview failed", str(exc))
            return
        self._active_video_id = representative.id
        self._active_combined_video = combined
        self._select_line_video_in_table(combined_row_key(combined))
        self.line_selector.setEnabled(True)
        self.add_line_button.setEnabled(True)
        self.draw_line_mode_button.setEnabled(True)
        self.draw_detection_zone_button.setEnabled(True)
        self.save_line_button.setText("Save for combined video")
        self.apply_day_line_button.setText("Lines apply to every source recording")
        self.line_heading.setText(
            f"{combined.path.name} · {combined.camera} · {combined.day.isoformat()}"
        )
        self._populate_line_selector(
            representative,
            selected_line_name=selected_line_name,
        )
        self.apply_day_line_button.setEnabled(False)
        self.save_line_button.setEnabled(False)

    def _populate_line_selector(
        self,
        video: VideoRecord,
        selected_line_id: int | None = None,
        selected_line_name: str | None = None,
    ) -> None:
        self.line_selector.blockSignals(True)
        self.line_selector.clear()
        for line in video.counting_lines:
            self.line_selector.addItem(line.name, line.id)
        if not video.counting_lines:
            self.line_selector.addItem("No saved lines", None)
        index = (
            self.line_selector.findText(selected_line_name)
            if selected_line_name
            else self.line_selector.findData(selected_line_id)
        )
        self.line_selector.setCurrentIndex(index if index >= 0 else 0)
        self.line_selector.blockSignals(False)
        active_id = self.line_selector.currentData()
        active_id = active_id if isinstance(active_id, int) else None
        self.preview.set_lines(video.counting_lines, active_id)
        self.preview.set_detection_zone(video.distant_detection_zone)
        self.clear_detection_zone_button.setEnabled(True)
        self.detection_zone_status.setText(
            "Zone saved" if video.distant_detection_zone is not None else "No zone"
        )
        self.delete_line_button.setEnabled(active_id is not None)
        self.redraw_line_button.setEnabled(active_id is not None)
        self.apply_day_line_button.setEnabled(
            bool(video.counting_lines) and self._active_combined_video is None
        )
        self.swap_directions_button.setEnabled(active_id is not None)
        self.line_status.setText(
            f"{len(video.counting_lines)} saved line(s)"
            if video.counting_lines
            else "Use Add line to draw the first counting line"
        )

    def _line_selection_changed(self) -> None:
        if self._active_video_id is None:
            return
        video = self.repository.get_video(self._active_video_id)
        data = self.line_selector.currentData()
        if isinstance(data, int):
            self.preview.set_lines(video.counting_lines, data)
            selected = next((line for line in video.counting_lines if line.id == data), None)
            self.line_status.setText(
                f"{selected.name} selected — click twice to redraw"
                if selected
                else "Select a saved line"
            )
            self.delete_line_button.setEnabled(True)
            self.redraw_line_button.setEnabled(True)
            self.apply_day_line_button.setEnabled(self._active_combined_video is None)
            self.swap_directions_button.setEnabled(True)
        elif isinstance(data, str) and data.startswith("new:"):
            name = data.removeprefix("new:")
            self.preview.set_lines(video.counting_lines, None)
            self.preview.start_new_line(name)
            self.line_status.setText(f"Click twice to draw {name}")
            self.delete_line_button.setEnabled(True)
            self.redraw_line_button.setEnabled(False)
            self.apply_day_line_button.setEnabled(False)
            self.swap_directions_button.setEnabled(True)
        else:
            self.preview.set_lines(video.counting_lines, None)
            self.delete_line_button.setEnabled(False)
            self.redraw_line_button.setEnabled(False)
            self.apply_day_line_button.setEnabled(
                bool(video.counting_lines) and self._active_combined_video is None
            )
            self.swap_directions_button.setEnabled(False)
        self.save_line_button.setEnabled(False)

    def _redraw_selected_line(self) -> None:
        if self.preview.start_redraw_active_line():
            self.line_status.setText(
                f"Click twice on the preview to redraw {self.line_selector.currentText()}"
            )
            self.save_line_button.setEnabled(False)
            self.apply_day_line_button.setEnabled(False)

    def _add_line(self) -> None:
        if self._active_video_id is None:
            QMessageBox.information(self, "Select a recording", "Open a recording first.")
            return
        video = self.repository.get_video(self._active_video_id)
        existing_names = {line.name for line in video.counting_lines}
        number = 1
        while f"Line {number}" in existing_names:
            number += 1
        name, ok = QInputDialog.getText(
            self, "Add counting line", "Line name", text=f"Line {number}"
        )
        name = name.strip()
        if not ok or not name:
            return
        if name in existing_names:
            QMessageBox.warning(self, "Duplicate line name", "Choose a different line name.")
            return
        if self.line_selector.count() == 1 and self.line_selector.itemData(0) is None:
            self.line_selector.clear()
        self.line_selector.addItem(name, f"new:{name}")
        self.line_selector.setCurrentIndex(self.line_selector.count() - 1)

    def _delete_line(self) -> None:
        if self._active_video_id is None:
            return
        data = self.line_selector.currentData()
        if isinstance(data, str) and data.startswith("new:"):
            video = self.repository.get_video(self._active_video_id)
            self._populate_line_selector(video)
            return
        if not isinstance(data, int):
            return
        name = self.line_selector.currentText()
        answer = QMessageBox.question(
            self,
            "Delete counting line?",
            f'Delete "{name}"? Detections and evidence created by this line will also be removed.',
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._active_combined_video is not None:
            combined_path = self._active_combined_video.path
            video = self.repository.get_video(self._active_video_id)
            remaining = tuple(line for line in video.counting_lines if line.id != data)
            self.repository.replace_counting_lines_for_videos(
                self._active_combined_video.source_video_ids,
                remaining,
            )
            self._refresh_all()
            combined = next(
                (item for item in self._combined_videos if item.path == combined_path),
                None,
            )
            if combined is not None:
                self._load_combined_line_video(combined)
            return
        self.repository.delete_counting_line(self._active_video_id, data)
        self._refresh_all()
        self._load_line_video(self._active_video_id)

    def _line_changed(self, _line: CountingLine) -> None:
        self.line_status.setText(f"{_line.name} ready to save")
        self.save_line_button.setEnabled(True)
        self.apply_day_line_button.setEnabled(False)

    def _select_line_drawing_mode(self) -> None:
        if self._active_video_id is None:
            self._sync_drawing_mode_ui(self.preview.drawing_mode())
            return
        self.preview.set_drawing_mode("line")
        self.line_status.setText("LINE mode active — click twice to draw or redraw a line")

    def _sync_drawing_mode_ui(self, mode: str) -> None:
        line_mode = mode == "line"
        self.draw_line_mode_button.blockSignals(True)
        self.draw_detection_zone_button.blockSignals(True)
        self.draw_line_mode_button.setChecked(line_mode)
        self.draw_detection_zone_button.setChecked(not line_mode)
        self.draw_line_mode_button.blockSignals(False)
        self.draw_detection_zone_button.blockSignals(False)
        self.drawing_mode_status.setText(f"Active mode: {mode.upper()}")

    def _draw_distant_detection_zone(self) -> None:
        if self._active_video_id is None:
            self._sync_drawing_mode_ui(self.preview.drawing_mode())
            return
        if self.save_line_button.isEnabled():
            QMessageBox.information(
                self,
                "Save the counting line first",
                "Save or finish the current counting-line change before drawing the distant zone.",
            )
            self._sync_drawing_mode_ui(self.preview.drawing_mode())
            return
        self.preview.start_distant_detection_zone()
        self.detection_zone_status.setText(
            "Zone saved" if self.preview.detection_zone() is not None else "No zone"
        )

    def _distant_detection_zone_changed(self, zone) -> None:
        if self._active_video_id is None:
            return
        try:
            if self._active_combined_video is not None:
                source_ids = self._active_combined_video.source_video_ids
                count = self.repository.set_distant_detection_zone_for_videos(source_ids, zone)
                message = f"Distant zone saved to all {count} source recordings"
            else:
                self.repository.set_distant_detection_zone(self._active_video_id, zone)
                message = "Distant zone saved for this recording"
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Could not save zone", str(exc))
            return
        self.clear_detection_zone_button.setEnabled(True)
        self.detection_zone_status.setText(message)
        self._refresh_all()

    def _clear_distant_detection_zone(self) -> None:
        if self._active_video_id is None:
            return
        if self._active_combined_video is not None:
            self.repository.set_distant_detection_zone_for_videos(
                self._active_combined_video.source_video_ids,
                None,
            )
        else:
            self.repository.set_distant_detection_zone(self._active_video_id, None)
        self.preview.set_detection_zone(None)
        self.preview.set_drawing_mode("zone")
        self._sync_drawing_mode_ui("zone")
        self.clear_detection_zone_button.setEnabled(True)
        self.detection_zone_status.setText("No zone")
        self._refresh_all()

    def _swap_line_directions(self) -> None:
        line = self.preview.swap_direction_labels()
        self.apply_day_line_button.setEnabled(False)
        if line is not None:
            self.line_status.setText(f"{line.name} direction labels ready to save")
            self.save_line_button.setEnabled(True)
        else:
            self.line_status.setText("Direction labels swapped — draw the line to continue")

    def _save_line(self) -> None:
        line = self.preview.line()
        if self._active_video_id is None or line is None:
            return
        if self._active_combined_video is not None:
            combined_path = self._active_combined_video.path
            active_video = self.repository.get_video(self._active_video_id)
            updated_lines = list(active_video.counting_lines)
            if line.id is None:
                updated_lines.append(line)
            else:
                updated_lines = [line if saved.id == line.id else saved for saved in updated_lines]
            try:
                source_count = self.repository.replace_counting_lines_for_videos(
                    self._active_combined_video.source_video_ids,
                    updated_lines,
                )
            except (KeyError, ValueError) as exc:
                QMessageBox.warning(self, "Could not save line", str(exc))
                return
            self._refresh_all()
            combined = next(
                (item for item in self._combined_videos if item.path == combined_path),
                None,
            )
            if combined is not None:
                self._load_combined_line_video(combined, selected_line_name=line.name)
            self.line_status.setText(f"{line.name} saved to all {source_count} source recordings")
            self.save_line_button.setEnabled(False)
            return
        try:
            line_id = self.repository.save_counting_line(self._active_video_id, line)
        except ValueError as exc:
            QMessageBox.warning(self, "Could not save line", str(exc))
            return
        active_video_id = self._active_video_id
        self._refresh_all()
        self._load_line_video(active_video_id, line_id)
        self.line_status.setText(f"{line.name} saved for this recording")
        self.save_line_button.setEnabled(False)

    def _apply_line_to_day(self) -> None:
        if self._active_video_id is None:
            return
        if self._active_combined_video is not None:
            return
        answer = QMessageBox.question(
            self,
            "Apply lines and distant zone?",
            "Replace the saved line set and distant detection zone on the other recordings "
            "from this camera and date? "
            "Their existing detections and evidence will be removed so they can be reprocessed.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.repository.apply_counting_lines_to_day(self._active_video_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Timestamp required", str(exc))
            return
        self.line_status.setText(
            f"Lines and distant zone copied to {count} other recording fragments"
        )
        self.save_line_button.setEnabled(False)
        self.apply_day_line_button.setEnabled(False)
        self._refresh_all()

    def _process_selected(self) -> None:
        if self._combined_video_thread is not None:
            QMessageBox.information(
                self,
                "Combined video build in progress",
                "Finish or cancel Preprocessing before processing recordings.",
            )
            return
        if self._qc_video_thread is not None:
            QMessageBox.information(
                self,
                "Annotated video generation in progress",
                "Finish or cancel the annotated video before processing recordings.",
            )
            return
        if self._intake_thread is not None:
            QMessageBox.information(
                self,
                "Videos are loading",
                "Wait for video loading to finish or cancel it before processing.",
            )
            return
        video_ids = self._checked_video_ids()
        if not video_ids:
            QMessageBox.information(
                self,
                "Check recordings",
                "Check every recording you want to process first.",
            )
            return
        videos = [self.repository.get_video(video_id) for video_id in video_ids]
        problems = []
        if self.detection_source_mode.currentData() == "combined":
            selected_combined = [
                combined
                for combined in self._combined_videos
                if combined_row_key(combined) in self._detection_checked_video_ids
            ]
            video_by_id = {video.id: video for video in videos}
            for combined in selected_combined:
                sources = [
                    video_by_id[video_id]
                    for video_id in combined.source_video_ids
                    if video_id in video_by_id
                ]
                line_signatures = {
                    tuple(
                        (
                            line.name,
                            line.x1,
                            line.y1,
                            line.x2,
                            line.y2,
                            line.direction_a_label,
                            line.direction_b_label,
                        )
                        for line in video.counting_lines
                    )
                    for video in sources
                }
                if len(line_signatures) > 1:
                    problems.append(
                        f"{combined.path.name}: source recording lines do not match; "
                        "open the combined video in Line Setup and save them together"
                    )
        for video in videos:
            if (
                video.recorded_at is None
                or video.timestamp_source == TimestampSource.MISSING
                or (
                    video.timestamp_source == TimestampSource.BURNED_IN_OCR
                    and video.timestamp_confidence < 1
                )
            ):
                problems.append(f"{video.path.name}: verify the video timestamp")
            if not video.counting_lines:
                problems.append(f"{video.path.name}: save at least one counting line")
        if problems:
            QMessageBox.information(
                self,
                "Checked recordings need attention",
                "Fix these items before processing:\n\n" + "\n".join(problems),
            )
            return
        if self._thread is not None:
            return

        model_path, frame_stride, image_size = self.processing_mode.currentData()

        invalidated_qc_paths = {
            annotated_video_path(
                self.project_path,
                video.recording_day,
                video.camera,
                frame_stride,
            )
            for video in videos
            if video.recording_day is not None
        }
        if self._qc_video_output in invalidated_qc_paths:
            self.final_qc_player.clear()
            self._qc_video_output = None
            self.download_qc_video_button.setEnabled(False)
            # QMediaPlayer releases its Windows file handle asynchronously.
            QGuiApplication.processEvents()
        existing_qc_paths = {path for path in invalidated_qc_paths if path.exists()}
        locked_qc_paths = {
            path for path in existing_qc_paths if not try_remove_annotated_video(path)
        }
        if locked_qc_paths:
            self.statusBar().showMessage(
                "The previous annotated QC video is still closing. Processing will continue "
                "and replace it after Windows releases the file.",
                10000,
            )
        elif existing_qc_paths:
            self.statusBar().showMessage(
                "Previous annotated QC video removed because its recordings are being reprocessed.",
                8000,
            )

        self._thread = QThread(self)
        create_annotated_video = self.create_annotated_during_detection.isChecked()
        save_review_snapshots = self.save_review_snapshots.isChecked()
        self._worker = ProcessingWorker(
            self.project_path,
            video_ids,
            model_path,
            frame_stride,
            create_annotated_video=create_annotated_video,
            save_review_snapshots=save_review_snapshots,
            inference_batch_size=0,
            inference_image_size=image_size,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._processing_progress)
        self._worker.stage.connect(self._processing_stage)
        self._worker.assembly_progress.connect(self._processing_assembly_progress)
        self._worker.annotated_ready.connect(self._processing_annotated_ready)
        self._worker.annotated_warning.connect(self._processing_annotation_warning)
        self._worker.completed.connect(self._processing_complete)
        self._worker.cancelled.connect(self._processing_cancelled)
        self._worker.failed.connect(self._processing_failed)
        self._worker.finished.connect(self._processing_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self.process_button.setEnabled(False)
        self.add_video_button.setEnabled(False)
        self.create_annotated_during_detection.setEnabled(False)
        self.save_review_snapshots.setEnabled(False)
        self.processing_mode.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._processing_video_count = len(videos)
        self._processing_save_review_snapshots = save_review_snapshots
        noun = "recording" if len(videos) == 1 else "recordings"
        self.progress.setFormat(
            f"Starting detector for {len(videos)} {noun} · "
            f"{self.processing_mode.currentText()}…"
        )
        self._processing_started_at = monotonic()
        self._processing_phase = "detection"
        self._processing_current = 0
        self._processing_total = 1000
        self._processing_detection_share = 900 if create_annotated_video else 1000
        self._processing_annotated_outputs = []
        self._processing_annotation_warnings = []
        self._refresh_progress_times()
        self._thread.start()

    def _cancel_processing(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.progress.setFormat("Cancelling after current inference batch…")

    def _processing_progress(self, current: int, total: int) -> None:
        value = int(current / total * self._processing_detection_share) if total else 0
        self.progress.setValue(value)
        self.progress.setFormat(f"Detecting {current:,} / {total:,} frames — %p% overall")
        self._processing_current = value
        self._processing_total = 1000
        self._refresh_progress_times()

    def _processing_stage(self, stage: str) -> None:
        self.progress.setFormat(stage)

    def _processing_assembly_progress(self, current: int, total: int, stage: str) -> None:
        self._processing_phase = "assembly"
        assembly_share = 1000 - self._processing_detection_share
        assembly_value = int(current / total * assembly_share) if total else 0
        value = min(self._processing_detection_share + assembly_value, 1000)
        self._processing_current = value
        self._processing_total = 1000
        self.progress.setValue(value)
        self.progress.setFormat(f"{stage} — %p% overall")
        self._refresh_progress_times()

    def _processing_annotated_ready(self, paths: list[str]) -> None:
        self._processing_annotated_outputs = [Path(path) for path in paths]

    def _processing_annotation_warning(self, message: str) -> None:
        self._processing_annotation_warnings.append(message)

    def _processing_complete(self, count: int) -> None:
        self.progress.setValue(1000)
        recording_noun = "recording" if self._processing_video_count == 1 else "recordings"
        self.progress.setFormat(
            f"Complete — {self._processing_video_count} {recording_noun} processed; "
            + (
                f"{count:,} crossing events ready for review"
                if self._processing_save_review_snapshots
                else f"{count:,} crossing events automatically accepted"
            )
            + (
                f"; {len(self._processing_annotated_outputs)} annotated QC "
                + (
                    "video ready"
                    if len(self._processing_annotated_outputs) == 1
                    else "videos ready"
                )
                if self._processing_annotated_outputs
                else ""
            )
        )
        elapsed = (
            monotonic() - self._processing_started_at
            if self._processing_started_at is not None
            else 0.0
        )
        self.processing_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining 00:00:00")
        self._processing_started_at = None
        self._processing_phase = "idle"
        if self._processing_annotation_warnings:
            QMessageBox.warning(
                self,
                "Annotated video needs attention",
                "\n\n".join(self._processing_annotation_warnings),
            )

    def _processing_failed(self, message: str) -> None:
        self.progress.setFormat("Processing failed")
        self._finish_processing_time_without_estimate()
        QMessageBox.critical(self, "Processing failed", message)

    def _processing_cancelled(self) -> None:
        self.progress.setFormat(f"Cancelled at {self.progress.value() / 10:.1f}% overall")
        self._finish_processing_time_without_estimate()
        self.statusBar().showMessage("Processing cancelled", 5000)

    def _processing_finished(self) -> None:
        if self._processing_started_at is not None:
            self._finish_processing_time_without_estimate()
        self.process_button.setEnabled(self._intake_worker is None)
        self.add_video_button.setEnabled(self._intake_worker is None)
        self.create_annotated_during_detection.setEnabled(True)
        self.save_review_snapshots.setEnabled(True)
        self.processing_mode.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self._worker = None
        self._thread = None
        self._refresh_all()
        if (
            self._close_requested
            and self._intake_worker is None
            and self._qc_video_worker is None
            and self._combined_video_worker is None
            and self._weather_worker is None
        ):
            QTimer.singleShot(0, self.close)

    def _refresh_progress_times(self) -> None:
        now = monotonic()
        if self._intake_started_at is not None:
            self.intake_time_label.setText(
                progress_time_text(
                    self._intake_started_at,
                    self._intake_current,
                    self._intake_total,
                    now,
                )
            )
        if self._processing_started_at is not None:
            self.processing_time_label.setText(
                progress_time_text(
                    self._processing_started_at,
                    self._processing_current,
                    self._processing_total,
                    now,
                )
            )
        if self._qc_video_started_at is not None:
            self.final_qc_time_label.setText(
                progress_time_text(
                    self._qc_video_started_at,
                    self._qc_video_current,
                    self._qc_video_total,
                    now,
                )
            )
        if self._combined_video_started_at is not None:
            self.combined_video_time_label.setText(
                progress_time_text(
                    self._combined_video_started_at,
                    self._combined_video_current,
                    self._combined_video_total,
                    now,
                )
            )

    def _finish_processing_time_without_estimate(self) -> None:
        elapsed = (
            monotonic() - self._processing_started_at
            if self._processing_started_at is not None
            else 0.0
        )
        self.processing_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining —")
        self._processing_started_at = None
        self._processing_phase = "idle"

    def _save_detection_modes(self) -> None:
        selected = [mode for mode, checkbox in self.mode_checkboxes.items() if checkbox.isChecked()]
        try:
            self.repository.set_selected_modes(selected)
        except ValueError as exc:
            QMessageBox.warning(self, "Select object types", str(exc))
            return
        self.statusBar().showMessage("Detection choices saved: " + ", ".join(selected), 8000)

    def _change_zoom(self, delta: float) -> None:
        self._set_zoom(self._zoom_factor + delta)

    def _set_zoom(self, factor: float) -> None:
        self._zoom_factor = min(1.5, max(0.8, round(factor, 1)))
        self.repository.set_ui_zoom(self._zoom_factor)
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        scale = self._zoom_factor
        normal = round(14 * scale)
        title = round(28 * scale)
        version = round(12 * scale)
        section = round(17 * scale)
        vertical_padding = round(8 * scale)
        horizontal_padding = round(14 * scale)
        self.setStyleSheet(
            f"""
            QWidget {{ font-size: {normal}px; }}
            QLabel#title {{ font-size: {title}px; }}
            QLabel#sidebarVersion {{
                color: #64748b;
                font-size: {version}px;
                padding: {round(9 * scale)}px 2px 3px 2px;
                border-top: 1px solid #d9e2ec;
            }}
            QLabel#sectionTitle {{ font-size: {section}px; }}
            QPushButton {{ padding: {vertical_padding}px {horizontal_padding}px; }}
            QPushButton#lineModeButton:checked {{
                background: #2563eb;
                border: 2px solid #1d4ed8;
                color: white;
                font-weight: 700;
            }}
            QPushButton#zoneModeButton:checked {{
                background: #d97706;
                border: 2px solid #b45309;
                color: white;
                font-weight: 700;
            }}
            QTabBar::tab {{ padding: {round(11 * scale)}px {round(20 * scale)}px; }}
            QWidget#sidePanel {{
                background: #eef2f7;
                border: 1px solid #d7e0ea;
                border-radius: {round(14 * scale)}px;
            }}
            QTreeWidget#sideNavigation {{
                background: transparent;
                border: 0;
                outline: 0;
            }}
            QTreeWidget#sideNavigation::branch {{
                background: transparent;
                border: 0;
            }}
            QTreeWidget#sideNavigation::item {{
                background: rgba(255, 255, 255, 185);
                color: #1f2937;
                padding: {round(7 * scale)}px {round(8 * scale)}px;
                margin: {round(4 * scale)}px {round(2 * scale)}px;
                border: 1px solid transparent;
                border-radius: {round(9 * scale)}px;
            }}
            QTreeWidget#sideNavigation::item:hover {{
                background: #ffffff;
                border: 1px solid #b9cce8;
            }}
            QTreeWidget#sideNavigation::item:selected {{
                background: #173f73;
                color: white;
                border: 1px solid #173f73;
                border-left: 4px solid #61be81;
                border-radius: {round(9 * scale)}px;
            }}
            QHeaderView::section {{ padding: {round(9 * scale)}px; }}
            QLineEdit, QSpinBox, QComboBox, QDateTimeEdit {{
                padding: {round(7 * scale)}px;
            }}
            """
        )
        self.zoom_reset.setText(f"{round(scale * 100)}%")
        self._resize_brand_logo()
        self._content_container.setMinimumSize(round(1000 * scale), round(720 * scale))
        row_height = round(30 * scale)
        for name in (
            "project_table",
            "video_table",
            "line_video_table",
            "detection_video_table",
            "qc_table",
            "event_table",
            "report_table",
            "daily_report_table",
        ):
            table = getattr(self, name, None)
            if table is not None:
                table.verticalHeader().setDefaultSectionSize(row_height)
        self._resize_project_table()

    def _resize_brand_logo(self) -> None:
        width = round(70 * self._zoom_factor)
        height = round(78 * self._zoom_factor)
        self.brand_logo.setFixedSize(width, height)
        if self._brand_logo_pixmap.isNull():
            self.brand_logo.clear()
            return
        self.brand_logo.setPixmap(
            self._brand_logo_pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _delete_selected_events(self) -> None:
        event_ids = self._checked_event_ids()
        if not event_ids:
            QMessageBox.information(self, "Check detections", "Check detections to delete.")
            return
        answer = QMessageBox.question(
            self,
            "Delete checked detections?",
            f"Permanently delete {len(event_ids)} detections and their evidence frames?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repository.delete_events(event_ids)
            self._refresh_all()

    def _show_selected_evidence(self) -> None:
        row_index = self.event_table.currentRow()
        event_id = table_row_identity(self.event_table, row_index)
        row = getattr(self, "_event_rows_by_id", {}).get(event_id)
        if row is None:
            self.evidence_label.clear_evidence_pixmap()
            self.evidence_label.setText("Select a detection to view its boxed frame")
            self.evidence_details.clear()
            return
        evidence_path = row["evidence_path"]
        if evidence_path and Path(evidence_path).exists():
            pixmap = QPixmap(evidence_path)
            if not pixmap.isNull():
                self.evidence_label.set_evidence_pixmap(pixmap)
            else:
                self.evidence_label.clear_evidence_pixmap()
                self.evidence_label.setText("The saved snapshot could not be opened.")
        else:
            self.evidence_label.clear_evidence_pixmap()
            self.evidence_label.setText(
                "No snapshot was saved for this detection. To create snapshots, enable "
                "Save detection snapshots for manual review before processing."
            )
        self.evidence_details.setText(
            f"{row['mode']} · {row['line_name']} · {row['direction_label']} · "
            f"Confidence {row['confidence']:.2f} · Track {row['track_id']}\n"
            f"{row['occurred_at']} · {Path(row['video_path']).name}"
        )

    def _review_selected(self, status: ReviewStatus) -> None:
        event_ids = self._checked_event_ids()
        if not event_ids:
            QMessageBox.information(
                self, "Check detections", f"Check detections to mark as {status.value}."
            )
            return
        self.repository.set_review_status(event_ids, status)
        self._refresh_events()
        self._refresh_report()
        self._refresh_camera_comparison()
        self._refresh_daily_report()

    def _checked_event_ids(self) -> list[int]:
        checked = []
        for row in range(self.event_table.rowCount()):
            event_id = table_row_identity(self.event_table, row)
            item = self.event_table.item(row, 0)
            if (
                event_id is not None
                and item is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                checked.append(event_id)
        return checked

    def _set_all_event_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._updating_event_checks = True
        try:
            for row in range(self.event_table.rowCount()):
                item = self.event_table.item(row, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self._updating_event_checks = False
        self._event_check_anchor = None
        self._update_event_check_summary()

    def _event_selection_changed(self) -> None:
        if (
            not self._populating_event_table
            and QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            and not (
                self.event_table.currentColumn() == 0
                and QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton
            )
        ):
            self._check_highlighted_rows(self.event_table, "_updating_event_checks")
            self._update_event_check_summary()
        self._show_selected_evidence()

    def _event_check_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating_event_table or self._updating_event_checks or item.column() != 0:
            return
        row = item.row()
        if (
            QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            and self._event_check_anchor is not None
        ):
            self._set_check_range(
                self.event_table,
                self._event_check_anchor,
                row,
                item.checkState(),
                "_updating_event_checks",
            )
        self._event_check_anchor = row
        self._update_event_check_summary()

    def _update_event_check_summary(self) -> None:
        count = len(self._checked_event_ids())
        noun = "detection" if count == 1 else "detections"
        self.event_check_summary.setText(f"{count} {noun} checked")

    def _check_highlighted_rows(self, table: QTableWidget, guard_name: str) -> None:
        selection_model = table.selectionModel()
        if selection_model is None:
            return
        setattr(self, guard_name, True)
        try:
            for index in selection_model.selectedRows():
                check_item = table.item(index.row(), 0)
                if check_item is not None and check_item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    check_item.setCheckState(Qt.CheckState.Checked)
        finally:
            setattr(self, guard_name, False)

    def _set_check_range(
        self,
        table: QTableWidget,
        first_row: int,
        last_row: int,
        state: Qt.CheckState,
        guard_name: str,
    ) -> None:
        setattr(self, guard_name, True)
        try:
            for row in range(min(first_row, last_row), max(first_row, last_row) + 1):
                check_item = table.item(row, 0)
                if check_item is not None:
                    check_item.setCheckState(state)
        finally:
            setattr(self, guard_name, False)

    def _accept_pending_at_or_above_threshold(self) -> None:
        threshold_percent = self.review_confidence_threshold.value()
        pending_ids = self.repository.pending_event_ids_at_or_above(threshold_percent / 100)
        if not pending_ids:
            QMessageBox.information(
                self,
                "No matching detections",
                f"There are no pending detections at or above {threshold_percent}% confidence.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Accept matching pending detections?",
            f"Accept {len(pending_ids):,} pending detections at or above "
            f"{threshold_percent}% confidence?\n\n"
            "High confidence does not guarantee the class, direction, or line crossing is correct.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repository.set_review_status(pending_ids, ReviewStatus.ACCEPTED)
            self._refresh_events()
            self._refresh_report()
            self._refresh_camera_comparison()
            self._refresh_daily_report()
            self.statusBar().showMessage(
                f"Accepted {len(pending_ids):,} pending detections at or above "
                f"{threshold_percent}% confidence",
                8000,
            )

    def _export(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose export folder")
        if not directory:
            return
        bundle = export_clean_csvs(self.repository, Path(directory))
        QMessageBox.information(
            self,
            "Export complete",
            f"Created:\n{bundle.events.name}\n{bundle.hourly_counts.name}\n{bundle.coverage.name}",
        )

    def _create_report(self) -> None:
        day_text = self.report_day.currentData()
        camera = self.report_camera.currentData()
        if not day_text or not camera:
            QMessageBox.information(self, "No day selected", "Add timestamped recordings first.")
            return
        default = f"camera_report_{day_text}_{camera.replace(' ', '_')}.html"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save camera report", default, "HTML report (*.html)"
        )
        if path:
            output = generate_html_report(
                self.repository, Path(path), datetime.fromisoformat(day_text).date(), camera
            )
            QMessageBox.information(self, "Report created", output.name)

    def _create_camera_comparison_report(self) -> None:
        day_text = self.camera_comparison_day.currentData()
        if not day_text:
            QMessageBox.information(self, "No day selected", "Add timestamped recordings first.")
            return
        default = f"camera_comparison_{day_text}.html"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save camera comparison report", default, "HTML report (*.html)"
        )
        if path:
            output = generate_camera_comparison_html_report(
                self.repository,
                Path(path),
                datetime.fromisoformat(day_text).date(),
            )
            QMessageBox.information(self, "Report created", output.name)

    def _create_daily_report(self) -> None:
        if not available_days(self._videos):
            QMessageBox.information(self, "No dates available", "Add recordings first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Daily Trends",
            "daily_trends.html",
            "HTML report (*.html)",
        )
        if path:
            output = generate_daily_html_report(self.repository, Path(path))
            QMessageBox.information(self, "Report created", output.name)

    def _load_daily_weather(self) -> None:
        if self._weather_thread is not None:
            return
        days = available_days(self._videos)
        if not days:
            QMessageBox.information(
                self,
                "No dates available",
                "Add timestamped recordings before loading weather.",
            )
            return
        location = " ".join(self.daily_weather_location.text().split())
        if not location:
            QMessageBox.information(
                self,
                "Enter a location",
                "Enter a city or location for the weather data.",
            )
            return
        self._weather_thread = QThread(self)
        self._weather_worker = WeatherWorker(
            location,
            [recording_day.isoformat() for recording_day in days],
        )
        self._weather_worker.moveToThread(self._weather_thread)
        self._weather_thread.started.connect(self._weather_worker.run)
        self._weather_worker.completed.connect(self._daily_weather_loaded)
        self._weather_worker.failed.connect(self._daily_weather_failed)
        self._weather_worker.finished.connect(self._weather_thread.quit)
        self._weather_worker.finished.connect(self._weather_worker.deleteLater)
        self._weather_thread.finished.connect(self._daily_weather_finished)
        self._weather_thread.finished.connect(self._weather_thread.deleteLater)
        self.load_daily_weather_button.setEnabled(False)
        self.daily_weather_location.setEnabled(False)
        self.daily_weather_status.setText("Loading historical weather…")
        self._weather_thread.start()

    def _daily_weather_loaded(self, result) -> None:
        count = self.repository.save_daily_weather(
            self.daily_weather_location.text(),
            result.location_name,
            result.records,
        )
        self.daily_weather_location.setText(self.repository.get_weather_location())
        self.daily_weather_status.setText(
            f"{count} dates loaded for {result.location_name}"
        )
        self._refresh_daily_report()

    def _daily_weather_failed(self, message: str) -> None:
        self.daily_weather_status.setText("Weather could not be loaded")
        QMessageBox.warning(self, "Weather could not be loaded", message)

    def _daily_weather_finished(self) -> None:
        self._weather_worker = None
        self._weather_thread = None
        self.load_daily_weather_button.setEnabled(True)
        self.daily_weather_location.setEnabled(True)
        if (
            self._close_requested
            and self._worker is None
            and self._intake_worker is None
            and self._qc_video_worker is None
            and self._weather_worker is None
            and self._combined_video_worker is None
        ):
            QTimer.singleShot(0, self.close)

    def _refresh_all(self) -> None:
        self._videos = self.repository.list_videos()
        self._combined_videos = discover_combined_videos(self.project_path, self._videos)
        self._refresh_home()
        self._refresh_filters()
        self._refresh_videos()
        self._refresh_line_videos()
        self._refresh_detection_videos()
        self._refresh_qc()
        self._refresh_events()
        self._refresh_final_qc()
        self._refresh_report()
        self._refresh_camera_comparison()
        self._refresh_daily_report()

    def _update_project_title(self) -> None:
        self.setWindowTitle(f"OSBA Traffic Counter — {self._current_project.name}")

    def _refresh_projects(self) -> None:
        projects = self.project_catalog.list_projects()
        self._projects_by_path = {
            str(project.database_path.resolve()): project for project in projects
        }
        current_path = str(self.project_path.resolve())
        self.current_project_label.setText(
            f"Current project: {self._current_project.name}\n"
            f"{self._current_project.directory}"
        )
        sort_state = begin_table_refresh(self.project_table)
        self.project_table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            path_key = str(project.database_path.resolve())
            try:
                relative_folder = project.directory.relative_to(
                    self.project_catalog.data_root
                )
                folder_text = (
                    self.project_catalog.data_root.name
                    if relative_folder == Path(".")
                    else relative_folder.as_posix()
                )
            except ValueError:
                folder_text = str(project.directory)
            values = [
                project.name,
                folder_text,
                "Current" if path_key == current_path else "Available",
            ]
            for column, value in enumerate(values):
                item = SortableTableWidgetItem(value)
                item.setToolTip(str(project.directory))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, path_key)
                self.project_table.setItem(row, column, item)
        finish_table_refresh(self.project_table, sort_state)
        self._resize_project_table()
        self.move_default_project_button.setVisible(
            self.project_catalog.can_move_legacy_project()
        )
        self.project_table.clearSelection()
        for row in range(self.project_table.rowCount()):
            item = self.project_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == current_path:
                self.project_table.selectRow(row)
                break

    def _resize_project_table(self) -> None:
        table = getattr(self, "project_table", None)
        if table is None:
            return
        visible_rows = max(1, min(table.rowCount(), 5))
        header_height = max(
            table.horizontalHeader().height(),
            table.horizontalHeader().sizeHint().height(),
        )
        row_height = table.verticalHeader().defaultSectionSize()
        frame_height = table.frameWidth() * 2
        table.setFixedHeight(header_height + visible_rows * row_height + frame_height + 2)

    def _selected_project(self) -> ProjectInfo | None:
        row = self.project_table.currentRow()
        item = self.project_table.item(row, 0) if row >= 0 else None
        path_key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return getattr(self, "_projects_by_path", {}).get(path_key)

    def _create_project(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Create project",
            "Project or event name",
            text="",
        )
        if not ok:
            return
        try:
            project = self.project_catalog.create_project(name)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Project could not be created", str(exc))
            return
        self._switch_project(project)

    def _rename_selected_project(self) -> None:
        project = self._selected_project()
        if project is None:
            QMessageBox.information(self, "Select a project", "Select one project to rename.")
            return
        name, ok = QInputDialog.getText(
            self,
            "Rename project",
            "Project or event name",
            text=project.name,
        )
        if not ok:
            return
        current_project = project.database_path.resolve() == self.project_path.resolve()
        if current_project:
            active_tasks = self._active_project_tasks()
            if active_tasks:
                QMessageBox.information(
                    self,
                    "Finish current work first",
                    f"Finish or cancel {' and '.join(active_tasks)} before renaming the project.",
                )
                return
            self.combined_video_player.clear()
            self.final_qc_player.clear()
        try:
            renamed = self.project_catalog.rename_project(project, name)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Project could not be renamed", str(exc))
            return
        if current_project:
            self.repository = ProjectRepository(renamed.database_path)
            self.project_path = renamed.database_path
            self._current_project = renamed
            self._combined_video_output = None
            self._qc_video_output = None
            self._update_project_title()
            self._refresh_all()
        self._refresh_projects()
        self.statusBar().showMessage(
            f'Renamed project and folder to "{renamed.name}"', 7000
        )

    def _move_default_project(self) -> None:
        active_tasks = self._active_project_tasks()
        if active_tasks:
            QMessageBox.information(
                self,
                "Finish current work first",
                f"Finish or cancel {' and '.join(active_tasks)} before moving the project.",
            )
            return
        name, ok = QInputDialog.getText(
            self,
            "Move Default Project",
            "New project name",
            text="Test",
        )
        if not ok:
            return
        clean_name = " ".join(name.split())
        if not clean_name:
            QMessageBox.warning(self, "Enter a project name", "Enter a project name.")
            return
        answer = QMessageBox.question(
            self,
            "Move Default Project?",
            (
                f'Move the Default Project into a new project named "{clean_name}"?\n\n'
                "The database, evidence frames, preprocessed videos, and Final QC videos "
                "will move together. Original source recordings stay in their current "
                "locations.\n\n"
                "Before continuing, close OSBA Traffic Counter on every other computer."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            project = self.project_catalog.move_legacy_project(clean_name)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Default Project was not moved", str(exc))
            self._refresh_projects()
            return
        cleanup_note = ""
        if self.project_catalog.legacy_cleanup_pending():
            cleanup_note = (
                "\n\nWindows is still using the old database file. The app will remove "
                "that leftover file automatically the next time it starts."
            )
        self._switch_project(project)
        QMessageBox.information(
            self,
            "Default Project moved",
            (
                f'The old Default Project is now "{project.name}".\n'
                f"{project.directory}"
                f"{cleanup_note}"
            ),
        )

    def _open_selected_project(self) -> None:
        project = self._selected_project()
        if project is None:
            QMessageBox.information(self, "Select a project", "Select one project to open.")
            return
        self._switch_project(project)

    def _show_selected_project_folder(self) -> None:
        project = self._selected_project() or self._current_project
        try:
            project.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Project folder could not be opened", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(project.directory.resolve()))):
            QMessageBox.information(
                self,
                "Project folder",
                str(project.directory.resolve()),
            )

    def _active_project_tasks(self) -> list[str]:
        active_tasks: list[str] = []
        if self._worker is not None:
            active_tasks.append("detection")
        if self._intake_worker is not None:
            active_tasks.append("video loading")
        if self._qc_video_worker is not None:
            active_tasks.append("annotated video generation")
        if self._combined_video_worker is not None:
            active_tasks.append("Preprocessing")
        if self._weather_worker is not None:
            active_tasks.append("weather loading")
        return active_tasks

    def _switch_project(self, project: ProjectInfo) -> None:
        active_tasks = self._active_project_tasks()
        if active_tasks:
            QMessageBox.information(
                self,
                "Finish current work first",
                f"Finish or cancel {' and '.join(active_tasks)} before switching projects.",
            )
            return
        if project.database_path.resolve() == self.project_path.resolve():
            self.project_catalog.repair_project_video_paths(project)
            self.project_catalog.set_active(project)
            self._refresh_projects()
            return
        try:
            repository = ProjectRepository(project.database_path)
            self.project_catalog.repair_project_video_paths(project)
            self.project_catalog.set_active(project)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Project could not be opened", str(exc))
            return
        self.repository = repository
        self.project_path = project.database_path
        self._current_project = project
        self._zoom_factor = self.repository.get_ui_zoom()
        self._videos = []
        self._combined_videos = []
        self._detection_checked_video_ids.clear()
        self._active_video_id = None
        self._active_combined_video = None
        self._combined_video_output = None
        self._qc_video_output = None
        self.combined_video_player.clear()
        self.final_qc_player.clear()
        self._clear_line_editor()
        selected_modes = set(self.repository.get_selected_modes())
        for mode, checkbox in self.mode_checkboxes.items():
            checkbox.setChecked(mode in selected_modes)
        self._update_project_title()
        self._apply_zoom()
        self._refresh_all()
        self._navigate("home")
        self.statusBar().showMessage(f'Opened project "{project.name}"', 7000)

    def _refresh_home(self) -> None:
        self._refresh_projects()
        events = self.repository.list_events()
        pending = sum(row["review_status"] == ReviewStatus.PENDING for row in events)
        accepted = sum(row["review_status"] == ReviewStatus.ACCEPTED for row in events)
        missing_time = sum(
            video.timestamp_source == TimestampSource.MISSING
            or (
                video.timestamp_source == TimestampSource.BURNED_IN_OCR
                and video.timestamp_confidence < 1
            )
            for video in self._videos
        )
        missing_line = sum(not video.counting_lines for video in self._videos)
        days = len(available_days(self._videos))
        self.home_overview.setText(
            f"{self._current_project.name} · {len(self._videos)} recordings · {days} dates · "
            f"{pending} detections awaiting review · {accepted} accepted detections · "
            f"{missing_time} timestamps need verification · {missing_line} lines missing"
        )

    def _refresh_filters(self) -> None:
        days = available_days(self._videos)
        cameras = sorted({video.camera for video in self._videos})
        current_day = self.day_filter.currentData()
        self.day_filter.blockSignals(True)
        self.day_filter.clear()
        self.day_filter.addItem("All dates", None)
        for day in days:
            self.day_filter.addItem(day.isoformat(), day.isoformat())
        index = self.day_filter.findData(current_day)
        self.day_filter.setCurrentIndex(index if index >= 0 else 0)
        self.day_filter.blockSignals(False)

        for combo in (
            self.qc_day,
            self.final_qc_day,
            self.report_day,
            self.camera_comparison_day,
        ):
            selected = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for day in days:
                combo.addItem(day.isoformat(), day.isoformat())
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else (0 if days else -1))
            combo.blockSignals(False)
        for combo in (self.qc_camera, self.final_qc_camera, self.report_camera):
            selected = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for camera in cameras:
                combo.addItem(camera, camera)
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else (0 if cameras else -1))
            combo.blockSignals(False)

    def _refresh_videos(self) -> None:
        selected_day = self.day_filter.currentData()
        visible = [
            video
            for video in self._videos
            if selected_day is None
            or (video.recording_day and video.recording_day.isoformat() == selected_day)
        ]
        self._populating_video_table = True
        sort_state = begin_table_refresh(self.video_table)
        self.video_table.blockSignals(True)
        self.video_table.clearSelection()
        self.video_table.setRowCount(len(visible))
        for row, video in enumerate(visible):
            start = video.recorded_at.strftime("%H:%M:%S") if video.recorded_at else "Needs review"
            end = video.recorded_end_at.strftime("%H:%M:%S") if video.recorded_end_at else "—"
            source_names = {
                TimestampSource.FILENAME: "Filename",
                TimestampSource.BURNED_IN_OCR: "Legacy automatic time",
                TimestampSource.MANUAL_OVERLAY: "Manually edited",
                TimestampSource.MISSING: "Missing",
            }
            source_text = source_names[video.timestamp_source]
            if (
                video.timestamp_source == TimestampSource.BURNED_IN_OCR
                and video.timestamp_confidence < 1
            ):
                source_text = "Legacy time needs verification"
            values = [
                video.recording_day.isoformat() if video.recording_day else "—",
                video.path.name,
                video.camera,
                start,
                end,
                format_duration(video.duration_seconds),
                source_text,
                (
                    f"{len(video.counting_lines)} line"
                    + ("s" if len(video.counting_lines) != 1 else "")
                    if video.counting_lines
                    else "Missing"
                ),
                video.status.value.title(),
            ]
            sort_values = [
                video.recording_day.isoformat() if video.recording_day else "9999-12-31",
                video.path.name.casefold(),
                video.camera.casefold(),
                video.recorded_at.isoformat() if video.recorded_at else "9999-12-31T23:59:59",
                (
                    video.recorded_end_at.isoformat()
                    if video.recorded_end_at
                    else "9999-12-31T23:59:59"
                ),
                video.duration_seconds,
                source_text.casefold(),
                len(video.counting_lines),
                video.status.value,
            ]
            for column, (value, sort_value) in enumerate(zip(values, sort_values, strict=True)):
                table_item = SortableTableWidgetItem(value, sort_value)
                if column == 0:
                    table_item.setData(Qt.ItemDataRole.UserRole, video.id)
                self.video_table.setItem(row, column, table_item)
        finish_table_refresh(self.video_table, sort_state)
        self.video_table.blockSignals(False)
        self._populating_video_table = False

        valid = [
            video
            for video in visible
            if video.recorded_at
            and not (
                video.timestamp_source == TimestampSource.BURNED_IN_OCR
                and video.timestamp_confidence < 1
            )
        ]
        captured = sum(video.duration_seconds for video in valid) / 3600
        missing_timestamps = len(visible) - len(valid)
        if selected_day:
            self.day_summary.setText(
                f"{selected_day} contains {len(visible)} recording fragments "
                f"({captured:.2f} raw recording hours). Missing timestamps: {missing_timestamps}. "
                "Hourly coverage below accounts for gaps and overlaps."
            )
        else:
            day_count = len(
                {video.recording_day for video in visible if video.recording_day is not None}
            )
            self.day_summary.setText(
                f"{len(visible)} recording fragments across {day_count} dates. "
                f"Missing timestamps: {missing_timestamps}."
            )

    def _refresh_line_videos(self) -> None:
        self._populating_line_video_table = True
        sort_state = begin_table_refresh(self.line_video_table)
        self.line_video_table.blockSignals(True)
        combined_mode = self.line_source_mode.currentData() == "combined"
        entries = self._combined_videos if combined_mode else self._videos
        self.line_video_table.setRowCount(len(entries))
        video_by_id = {video.id: video for video in self._videos}
        for row, entry in enumerate(entries):
            if isinstance(entry, CombinedVideoRecord):
                sources = [
                    video_by_id[video_id]
                    for video_id in entry.source_video_ids
                    if video_id in video_by_id
                ]
                line_counts = {len(video.counting_lines) for video in sources}
                line_text = str(next(iter(line_counts))) if len(line_counts) == 1 else "Mixed"
                values = [
                    entry.day.isoformat(),
                    entry.coverage_start.strftime("%H:%M:%S"),
                    entry.path.name,
                    entry.camera,
                    line_text,
                ]
                sort_values = [
                    entry.day.isoformat(),
                    entry.coverage_start.isoformat(),
                    entry.path.name.casefold(),
                    entry.camera.casefold(),
                    next(iter(line_counts)) if len(line_counts) == 1 else -1,
                ]
                identity = combined_row_key(entry)
            else:
                values = [
                    entry.recording_day.isoformat() if entry.recording_day else "—",
                    (
                        entry.recorded_at.strftime("%H:%M:%S")
                        if entry.recorded_at
                        else "Needs review"
                    ),
                    entry.path.name,
                    entry.camera,
                    str(len(entry.counting_lines)),
                ]
                sort_values = [
                    (entry.recording_day.isoformat() if entry.recording_day else "9999-12-31"),
                    (entry.recorded_at.isoformat() if entry.recorded_at else "9999-12-31T23:59:59"),
                    entry.path.name.casefold(),
                    entry.camera.casefold(),
                    len(entry.counting_lines),
                ]
                identity = entry.id
            for column, (value, sort_value) in enumerate(zip(values, sort_values, strict=True)):
                table_item = SortableTableWidgetItem(value, sort_value)
                if column == 0:
                    table_item.setData(Qt.ItemDataRole.UserRole, identity)
                self.line_video_table.setItem(row, column, table_item)
        finish_table_refresh(self.line_video_table, sort_state)
        self.line_video_table.clearSelection()
        active_key = (
            combined_row_key(self._active_combined_video)
            if self._active_combined_video is not None
            else self._active_video_id
        )
        active_row = (
            find_table_row_by_key(self.line_video_table, active_key)
            if active_key is not None
            else None
        )
        if active_row is not None:
            self.line_video_table.selectRow(active_row)
            if self._active_combined_video is not None:
                current = next(
                    (
                        video
                        for video in self._combined_videos
                        if combined_row_key(video) == active_key
                    ),
                    None,
                )
                if current is not None:
                    self._active_combined_video = current
                    self.line_heading.setText(
                        f"{current.path.name} · {current.camera} · {current.day.isoformat()}"
                    )
            elif self._active_video_id is not None:
                video = next(video for video in self._videos if video.id == self._active_video_id)
                date_text = (
                    video.recording_day.isoformat() if video.recording_day else "timestamp missing"
                )
                self.line_heading.setText(f"{video.path.name} · {video.camera} · {date_text}")
        elif active_key is not None:
            self._active_video_id = None
            self._active_combined_video = None
            self._clear_line_editor()
        self.line_video_table.blockSignals(False)
        self._populating_line_video_table = False

    def _refresh_detection_videos(self) -> None:
        combined_mode = self.detection_source_mode.currentData() == "combined"
        valid_ids = (
            {combined_row_key(video) for video in self._combined_videos}
            if combined_mode
            else {video.id for video in self._videos}
        )
        self._detection_checked_video_ids.intersection_update(valid_ids)
        self._populating_detection_table = True
        sort_state = begin_table_refresh(self.detection_video_table)
        self.detection_video_table.blockSignals(True)
        self.detection_video_table.clearSelection()
        entries = self._combined_videos if combined_mode else self._videos
        self.detection_video_table.setRowCount(len(entries))
        video_by_id = {video.id: video for video in self._videos}
        for row, entry in enumerate(entries):
            if isinstance(entry, CombinedVideoRecord):
                sources = [
                    video_by_id[video_id]
                    for video_id in entry.source_video_ids
                    if video_id in video_by_id
                ]
                line_signatures = {
                    tuple(
                        (
                            line.name,
                            line.x1,
                            line.y1,
                            line.x2,
                            line.y2,
                            line.direction_a_label,
                            line.direction_b_label,
                        )
                        for line in video.counting_lines
                    )
                    for video in sources
                }
                if not sources:
                    readiness = "Source missing"
                elif any(not video.counting_lines for video in sources):
                    readiness = "Add line"
                elif len(line_signatures) != 1:
                    readiness = "Lines differ"
                else:
                    readiness = "Ready"
                line_count = len(sources[0].counting_lines) if sources else 0
                identity = combined_row_key(entry)
                values = [
                    entry.day.isoformat(),
                    entry.path.name,
                    entry.camera,
                    entry.coverage_start.strftime("%H:%M:%S"),
                    entry.coverage_end.strftime("%H:%M:%S"),
                    format_duration(sum(video.duration_seconds for video in sources)),
                    (
                        f"{line_count} line" + ("s" if line_count != 1 else "")
                        if line_count
                        else "Missing"
                    ),
                    readiness,
                ]
                sort_values = [
                    entry.day.isoformat(),
                    entry.path.name.casefold(),
                    entry.camera.casefold(),
                    entry.coverage_start.isoformat(),
                    entry.coverage_end.isoformat(),
                    sum(video.duration_seconds for video in sources),
                    line_count,
                    readiness.casefold(),
                ]
            else:
                identity = entry.id
                start = (
                    entry.recorded_at.strftime("%H:%M:%S") if entry.recorded_at else "Needs review"
                )
                end = entry.recorded_end_at.strftime("%H:%M:%S") if entry.recorded_end_at else "—"
                if (
                    entry.recorded_at is None
                    or entry.timestamp_source == TimestampSource.MISSING
                    or (
                        entry.timestamp_source == TimestampSource.BURNED_IN_OCR
                        and entry.timestamp_confidence < 1
                    )
                ):
                    readiness = "Verify time"
                elif not entry.counting_lines:
                    readiness = "Add line"
                else:
                    readiness = "Ready"
                values = [
                    entry.recording_day.isoformat() if entry.recording_day else "—",
                    entry.path.name,
                    entry.camera,
                    start,
                    end,
                    format_duration(entry.duration_seconds),
                    (
                        f"{len(entry.counting_lines)} line"
                        + ("s" if len(entry.counting_lines) != 1 else "")
                        if entry.counting_lines
                        else "Missing"
                    ),
                    readiness,
                ]
                sort_values = [
                    (entry.recording_day.isoformat() if entry.recording_day else "9999-12-31"),
                    entry.path.name.casefold(),
                    entry.camera.casefold(),
                    (entry.recorded_at.isoformat() if entry.recorded_at else "9999-12-31T23:59:59"),
                    (
                        entry.recorded_end_at.isoformat()
                        if entry.recorded_end_at
                        else "9999-12-31T23:59:59"
                    ),
                    entry.duration_seconds,
                    len(entry.counting_lines),
                    readiness.casefold(),
                ]
            check_item = SortableTableWidgetItem("", 0)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked
                if identity in self._detection_checked_video_ids
                else Qt.CheckState.Unchecked
            )
            check_item.setData(Qt.ItemDataRole.UserRole, identity)
            self.detection_video_table.setItem(row, 0, check_item)
            for column, (value, sort_value) in enumerate(
                zip(values, sort_values, strict=True), start=1
            ):
                self.detection_video_table.setItem(
                    row, column, SortableTableWidgetItem(value, sort_value)
                )
        finish_table_refresh(self.detection_video_table, sort_state)
        self.detection_video_table.blockSignals(False)
        self._populating_detection_table = False
        self._detection_check_anchor = None
        self._update_video_check_summary()

    def _preprocessing_filter_changed(self) -> None:
        if self._combined_video_worker is not None:
            return
        self._combined_video_output = None
        self.combined_video_player.clear()
        self.download_combined_video_button.setEnabled(False)
        self._refresh_qc()
        self._final_qc_coverage_window_changed()

    def _refresh_qc(self) -> None:
        day_text = self.qc_day.currentData()
        camera = self.qc_camera.currentData()
        if not day_text or not camera:
            self.qc_summary.setText("Add timestamped recordings to prepare a combined daily video.")
            self.qc_timeline.clear()
            self.qc_video_table.setRowCount(0)
            self.qc_table.setRowCount(0)
            self.build_combined_video_button.setEnabled(False)
            self.download_combined_video_button.setEnabled(False)
            return
        start_hour = self.qc_start_hour.value()
        end_hour = self.qc_end_hour.value()
        if start_hour >= end_hour:
            self.qc_summary.setText("Expected end hour must be later than the start hour.")
            self.qc_timeline.clear()
            self.qc_video_table.setRowCount(0)
            self.qc_table.setRowCount(0)
            self.build_combined_video_button.setEnabled(False)
            self.download_combined_video_button.setEnabled(False)
            return
        day = datetime.fromisoformat(day_text).date()
        recordings = recordings_for_date(self._videos, day, camera)
        sort_state = begin_table_refresh(self.qc_video_table)
        self.qc_video_table.setRowCount(len(recordings))
        for row_index, video in enumerate(recordings):
            start = video.recorded_at.strftime("%H:%M:%S")
            end = video.recorded_end_at.strftime("%H:%M:%S") if video.recorded_end_at else "—"
            values = [
                video.path.name,
                start,
                end,
                format_duration(video.duration_seconds),
                f"{video.fps:.2f}",
                f"{video.frame_count:,}",
            ]
            sort_values = [
                video.path.name.casefold(),
                video.recorded_at.isoformat(),
                (
                    video.recorded_end_at.isoformat()
                    if video.recorded_end_at
                    else "9999-12-31T23:59:59"
                ),
                video.duration_seconds,
                video.fps,
                video.frame_count,
            ]
            for column, (value, sort_value) in enumerate(zip(values, sort_values, strict=True)):
                self.qc_video_table.setItem(
                    row_index, column, SortableTableWidgetItem(value, sort_value)
                )
        finish_table_refresh(self.qc_video_table, sort_state)

        gaps = find_recording_gaps(
            self._videos, day, camera, start_hour=start_hour, end_hour=end_hour
        )
        day_start = datetime.combine(day, datetime.min.time())
        window_start = day_start + timedelta(hours=start_hour)
        window_end = day_start + timedelta(hours=end_hour)
        segments = build_coverage_segments(day, start_hour, end_hour, gaps)
        self.qc_timeline.set_timeline(window_start, window_end, segments)
        expected_seconds = (window_end - window_start).total_seconds()
        gap_seconds = sum(gap.duration_seconds for gap in gaps)
        captured_seconds = expected_seconds - gap_seconds
        fragments = sum(
            video.camera == camera
            and video.recorded_at is not None
            and not (
                video.timestamp_source == TimestampSource.BURNED_IN_OCR
                and video.timestamp_confidence < 1
            )
            and (
                video.recorded_end_at
                or video.recorded_at + timedelta(seconds=video.duration_seconds)
            )
            > window_start
            and video.recorded_at < window_end
            for video in self._videos
        )
        summaries = build_hourly_summary(self._videos, [], day, camera)
        overlap_seconds = sum(summary.overlap_seconds for summary in summaries[start_hour:end_hour])
        unverified = sum(
            video.camera == camera
            and (
                video.recorded_at is None
                or (
                    video.timestamp_source == TimestampSource.BURNED_IN_OCR
                    and video.timestamp_confidence < 1
                )
            )
            for video in self._videos
        )
        coverage_percent = captured_seconds / expected_seconds * 100 if expected_seconds else 0
        gap_text = (
            "No gaps detected"
            if not gaps
            else f"{len(gaps)} gaps totaling {format_duration(gap_seconds)}"
        )
        self.qc_summary.setText(
            f"{gap_text} from {start_hour:02d}:00 to {end_hour:02d}:00 · "
            f"{coverage_percent:.1f}% covered · {fragments} recording fragments · "
            f"{format_duration(overlap_seconds)} overlap · "
            f"{unverified} camera recordings need timestamp verification."
        )
        self.qc_table.setRowCount(len(gaps))
        for row_index, gap in enumerate(gaps):
            values = [
                gap.kind,
                gap.start.strftime("%Y-%m-%d %H:%M:%S"),
                gap.end.strftime("%Y-%m-%d %H:%M:%S"),
                format_duration(gap.duration_seconds),
                gap.previous_recording or "—",
                gap.next_recording or "—",
            ]
            for column, value in enumerate(values):
                self.qc_table.setItem(row_index, column, QTableWidgetItem(value))

        output = combined_video_path(self.project_path, day, camera, start_hour, end_hour)
        output_is_current = combined_video_is_current(output, recordings, window_start, window_end)
        if output_is_current:
            if self._combined_video_output != output:
                self._combined_video_output = output
                self.combined_video_player.set_video(output)
            self.download_combined_video_button.setEnabled(True)
            self.combined_video_progress.setValue(1000)
            self.combined_video_progress.setFormat("Combined video ready")
        elif self._combined_video_worker is None:
            if self._combined_video_output is not None:
                self._combined_video_output = None
                self.combined_video_player.clear()
            self.download_combined_video_button.setEnabled(False)
            self.combined_video_progress.setValue(0)
            self.combined_video_progress.setFormat(
                "Source list changed — rebuild required" if output.exists() else "Ready"
            )
        busy = (
            self._combined_video_worker is not None
            or self._worker is not None
            or self._intake_worker is not None
            or self._qc_video_worker is not None
        )
        self.build_combined_video_button.setText(
            "Rebuild combined video" if output.exists() else "Build combined video"
        )
        self.build_combined_video_button.setEnabled(bool(recordings) and not busy)

    def _build_combined_video(self) -> None:
        if self._combined_video_thread is not None:
            return
        if (
            self._worker is not None
            or self._intake_worker is not None
            or self._qc_video_worker is not None
        ):
            QMessageBox.information(
                self,
                "Another task is running",
                "Finish or cancel the current video task before building the combined video.",
            )
            return
        day_text = self.qc_day.currentData()
        camera = self.qc_camera.currentData()
        if not day_text or not camera:
            return
        start_hour = self.qc_start_hour.value()
        end_hour = self.qc_end_hour.value()
        if start_hour >= end_hour:
            QMessageBox.information(
                self,
                "Expected hours need attention",
                "Set an expected end hour later than the start hour.",
            )
            return
        day = datetime.fromisoformat(day_text).date()
        recordings = recordings_for_date(self._videos, day, camera)
        if not recordings:
            QMessageBox.information(
                self,
                "No recordings",
                "No verified recordings overlap this date and camera.",
            )
            return
        day_start = datetime.combine(day, datetime.min.time())
        coverage_start = day_start + timedelta(hours=start_hour)
        coverage_end = day_start + timedelta(hours=end_hour)
        output = combined_video_path(self.project_path, day, camera, start_hour, end_hour)
        if output.exists():
            answer = QMessageBox.question(
                self,
                "Replace combined video?",
                "A combined video already exists for this date, camera, and expected window. "
                "Replace it?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.combined_video_player.clear()
        self._combined_video_output = None
        self._combined_video_thread = QThread(self)
        self._combined_video_worker = CombinedVideoWorker(
            self.project_path,
            [video.id for video in recordings],
            str(output),
            coverage_start.isoformat(),
            coverage_end.isoformat(),
        )
        self._combined_video_worker.moveToThread(self._combined_video_thread)
        self._combined_video_thread.started.connect(self._combined_video_worker.run)
        self._combined_video_worker.progress.connect(self._combined_video_progress)
        self._combined_video_worker.completed.connect(self._combined_video_complete)
        self._combined_video_worker.cancelled.connect(self._combined_video_cancelled)
        self._combined_video_worker.failed.connect(self._combined_video_failed)
        self._combined_video_worker.finished.connect(self._combined_video_thread.quit)
        self._combined_video_worker.finished.connect(self._combined_video_worker.deleteLater)
        self._combined_video_thread.finished.connect(self._combined_video_finished)
        self._combined_video_thread.finished.connect(self._combined_video_thread.deleteLater)
        self.build_combined_video_button.setEnabled(False)
        self.cancel_combined_video_button.setEnabled(True)
        self.download_combined_video_button.setEnabled(False)
        self.qc_day.setEnabled(False)
        self.qc_camera.setEnabled(False)
        self.qc_start_hour.setEnabled(False)
        self.qc_end_hour.setEnabled(False)
        self.process_button.setEnabled(False)
        self.add_video_button.setEnabled(False)
        self.generate_qc_video_button.setEnabled(False)
        self.combined_video_progress.setValue(0)
        self.combined_video_progress.setFormat("Preparing combined video…")
        self._combined_video_started_at = monotonic()
        self._combined_video_current = 0
        self._combined_video_total = sum(max(1, video.frame_count) for video in recordings)
        self._refresh_progress_times()
        self._combined_video_thread.start()

    def _cancel_combined_video(self) -> None:
        if self._combined_video_worker is not None:
            self._combined_video_worker.cancel()
            self.cancel_combined_video_button.setEnabled(False)
            self.combined_video_progress.setFormat("Cancelling the current join step…")

    def _combined_video_progress(self, current: int, total: int, stage: str) -> None:
        self._combined_video_current = current
        self._combined_video_total = total
        self.combined_video_progress.setValue(int(current / total * 1000) if total else 0)
        self.combined_video_progress.setFormat(f"{stage} — %p%")
        self._refresh_progress_times()

    def _combined_video_complete(self, path: str) -> None:
        self._combined_video_output = Path(path)
        self.combined_video_progress.setValue(1000)
        self.combined_video_progress.setFormat("Complete — combined video ready")
        elapsed = (
            monotonic() - self._combined_video_started_at
            if self._combined_video_started_at is not None
            else 0.0
        )
        self.combined_video_time_label.setText(
            f"Elapsed {format_clock(elapsed)} · Remaining 00:00:00"
        )
        self._combined_video_started_at = None
        self.combined_video_player.set_video(self._combined_video_output)
        self.download_combined_video_button.setEnabled(True)

    def _combined_video_cancelled(self) -> None:
        self.combined_video_progress.setFormat("Build cancelled")
        self._finish_combined_video_time_without_estimate()

    def _combined_video_failed(self, message: str) -> None:
        self.combined_video_progress.setFormat("Build failed")
        self._finish_combined_video_time_without_estimate()
        QMessageBox.critical(self, "Combined video failed", message)

    def _combined_video_finished(self) -> None:
        if self._combined_video_started_at is not None:
            self._finish_combined_video_time_without_estimate()
        self._combined_video_worker = None
        self._combined_video_thread = None
        self.cancel_combined_video_button.setEnabled(False)
        self.qc_day.setEnabled(True)
        self.qc_camera.setEnabled(True)
        self.qc_start_hour.setEnabled(True)
        self.qc_end_hour.setEnabled(True)
        self.process_button.setEnabled(self._intake_worker is None)
        self.add_video_button.setEnabled(self._intake_worker is None)
        self._refresh_all()
        if (
            self._close_requested
            and self._worker is None
            and self._intake_worker is None
            and self._qc_video_worker is None
        ):
            QTimer.singleShot(0, self.close)

    def _finish_combined_video_time_without_estimate(self) -> None:
        elapsed = (
            monotonic() - self._combined_video_started_at
            if self._combined_video_started_at is not None
            else 0.0
        )
        self.combined_video_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining —")
        self._combined_video_started_at = None

    def _save_combined_video_copy(self) -> None:
        source = self._combined_video_output
        if source is None or not source.exists():
            QMessageBox.information(self, "No combined video", "Build the combined video first.")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Download combined daily video",
            source.name,
            "MP4 video (*.mp4)",
        )
        if not destination:
            return
        destination_path = Path(destination)
        if destination_path.suffix.lower() != ".mp4":
            destination_path = destination_path.with_suffix(".mp4")
        if destination_path.resolve() != source.resolve():
            shutil.copy2(source, destination_path)
        QMessageBox.information(self, "Combined video saved", destination_path.name)

    def _final_qc_filter_changed(self) -> None:
        if self._qc_video_worker is not None:
            return
        day_text = self.final_qc_day.currentData()
        camera = self.final_qc_camera.currentData()
        day_index = self.qc_day.findData(day_text)
        camera_index = self.qc_camera.findData(camera)
        if day_index >= 0:
            self.qc_day.setCurrentIndex(day_index)
        if camera_index >= 0:
            self.qc_camera.setCurrentIndex(camera_index)
        self._qc_video_output = None
        self.final_qc_player.clear()
        self.download_qc_video_button.setEnabled(False)
        self._refresh_final_qc()

    def _final_qc_coverage_window_changed(self) -> None:
        self._refresh_final_qc()

    def _refresh_final_qc(self) -> None:
        self._refresh_final_qc_video_lists()
        day_text = self.final_qc_day.currentData()
        camera = self.final_qc_camera.currentData()
        if not day_text or not camera:
            self.final_qc_summary.setText(
                "Add timestamped recordings to create an annotated video."
            )
            self.generate_qc_video_button.setEnabled(False)
            return
        day = datetime.fromisoformat(day_text).date()
        recordings = recordings_for_date(self._videos, day, camera)
        self._final_qc_recordings = recordings
        start_hour = self.qc_start_hour.value()
        end_hour = self.qc_end_hour.value()
        recorded_seconds = sum(video.duration_seconds for video in recordings)
        cached_annotations_ready = False
        try:
            stride, _model_path, _modes = resolve_detection_settings(recordings)
            cached_annotations_ready = cached_annotated_fragments_ready(
                self.project_path, recordings, stride
            )
            ready = True
            existing = annotated_video_path(self.project_path, day, camera, stride)
            if existing.is_file() and existing.stat().st_size > 0:
                status_text = "Completed. Click View above to watch it."
            elif cached_annotations_ready:
                status_text = "Not generated. Generate will assemble the saved annotated frames."
            else:
                status_text = "Not generated. Generate will run detection and create the video."
        except ValueError as exc:
            status_text = f"Not ready to generate: {exc}"
            ready = False
        if start_hour >= end_hour:
            status_text = "Set an end hour later than the start hour in Preprocessing."
            ready = False
        recording_word = "recording" if len(recordings) == 1 else "recordings"
        self.final_qc_summary.setText(
            f"{day.isoformat()} · {camera} · {len(recordings)} {recording_word} · "
            f"{format_duration(recorded_seconds)}. {status_text}"
        )
        busy = (
            self._qc_video_worker is not None
            or self._worker is not None
            or self._intake_worker is not None
            or self._combined_video_worker is not None
        )
        self.generate_qc_video_button.setText(
            "Assemble cached annotated video"
            if cached_annotations_ready
            else "Generate full annotated video"
        )
        self.generate_qc_video_button.setEnabled(
            ready and not busy
        )

    def _refresh_final_qc_video_lists(self) -> None:
        completed = []
        not_generated = []
        camera_days = sorted(
            {
                (video.recording_day, video.camera)
                for video in self._videos
                if video.recording_day is not None
                and video.recorded_at is not None
                and video.timestamp_source != TimestampSource.MISSING
            },
            key=lambda value: (value[0], value[1].casefold()),
        )
        for day, camera in camera_days:
            recordings = recordings_for_date(self._videos, day, camera)
            output = None
            generation_error = None
            try:
                stride, _model_path, _modes = resolve_detection_settings(recordings)
                candidate = annotated_video_path(self.project_path, day, camera, stride)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    output = candidate
            except ValueError as exc:
                generation_error = str(exc)
            item = (day, camera, recordings, output, generation_error)
            (completed if output is not None else not_generated).append(item)

        self.final_qc_video_tabs.setTabText(0, f"Completed ({len(completed)})")
        self.final_qc_video_tabs.setTabText(
            1, f"Not generated ({len(not_generated)})"
        )
        if not completed and not_generated:
            self.final_qc_video_tabs.setCurrentIndex(1)
        busy = (
            self._qc_video_worker is not None
            or self._worker is not None
            or self._intake_worker is not None
            or self._combined_video_worker is not None
        )
        self._populate_final_qc_video_table(
            self.final_qc_completed_table, completed, "View", busy
        )
        self._populate_final_qc_video_table(
            self.final_qc_pending_table, not_generated, "Generate", busy
        )

    def _populate_final_qc_video_table(self, table, items, action_text, busy) -> None:
        table.setRowCount(len(items))
        for row, (day, camera, recordings, _output, generation_error) in enumerate(items):
            recorded_seconds = sum(video.duration_seconds for video in recordings)
            values = (
                day.isoformat(),
                camera,
                str(len(recordings)),
                format_duration(recorded_seconds),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
            action = QPushButton(action_text)
            action.setEnabled(not busy)
            if action_text == "View":
                action.clicked.connect(
                    lambda _checked=False, selected_day=day.isoformat(), selected_camera=camera: (
                        self._select_final_qc_video(
                            selected_day, selected_camera, generate=False
                        )
                    )
                )
            else:
                action.setObjectName("primaryButton")
                action.setEnabled(not busy and generation_error is None)
                if generation_error:
                    action.setToolTip(generation_error)
                action.clicked.connect(
                    lambda _checked=False, selected_day=day.isoformat(), selected_camera=camera: (
                        self._select_final_qc_video(
                            selected_day, selected_camera, generate=True
                        )
                    )
                )
            table.setCellWidget(row, 4, action)

    def _select_final_qc_video(self, day_text: str, camera: str, generate: bool) -> None:
        if self._qc_video_worker is not None:
            return
        self.final_qc_day.blockSignals(True)
        self.final_qc_camera.blockSignals(True)
        day_index = self.final_qc_day.findData(day_text)
        camera_index = self.final_qc_camera.findData(camera)
        if day_index >= 0:
            self.final_qc_day.setCurrentIndex(day_index)
        if camera_index >= 0:
            self.final_qc_camera.setCurrentIndex(camera_index)
        self.final_qc_day.blockSignals(False)
        self.final_qc_camera.blockSignals(False)
        self._final_qc_filter_changed()
        if generate:
            self._generate_final_qc_video()
            return
        day = datetime.fromisoformat(day_text).date()
        recordings = recordings_for_date(self._videos, day, camera)
        try:
            stride, _model_path, _modes = resolve_detection_settings(recordings)
        except ValueError:
            return
        output = annotated_video_path(self.project_path, day, camera, stride)
        if output.is_file() and output.stat().st_size > 0:
            self._qc_video_output = output
            self.final_qc_player.set_video(output)
            self.download_qc_video_button.setEnabled(True)
            self.final_qc_progress.setValue(1000)
            self.final_qc_progress.setFormat("Ready to view")

    def _generate_final_qc_video(self) -> None:
        if self._qc_video_thread is not None:
            return
        if self._combined_video_worker is not None:
            QMessageBox.information(
                self,
                "Combined video is being built",
                "Finish or cancel Preprocessing before generating the annotated video.",
            )
            return
        day_text = self.final_qc_day.currentData()
        camera = self.final_qc_camera.currentData()
        if not day_text or not camera:
            return
        day = datetime.fromisoformat(day_text).date()
        recordings = recordings_for_date(self._videos, day, camera)
        start_hour = self.qc_start_hour.value()
        end_hour = self.qc_end_hour.value()
        if start_hour >= end_hour:
            QMessageBox.information(
                self,
                "Expected hours need attention",
                "Set an expected end hour later than the start hour in Preprocessing.",
            )
            return
        day_start = datetime.combine(day, datetime.min.time())
        coverage_start = day_start + timedelta(hours=start_hour)
        coverage_end = day_start + timedelta(hours=end_hour)
        try:
            stride, model_path, _modes = resolve_detection_settings(recordings)
        except ValueError as exc:
            QMessageBox.information(self, "Annotated video is not ready", str(exc))
            return
        output = annotated_video_path(self.project_path, day, camera, stride)
        if output.exists():
            answer = QMessageBox.question(
                self,
                "Replace annotated video?",
                "An annotated video already exists for this date, camera, and stride. "
                "Replace it with a new QC export?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.final_qc_player.clear()
        self._qc_video_output = None
        self._qc_video_thread = QThread(self)
        self._qc_video_worker = AnnotatedVideoWorker(
            self.project_path,
            [video.id for video in recordings],
            str(output),
            model_path,
            stride,
            coverage_start.isoformat(),
            coverage_end.isoformat(),
        )
        self._qc_video_worker.moveToThread(self._qc_video_thread)
        self._qc_video_thread.started.connect(self._qc_video_worker.run)
        self._qc_video_worker.progress.connect(self._final_qc_video_progress)
        self._qc_video_worker.completed.connect(self._final_qc_video_complete)
        self._qc_video_worker.cancelled.connect(self._final_qc_video_cancelled)
        self._qc_video_worker.failed.connect(self._final_qc_video_failed)
        self._qc_video_worker.finished.connect(self._qc_video_thread.quit)
        self._qc_video_worker.finished.connect(self._qc_video_worker.deleteLater)
        self._qc_video_thread.finished.connect(self._final_qc_video_finished)
        self._qc_video_thread.finished.connect(self._qc_video_thread.deleteLater)
        self.generate_qc_video_button.setEnabled(False)
        self.cancel_qc_video_button.setEnabled(True)
        self.download_qc_video_button.setEnabled(False)
        self.final_qc_day.setEnabled(False)
        self.final_qc_camera.setEnabled(False)
        self.process_button.setEnabled(False)
        self.add_video_button.setEnabled(False)
        self.final_qc_progress.setValue(0)
        self.final_qc_progress.setFormat("Preparing annotated video…")
        self._qc_video_started_at = monotonic()
        self._qc_video_current = 0
        self._qc_video_total = sum(
            max(1, (video.frame_count + stride - 1) // stride) for video in recordings
        )
        self._refresh_progress_times()
        self._qc_video_thread.start()

    def _cancel_final_qc_video(self) -> None:
        if self._qc_video_worker is not None:
            self._qc_video_worker.cancel()
            self.cancel_qc_video_button.setEnabled(False)
            self.final_qc_progress.setFormat("Cancelling after the current frame…")

    def _final_qc_video_progress(self, current: int, total: int, stage: str) -> None:
        self._qc_video_current = current
        self._qc_video_total = total
        self.final_qc_progress.setValue(int(current / total * 1000) if total else 0)
        self.final_qc_progress.setFormat(f"{stage} — %p%")
        self._refresh_progress_times()

    def _final_qc_video_complete(self, path: str) -> None:
        self._qc_video_output = Path(path)
        self.final_qc_progress.setValue(1000)
        self.final_qc_progress.setFormat("Complete — annotated video ready")
        elapsed = (
            monotonic() - self._qc_video_started_at
            if self._qc_video_started_at is not None
            else 0.0
        )
        self.final_qc_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining 00:00:00")
        self._qc_video_started_at = None
        self.final_qc_player.set_video(self._qc_video_output)
        self.download_qc_video_button.setEnabled(True)

    def _final_qc_video_cancelled(self) -> None:
        self.final_qc_progress.setFormat("Generation cancelled")
        self._finish_final_qc_time_without_estimate()

    def _final_qc_video_failed(self, message: str) -> None:
        self.final_qc_progress.setFormat("Generation failed")
        self._finish_final_qc_time_without_estimate()
        QMessageBox.critical(self, "Annotated video failed", message)

    def _final_qc_video_finished(self) -> None:
        if self._qc_video_started_at is not None:
            self._finish_final_qc_time_without_estimate()
        self._qc_video_worker = None
        self._qc_video_thread = None
        self.cancel_qc_video_button.setEnabled(False)
        self.final_qc_day.setEnabled(True)
        self.final_qc_camera.setEnabled(True)
        self.process_button.setEnabled(self._intake_worker is None)
        self.add_video_button.setEnabled(self._intake_worker is None)
        self._refresh_final_qc()
        if (
            self._close_requested
            and self._worker is None
            and self._intake_worker is None
            and self._combined_video_worker is None
            and self._weather_worker is None
        ):
            QTimer.singleShot(0, self.close)

    def _finish_final_qc_time_without_estimate(self) -> None:
        elapsed = (
            monotonic() - self._qc_video_started_at
            if self._qc_video_started_at is not None
            else 0.0
        )
        self.final_qc_time_label.setText(f"Elapsed {format_clock(elapsed)} · Remaining —")
        self._qc_video_started_at = None

    def _save_final_qc_video_copy(self) -> None:
        source = self._qc_video_output
        if source is None or not source.exists():
            QMessageBox.information(self, "No annotated video", "Generate the video first.")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Download annotated QC video",
            source.name,
            "MP4 video (*.mp4)",
        )
        if not destination:
            return
        destination_path = Path(destination)
        if destination_path.suffix.lower() != ".mp4":
            destination_path = destination_path.with_suffix(".mp4")
        if destination_path.resolve() != source.resolve():
            shutil.copy2(source, destination_path)
        QMessageBox.information(self, "Annotated video saved", destination_path.name)

    def _refresh_events(self) -> None:
        checked_ids = set(self._checked_event_ids())
        all_rows = self.repository.list_events()
        counts = Counter(row["review_status"] for row in all_rows)
        tab_details = (
            ("Pending", ReviewStatus.PENDING.value),
            ("Accepted", ReviewStatus.ACCEPTED.value),
            ("Rejected", ReviewStatus.REJECTED.value),
            ("All", None),
        )
        for index, (label, status) in enumerate(tab_details):
            count = len(all_rows) if status is None else counts[status]
            self.review_filter_tabs.setTabText(index, f"{label} ({count:,})")
        selected_status = self.review_filter_tabs.tabData(self.review_filter_tabs.currentIndex())
        rows = [
            row
            for row in all_rows
            if selected_status is None or row["review_status"] == selected_status
        ]
        self._event_rows_by_id = {int(row["id"]): row for row in rows}
        self._populating_event_table = True
        sort_state = begin_table_refresh(self.event_table)
        self.event_table.blockSignals(True)
        self.event_table.clearSelection()
        self.event_table.setRowCount(len(rows))
        for table_row, row in enumerate(rows):
            check_item = SortableTableWidgetItem("", 0)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if row["id"] in checked_ids else Qt.CheckState.Unchecked
            )
            check_item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.event_table.setItem(table_row, 0, check_item)
            event_time = (
                datetime.fromisoformat(row["occurred_at"]).strftime("%Y-%m-%d %H:%M:%S")
                if row["occurred_at"]
                else "Missing"
            )
            values = [
                row["review_status"].title(),
                event_time,
                Path(row["video_path"]).name,
                row["mode"],
                row["direction_label"],
                row["line_name"],
                f"{row['confidence']:.2f}",
                str(row["track_id"]),
                row["camera"],
            ]
            sort_values = [
                row["review_status"],
                row["occurred_at"] or "9999-12-31T23:59:59",
                Path(row["video_path"]).name.casefold(),
                row["mode"].casefold(),
                row["direction_label"].casefold(),
                row["line_name"].casefold(),
                float(row["confidence"]),
                int(row["track_id"]),
                row["camera"].casefold(),
            ]
            for column, (value, sort_value) in enumerate(
                zip(values, sort_values, strict=True), start=1
            ):
                self.event_table.setItem(
                    table_row, column, SortableTableWidgetItem(value, sort_value)
                )
        finish_table_refresh(self.event_table, sort_state)
        self.event_table.blockSignals(False)
        self._populating_event_table = False
        self._event_check_anchor = None
        self._update_event_check_summary()
        self._show_selected_evidence()

    def _current_summary(self, day_combo: QComboBox, camera_combo: QComboBox):
        day_text = day_combo.currentData()
        camera = camera_combo.currentData()
        if not day_text or not camera:
            return None, None, []
        day = datetime.fromisoformat(day_text).date()
        events = self.repository.list_events(ReviewStatus.ACCEPTED)
        return day, camera, build_hourly_summary(self._videos, events, day, camera)

    @staticmethod
    def _build_hourly_chart(summaries, title: str, modes) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        if summaries:
            series = QStackedBarSeries()
            for mode in modes:
                bar_set = QBarSet(mode)
                bar_set.append([summary.counts[mode] for summary in summaries])
                series.append(bar_set)
            chart.addSeries(series)
            axis_x = QBarCategoryAxis()
            axis_x.append([f"{hour:02d}" for hour in range(24)])
            axis_y = QValueAxis()
            axis_y.setTitleText("Counts")
            axis_y.setLabelFormat("%d")
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
            chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        return chart

    @staticmethod
    def _build_camera_chart(summaries, title: str, modes, metric: str) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        if summaries:
            per_recorded_hour = metric == "per_recorded_hour"
            series = QStackedBarSeries()
            for mode in modes:
                bar_set = QBarSet(mode)
                bar_set.append(
                    [
                        summary.crossings_per_recorded_hour(mode)
                        if per_recorded_hour
                        else summary.counts[mode]
                        for summary in summaries
                    ]
                )
                series.append(bar_set)
            chart.addSeries(series)
            axis_x = QBarCategoryAxis()
            axis_x.append([summary.camera for summary in summaries])
            axis_y = QValueAxis()
            axis_y.setTitleText(
                "Counts per recorded hour"
                if per_recorded_hour
                else "Total counts"
            )
            axis_y.setLabelFormat("%.1f" if per_recorded_hour else "%d")
            maximum = max(
                (summary.crossings_per_recorded_hour() if per_recorded_hour else summary.total)
                for summary in summaries
            )
            axis_y.setRange(0, max(1, maximum))
            axis_y.applyNiceNumbers()
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
            chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        return chart

    @staticmethod
    def _build_camera_hourly_chart(hourly_by_camera, title: str) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        if hourly_by_camera:
            maximum = 0
            series_list = []
            for camera, summaries in hourly_by_camera:
                series = QLineSeries()
                series.setName(camera)
                for hour, summary in enumerate(summaries):
                    series.append(hour, summary.total)
                    maximum = max(maximum, summary.total)
                chart.addSeries(series)
                series_list.append(series)
            axis_x = QValueAxis()
            axis_x.setTitleText("Hour")
            axis_x.setRange(0, 23)
            axis_x.setTickCount(13)
            axis_x.setLabelFormat("%d")
            axis_y = QValueAxis()
            axis_y.setTitleText("Counts")
            axis_y.setLabelFormat("%d")
            axis_y.setRange(0, max(1, maximum))
            axis_y.applyNiceNumbers()
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            for series in series_list:
                series.attachAxis(axis_x)
                series.attachAxis(axis_y)
            chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        return chart

    @staticmethod
    def _build_camera_hourly_stacked_chart(hourly_by_camera, title: str) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        if hourly_by_camera:
            series = QStackedBarSeries()
            for camera, summaries in hourly_by_camera:
                bar_set = QBarSet(camera)
                bar_set.append([summary.total for summary in summaries])
                series.append(bar_set)
            chart.addSeries(series)
            axis_x = QBarCategoryAxis()
            axis_x.append([f"{hour:02d}" for hour in range(24)])
            axis_y = QValueAxis()
            axis_y.setTitleText("Counts")
            axis_y.setLabelFormat("%d")
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
            axis_y.setRange(0, max(1, maximum))
            axis_y.applyNiceNumbers()
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
            chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        return chart

    def _populate_camera_hourly_panels(self, hourly_by_camera, day: date | None) -> None:
        while self.camera_comparison_panels_layout.count():
            item = self.camera_comparison_panels_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not hourly_by_camera:
            message = QLabel("No camera hourly plots are available for this date.")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.camera_comparison_panels_layout.addWidget(message, 0, 0)
            return
        for index, (camera, summaries) in enumerate(hourly_by_camera):
            title = (
                f"Hourly counts · {camera} · {day.isoformat()}"
                if day is not None
                else f"Hourly counts · {camera}"
            )
            chart_view = QChartView(
                self._build_hourly_chart(summaries, title, MODES)
            )
            chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
            chart_view.setMinimumSize(460, 320)
            self.camera_comparison_panels_layout.addWidget(
                chart_view,
                index // 2,
                index % 2,
            )
        self.camera_comparison_panels_layout.setColumnStretch(0, 1)
        self.camera_comparison_panels_layout.setColumnStretch(1, 1)

    def _refresh_camera_comparison(self) -> None:
        day_text = self.camera_comparison_day.currentData()
        metric = self.camera_comparison_metric.currentData() or "direction_summary"
        show_summary = metric == "direction_summary"
        show_hourly_charts = metric == "hourly_by_camera"
        show_panels = metric == "hourly_panels"
        show_chart = metric in {"total", "per_recorded_hour"}
        self.camera_comparison_table.setVisible(show_summary)
        self.camera_comparison_chart.setVisible(show_chart)
        self.camera_comparison_hourly_charts.setVisible(show_hourly_charts)
        self.camera_comparison_panels.setVisible(show_panels)
        if not day_text:
            self.camera_comparison_quality.setText("Add timestamped recordings to compare cameras.")
            self._populate_camera_comparison_table([])
            if show_chart:
                self.camera_comparison_chart.setChart(
                    self._build_camera_chart(
                        [], "Add recordings to compare cameras", MODES, metric
                    )
                )
            if show_hourly_charts:
                self.camera_comparison_hourly_line_chart.setChart(
                    self._build_camera_hourly_chart(
                        [], "Add recordings to compare hourly counts — line chart"
                    )
                )
                self.camera_comparison_hourly_stacked_chart.setChart(
                    self._build_camera_hourly_stacked_chart(
                        [], "Add recordings to compare hourly counts — stacked bar chart"
                    )
                )
            if show_panels:
                self._populate_camera_hourly_panels([], None)
            return
        day = datetime.fromisoformat(day_text).date()
        events = self.repository.list_events(ReviewStatus.ACCEPTED)
        summaries = build_camera_summary(self._videos, events, day)
        if not summaries:
            self.camera_comparison_quality.setText(
                f"No timestamped camera recordings are available for {day.isoformat()}."
            )
            self._populate_camera_comparison_table([])
            if show_chart:
                self.camera_comparison_chart.setChart(
                    self._build_camera_chart(
                        [], "No cameras available for this date", MODES, metric
                    )
                )
            if show_hourly_charts:
                self.camera_comparison_hourly_line_chart.setChart(
                    self._build_camera_hourly_chart(
                        [], "No camera hourly counts available — line chart"
                    )
                )
                self.camera_comparison_hourly_stacked_chart.setChart(
                    self._build_camera_hourly_stacked_chart(
                        [], "No camera hourly counts available — stacked bar chart"
                    )
                )
            if show_panels:
                self._populate_camera_hourly_panels([], day)
            return

        hourly_by_camera = (
            [
                (
                    summary.camera,
                    build_hourly_summary(self._videos, events, day, summary.camera),
                )
                for summary in summaries
            ]
            if metric in {"hourly_by_camera", "hourly_panels"}
            else []
        )
        coverage_hours = [summary.recorded_hours for summary in summaries]
        accepted_total = sum(summary.total for summary in summaries)
        accepted_enters = sum(summary.direction_counts["Enter"] for summary in summaries)
        accepted_exits = sum(summary.direction_counts["Exit"] for summary in summaries)
        if metric == "direction_summary":
            metric_note = "The table separates accepted Enter and Exit counts by camera."
        elif metric == "per_recorded_hour":
            metric_note = (
                "Rates use each camera's unique recorded coverage; overlapping fragments "
                "count once."
            )
        elif metric == "hourly_by_camera":
            metric_note = (
                "The line and stacked bar charts show each camera's total counts for every hour."
            )
        elif metric == "hourly_panels":
            metric_note = (
                "Each panel repeats the Camera Report hourly class plot for one camera."
            )
        else:
            metric_note = (
                "Raw totals are shown; use counts per recorded hour when coverage differs."
            )
        self.camera_comparison_quality.setText(
            f"{len(summaries)} cameras compared for {day.isoformat()} with "
            f"{accepted_total:,} accepted counts ({accepted_enters:,} Enter and "
            f"{accepted_exits:,} Exit). Recorded coverage ranges from "
            f"{min(coverage_hours):.2f} to {max(coverage_hours):.2f} hours per camera. "
            f"{metric_note}"
        )
        if show_chart:
            title = (
                "Counts per recorded hour by camera"
                if metric == "per_recorded_hour"
                else "Total counts by camera"
            )
            self.camera_comparison_chart.setChart(
                self._build_camera_chart(
                    summaries,
                    f"{title} · {day.isoformat()}",
                    MODES,
                    metric,
                )
            )
        if show_hourly_charts:
            self.camera_comparison_hourly_line_chart.setChart(
                self._build_camera_hourly_chart(
                    hourly_by_camera,
                    f"Hourly counts by camera — line chart · {day.isoformat()}",
                )
            )
            self.camera_comparison_hourly_stacked_chart.setChart(
                self._build_camera_hourly_stacked_chart(
                    hourly_by_camera,
                    f"Hourly counts by camera — stacked bar chart · {day.isoformat()}",
                )
            )
        if show_panels:
            self._populate_camera_hourly_panels(hourly_by_camera, day)
        self._populate_camera_comparison_table(summaries)

    def _populate_camera_comparison_table(self, summaries) -> None:
        self.camera_comparison_table.setRowCount(len(summaries) + (1 if summaries else 0))
        total_enter = 0
        total_exit = 0
        for row_index, summary in enumerate(summaries):
            enters = summary.direction_counts["Enter"]
            exits = summary.direction_counts["Exit"]
            total_enter += enters
            total_exit += exits
            values = [summary.camera, f"{enters:,}", f"{exits:,}", f"{summary.total:,}"]
            for column, value in enumerate(values):
                sort_value = value if column == 0 else int(value.replace(",", ""))
                self.camera_comparison_table.setItem(
                    row_index,
                    column,
                    SortableTableWidgetItem(value, sort_value),
                )
        if summaries:
            total_row = len(summaries)
            values = [
                "Overall total",
                f"{total_enter:,}",
                f"{total_exit:,}",
                f"{sum(summary.total for summary in summaries):,}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setBackground(Qt.GlobalColor.lightGray)
                self.camera_comparison_table.setItem(total_row, column, item)

    @staticmethod
    def _create_daily_weather_strip() -> tuple[QScrollArea, QHBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(76)
        scroll.setMaximumHeight(92)
        container = QWidget()
        weather_layout = QHBoxLayout(container)
        weather_layout.setContentsMargins(8, 6, 8, 6)
        weather_layout.setSpacing(10)
        scroll.setWidget(container)
        scroll.setVisible(False)
        return scroll, weather_layout

    @staticmethod
    def _weather_icon_pixmap(weather_code: int, size: int = 30) -> QPixmap:
        pixmap = QPixmap()
        pixmap.loadFromData(
            weather_icon_svg(weather_code, size).encode("utf-8"),
            "SVG",
        )
        return pixmap

    def _populate_daily_weather_strip(
        self,
        scroll: QScrollArea,
        weather_layout: QHBoxLayout,
        summaries,
        weather_by_day,
    ) -> None:
        while weather_layout.count():
            item = weather_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        available = [
            (summary, weather_by_day.get(summary.day))
            for summary in summaries
            if weather_by_day.get(summary.day) is not None
        ]
        scroll.setVisible(bool(available))
        for summary, weather in available:
            card = QWidget()
            card.setStyleSheet(
                "background:#f8fafc;border:1px solid #dbe3ec;border-radius:8px;"
            )
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(9, 5, 9, 5)
            card_layout.setSpacing(7)
            icon = QLabel()
            icon.setPixmap(self._weather_icon_pixmap(weather.weather_code, 30))
            icon.setFixedSize(32, 32)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("background:transparent;border:0;")
            details = QLabel(
                f"<b>{summary.day.isoformat()}</b><br>"
                f"{weather.condition} · {weather.high_c:.0f}°/{weather.low_c:.0f}°"
            )
            details.setStyleSheet("background:transparent;border:0;")
            card_layout.addWidget(icon)
            card_layout.addWidget(details)
            weather_layout.addWidget(card)
        weather_layout.addStretch()

    @staticmethod
    def _build_daily_chart(
        summaries,
        title: str,
        direction: bool = False,
        weather_by_day=None,
    ) -> QChart:
        chart = QChart()
        chart.setTitle(title)
        if not summaries:
            return chart

        axis_x = QDateTimeAxis()
        axis_x.setTitleText("Date")
        axis_x.setFormat("MMM d")
        axis_x.setTickCount(min(8, max(2, len(summaries))))

        first_dt = QDateTime.fromString(summaries[0].day.isoformat(), "yyyy-MM-dd")
        last_dt = QDateTime.fromString(summaries[-1].day.isoformat(), "yyyy-MM-dd")
        if first_dt == last_dt:
            axis_x.setRange(first_dt.addDays(-1), last_dt.addDays(1))
        else:
            axis_x.setRange(first_dt, last_dt)

        axis_y = QValueAxis()
        axis_y.setTitleText("Counts")
        axis_y.setLabelFormat("%d")

        if direction:
            series_names = ("Enter", "Exit")
            values_for = lambda summary, name: summary.direction_counts[name]
            colors = {"Enter": "#2f9e72", "Exit": "#e45756"}
        else:
            series_names = MODES
            values_for = lambda summary, name: summary.counts[name]
            colors = {
                "Pedestrian": "#2f9e72",
                "Bicycle": "#4c78a8",
                "Car": "#f2a65a",
                "Truck": "#9c6ade",
                "Bus": "#e45756",
                "Motorcycle": "#36a3a8",
            }

        maximum = max(
            (values_for(summary, series_name) for summary in summaries for series_name in series_names),
            default=0,
        )
        axis_y.setRange(0, max(1, maximum))
        axis_y.applyNiceNumbers()

        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        for series_name in series_names:
            line = QLineSeries()
            line.setName(series_name)
            if series_name in colors:
                line.setColor(QColor(colors[series_name]))
            line.setPointsVisible(True)
            for summary in summaries:
                point_dt = QDateTime.fromString(summary.day.isoformat(), "yyyy-MM-dd")
                line.append(point_dt.toMSecsSinceEpoch(), values_for(summary, series_name))
            chart.addSeries(line)
            line.attachAxis(axis_x)
            line.attachAxis(axis_y)

        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        return chart

    def _refresh_daily_report(self) -> None:
        events = self.repository.list_events(ReviewStatus.ACCEPTED)
        summaries = build_daily_summary(self._videos, events)
        weather_by_day = self.repository.list_daily_weather()
        if (
            self._weather_thread is None
            and not self.daily_weather_location.hasFocus()
        ):
            self.daily_weather_location.setText(
                self.repository.get_weather_location()
            )
        overall_total = sum(summary.total for summary in summaries)
        total_enter = sum(summary.direction_counts["Enter"] for summary in summaries)
        total_exit = sum(summary.direction_counts["Exit"] for summary in summaries)
        if summaries:
            self.daily_report_quality.setText(
                f"{len(summaries)} dates · {overall_total:,} accepted counts "
                f"({total_enter:,} Enter and {total_exit:,} Exit)."
            )
        else:
            self.daily_report_quality.setText(
                "Add timestamped recordings to compare counts by date."
            )
        resolved_location = self.repository.get_weather_resolved_location()
        weather_date_count = sum(
            summary.day in weather_by_day for summary in summaries
        )
        if weather_by_day:
            self.daily_weather_status.setText(
                f"Weather loaded for {resolved_location or 'the selected location'} · "
                f"{weather_date_count} of {len(summaries)} dates"
            )
        elif self._weather_thread is None:
            self.daily_weather_status.setText("Weather has not been loaded")
        self.daily_weather_attribution.setVisible(bool(weather_by_day))

        sort_state = begin_table_refresh(self.daily_report_table)
        self.daily_report_table.setRowCount(len(summaries))
        for row_index, summary in enumerate(summaries):
            weather = weather_by_day.get(summary.day)
            values = [
                summary.day.isoformat(),
                *(summary.counts[mode] for mode in MODES),
                summary.direction_counts["Enter"],
                summary.direction_counts["Exit"],
                summary.total,
                f"{weather.high_c:.1f}" if weather is not None else "—",
                f"{weather.low_c:.1f}" if weather is not None else "—",
                weather.condition if weather is not None else "—",
            ]
            sort_values = [
                summary.day.isoformat(),
                *(summary.counts[mode] for mode in MODES),
                summary.direction_counts["Enter"],
                summary.direction_counts["Exit"],
                summary.total,
                weather.high_c if weather is not None else float("-inf"),
                weather.low_c if weather is not None else float("-inf"),
                weather.condition.casefold() if weather is not None else "",
            ]
            for column, value in enumerate(values):
                item = SortableTableWidgetItem(str(value), sort_values[column])
                if column == len(values) - 1 and weather is not None:
                    item.setIcon(QIcon(self._weather_icon_pixmap(weather.weather_code, 24)))
                self.daily_report_table.setItem(row_index, column, item)
        finish_table_refresh(self.daily_report_table, sort_state)
        self._populate_daily_weather_strip(
            self.daily_class_weather_strip,
            self.daily_class_weather_strip_layout,
            summaries,
            weather_by_day,
        )
        self._populate_daily_weather_strip(
            self.daily_direction_weather_strip,
            self.daily_direction_weather_strip_layout,
            summaries,
            weather_by_day,
        )
        self.daily_class_chart.setChart(
            self._build_daily_chart(
                summaries,
                "Daily multimodal traffic counts",
                weather_by_day=weather_by_day,
            )
        )
        self.daily_direction_chart.setChart(
            self._build_daily_chart(
                summaries,
                "Daily enter and exit counts",
                direction=True,
                weather_by_day=weather_by_day,
            )
        )

    def _refresh_report(self) -> None:
        report_view = self.report_view.currentData()
        day, camera, summaries = self._current_summary(self.report_day, self.report_camera)
        if not summaries:
            self.report_quality.setText("Add timestamped recordings to build a daily report.")
            self.report_table.setRowCount(0)
            self.report_table.setVisible(report_view != "plots")
            self.report_chart.setVisible(report_view == "plots")
            if report_view == "plots":
                self.report_chart.setChart(
                    self._build_hourly_chart([], "Add recordings to build report plots", MODES)
                )
            return
        captured = sum(summary.recorded_seconds for summary in summaries) / 3600
        fragments = sum(
            video.camera == camera and video.recording_day == day for video in self._videos
        )
        fragment_word = "fragment" if fragments == 1 else "fragments"
        self.report_quality.setText(
            f"{fragments} recording {fragment_word} combined for {day.isoformat()}, providing "
            f"{captured * 60:.1f} minutes ({captured:.2f} hours) of video. "
            "The results below include accepted detections only."
        )
        if report_view == "plots":
            self.report_table.setVisible(False)
            self.report_chart.setVisible(True)
            self.report_chart.setChart(
                self._build_hourly_chart(
                    summaries,
                    f"Hourly counts · {camera} · {day.isoformat()}",
                    MODES,
                )
            )
            return

        self.report_chart.setVisible(False)
        self.report_table.setVisible(True)
        if report_view == "hourly":
            self.report_table.setColumnCount(len(MODES) + 4)
            self.report_table.setHorizontalHeaderLabels(
                ["Hour", "Recorded", "Coverage", *MODES, "Total"]
            )
            self.report_table.setRowCount(len(summaries))
            for row_index, summary in enumerate(summaries):
                values = [
                    f"{summary.hour.hour:02d}:00",
                    f"{summary.recorded_seconds / 60:.1f} min",
                    summary.coverage_status,
                    *(summary.counts[mode] for mode in MODES),
                    summary.total,
                ]
                for column, value in enumerate(values):
                    self.report_table.setItem(row_index, column, QTableWidgetItem(str(value)))
            return

        self.report_table.setColumnCount(4)
        self.report_table.setHorizontalHeaderLabels(["Mode", "Enter", "Exit", "Total"])
        direction_counts: Counter[tuple[str, str]] = Counter()
        events = self.repository.list_events(ReviewStatus.ACCEPTED)
        for row in events:
            if row["camera"] != camera or not row["occurred_at"]:
                continue
            occurred = datetime.fromisoformat(row["occurred_at"])
            if occurred.date() == day:
                direction_counts[(row["mode"], row["direction_label"])] += 1
        self.report_table.setRowCount(len(MODES))
        for row_index, mode in enumerate(MODES):
            enters = direction_counts[(mode, "Enter")]
            exits = direction_counts[(mode, "Exit")]
            for column, value in enumerate((mode, enters, exits, enters + exits)):
                self.report_table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def closeEvent(self, event) -> None:
        active_tasks = []
        if self._worker is not None:
            active_tasks.append("detection processing")
        if self._intake_worker is not None:
            active_tasks.append("video loading")
        if self._qc_video_worker is not None:
            active_tasks.append("annotated video generation")
        if self._combined_video_worker is not None:
            active_tasks.append("combined video building")
        if self._weather_worker is not None:
            active_tasks.append("weather loading")
        if active_tasks:
            answer = QMessageBox.question(
                self,
                "Work is still running",
                f"Cancel {' and '.join(active_tasks)} and close after the current step finishes?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_requested = True
            if self._worker is not None:
                self._worker.cancel()
            if self._intake_worker is not None:
                self._intake_worker.cancel()
            if self._qc_video_worker is not None:
                self._qc_video_worker.cancel()
            if self._combined_video_worker is not None:
                self._combined_video_worker.cancel()
            if self._weather_worker is not None:
                self._weather_worker.cancel()
            event.ignore()
            return
        event.accept()
