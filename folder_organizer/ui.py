"""Native Korean desktop interface. All filesystem work runs outside the GUI thread."""
from __future__ import annotations

import csv
import hashlib
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt,
    QThread, QUrl, QSettings, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QSplitter, QTableView, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from . import updater
from .matching import find_duplicates, find_similar
from .models import FileEntry, MatchGroup, ScanResult
from .operations import build_plan, execute_plan, list_history, undo_operation
from .recovery import inspect_file, repair_file
from .scanner import scan_folder


STYLE = """
QWidget { background: #101820; color: #e6edf3; font-size: 13px; }
QMainWindow { background: #101820; }
QFrame#sidebar { background: #0b1118; border-right: 1px solid #26323e; }
QFrame#card { background: #17232e; border: 1px solid #2a3947; border-radius: 12px; }
QLabel { background: transparent; }
QLabel#brand { color: #7de4ca; font-size: 22px; font-weight: 700; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #96a9bb; }
QLabel#cardValue { color: #edf7fa; font-size: 25px; font-weight: 700; }
QLabel#warning { color: #f4cd82; padding: 8px; background: #2c251a; border-radius: 6px; }
QLabel#section { font-size: 15px; font-weight: 600; }
QPushButton { padding: 9px 13px; border: 1px solid #344758; border-radius: 7px; background: #1d2b38; }
QPushButton:hover { background: #293f50; border-color: #6bcdb8; }
QPushButton:pressed { background: #364e5d; }
QPushButton:disabled { color: #667a8d; background: #16212b; border-color: #24323f; }
QPushButton#primary { background: #75dfc2; color: #0c2823; font-weight: 700; border-color: #75dfc2; }
QPushButton#primary:hover { background: #9cf1da; }
QPushButton#primary:disabled { background: #294b45; color: #77968e; border-color: #294b45; }
QPushButton#caution { border-color: #a78449; color: #f2d293; }
QPushButton#nav { text-align: left; border: 0; background: transparent; padding: 12px; }
QPushButton#nav:hover { background: #1c2b36; }
QLineEdit, QComboBox, QDoubleSpinBox { border: 1px solid #354757; border-radius: 7px; padding: 8px; background: #14212c; }
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border-color: #75dfc2; }
QComboBox::drop-down { border: 0; width: 24px; }
QCheckBox { spacing: 7px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; }
QTableView, QTreeWidget, QPlainTextEdit { background: #14202a; alternate-background-color: #172630; border: 1px solid #2d3c49; border-radius: 8px; selection-background-color: #2b5661; selection-color: #ffffff; }
QTableView::item, QTreeWidget::item { padding: 7px; border: 0; }
QTableView::item:selected, QTreeWidget::item:selected { background: #2b5661; }
QHeaderView::section { padding: 10px 7px; background: #1d2c38; color: #aec1cf; border: 0; border-bottom: 1px solid #344857; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #14212c; color: #93a9b9; padding: 7px 15px; border-bottom: 2px solid #263742; }
QTabBar::tab:selected { color: #7de4ca; border-bottom-color: #7de4ca; }
QProgressBar { border: 0; border-radius: 3px; background: #253644; max-height: 5px; }
QProgressBar::chunk { background: #75dfc2; border-radius: 3px; }
QToolTip { background: #24394a; color: #ffffff; border: 1px solid #6e9da4; padding: 5px; }
QScrollBar:vertical { background: #13202a; width: 11px; }
QScrollBar::handle:vertical { background: #3b5262; min-height: 25px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: #182a37; border: 1px solid #354b5b; }
"""


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value):,} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return "0 B"


def format_date(value: float | None) -> str:
    if value is None:
        return "제공되지 않음"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "날짜 범위 초과"


_DETAIL_LABELS = {
    "id": "작업 번호", "root": "선택 폴더", "mode": "작업 방식", "created_at": "작업 시각",
    "status": "상태", "state": "처리 상태", "items": "파일별 결과", "results": "결과",
    "source": "원본", "target": "대상", "path": "파일", "output": "복구본", "action": "작업",
    "error": "오류", "errors": "오류 목록", "warnings": "주의 사항", "details": "상세 내용",
    "format": "형식", "size": "파일 크기", "recovered_entries": "복구한 항목 수",
    "journal_path": "작업 기록 위치", "undo_status": "복원 상태", "undone_at": "복원 시각",
    "skipped": "건너뛴 항목", "complete": "완료", "partial": "부분 완료", "failed": "실패",
}
_DETAIL_VALUES = {
    "healthy": "정상", "damaged": "손상 감지", "unsupported": "지원하지 않는 형식 또는 크기",
    "error": "오류", "repaired": "복구본 생성", "partial": "부분 처리 · 결과 확인 필요",
    "failed": "실패", "completed": "완료", "complete": "완료", "done": "완료",
    "pending": "처리 대기", "undone": "실행 취소 완료", "conflict": "충돌 · 원본 보존",
    "skipped": "건너뜀", "copy": "복사", "move": "이동", "quarantine": "안전 보관",
    "date": "날짜별 정리", "extension": "확장자별 정리", "ok": "정상",
}
_INTERNAL_DETAIL_FIELDS = {"signature", "sha256", "mtime_ns", "device", "inode", "version", "token", "fingerprint"}


def readable_details(value, indent: int = 0) -> str:
    """Present actionable results without exposing internal plan fields."""
    prefix = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if key in _INTERNAL_DETAIL_FIELDS:
                continue
            label = _DETAIL_LABELS.get(key, key)
            if isinstance(item, (list, dict)):
                lines.append(f"{prefix}{label}")
                lines.append(readable_details(item, indent + 1) or f"{prefix}  없음")
            elif key == "size" and isinstance(item, int):
                lines.append(f"{prefix}{label}: {format_size(item)}")
            else:
                if isinstance(item, bool):
                    display = "예" if item else "아니요"
                elif isinstance(item, str):
                    display = _DETAIL_VALUES.get(item, item)
                else:
                    display = "없음" if item is None else str(item)
                lines.append(f"{prefix}{label}: {display}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        return "\n\n".join(f"{prefix}[{i}]\n{readable_details(item, indent + 1)}"
                              for i, item in enumerate(value, 1))
    return prefix + str(value)


