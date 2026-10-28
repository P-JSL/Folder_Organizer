"""Readable execution receipts; flat scalar Qt cells prevent nested-data crashes."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableView, QVBoxLayout,
)

from . import execution_log
from .log_view import _date, _scalar

ACTIONS = {"copy": "복사", "move": "이동", "quarantine": "안전 보관 삭제",
           "date": "날짜별 정리", "extension": "종류별 정리", "rules": "규칙 분류",
           "duplicate_auto": "중복 자동 정리", "undo": "되돌리기",
           "merge": "내용 병합", "repair": "파일 복구", "compact": "복원 데이터 최적화"}
STATUSES = {"started": "시작", "running": "진행 중", "completed": "완료",
            "partial": "일부 처리", "failed": "실패", "copy_preserved": "사본 보존 · 확인 필요",
            "restored": "복원 완료", "unchanged": "변경 없음", "undone": "되돌리기 완료",
            "undo_failed": "복원 충돌", "partial_undo": "일부 복원 · 확인 필요"}
STATUSES.update({"repaired": "복구본 생성", "healthy": "정상", "unsupported": "지원하지 않는 형식", "error": "오류"})
EVENTS = {"execution.started": "작업 시작", "execution.item": "파일 처리",
          "execution.finished": "작업 결과", "execution.failed": "작업 중단"}


def execution_details(record: dict) -> str:
    status = _scalar(record.get("status"), 160)
    action = _scalar(record.get("action"), 160)
    lines = [f"{ACTIONS.get(action, action)} · {STATUSES.get(status, status)}",
             f"시각: {_date(record.get('timestamp'))}", "",
             _scalar(record.get("message"), 4096)]
    if record.get("source"):
        lines.extend(("", "원래 파일", _scalar(record["source"], 4096)))
    if record.get("target"):
        lines.extend(("", "대상 파일", _scalar(record["target"], 4096)))
    if record.get("error"):
        lines.extend(("", "확인할 내용", _scalar(record["error"], 4096)))
    if record.get("completed") is not None:
        total = record.get("total")
        lines.extend(("", f"처리한 항목: {record['completed']:,}" + (f" / {total:,}" if type(total) is int else "")))
    if status in {"partial", "failed", "copy_preserved", "undo_failed", "partial_undo"}:
        lines.extend(("", "다음에 할 일", "오류와 원래 파일·대상 위치를 확인하세요. 정리를 되돌리려면 ‘작업 기록 · 복원’을 여세요. 파일 충돌이 있으면 덮어쓰지 않습니다."))
    lines.extend(("", "작업 번호", _scalar(record.get("execution_id"), 160)))
    return "\n".join(lines)


class ExecutionLogModel(QAbstractTableModel):
    columns = ("시각", "작업", "상태", "파일 / 내용", "작업 번호")

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
        action, status = _scalar(record.get("action")), _scalar(record.get("status"))
        values = (_date(record.get("timestamp")), ACTIONS.get(action, action), STATUSES.get(status, status),
                  _scalar(record.get("source") or record.get("message"), 4096), _scalar(record.get("execution_id"), 160))
        value = values[index.column()]
        if index.column() == 4 and role == Qt.ItemDataRole.DisplayRole and len(value) > 16:
            return "…" + value[-8:]
        return value

    def set_records(self, records):
        self.beginResetModel()
        self.records = records
        self.endResetModel()


class ExecutionLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실행 로그 · 파일별 처리 결과")
        self.resize(1040, 680)
        layout = QVBoxLayout(self)
        title = QLabel("어떤 파일을 어떻게 처리했는지 확인하세요")
        title.setObjectName("section")
        layout.addWidget(title)
        notice = QLabel("정리·복원·병합·복구를 실제로 실행한 결과입니다. 원본을 옮긴 정리를 되돌리려면 ‘작업 기록 · 복원’을 이용하세요.\n병합본·복구본 생성은 원본을 보존합니다. 로그는 이 컴퓨터에만 저장되며 파일 내용은 기록하지 않습니다. 내보내기 전 경로와 오류 정보를 확인하세요.")
        notice.setWordWrap(True)
        notice.setObjectName("muted")
        layout.addWidget(notice)
        toolbar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("파일 이름, 작업 번호, 오류 내용 검색")
        self.search_input.setClearButtonEnabled(True)
        self.action_filter, self.status_filter = QComboBox(), QComboBox()
        self.action_filter.addItem("모든 작업", None)
        self.status_filter.addItem("모든 상태", None)
        for value, label in ACTIONS.items():
            self.action_filter.addItem(label, value)
        for value, label in STATUSES.items():
            self.status_filter.addItem(label, value)
        self.refresh_button = QPushButton("새로 고침")
        for widget in (self.search_input, self.action_filter, self.status_filter, self.refresh_button):
            toolbar.addWidget(widget, 1 if widget is self.search_input else 0)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.model = ExecutionLogModel(self)
        self.table.setModel(self.model)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 125)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(4, 110)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(300)
        splitter.addWidget(self.details)
        splitter.setSizes([280, 220])
        layout.addWidget(splitter, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.export_button = QPushButton("표시 로그 내보내기")
        self.folder_button = QPushButton("로그 폴더 열기")
        footer.addWidget(self.export_button)
        footer.addWidget(self.folder_button)
        footer.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.refresh_logs)
        self.search_input.textChanged.connect(lambda _value: self.search_timer.start())
        self.search_input.returnPressed.connect(self.refresh_logs)
        self.action_filter.currentIndexChanged.connect(self.refresh_logs)
        self.status_filter.currentIndexChanged.connect(self.refresh_logs)
        self.refresh_button.clicked.connect(self.refresh_logs)
        self.export_button.clicked.connect(self.export_visible)
        self.folder_button.clicked.connect(self.open_folder)
        self.table.selectionModel().currentRowChanged.connect(self._selected)
        self.refresh_logs()

    def refresh_logs(self, *_args):
        self.search_timer.stop()
        query = self.search_input.text().strip().casefold()
        translated = query and any(query in label.casefold() for label in (*ACTIONS.values(), *STATUSES.values()))
        try:
            records = execution_log.read_execution_logs(limit=10_000 if translated else 1000,
                        status=self.status_filter.currentData(), action=self.action_filter.currentData(),
                        query="" if translated else query)
            if translated:
                records = [record for record in records if query in execution_details(record).casefold()][:1000]
            self.model.set_records(records)
            self.details.clear()
            self.export_button.setEnabled(bool(records))
            self.status_label.setText(f"최신 항목부터 {len(records):,}개 표시 · 최대 1,000개 · 보관 한도를 넘은 이전 로그는 순환 삭제됩니다."
                                      + ("\n" + execution_log.execution_logging_error() if execution_log.execution_logging_error() else ""))
            if records:
                self.table.selectRow(0)
            else:
                self.details.setPlainText("표시할 실행 로그가 없습니다.\n\n정리 미리보기에서 실행을 확정하면 파일별 처리 결과가 기록됩니다. 검색 중이라면 검색어와 필터를 지워보세요.")
        except (OSError, ValueError) as exc:
            self.model.set_records([])
            self.export_button.setEnabled(False)
            self.status_label.setText(f"실행 로그를 읽지 못했습니다: {exc}")

    def _selected(self, current, _previous):
        if current.isValid() and 0 <= current.row() < len(self.model.records):
            self.details.setPlainText(execution_details(self.model.records[current.row()]))
        else:
            self.details.clear()

    def export_visible(self):
        path, _filter = QFileDialog.getSaveFileName(self, "실행 로그를 새 파일로 저장", "folder-organizer-executions.jsonl", "JSON Lines 로그 (*.jsonl)")
        if not path:
            return
        try:
            output = execution_log.export_execution_logs(path, self.model.records)
            self.status_label.setText(f"실행 로그 {len(self.model.records):,}개를 저장했습니다: {output}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "실행 로그 내보내기 실패", str(exc))

    def open_folder(self):
        directory = execution_log.execution_log_directory()
        if not directory.is_dir():
            QMessageBox.information(self, "로그 폴더", "아직 로그 폴더가 없거나 접근할 수 없습니다.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
