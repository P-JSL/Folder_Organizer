"""Searchable local diagnostic viewer; records stay in Python, outside Qt roles."""
from __future__ import annotations

from datetime import datetime
import json

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from . import diagnostics

LEVELS = {"DEBUG": "상세", "INFO": "안내", "WARNING": "주의", "ERROR": "오류", "CRITICAL": "심각"}
EVENTS = {
    "application.started": "앱 시작", "task.started": "작업 시작",
    "task.phase": "작업 단계 진행", "task.completed": "작업 처리 완료",
    "task.failed": "작업 실패", "task.cancelled": "작업 중단",
    "task.display_failed": "결과 표시 문제", "operation.result": "파일 정리 결과",
    "operation.undo_requested": "파일 되돌리기 요청",
    "update.check_started": "새 버전 확인", "update.download_started": "업데이트 준비",
    "update.cancelled": "업데이트 중단", "update.current": "최신 버전 확인",
    "update.ready": "업데이트 준비 완료", "update.failed": "업데이트 확인 문제",
    "update.apply_requested": "업데이트 적용 요청", "update.apply_failed": "업데이트 적용 문제",
    "progress.invalid": "진행 상태 표시 문제", "rules.settings_invalid": "규칙 설정 확인 필요",
    "unhandled_exception": "예상하지 못한 오류",
}
_DETAIL_LABELS = {"status": "처리 결과", "items": "파일 수", "count": "처리 수",
                  "elapsed_seconds": "걸린 시간(초)", "version": "앱 버전", "total": "전체 수",
                  "phase": "진행 단계", "unit": "처리 단위", "exception_type": "오류 종류"}
_DETAIL_VALUES = {"completed": "완료", "failed": "실패", "partial": "일부 처리",
                  "undone": "되돌리기 완료", "partial_undo": "일부 되돌림 · 충돌 확인",
                  "files": "파일", "pairs": "비교 쌍", "groups": "그룹", "items": "항목",
                  "scan": "폴더 탐색", "duplicate_index": "중복 검사 준비",
                  "duplicate_sample": "다른 파일 먼저 제외", "duplicate_hash": "전체 내용 검증",
                  "duplicate_verify": "중복 내용 확인", "duplicate_complete": "중복 검사 완료",
                  "similar_inventory": "유사 비교 준비", "similar_pairs": "유사 후보 비교",
                  "similar_complete": "유사 비교 완료", "rules": "분류 규칙 확인",
                  "preview": "작업 미리보기 준비", "execute": "파일 처리", "undo": "파일 되돌리기",
                  "history": "작업 기록 읽기"}


def _scalar(value, limit=2048) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if isinstance(value, (list, tuple, dict)):
        return "기술 정보에서 확인할 수 있습니다."
    text = str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


def _date(value) -> str:
    timestamp = _scalar(value, 100)
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return timestamp


def log_details(record: dict) -> str:
    """Show the meaning and next action first, with bounded scalar metadata."""
    event = _scalar(record.get("event"), 128)
    level = _scalar(record.get("level", "INFO"), 32)
    lines = [EVENTS.get(event, "기록된 활동"),
             f"시각: {_date(record.get('timestamp'))} · 구분: {LEVELS.get(level, level)}",
             "", "무슨 일이 있었나요?", _scalar(record.get("message"), 4096)]
    if level in {"ERROR", "CRITICAL", "WARNING"}:
        lines.extend(("", "다음에 할 일", "아래 설명을 확인하세요. 파일 정리를 되돌리려면 ‘작업 기록 · 복원’을 이용하세요."))
    details = record.get("details")
    if isinstance(details, dict):
        shown = []
        for key, label in _DETAIL_LABELS.items():
            if key in details:
                value = _scalar(details[key])
                shown.append(f"{label}: {_DETAIL_VALUES.get(value, value)}")
        if shown:
            lines.extend(("", "작업 정보", *shown))
    lines.extend(("", f"지원 문의용 작업 번호: {_scalar(record.get('operation_id'))}"))
    return "\n".join(lines)


def _technical_details(record: dict) -> str:
    try:
        return json.dumps(record, ensure_ascii=False, indent=2)[:32_768]
    except (TypeError, ValueError, RecursionError):
        return "기술 정보 형식을 표시할 수 없습니다. 원본 로그 파일을 보존하세요."