class Worker(QThread):
    progress = Signal(str)

    def __init__(self, function: Callable[["Worker"], object], parent: QObject):
        super().__init__(parent)
        self.function = function
        self.cancel_event = threading.Event()
        self.result: object = None
        self.error: str | None = None
        self._last_report = 0.0

    def report(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_report >= 0.07:
            self.progress.emit(str(message))
            self._last_report = now

    def run(self) -> None:
        try:
            self.result = self.function(self)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class FileTableModel(QAbstractTableModel):
    selectionChanged = Signal()
    HEADERS = ("선택", "파일 이름", "종류", "용량", "수정한 날짜", "만든 날짜", "상대 경로")

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.entries: list[FileEntry] = []
        self.checked: set[str] = set()
        self.path_rows: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return Qt.CheckState.Checked if entry.path in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return ("", entry.name, "응용 프로그램" if entry.kind == "application" else (entry.suffix.upper().lstrip(".") or "파일"),
                    format_size(entry.size), format_date(entry.modified), format_date(entry.created), entry.relative_path)[column]
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 5:
                return f"생성일 정보: {entry.created_source}. Linux 등에서 실제 생성일을 제공하지 않으면 표시하지 않습니다."
            return entry.path
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 3:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.isValid() and index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole:
            path = self.entries[index.row()].path
            if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
                self.checked.add(path)
            else:
                self.checked.discard(path)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            self.selectionChanged.emit()
            return True
        return False

    def set_entries(self, entries: list[FileEntry]) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.path_rows = {item.path: i for i, item in enumerate(self.entries)}
        self.checked.clear()
        self.endResetModel()
        self.selectionChanged.emit()

    def set_checked(self, paths: list[str] | set[str], replace: bool = True) -> None:
        paths = set(paths).intersection(self.path_rows)
        if replace:
            self.checked = paths
        else:
            self.checked.update(paths)
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.ItemDataRole.CheckStateRole])
        self.selectionChanged.emit()

    def sort_value(self, row: int, column: int):
        entry = self.entries[row]
        if column == 0:
            return entry.path in self.checked
        if column == 3:
            return entry.size
        if column == 4:
            return entry.modified
        if column == 5:
            return entry.created if entry.created is not None else float("-inf")
        return str(self.data(self.index(row, column))).casefold()


class FileFilter(QSortFilterProxyModel):
    IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
    DOCUMENT = {".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf", ".hwp"}
    ARCHIVE = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.kind = "all"

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        entry = model.entries[row]
        if self.query and self.query not in (entry.relative_path + " " + entry.name).casefold():
            return False
        suffix = entry.suffix.casefold()
        return (
            self.kind == "all" or
            (self.kind == "application" and entry.kind == "application") or
            (self.kind == "file" and entry.kind != "application") or
            (self.kind == "image" and suffix in self.IMAGE) or
            (self.kind == "document" and suffix in self.DOCUMENT) or
            (self.kind == "archive" and suffix in self.ARCHIVE)
        )

    def lessThan(self, left, right):
        model = self.sourceModel()
        return model.sort_value(left.row(), left.column()) < model.sort_value(right.row(), right.column())


