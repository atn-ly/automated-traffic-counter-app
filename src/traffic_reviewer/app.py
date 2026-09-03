from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from traffic_reviewer.project_management import ProjectCatalog
from traffic_reviewer.ui.main_window import MainWindow

STYLE = """
QWidget {
    background: #f8fafc;
    color: #172033;
    font-size: 14px;
}
QLabel#title {
    font-size: 28px;
    font-weight: 700;
    color: #102a43;
}
QLabel#subtitle { color: #64748b; }
QLabel#versionLabel { color: #94a3b8; font-size: 12px; }
QLabel#sectionTitle { font-size: 17px; font-weight: 650; color: #102a43; }
QLabel#infoBanner {
    background: #edf7f1;
    border: 1px solid #c8ead5;
    border-radius: 8px;
    color: #245c45;
    padding: 10px 12px;
}
QTabWidget::pane {
    border: 1px solid #dbe3ec;
    border-radius: 10px;
    background: white;
    top: -1px;
}
QTabBar::tab {
    background: #e9eff5;
    border: 1px solid #dbe3ec;
    padding: 11px 20px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:selected { background: white; color: #087f5b; font-weight: 650; }
QPushButton {
    background: white;
    border: 1px solid #cbd5e1;
    border-radius: 7px;
    padding: 8px 14px;
}
QPushButton:hover { border-color: #61be81; background: #f0fdf4; }
QPushButton:disabled { color: #94a3b8; background: #f1f5f9; }
QPushButton#primaryButton {
    background: #16845b;
    border-color: #16845b;
    color: white;
    font-weight: 650;
}
QPushButton#primaryButton:hover { background: #0f6b49; }
QPushButton#dangerButton {
    background: #fff7f7;
    border-color: #efb4b4;
    color: #a61b1b;
}
QPushButton#dangerButton:hover { background: #feecec; border-color: #dc6b6b; }
QGroupBox {
    background: white;
    border: 1px solid #dbe3ec;
    border-radius: 9px;
    margin-top: 12px;
    padding: 18px 12px 12px 12px;
    font-weight: 650;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QLabel#evidenceFrame {
    background: #111827;
    color: #cbd5e1;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 0;
}
QTableWidget {
    background: white;
    alternate-background-color: #f8fafc;
    border: 1px solid #dbe3ec;
    border-radius: 8px;
    gridline-color: #e8edf3;
    selection-background-color: #d9fbe8;
    selection-color: #172033;
}
QHeaderView::section {
    background: #edf3f7;
    color: #475569;
    border: 0;
    border-bottom: 1px solid #dbe3ec;
    padding: 9px;
    font-weight: 650;
}
QLineEdit, QSpinBox {
    background: white;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 7px;
}
QProgressBar {
    background: #e8edf3;
    border: 0;
    border-radius: 7px;
    min-height: 20px;
    text-align: center;
}
QProgressBar::chunk { background: #61be81; border-radius: 7px; }
"""


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("OSBA Traffic Counter")
    application.setFont(QFont("Segoe UI", 10))
    application.setStyleSheet(STYLE)
    data_root = Path.cwd() / "data"
    project_catalog = ProjectCatalog(data_root)
    project = project_catalog.active_project()
    project_catalog.cleanup_migrated_legacy_database()
    window = MainWindow(project.database_path, project_catalog)
    window.show()
    return application.exec()