class LogTableModel(QAbstractTableModel):
    columns = ("시각", "구분", "활동", "내용", "작업 번호")

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
        timestamp = _date(record.get("timestamp", ""))
        level = str(record.get("level", "INFO"))
        event = _scalar(record.get("event", ""), 128)
        values = (timestamp, LEVELS.get(level, level), EVENTS.get(event, "기록된 활동"),
                  _scalar(record.get("message", ""), 4096), _scalar(record.get("operation_id") or "—", 128))
        value = values[index.column()]
        if index.column() == 4 and role == Qt.ItemDataRole.DisplayRole and len(value) > 16:
            value = "…" + value[-8:]
        return value[:4096]

    def set_records(self, records: list[dict]):
        self.beginResetModel()
        self.records = records
        self.endResetModel()


class LogViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("활동 로그 · 진행과 오류 확인")
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        title = QLabel("작업이 어떻게 진행됐는지 알고 싶을 때")
        title.setObjectName("section")
        layout.addWidget(title)
        distinction = QLabel("활동 로그는 진행 과정과 오류를 확인하는 곳입니다. 파일을 되돌리려면 ‘작업 기록 · 복원’을 이용하세요.")
        distinction.setWordWrap(True)
        distinction.setObjectName("muted")
        layout.addWidget(distinction)
        notice = QLabel("로그는 이 컴퓨터에만 저장됩니다. 파일 내용은 기록하지 않지만 경로와 오류 정보가 포함될 수 있습니다. 내보낸 파일을 공유하기 전에 확인하세요.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        toolbar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("예: 파일 이름, 오류 내용, 작업 번호")
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
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(4, 115)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("로그를 선택하면 어떤 일이 있었는지 쉬운 설명으로 확인할 수 있습니다.")
        self.details.setMaximumBlockCount(500)
        detail_layout.addWidget(self.details, 1)
        self.technical_toggle = QCheckBox("지원 문의용 기술 정보 보기")
        self.technical_toggle.setToolTip("개발자에게 전달할 원본 필드와 이벤트 이름을 확인합니다. 파일 내용은 포함하지 않습니다.")
        detail_layout.addWidget(self.technical_toggle)
        self.technical_details = QPlainTextEdit()
        self.technical_details.setReadOnly(True)
        self.technical_details.setMaximumBlockCount(500)
        self.technical_details.hide()
        detail_layout.addWidget(self.technical_details, 1)
        self.technical_toggle.toggled.connect(self._refresh_technical)
        splitter.addWidget(detail_panel)
        splitter.setSizes([245, 220])
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
            query = self.search_input.text().strip()
            # The stored event codes stay stable for support/export. Searching
            # their visible Korean descriptions should work as well.
            translated_query = query and any(query.casefold() in label.casefold()
                                             for label in (*EVENTS.values(), *LEVELS.values()))
            records = diagnostics.read_logs(level=self.level_filter.currentData(),
                                            query="" if translated_query else query,
                                            limit=10_000 if translated_query else 1000)
            if translated_query:
                records = [record for record in records
                           if query.casefold() in log_details(record).casefold()
                           or query.casefold() in _technical_details(record).casefold()][:1000]
            self.model.set_records(records)
            self.details.clear()
            self.technical_details.clear()
            self.status_label.setText(f"최신 항목부터 {len(records):,}개 표시 · 최대 1,000개 · 손상된 로그 줄은 건너뜁니다."
                                      + ("\n" + diagnostics.logging_error() if diagnostics.logging_error() else ""))
            self.export_button.setEnabled(bool(records))
            if records:
                self.table.selectRow(0)
            else:
                self.details.setPlainText("아직 표시할 기록이 없습니다.\n\n폴더를 분석하거나 정리를 실행하면 진행 과정이 기록됩니다. 검색 중이라면 검색어와 구분 필터를 지워보세요.")
        except (OSError, ValueError) as exc:
            self.model.set_records([])
            self.status_label.setText(f"로그를 읽을 수 없습니다: {exc}")
            self.export_button.setEnabled(False)

    def _selection_changed(self, current, _previous):
        if current.isValid() and 0 <= current.row() < len(self.model.records):
            record = self.model.records[current.row()]
            self.details.setPlainText(log_details(record))
        else:
            self.details.clear()
        self._refresh_technical()

    def _refresh_technical(self, *_args):
        self.technical_details.setVisible(self.technical_toggle.isChecked())
        row = self.table.currentIndex().row()
        if self.technical_toggle.isChecked() and 0 <= row < len(self.model.records):
            self.technical_details.setPlainText(_technical_details(self.model.records[row]))
        else:
            self.technical_details.clear()

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