class PlanTableModel(QAbstractTableModel):
    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        self.items = items

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("작업", "원본", "대상")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            item = self.items[index.row()]
            value = str(item.get(("action", "source", "target")[index.column()], ""))
            return _DETAIL_VALUES.get(value, value)
        return None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Folder Organizer · {__version__}")
        self.setMinimumSize(1100, 700)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1360, max(1100, available.width() - 60)) if available else 1280,
                    min(860, max(700, available.height() - 70)) if available else 800)
        self.root: str | None = None
        self.scan_result: ScanResult | None = None
        self.scan_complete = False
        self.groups: list[MatchGroup] = []
        self.match_kind: str | None = None
        self._worker: Worker | None = None
        self._success: Callable | None = None
        self._task_label = ""
        self._cancellable = False
        self._failure: Callable[[str], None] | None = None
        self._pending_update: dict | None = None
        self._update_worker: Worker | None = None
        self._active_update_manual = False
        self._close_requested = False
        self._update_diagnostic_notice = ""
        self._update_retry_requested = False
        self._deferred_update_manual = False
        self.settings = QSettings("P-JSL", "FolderOrganizer")
        self._work_controls: list[QWidget] = []
        self._build_ui()
        self.setStyleSheet(STYLE)
        self.set_busy(False)
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_timer.timeout.connect(self.request_update_check)
        self.update_timer.start()
        self.initial_update_timer = QTimer(self)
        self.initial_update_timer.setSingleShot(True)
        self.initial_update_timer.timeout.connect(self._initial_update_check)
        self.initial_update_timer.start(5000)
        self.deferred_update_timer = QTimer(self)
        self.deferred_update_timer.setSingleShot(True)
        self.deferred_update_timer.timeout.connect(self._resume_deferred_update)

    def _button(self, text, callback, *, primary=False, caution=False, work=True):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        if caution:
            button.setObjectName("caution")
        button.clicked.connect(callback)
        if work:
            self._work_controls.append(button)
        return button

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(195)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(18, 28, 18, 20)
        brand = QLabel("FOLDER\nORGANIZER")
        brand.setObjectName("brand")
        nav.addWidget(brand)
        local = QLabel("로컬 파일 정리 도구")
        local.setObjectName("muted")
        nav.addWidget(local)
        nav.addSpacing(30)
        for text, callback in (("▦  폴더 선택", self.choose_folder), ("◎  중복 파일 찾기", self.analyze_duplicates),
                               ("≈  유사 파일 찾기", self.analyze_similar), ("↶  작업 기록 · 복원", self.show_history),
                               ("↑  업데이트 확인", self.manual_update_check), ("?  사용 안내", self.show_help)):
            button = self._button(text, callback, work=text not in {"?  사용 안내", "↑  업데이트 확인"})
            button.setObjectName("nav")
            if text == "↑  업데이트 확인":
                self.update_button = button
            nav.addWidget(button)
        nav.addStretch()
        privacy = QLabel("내 PC에서 처리\n파일을 서버에 업로드하지\n않습니다.")
        privacy.setWordWrap(True)
        privacy.setObjectName("muted")
        nav.addWidget(privacy)
        nav.addSpacing(14)
        self.auto_update_check = QCheckBox("자동 업데이트")
        self.auto_update_check.setChecked(self.settings.value("updates/enabled", True, type=bool))
        self.auto_update_check.setToolTip("Windows 배포 버전에서 새 릴리스를 자동으로 준비하고 앱 종료 후 적용합니다.")
        self.auto_update_check.toggled.connect(self._auto_update_changed)
        nav.addWidget(self.auto_update_check)
        self.update_status = QLabel("종료 후 새 버전 적용" if updater.is_auto_update_supported() else "소스 실행 · 자동 적용 제외")
        self.update_status.setObjectName("muted")
        self.update_status.setWordWrap(True)
        nav.addWidget(self.update_status)
        version = QLabel(f"v{__version__}  ·  안전 보관 기본")
        version.setObjectName("muted")
        nav.addWidget(version)
        shell.addWidget(sidebar)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("내 파일, 한눈에 정리")
        title.setObjectName("title")
        titles.addWidget(title)
        subtitle = QLabel("폴더를 살펴보고, 비교하고, 확인한 파일만 정리하세요.")
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.folder_button = self._button("폴더 선택", self.choose_folder, primary=True)
        header.addWidget(self.folder_button)
        layout.addLayout(header)

        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setReadOnly(True)
        self.folder_input.setPlaceholderText("개인 컴퓨터의 폴더를 선택하세요")
        folder_row.addWidget(self.folder_input, 1)
        self.scan_button = self._button("다시 분석", self.start_scan)
        folder_row.addWidget(self.scan_button)
        layout.addLayout(folder_row)
        options = QHBoxLayout()
        self.recursive_check = QCheckBox("하위 폴더 포함")
        self.recursive_check.setChecked(True)
        self.hidden_check = QCheckBox("숨김 파일 포함")
        options.addWidget(self.recursive_check)
        options.addWidget(self.hidden_check)
        options.addStretch()
        note = QLabel("생성일이 없는 파일은 ‘제공되지 않음’으로 표시합니다.")
        note.setObjectName("muted")
        options.addWidget(note)
        layout.addLayout(options)
        self._work_controls.extend((self.recursive_check, self.hidden_check))

        cards = QHBoxLayout()
        self.count_label = self._card(cards, "분석한 파일", "—")
        self.size_label = self._card(cards, "전체 용량", "—")
        self.selected_label = self._card(cards, "선택한 항목", "0개")
        self.reclaim_label = self._card(cards, "확인된 중복 용량", "—")
        layout.addLayout(cards)

        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("파일 이름 또는 경로 검색")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_changed)
        filters.addWidget(self.search_input, 1)
        self.type_filter = QComboBox()
        for label, value in (("모든 종류", "all"), ("일반 파일", "file"), ("응용 프로그램", "application"),
                             ("이미지", "image"), ("문서", "document"), ("압축 파일", "archive")):
            self.type_filter.addItem(label, value)
        self.type_filter.currentIndexChanged.connect(self._filter_changed)
        filters.addWidget(self.type_filter)
        filters.addWidget(self._button("표시 항목 선택", self.select_visible))
        filters.addWidget(self._button("선택 해제", self.clear_selection))
        layout.addLayout(filters)

        self.model = FileTableModel(self)
        self.proxy = FileFilter(self)
        self.proxy.setSourceModel(self.model)
        self.model.selectionChanged.connect(self._selection_changed)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((50, 220, 85, 90, 155, 155, 240)):
            self.table.setColumnWidth(column, width)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.doubleClicked.connect(self._reveal_index)

        self.match_panel = QWidget()
        matches_layout = QVBoxLayout(self.match_panel)
        matches_layout.setContentsMargins(0, 7, 0, 0)
        match_header = QHBoxLayout()
        self.match_label = QLabel("중복 · 유사 파일 비교 결과")
        self.match_label.setObjectName("section")
        match_header.addWidget(self.match_label)
        match_header.addStretch()
        self.select_duplicates_button = self._button("각 그룹의 첫 파일을 남기고 선택", self.select_duplicate_candidates)
        self.select_duplicates_button.setToolTip("완전히 동일한 중복 파일만 대상으로 합니다. 선택 후 별도로 작업을 확인해야 합니다.")
        match_header.addWidget(self.select_duplicates_button)
        matches_layout.addLayout(match_header)
        self.match_tree = QTreeWidget()
        self.match_tree.setHeaderLabels(("그룹 / 파일", "판별 근거", "점수"))
        self.match_tree.setColumnWidth(0, 410)
        self.match_tree.setColumnWidth(1, 400)
        self.match_tree.itemChanged.connect(self._match_selection_changed)
        self.match_tree.setMinimumHeight(110)
        matches_layout.addWidget(self.match_tree)
        self.views = QTabWidget()
        self.views.addTab(self.table, "전체 파일")
        self.views.addTab(self.match_panel, "비교 결과")
        self.views.setTabVisible(1, False)
        layout.addWidget(self.views, 1)

        analysis = QHBoxLayout()
        self.duplicates_button = self._button("중복 검사", self.analyze_duplicates)
        self.similar_button = self._button("유사 파일 비교", self.analyze_similar)
        analysis.addWidget(self.duplicates_button)
        analysis.addWidget(self.similar_button)
        threshold_label = QLabel("유사도 기준")
        threshold_label.setObjectName("muted")
        analysis.addWidget(threshold_label)
        self.similarity_threshold = QDoubleSpinBox()
        self.similarity_threshold.setRange(0.50, 0.99)
        self.similarity_threshold.setSingleStep(0.01)
        self.similarity_threshold.setDecimals(2)
        self.similarity_threshold.setValue(0.82)
        self.similarity_threshold.setToolTip("유사도 점수는 후보를 찾는 참고 값입니다. 내용이 같은 파일임을 보장하지 않습니다.")
        analysis.addWidget(self.similarity_threshold)
        self._work_controls.append(self.similarity_threshold)
        analysis.addStretch()
        analysis.addWidget(self._button("CSV 내보내기", self.export_csv))
        analysis.addWidget(self._button("위치 열기", self.reveal_selected))
        layout.addLayout(analysis)

        actions = QHBoxLayout()
        self.action_combo = QComboBox()
        for label, mode, basis in (("안전 보관 (실행 취소 가능)", "quarantine", "modified"),
                                   ("선택 폴더에 복사", "copy", "modified"),
                                   ("선택 폴더로 이동", "move", "modified"),
                                   ("수정일별 폴더로 정리", "date", "modified"),
                                   ("생성일별 폴더로 정리", "date", "created"),
                                   ("확장자별 폴더로 정리", "extension", "modified")):
            self.action_combo.addItem(label, (mode, basis))
        actions.addWidget(self.action_combo, 1)
        self._work_controls.append(self.action_combo)
        self.preview_button = self._button("작업 미리보기", self.preview_selected, primary=True)
        actions.addWidget(self.preview_button)
        self.assume_duplicates_button = self._button("선택 항목을 중복으로 간주", self.assume_duplicates, caution=True)
        self.assume_duplicates_button.setToolTip("해시 검증 없이 사용자가 직접 지정한 파일을 안전 보관합니다. 원본 하나를 남길지 직접 확인하세요.")
        actions.addWidget(self.assume_duplicates_button)
        layout.addLayout(actions)

        recovery = QHBoxLayout()
        recovery.addWidget(self._button("선택 파일 손상 검사", self.inspect_selected))
        recovery.addWidget(self._button("복구본 만들기", self.repair_selected))
        recovery.addWidget(self._button("작업 기록 · 실행 취소", self.show_history))
        recovery.addStretch()
        self.visible_label = QLabel("분석 대기 중")
        self.visible_label.setObjectName("muted")
        recovery.addWidget(self.visible_label)
        layout.addLayout(recovery)
        self.warning_label = QLabel()
        self.warning_label.setObjectName("warning")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        footer = QHBoxLayout()
        self.status_label = QLabel("폴더를 선택하면 파일 용량과 날짜를 분석합니다.")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.cancel_button = self._button("분석 중단", self.cancel_task, work=False)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        shell.addWidget(content, 1)
        self._work_controls.append(self.table)

    def _card(self, parent_layout, title, value):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedHeight(76)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 13, 16, 13)
        title_widget = QLabel(title)
        title_widget.setObjectName("muted")
        box.addWidget(title_widget)
        value_widget = QLabel(value)
        value_widget.setObjectName("cardValue")
        box.addWidget(value_widget)
        parent_layout.addWidget(card, 1)
        return value_widget

    def set_busy(self, busy: bool, *, cancellable: bool = False):
        for control in self._work_controls:
            control.setEnabled(not busy)
        self.progress_bar.setVisible(busy)
        self.cancel_button.setVisible(busy and cancellable)
        self.cancel_button.setEnabled(True)
        if not busy:
            self.select_duplicates_button.setEnabled(self.match_kind == "duplicate" and bool(self.groups) and self.scan_complete)

    def _start_task(self, label: str, function: Callable[[Worker], object], success: Callable,
                    *, cancellable: bool = False, failure: Callable[[str], None] | None = None):
        if self._worker is not None:
            return
        self._task_label = label
        self._success = success
        self._failure = failure
        self._cancellable = cancellable
        self.status_label.setText(label)
        self.set_busy(True, cancellable=cancellable)
        worker = Worker(function, self)
        self._worker = worker
        worker.progress.connect(self.status_label.setText)
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    @Slot()
    def _on_worker_finished(self):
        worker = self._worker
        if worker is None:
            return
        callback = self._success
        failure = self._failure
        error = worker.error
        result = worker.result
        cancelled = worker.cancel_event.is_set()
        self._worker = None
        self._success = None
        self._failure = None
        worker.deleteLater()
        self.set_busy(False)
        if self._close_requested:
            if error:
                QMessageBox.warning(self, "종료 전 작업 결과", error)
            if self._update_worker is None:
                QTimer.singleShot(0, self.close)
            return
        if error:
            self.status_label.setText(f"{self._task_label} 실패")
            if failure:
                failure(error)
            else:
                QMessageBox.warning(self, "작업을 완료하지 못했습니다", error)
            self._resume_deferred_update()
            return
        try:
            if callback:
                callback(result)
            if cancelled:
                self.status_label.setText("분석을 중단했습니다. 부분 결과는 정리에 사용할 수 없습니다. 다시 분석하세요.")
        except Exception as exc:
            QMessageBox.warning(self, "결과 표시 오류", f"{type(exc).__name__}: {exc}")
        self._resume_deferred_update()

    def cancel_task(self):
        if self._worker is not None and self._cancellable:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("진행 중인 파일 처리가 끝나면 분석을 중단합니다…")

    def closeEvent(self, event):
        file_busy = self._worker is not None and self._worker.isRunning()
        update_busy = self._update_worker is not None and self._update_worker.isRunning()
        if file_busy or update_busy:
            self._close_requested = True
            if file_busy and self._cancellable:
                self._worker.cancel_event.set()
            if update_busy:
                self._update_worker.cancel_event.set()
            self.set_busy(True)
            self.status_label.setText("진행 중인 작업을 안전하게 마친 뒤 자동으로 종료합니다. 업데이트 다운로드는 중단합니다.")
            event.ignore()
            return
        if self._pending_update is not None:
            try:
                updater.start_update_helper(self._pending_update)
            except Exception as exc:
                self._close_requested = False
                self.set_busy(False)
                self.update_status.setText("업데이트 적용 시작 실패")
                QMessageBox.warning(self, "업데이트를 시작하지 못했습니다",
                                    f"현재 버전과 준비한 파일은 유지됩니다. 다시 종료하면 재시도합니다.\n"
                                    f"업데이트 없이 종료하려면 자동 업데이트 체크를 해제하세요.\n\n{exc}")
                event.ignore()
                return
            self._pending_update = None
        event.accept()

    def _initial_update_check(self):
        # A bounded local diagnostic; never use any returned path for mutations.
        try:
            result = updater.last_update_result()
        except Exception:
            result = None
        if isinstance(result, dict):
            status = str(result.get("status", ""))
            version = str(result.get("version", ""))
            details = str(result.get("details", ""))[:2000]
            fingerprint = hashlib.sha256(repr((status, version, details)).encode("utf-8")).hexdigest()
            if self.settings.value("updates/lastDiagnostic", "") != fingerprint:
                self.settings.setValue("updates/lastDiagnostic", fingerprint)
                if status == "failed":
                    self._update_diagnostic_notice = "지난 업데이트를 적용하지 못했습니다. 현재 버전을 유지합니다. " + details
                    self.warning_label.setText(self._update_diagnostic_notice)
                    self.warning_label.show()
                    self.update_status.setText("이전 업데이트 실패 · 현재 버전 유지")
                    self.update_status.setToolTip(details)
                elif status == "updated":
                    self.update_status.setText(f"v{version} 업데이트 완료")
        self.request_update_check()

    def _auto_update_changed(self, enabled: bool):
        self.settings.setValue("updates/enabled", enabled)
        if not enabled:
            self._pending_update = None
            self._update_retry_requested = False
            self._deferred_update_manual = False
            if self._update_worker is not None:
                self._update_worker.cancel_event.set()
            self.update_status.setText("자동 업데이트 꺼짐")
        else:
            self.update_status.setText("자동 업데이트 켜짐")
            self.request_update_check()

    def manual_update_check(self):
        self.request_update_check(manual=True)

    def request_update_check(self, manual: bool = False):
        if self._close_requested:
            return
        if not manual and not self.auto_update_check.isChecked():
            return
        if not updater.is_auto_update_supported():
            self.update_status.setText("소스 실행 · 자동 적용 제외")
            if manual:
                QMessageBox.information(self, "업데이트 안내",
                    "자동 업데이트는 Windows 배포 파일로 실행할 때 사용할 수 있습니다.\n"
                    "소스 실행 버전은 GitHub Releases에서 새 버전을 받아주세요.\n"
                    "https://github.com/P-JSL/Folder_Organizer/releases")
            return
        if self._pending_update is not None:
            self.update_status.setText("새 버전 준비 완료 · 종료 후 적용")
            if manual:
                QMessageBox.information(self, "업데이트 준비 완료", "앱의 파일 작업을 마친 후 종료하면 새 버전을 적용합니다.")
            return
        if self._update_worker is not None:
            return
        if self._worker is not None or QApplication.activeModalWidget() is not None:
            self._update_retry_requested = True
            self._deferred_update_manual |= manual
            self.update_status.setText("현재 작업 완료 후 업데이트 확인")
            if self._worker is None:
                self.deferred_update_timer.start(2000)
            return
        self.update_status.setText("새 릴리스 확인 중…")
        def task(worker):
            release = updater.check_for_update(__version__, cancel=worker.cancel_event)
            if release is None:
                return None
            worker.report("새 버전 다운로드 · 검증 중…")
            plan = updater.prepare_update(release, updater.install_directory(), cancel=worker.cancel_event)
            return release, plan
        self._active_update_manual = manual
        self.update_button.setEnabled(False)
        worker = Worker(task, self)
        self._update_worker = worker
        worker.progress.connect(self.update_status.setText)
        worker.finished.connect(self._on_update_worker_finished)
        worker.start()

    @Slot()
    def _on_update_worker_finished(self):
        worker = self._update_worker
        if worker is None:
            return
        result, error, cancelled = worker.result, worker.error, worker.cancel_event.is_set()
        manual = self._active_update_manual
        self._update_worker = None
        worker.deleteLater()
        self.update_button.setEnabled(True)
        if cancelled:
            self.update_status.setText("업데이트 중단 · 현재 버전 유지")
        elif error:
            self._update_failed(error, manual)
        else:
            self._update_ready(result, manual)
        if self._close_requested and self._worker is None:
            QTimer.singleShot(0, self.close)

    def _update_ready(self, result, manual: bool):
        if result is None:
            self.update_status.setText("최신 버전입니다")
            if self._worker is None:
                self.status_label.setText("업데이트 확인 완료 · 최신 버전입니다.")
            if manual:
                QMessageBox.information(self, "업데이트 확인", "현재 사용 중인 버전이 최신 버전입니다.")
            return
        release, plan = result
        if not manual and not self.auto_update_check.isChecked():
            self.update_status.setText("자동 업데이트 꺼짐 · 현재 버전 유지")
            return
        self._pending_update = plan
        self.update_status.setText(f"v{release.get('version', '새 버전')} 준비 완료\n종료 후 적용합니다")
        if self._worker is None:
            self.status_label.setText("새 버전 다운로드와 검증을 마쳤습니다. 파일 작업을 마치고 종료하면 적용합니다.")
        if manual:
            QMessageBox.information(self, "업데이트 준비 완료", "새 버전을 준비했습니다. 앱 종료 후 적용하며 현재 파일 작업은 그대로 이어갈 수 있습니다.")

    def _update_failed(self, error: str, manual: bool):
        self.update_status.setText("업데이트 확인 실패 · 현재 버전 유지")
        if self._worker is None:
            self.status_label.setText("업데이트 서버에 연결하지 못했거나 패키지 검증에 실패했습니다. 기존 버전으로 계속 사용할 수 있습니다.")
        self.update_status.setToolTip(error)
        if manual:
            QMessageBox.warning(self, "업데이트 실패", error)

    def _resume_deferred_update(self):
        if self._update_retry_requested and self._worker is None and not self._close_requested:
            if QApplication.activeModalWidget() is not None:
                self.deferred_update_timer.start(2000)
                return
            manual = self._deferred_update_manual
            self._update_retry_requested = False
            self._deferred_update_manual = False
            self.request_update_check(manual=manual)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "분석할 폴더 선택", self.root or str(Path.home()))
        if path:
            self.root = str(Path(path).resolve())
            self.folder_input.setText(self.root)
            self.model.set_entries([])
            self.scan_result = None
            self.scan_complete = False
            self.groups = []
            self.match_kind = None
            self.match_tree.clear()
            self.views.setCurrentIndex(0)
            self.views.setTabVisible(1, False)
            self.start_scan()

    def start_scan(self):
        if not self.root:
            self.choose_folder()
            return
        root = self.root
        recursive = self.recursive_check.isChecked()
        hidden = self.hidden_check.isChecked()
        self.scan_complete = False
        self._start_task("폴더를 분석하고 있습니다…", lambda worker: scan_folder(
            root, recursive=recursive, include_hidden=hidden,
            cancel=worker.cancel_event, progress=worker.report), self.load_scan_result, cancellable=True)

    def load_scan_result(self, result: ScanResult):
        """Load a completed (or explicitly partial) scan; useful for integration tests."""
        self.scan_result = result
        self.root = result.root
        self.folder_input.setText(result.root)
        self.scan_complete = not result.cancelled
        self.model.set_entries(result.files)
        self.groups = []
        self.match_kind = None
        self.match_tree.clear()
        self.views.setCurrentIndex(0)
        self.views.setTabVisible(1, False)
        self.views.setTabText(1, "비교 결과")
        self.count_label.setText(f"{len(result.files):,}개")
        self.size_label.setText(format_size(result.total_size))
        self.reclaim_label.setText("—")
        self.select_duplicates_button.setEnabled(False)
        notices = [self._update_diagnostic_notice] if self._update_diagnostic_notice else []
        if result.cancelled:
            notices.append("중단된 분석입니다. 정리하려면 분석을 완료하세요.")
        if result.warnings:
            notices.append(f"읽을 수 없거나 제외된 항목 {len(result.warnings):,}건: " + " / ".join(result.warnings[:2]))
        self.warning_label.setText("\n".join(notices))
        self.warning_label.setToolTip("\n".join(result.warnings))
        self.warning_label.setVisible(bool(notices))
        self.status_label.setText(f"분석 완료 · 파일 {len(result.files):,}개 · {format_size(result.total_size)} · 심볼릭 링크와 내부 보관 폴더는 제외합니다.")
        self._filter_changed()

    def _filter_changed(self):
        if not hasattr(self, "proxy"):
            return
        self.views.setCurrentIndex(0)
        self.proxy.beginFilterChange()
        self.proxy.query = self.search_input.text().strip().casefold()
        self.proxy.kind = self.type_filter.currentData() or "all"
        self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        self.visible_label.setText(f"표시 {self.proxy.rowCount():,} / 전체 {len(self.model.entries):,}개")

    def selected_paths(self) -> list[str]:
        return [entry.path for entry in self.model.entries if entry.path in self.model.checked]

    def select_visible(self):
        self.model.set_checked([self.model.entries[self.proxy.mapToSource(self.proxy.index(row, 0)).row()].path
                                for row in range(self.proxy.rowCount())], replace=False)

    def clear_selection(self):
        self.model.set_checked([])

    def _selection_changed(self):
        if not hasattr(self, "selected_label"):
            return
        paths = self.model.checked
        self.selected_label.setText(f"{len(paths):,}개")
        self.selected_label.setToolTip(f"선택 용량 {format_size(sum(entry.size for entry in self.model.entries if entry.path in paths))}. 검색으로 숨긴 항목도 선택 상태를 유지합니다.")
        if hasattr(self, "match_tree"):
            self.match_tree.blockSignals(True)
            for i in range(self.match_tree.topLevelItemCount()):
                group = self.match_tree.topLevelItem(i)
                for j in range(group.childCount()):
                    child = group.child(j)
                    child.setCheckState(0, Qt.CheckState.Checked if child.data(0, Qt.ItemDataRole.UserRole) in paths else Qt.CheckState.Unchecked)
            self.match_tree.blockSignals(False)

    def _require_scan(self, selection=False) -> bool:
        if not self.scan_result or not self.scan_complete:
            QMessageBox.information(self, "폴더 분석 필요", "폴더를 선택하고 분석을 완료한 뒤 사용하세요.")
            return False
        if selection and not self.selected_paths():
            QMessageBox.information(self, "파일 선택 필요", "파일 목록의 체크 상자로 처리할 파일을 선택하세요.")
            return False
        return True

    def analyze_duplicates(self):
        if not self._require_scan():
            return
        files = list(self.model.entries)
        def task(worker):
            groups = find_duplicates(files, cancel=worker.cancel_event, progress=worker.report)
            return groups, worker.cancel_event.is_set()
        self._start_task("SHA-256과 실제 내용을 비교하고 있습니다…", task,
                         lambda result: self._load_groups(result[0], "duplicate") if not result[1] else None,
                         cancellable=True)

    def analyze_similar(self):
        if not self._require_scan():
            return
        files = list(self.model.entries)
        threshold = self.similarity_threshold.value()
        def task(worker):
            groups = find_similar(files, threshold=threshold, cancel=worker.cancel_event, progress=worker.report)
            return groups, worker.cancel_event.is_set()
        self._start_task("파일 이름 · 텍스트 · 이미지를 비교하고 있습니다…", task,
                         lambda result: self._load_groups(result[0], "similar") if not result[1] else None,
                         cancellable=True)

    def _load_groups(self, groups: list[MatchGroup], kind: str):
        self.groups = groups
        self.match_kind = kind
        self.match_tree.blockSignals(True)
        self.match_tree.clear()
        for index, group in enumerate(groups, 1):
            label = f"그룹 {index} · {len(group.paths)}개"
            reason = {
                "duplicate": "크기·SHA-256·실제 바이트 일치 (하드링크 제외)",
                "similar_name": "정규화한 파일 이름 유사 · 내용 동일 여부는 확인 필요",
                "similar_text": "UTF-8 어구 겹침 (앞 4,096단어·최대 256KB) · 직접 검토",
                "similar_image": "이미지 구조·색·종횡비 유사 (16×16 비교) · 직접 검토",
            }.get(group.kind, group.reason)
            parent = QTreeWidgetItem((label, reason, f"{group.score:.0%}"))
            parent.setToolTip(1, reason + "\n" + group.reason)
            self.match_tree.addTopLevelItem(parent)
            for position, path in enumerate(group.paths):
                item = QTreeWidgetItem((str(Path(path).relative_to(self.root)) if self.root and Path(path).is_relative_to(self.root) else path,
                                       "기본 보존 파일" if kind == "duplicate" and position == 0 else "직접 비교 후 선택", ""))
                item.setToolTip(0, path)
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if path in self.model.checked else Qt.CheckState.Unchecked)
                parent.addChild(item)
            parent.setExpanded(index <= 6)
        self.match_tree.blockSignals(False)
        self.views.setTabVisible(1, True)
        self.views.setTabText(1, f"비교 결과 · {len(groups):,}그룹")
        self.views.setCurrentIndex(1)
        title = "완전히 같은 파일" if kind == "duplicate" else "유사 후보 · 최대 25,000쌍 비교 · 동일 파일로 보장하지 않습니다"
        self.match_label.setText(f"{title} · {len(groups):,}그룹")
        self.select_duplicates_button.setEnabled(kind == "duplicate" and bool(groups) and self.scan_complete)
        if kind == "duplicate":
            sizes = {entry.path: entry.size for entry in self.model.entries}
            reclaim = sum(sum(sizes.get(path, 0) for path in group.paths[1:]) for group in groups)
            self.reclaim_label.setText(format_size(reclaim))
        self.status_label.setText(
            f"비교 완료 · {len(groups):,}개 그룹. 체크 후 미리보기에서 확인하세요. 안전 보관은 디스크 공간을 즉시 반환하지 않습니다."
            if kind == "duplicate" else
            "유사 후보를 찾았습니다. 같은 확장자 내 최대 25,000쌍만 비교하므로 큰 폴더는 범위를 좁혀 다시 검사하세요.")

    def _match_selection_changed(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return
        checked = set(self.model.checked)
        if item.checkState(0) == Qt.CheckState.Checked:
            checked.add(path)
        else:
            checked.discard(path)
        self.model.set_checked(checked)

    def select_duplicate_candidates(self):
        if self.match_kind != "duplicate" or not self._require_scan():
            return
        paths = [path for group in self.groups for path in group.paths[1:]]
        self.model.set_checked(paths)
        self.status_label.setText("각 중복 그룹의 첫 파일을 남기고 나머지를 선택했습니다. 미리보기에서 경로를 다시 확인하세요.")

    def preview_selected(self):
        if self._require_scan(selection=True):
            mode, basis = self.action_combo.currentData()
            self._build_preview(mode, date_basis=basis)

    def assume_duplicates(self):
        if not self._require_scan(selection=True):
            return
        answer = QMessageBox.warning(
            self, "중복으로 간주하고 안전 보관",
            f"선택한 {len(self.selected_paths()):,}개 파일은 중복 여부를 검증하지 않습니다.\n"
            "선택한 파일 모두가 원래 위치에서 안전 보관 폴더로 이동합니다.\n"
            "남길 원본이 선택되지 않았는지 확인하세요. 다음 화면에서 경로를 검토합니다.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Ok:
            self._build_preview("quarantine", assumed=True)

    def _build_preview(self, mode: str, *, date_basis="modified", assumed=False):
        paths = self.selected_paths()
        root = self.root
        destination = None
        if mode in ("copy", "move"):
            destination = QFileDialog.getExistingDirectory(self, "복사할 폴더 선택" if mode == "copy" else "이동할 폴더 선택", root or "")
            if not destination:
                return
        self._start_task("변경 계획을 검증하고 있습니다…", lambda worker: build_plan(
            root, paths, mode, destination=destination, date_basis=date_basis),
            lambda plan: self._show_preview(plan, assumed=assumed))

    def _show_preview(self, plan: dict, *, assumed=False):
        items = plan.get("items", [])
        if not items:
            self._show_details("변경할 파일이 없습니다", plan)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("작업 미리보기 · 확인 전에는 파일을 변경하지 않습니다")
        dialog.resize(1000, 580)
        layout = QVBoxLayout(dialog)
        text = f"총 {len(items):,}개 · {format_size(sum(item.get('size', 0) for item in items))}"
        if assumed:
            text += "\n사용자 지정: 중복 검증 없이 선택 항목 모두를 안전 보관합니다."
        intro = QLabel(text + "\n파일을 덮어쓰지 않으며 작업 기록에서 실행 취소할 수 있습니다.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        if plan.get("warnings"):
            warnings = QLabel("\n".join(map(str, plan["warnings"])))
            warnings.setWordWrap(True)
            warnings.setObjectName("warning")
            layout.addWidget(warnings)
        table = QTableView()
        table.setModel(PlanTableModel(items, table))
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setColumnWidth(0, 90)
        table.setColumnWidth(1, 400)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Apply)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("확인한 작업 실행")
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._start_task("파일을 처리하고 작업 기록을 저장하고 있습니다…", lambda worker: execute_plan(plan), self._operation_done)
        else:
            self.status_label.setText("작업을 취소했습니다. 파일을 변경하지 않았습니다.")

    def _operation_done(self, result: dict):
        self.scan_complete = False
        self._show_details("파일 작업 결과", result)
        self.start_scan()

    def _reveal_index(self, index):
        if index.isValid() and index.column() != 0:
            source = self.proxy.mapToSource(index)
            self._reveal_path(self.model.entries[source.row()].path)

    def _reveal_path(self, path):
        parent = Path(path).parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent))):
            QMessageBox.information(self, "폴더 위치", str(parent))

    def reveal_selected(self):
        paths = self.selected_paths()
        if not paths and self.table.currentIndex().isValid():
            row = self.proxy.mapToSource(self.table.currentIndex()).row()
            paths = [self.model.entries[row].path]
        if paths:
            self._reveal_path(paths[0])
        elif self.root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.root))
        else:
            QMessageBox.information(self, "위치 열기", "먼저 폴더를 선택하세요.")

    def export_csv(self):
        if not self._require_scan():
            return
        path, _ = QFileDialog.getSaveFileName(self, "새 파일로 목록 저장 (덮어쓰기 없음)", str(Path.home() / "folder-organizer.csv"), "CSV 파일 (*.csv)")
        if not path:
            return
        entries = [self.model.entries[self.proxy.mapToSource(self.proxy.index(row, 0)).row()]
                   for row in range(self.proxy.rowCount())]
        def task(worker):
            def safe(value):
                value = str(value)
                return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            with open(path, "x", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("이름", "상대 경로", "종류", "바이트", "수정일", "생성일", "생성일 출처"))
                for entry in entries:
                    writer.writerow((safe(entry.name), safe(entry.relative_path), safe(entry.kind), entry.size,
                                     format_date(entry.modified), format_date(entry.created), safe(entry.created_source)))
            return len(entries)
        self._start_task("표시된 파일 목록을 저장하고 있습니다…", task,
                         lambda count: self.status_label.setText(f"CSV 저장 완료 · {count:,}개 · {path}"))

    def inspect_selected(self):
        if not self._require_scan(selection=True):
            return
        paths = self.selected_paths()
        def task(worker):
            results = []
            for path in paths:
                if worker.cancel_event.is_set():
                    break
                worker.report(f"손상 검사: {Path(path).name}")
                try:
                    results.append(inspect_file(path))
                except Exception as exc:
                    results.append({"path": path, "status": "error", "details": str(exc)})
            return {"검사 수": len(results), "선택 수": len(paths), "중단": worker.cancel_event.is_set(), "results": results}
        self._start_task("선택한 파일의 무결성을 검사하고 있습니다…", task,
                         lambda results: self._show_details("손상 검사 결과", results), cancellable=True)

    def repair_selected(self):
        if not self._require_scan(selection=True):
            return
        paths = self.selected_paths()
        destination = QFileDialog.getExistingDirectory(self, "복구본을 저장할 폴더 선택", self.root or "")
        if not destination:
            return
        answer = QMessageBox.information(
            self, "원본을 보존하고 복구본 만들기",
            "원본 파일은 유지하고 새 파일로 저장합니다.\n"
            "지원 형식은 이미지, ZIP·일부 Office 문서, PDF입니다.\n"
            "남아 있는 데이터만 복구할 수 있으며 완전 복구를 보장하지 않습니다.\n"
            "디스크에서 이미 삭제한 파일을 찾아내는 기능은 포함하지 않습니다.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        def task(worker):
            results = []
            for path in paths:
                worker.report(f"복구본 생성: {Path(path).name}")
                try:
                    results.append({"path": path, **repair_file(path, destination)})
                except Exception as exc:
                    results.append({"path": path, "status": "failed", "details": str(exc)})
            return {"저장 폴더": destination, "results": results}
        self._start_task("원본을 보존하며 복구 가능한 내용을 새 파일에 저장하고 있습니다…", task,
                         self._repair_done)

    def _repair_done(self, results):
        self._show_details("복구본 생성 결과", results)
        self.start_scan()

    def show_history(self):
        if not self.root:
            QMessageBox.information(self, "작업 기록", "기록을 확인할 원래 폴더를 선택하세요.")
            return
        root = self.root
        self._start_task("작업 기록을 읽고 있습니다…", lambda worker: list_history(root), self._show_history)

    def _show_history(self, history: list[dict]):
        if not history:
            QMessageBox.information(self, "작업 기록", "이 폴더에는 저장된 정리 작업이 없습니다.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("작업 기록 · 실행 취소")
        dialog.resize(900, 540)
        layout = QVBoxLayout(dialog)
        label = QLabel("원래 폴더에 복원합니다. 같은 이름의 파일이 생겼거나 대상이 변경되었으면 덮어쓰지 않습니다.")
        label.setWordWrap(True)
        layout.addWidget(label)
        tree = QTreeWidget()
        tree.setHeaderLabels(("작업 ID", "시각", "방식", "상태", "항목"))
        tree.setColumnWidth(0, 220)
        tree.setColumnWidth(1, 190)
        for record in history:
            created = record.get("created_at", "")
            if isinstance(created, (float, int)):
                created = format_date(created)
            item = QTreeWidgetItem((str(record.get("id", "")), str(created), _DETAIL_VALUES.get(str(record.get("mode", "")), str(record.get("mode", ""))),
                                    _DETAIL_VALUES.get(str(record.get("status", "")), str(record.get("status", ""))), str(len(record.get("items", [])))))
            item.setData(0, Qt.ItemDataRole.UserRole, record)
            tree.addTopLevelItem(item)
        layout.addWidget(tree, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        undo = buttons.addButton("선택 작업 실행 취소", QDialogButtonBox.ButtonRole.ActionRole)
        def do_undo():
            item = tree.currentItem()
            if item is None:
                return
            record = item.data(0, Qt.ItemDataRole.UserRole)
            operation_id = record.get("id")
            if not operation_id:
                return
            answer = QMessageBox.question(dialog, "실행 취소 확인", "선택한 작업의 파일을 원래 위치로 복원할까요?\n변경되거나 충돌하는 파일은 건너뜁니다.",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                dialog.accept()
                root = self.root
                self._start_task("변경되지 않은 파일을 원래 위치로 복원하고 있습니다…",
                                 lambda worker: undo_operation(root, operation_id), self._operation_done)
        undo.clicked.connect(do_undo)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_details(self, title: str, result):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(880, 570)
        layout = QVBoxLayout(dialog)
        label = QLabel("상태와 파일별 결과를 확인하세요. 실패·충돌·부분 복구 항목은 처리되지 않은 내용이 있을 수 있습니다.")
        label.setWordWrap(True)
        layout.addWidget(label)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(readable_details(result))
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        self.status_label.setText(title + " · 세부 결과 확인 완료")

    def show_help(self):
        self._show_details("Folder Organizer 사용 안내", {
            "시작": "폴더 선택 → 파일 확인 → 중복/유사 비교 → 체크 상자 선택 → 작업 미리보기 → 실행",
            "중복": "같은 크기, SHA-256, 실제 내용으로 검사합니다. 자동 선택은 각 그룹의 첫 파일 하나를 남깁니다.",
            "유사": "이름·텍스트·이미지 특징이 비슷한 후보입니다. 같은 파일을 의미하지 않으며 자동 정리하지 않습니다.",
            "중복으로 간주": "별도 버튼으로 검증 없이 사용자가 선택한 모든 파일을 안전 보관합니다. 남길 원본을 직접 확인하세요.",
            "안전 보관": "선택한 폴더의 .folder-organizer에 보관합니다. 영구 삭제하지 않으며 보관된 파일도 용량을 차지합니다.",
            "정리": "날짜별·확장자별 정리, 다른 폴더로 복사/이동을 지원합니다. 모든 변경 전 경로를 확인하세요.",
            "실행 취소": "같은 폴더의 작업 기록에서 복원합니다. 충돌 파일과 변경된 파일을 덮어쓰지 않습니다.",
            "생성일": "운영체제가 제공하는 실제 생성일만 표시합니다. Linux 등에서는 제공되지 않을 수 있습니다.",
            "응용 프로그램": "폴더 안의 실행 파일과 설치 패키지를 표시합니다. 설치된 앱을 제거하거나 프로그램을 실행하지 않습니다.",
            "손상 검사와 복구": "지원 형식만 검사합니다. 원본을 보존하고 남은 데이터로 복구본을 만듭니다. 완전 복구와 삭제 파일 디스크 탐색은 지원하지 않습니다.",
            "대용량 폴더": "분석은 백그라운드에서 진행하며 중단할 수 있습니다. 유사 비교는 자원 제한 안에서 후보를 찾습니다.",
            "선택": "검색으로 숨긴 파일도 체크 상태를 유지합니다. 작업 미리보기에서 전체 목록을 확인하세요.",
            "개인정보": "파일은 내 PC에서 처리하며 서버에 업로드하지 않습니다. 심볼릭 링크와 내부 보관 디렉터리는 분석하지 않습니다.",
        })


def launch() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Folder Organizer")
    app.setOrganizationName("Folder Organizer")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    return app.exec()
