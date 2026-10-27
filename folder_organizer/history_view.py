"""Bounded history viewer which keeps complete journals outside Qt variants.

Only display strings cross the Qt model boundary. Large or unexpectedly nested
journals therefore cannot trigger QVariant's recursive container conversion.
Restoration is requested by ID and remains the caller's confirmed worker task.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QPlainTextEdit, QSplitter, QTableView, QVBoxLayout,
)

MAX_HISTORY_ROWS = 5_000
MAX_DETAIL_ITEMS = 200
MAX_VALUE_CHARS = 2_048
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MODES = {
    "copy": "복사", "move": "이동", "quarantine": "안전 보관",
    "date": "날짜별 정리", "extension": "확장자별 정리",
    "rules": "규칙별 분류", "classification": "규칙별 분류",
    "duplicate_auto": "중복 사본 안전 보관",
}


def restore_effect(record: dict | None) -> str:
    """Explain the real undo effect without exposing journal internals."""
    if not isinstance(record, dict):
        return "작업을 선택하면 무엇을 되돌릴 수 있는지 알려드립니다."
    items = record.get("items")
    if not isinstance(items, list) or not items:
        return "이 기록에는 되돌릴 파일 목록이 없습니다. 활동 로그에서 당시 진행 상황을 확인하세요."
    actions = {item.get("action") for item in items if isinstance(item, dict)
               and isinstance(item.get("action"), str)} if isinstance(items, list) else set()
    if not actions:
        actions = {"copy" if record.get("mode") == "copy" else "move"}
    messages = []
    if "copy" in actions:
        messages.append("복사 되돌리기: 원본은 그대로 두고, 앱이 만든 사본만 확인해 보호 보관으로 옮깁니다.")
    if actions - {"copy"}:
        messages.append("이동·안전 보관 되돌리기: 처리된 파일을 확인해 원래 위치로 돌려놓습니다.")
    messages.append("원래 위치에 다른 파일이 있거나 파일 내용이 바뀌면 덮어쓰지 않고 해당 항목을 남깁니다.")
    return "\n".join(messages)


def _status_guidance(status: str) -> str:
    return {
        "completed": "작업을 마쳤습니다. 아래에서 파일별 결과를 확인한 뒤 필요하면 되돌리세요.",
        "partial": "일부 파일만 처리했습니다. 완료한 파일과 처리하지 못한 파일을 아래에서 각각 확인하세요.",
        "failed": "작업을 완료하지 못했습니다. 파일별 결과와 ‘확인할 내용’을 살펴보세요. 남아 있는 사본도 표시합니다.",
        "partial_undo": "일부 파일은 되돌렸지만 충돌하는 파일은 보존했습니다. 충돌 원인을 확인한 뒤 다시 요청할 수 있습니다.",
        "undone": "이미 되돌린 작업입니다. 같은 작업을 다시 되돌릴 필요가 없습니다.",
        "running": "완료되지 않은 기록입니다. 파일별 상태를 확인하세요. 실제 오류 경위는 활동 로그에서 확인할 수 있습니다.",
    }.get(status, "파일별 상태를 확인하세요. 작업 과정이나 오류 경위가 필요하면 활동 로그를 확인하세요.")
_STATES = {
    "running": "진행 중 · 중단 여부 확인", "completed": "완료",
    "partial": "부분 완료", "failed": "실패", "pending": "대기",
    "copying": "복사 중", "copied": "복사됨", "copy_preserved": "사본 보존",
    "restored": "복원됨", "unchanged": "원본 유지", "undone": "복원 완료",
    "partial_undo": "일부 복원 · 충돌 확인", "undo_failed": "복원 충돌",
    "cancelled": "중단됨",
}


def _text(value, limit: int = MAX_VALUE_CHARS) -> str:
    """Render scalar values only; never recurse into a journal container."""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "… [표시 생략]"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if isinstance(value, int):
        # Do not send potentially out-of-range filesystem IDs through QVariant.
        return str(value) if value.bit_length() < 128 else "범위를 벗어난 값"
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "올바르지 않은 값"
    return "[지원하지 않는 값]"


def _date(value) -> str:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        pass
    return _text(value, 80)


def history_details(record: dict | None) -> str:
    """Show useful paths/statuses with explicit limits and no recursive dump."""
    if not isinstance(record, dict):
        return "작업 기록을 선택하세요."
    mode = _text(record.get("mode"))
    status = _text(record.get("status"))
    items = record.get("items")
    lines = [
        f"작업 방식: {_MODES.get(mode, mode)}",
        f"상태: {_STATES.get(status, status)}",
        _status_guidance(status),
        "", "되돌리면 어떻게 되나요?", restore_effect(record),
        "", f"시각: {_date(record.get('created_at'))}",
        f"선택 폴더: {_text(record.get('root'))}",
        f"지원 문의용 작업 번호: {_text(record.get('id'))}", "",
    ]
    if not isinstance(items, list):
        lines.append("파일 목록 형식이 올바르지 않습니다. 이 기록에서 복원을 요청할 수 없습니다.")
        return "\n".join(lines)
    lines.append(f"파일별 결과: {len(items):,}개")
    for number, item in enumerate(items[:MAX_DETAIL_ITEMS], 1):
        if not isinstance(item, dict):
            lines.append(f"\n[{number}] 올바르지 않은 항목")
            continue
        state = _text(item.get("state"))
        lines.extend((f"\n[{number}] {_STATES.get(state, state)}",
                      f"원래 위치: {_text(item.get('source'))}",
                      f"대상 위치: {_text(item.get('target'))}"))
        for key, label in (("original_backup", "보호 사본"), ("undo_backup", "복원 보관본"),
                           ("error", "확인할 내용")):
            if item.get(key) is not None:
                lines.append(f"{label}: {_text(item.get(key))}")
    if len(items) > MAX_DETAIL_ITEMS:
        lines.append(f"\n표시 한도: 처음 {MAX_DETAIL_ITEMS:,}개만 표시합니다. 전체 기록은 원본 작업 기록에 보존됩니다.")
    return "\n".join(lines)


class HistoryTableModel(QAbstractTableModel):
    """A flat model: Qt never stores or converts nested Python journals."""
    HEADERS = ("작업 시각", "작업 방식", "상태", "파일 수", "작업 번호")

    def __init__(self, history: list[dict], parent=None):
        super().__init__(parent)
        self.total_records = len(history) if isinstance(history, list) else 0
        self.records = [record if isinstance(record, dict) else {}
                        for record in history[:MAX_HISTORY_ROWS]] if isinstance(history, list) else []
        self._id_counts = Counter(record.get("id") for record in self.records
                                  if isinstance(record.get("id"), str))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.records):
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        record = self.records[index.row()]
        mode, status = _text(record.get("mode")), _text(record.get("status"))
        items = record.get("items")
        values = (_date(record.get("created_at")), _MODES.get(mode, mode),
                  _STATES.get(status, status), f"{len(items):,}" if isinstance(items, list) else "형식 오류",
                  _text(record.get("id"), 80))
        if not 0 <= index.column() < len(values):
            return None
        value = values[index.column()]
        if index.column() == 4 and role == Qt.ItemDataRole.DisplayRole and len(value) > 16:
            return "…" + value[-8:]
        return value

    def operation_id(self, row: int) -> str | None:
        if not 0 <= row < len(self.records):
            return None
        record = self.records[row]
        operation_id = record.get("id")
        if (not isinstance(operation_id, str) or not _ID.fullmatch(operation_id)
                or self._id_counts[operation_id] != 1 or record.get("status") == "undone"
                or not isinstance(record.get("items"), list) or not record["items"]):
            return None
        return operation_id

    def unavailable_reason(self, row: int) -> str:
        if not 0 <= row < len(self.records):
            return "되돌릴 작업을 위 목록에서 선택하세요."
        record = self.records[row]
        if record.get("status") == "undone":
            return "이미 되돌린 작업입니다. 파일별 결과를 확인할 수 있습니다."
        operation_id = record.get("id")
        if not isinstance(record.get("items"), list) or not record["items"]:
            return "되돌릴 파일 목록이 없는 기록입니다. 활동 로그에서 당시 진행 상황을 확인하세요."
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id) or self._id_counts[operation_id] != 1:
            return "이 기록을 안전하게 식별할 수 없어 되돌리기를 사용할 수 없습니다. 원본 기록을 보존하세요."
        return ""


class HistoryDialog(QDialog):
    """Inspect journals and request restoration without entering nested workers."""
    undo_requested = Signal(str)

    def __init__(self, history: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("작업 기록 · 복원")
        self.resize(1000, 620)
        layout = QVBoxLayout(self)
        heading = QLabel("파일 정리를 되돌리고 싶을 때")
        heading.setObjectName("section")
        layout.addWidget(heading)
        distinction = QLabel("작업 기록은 파일을 되돌리는 곳입니다. 진행 과정이나 오류 이유만 확인하려면 ‘활동 로그’를 여세요.")
        distinction.setWordWrap(True)
        distinction.setObjectName("muted")
        layout.addWidget(distinction)
        self.model = HistoryTableModel(history, self)
        count = self.model.total_records
        message = f"저장된 작업 {count:,}개 · 작업을 선택해 파일별 결과를 확인하세요."
        if count > MAX_HISTORY_ROWS:
            message += f" 화면에는 최근 {MAX_HISTORY_ROWS:,}개를 표시합니다."
        if not count:
            message = "아직 정리한 작업이 없습니다. 폴더 분석만으로는 기록이 생기지 않습니다. 미리보기를 확인하고 정리를 실행하면 이곳에서 되돌릴 수 있습니다."
        self.summary_label = QLabel(message)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((175, 125, 190, 75)):
            self.table.setColumnWidth(column, width)
        self.table.setColumnWidth(4, 145)
        splitter.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(1800)
        self.details.setPlainText(history_details(None))
        splitter.addWidget(self.details)
        splitter.setSizes((290, 220))
        layout.addWidget(splitter, 1)
        self.restore_hint = QLabel("되돌릴 작업을 위 목록에서 선택하세요.")
        self.restore_hint.setWordWrap(True)
        self.restore_hint.setObjectName("muted")
        layout.addWidget(self.restore_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.undo_button = buttons.addButton("선택 작업 되돌리기…", QDialogButtonBox.ButtonRole.ActionRole)
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._request_undo)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.selectionModel().currentRowChanged.connect(self._selection_changed)
        if self.model.rowCount():
            self.table.selectRow(0)

    def selected_operation_id(self) -> str | None:
        return self.model.operation_id(self.table.currentIndex().row())

    def _selection_changed(self, current, _previous):
        row = current.row()
        record = self.model.records[row] if 0 <= row < len(self.model.records) else None
        self.details.setPlainText(history_details(record))
        available = self.selected_operation_id() is not None
        self.undo_button.setEnabled(available)
        self.restore_hint.setText(restore_effect(record) if available else self.model.unavailable_reason(row))

    def _request_undo(self):
        operation_id = self.selected_operation_id()
        if operation_id is not None:
            self.undo_requested.emit(operation_id)
