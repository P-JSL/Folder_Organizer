"""Searchable local diagnostic viewer; records stay in Python, outside Qt roles."""
from __future__ import annotations

from datetime import datetime
import json

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableView, QVBoxLayout,
)

from . import diagnostics

LEVELS = {"DEBUG": "상세", "INFO": "안내", "WARNING": "주의", "ERROR": "오류", "CRITICAL": "심각"}


class LogTableModel(QAbstractTableModel):
    columns = ("시각", "수준", "작업 ID", "이벤트", "내용")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < len(self.columns):
            return self.columns[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.records) or not 0 <= index.column() < len(self.columns):
            return None
        if role not in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            return None
        record = self.records[index.row()]
        timestamp = str(record.get("timestamp", ""))
        try:
            timestamp = datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, OverflowError):
            pass
        level = str(record.get("level", "INFO"))
        values = (timestamp, LEVELS.get(level, level), str(record.get("operation_id") or "—"),
                  str(record.get("event", "")), str(record.get("message", "")))
        value = values[index.column()]
        return value[:4096]

    def set_records(self, records: list[dict]):
        self.beginResetModel()
        self.records = records
        self.endResetModel()


class LogViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("로그 · 작업 진단")
        self.resize(1080, 680)
        layout = QVBoxLayout(self)
        title = QLabel("어떤 작업이 진행됐는지 확인하세요")
        title.setObjectName("section")
        layout.addWidget(title)
        notice = QLabel("로그는 이 컴퓨터에만 저장됩니다. 파일 내용은 기록하지 않지만 경로와 오류 정보가 포함될 수 있습니다. 내보낸 파일을 공유하기 전에 확인하세요.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        toolbar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("작업 ID, 이벤트, 오류 내용 검색")
        self.search_input.setClearButtonEnabled(True)
        self.level_filter = QComboBox()
        self.level_filter.addItem("모든 수준", None)
        for value, label in LEVELS.items():
            self.level_filter.addItem(label, value)
        self.refresh_button = QPushButton("새로 고침")
        self.export_button = QPushButton("표시 로그 내보내기")
        self.folder_button = QPushButton("로그 폴더 열기")
        for widget in (self.search_input, self.level_filter, self.refresh_button, self.export_button, self.folder_button):
            toolbar.addWidget(widget, 1 if widget is self.search_input else 0)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.model = LogTableModel(self)
        self.table.setModel(self.model)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 65)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 160)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("로그를 선택하면 시각·작업 ID·세부 정보를 확인할 수 있습니다.")
        self.details.setMaximumBlockCount(500)
        splitter.addWidget(self.details)
        splitter.setSizes([390, 150])
        layout.addWidget(splitter, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        location = QLabel(str(diagnostics.log_directory()))
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        location.setObjectName("muted")
        layout.addWidget(location)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh_logs)
        self.search_input.textChanged.connect(lambda _text: self.search_timer.start())
        self.search_input.returnPressed.connect(self.refresh_logs)
        self.level_filter.currentIndexChanged.connect(self.refresh_logs)
        self.refresh_button.clicked.connect(self.refresh_logs)
        self.export_button.clicked.connect(self.export_visible)
        self.folder_button.clicked.connect(self.open_folder)
        self.table.selectionModel().currentRowChanged.connect(self._selection_changed)
        self.refresh_logs()

    def refresh_logs(self, *_args):
        self.search_timer.stop()
        try:
            records = diagnostics.read_logs(level=self.level_filter.currentData(), query=self.search_input.text())
            self.model.set_records(records)
            self.details.clear()
            self.status_label.setText(f"최신 항목부터 {len(records):,}개 표시 · 최대 1,000개 · 손상된 로그 줄은 건너뜁니다."
                                      + ("\n" + diagnostics.logging_error() if diagnostics.logging_error() else ""))
            self.export_button.setEnabled(bool(records))
            if records:
                self.table.selectRow(0)
        except (OSError, ValueError) as exc:
            self.model.set_records([])
            self.status_label.setText(f"로그를 읽을 수 없습니다: {exc}")
            self.export_button.setEnabled(False)

    def _selection_changed(self, current, _previous):
        if current.isValid() and 0 <= current.row() < len(self.model.records):
            record = self.model.records[current.row()]
            self.details.setPlainText(json.dumps(record, ensure_ascii=False, indent=2)[:32_768])
        else:
            self.details.clear()

    def export_visible(self):
        destination, _filter = QFileDialog.getSaveFileName(self, "새 파일로 표시 로그 저장", "folder-organizer-logs.jsonl", "JSON Lines 로그 (*.jsonl)")
        if not destination:
            return
        try:
            output = diagnostics.export_logs(destination, self.model.records)
            self.status_label.setText(f"{len(self.model.records):,}개 로그를 새 파일로 저장했습니다: {output}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "로그 내보내기 실패", str(exc))

    def open_folder(self):
        directory = diagnostics.log_directory()
        if not directory.is_dir():
            QMessageBox.information(self, "로그 폴더", "아직 로그 폴더가 없거나 접근할 수 없습니다.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
