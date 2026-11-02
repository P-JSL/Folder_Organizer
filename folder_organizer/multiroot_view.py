"""One workspace for single/multiple folder analysis and authenticated actions."""
from __future__ import annotations

import os
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from itertools import chain, islice
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QThread, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView, QTabWidget,
    QTreeView, QVBoxLayout, QWidget,
)

from .scanner import normalize_exclude_patterns
from . import workspace_actions
from .matching import find_similar
from .rules import default_rules
from .rule_view import ClassificationDialog
from .multiroot import MAX_ROOTS, MAX_WORKERS, MultiScanResult, scan_folders, write_multiroot_report

STATUS_LABELS = {"waiting": "대기", "scanning": "파일 조회 중", "complete": "완료",
                 "partial": "일부 확인 실패", "failed": "접근 실패", "cancelled": "취소됨"}


def _size(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return "0 B"


def _date(value) -> str:
    if value is None:
        return "제공되지 않음"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "날짜 범위 초과"


class _TableModel(QAbstractTableModel):
    headers: tuple[str, ...] = ()

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None


class _RootModel(_TableModel):
    headers = ("선택 폴더", "조회 상태", "파일 수", "용량", "경고")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.paths: list[str] = []
        self.status: dict[int, dict] = {}
        self.checked_rows: set[int] = set()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.paths)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.paths):
            return None
        row, column = index.row(), index.column()
        values = self.status.get(row, {})
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return Qt.CheckState.Checked if row in self.checked_rows else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return values.get("error") or self.paths[row]
        if role == Qt.ItemDataRole.DisplayRole:
            label = Path(self.paths[row]).name or self.paths[row]
            return (label, STATUS_LABELS.get(values.get("state", "waiting"), values.get("state", "대기")),
                    f"{values.get('files', 0):,}", _size(values.get("bytes", 0)),
                    str(values.get("warnings", 0)))[column]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.checked_rows.add(index.row())
        else:
            self.checked_rows.discard(index.row())
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def update_row(self, row: int, **values):
        if 0 <= row < len(self.paths):
            self.status.setdefault(row, {}).update(values)
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.headers) - 1))


class _FilesModel(_TableModel):
    headers = ("파일 이름", "종류", "용량", "수정한 날짜", "소속 폴더")
    checked_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = None
        self.checked = set()
        self.root_labels = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.result is None else len(self.result.files)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.result is None or not 0 <= index.row() < len(self.result.files):
            return None
        entry = self.result.files[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if entry.path in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.path
        if role == Qt.ItemDataRole.UserRole:
            return (entry.name.casefold(), entry.suffix, entry.size, entry.modified, entry.path)[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (entry.name, "응용 프로그램" if entry.kind == "application" else entry.suffix or "파일",
                    _size(entry.size), _date(entry.modified),
                    ", ".join(self.root_labels.get(root, root) for root in self.result.memberships.get(entry.path, ())))[index.column()]
        return None

    def flags(self, index):
        value = super().flags(index)
        return value | Qt.ItemFlag.ItemIsUserCheckable if index.isValid() and index.column() == 0 else value

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole or self.result is None:
            return False
        path = self.result.files[index.row()].path
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value): self.checked.add(path)
        else: self.checked.discard(path)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()
        return True

    def set_checked(self, paths):
        self.checked = set(paths)
        if self.rowCount(): self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount()-1, 0), [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
        self.checked.clear()
        self.root_labels = {root.root_id: Path(root.path or root.requested_path).name or root.requested_path for root in result.roots} if result else {}
        self.endResetModel()
        self.checked_changed.emit()


class _FilesProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_query(self, text):
        self.beginFilterChange()
        self.query = text.casefold().strip()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, _parent):
        model = self.sourceModel()
        return not self.query or self.query in model.result.files[row].path.casefold()


class _RowsModel(_TableModel):
    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.headers, self.rows = headers, []
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            return str(self.rows[index.row()][index.column()])
        return None
    def set_rows(self, rows):
        self.beginResetModel(); self.rows = list(rows); self.endResetModel()


class _GroupsModel(_TableModel):
    headers = ("중복 그룹", "파일 수", "파일당 용량", "소속 폴더", "첫 파일 경로")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: MultiScanResult | None = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.result is None else len(self.result.groups)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.result is None or not 0 <= index.row() < len(self.result.groups):
            return None
        group = self.result.groups[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return "그룹을 선택하면 오른쪽에서 포함된 경로와 검사 근거를 확인합니다. 첫 파일 보존 정리는 상단에서 실행합니다."
        if role == Qt.ItemDataRole.DisplayRole:
            return (str(index.row() + 1), str(len(group.paths)), _size(group.size),
                    ", ".join(self.root_labels.get(root, root) for root in group.root_ids), Path(group.paths[0]).name)[index.column()]
        return None

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
        self.root_labels = {root.root_id: Path(root.path or root.requested_path).name or root.requested_path
                            for root in result.roots} if result else {}
        self.endResetModel()


class _AnalysisWorker(QThread):
    progress = Signal(object)
    def __init__(self, paths, options, parent=None):
        super().__init__(parent)
        self.paths, self.options = paths, options
        self.cancel_event = Event()
        self.result = self.session = None
        self.error = None
    def run(self):
        try:
            self.session = workspace_actions.analyze_workspace(self.paths, **self.options, cancel=self.cancel_event, progress_detail=self.progress.emit)
            self.result = self.session.result
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class _TaskWorker(QThread):
    progress = Signal(object)
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.callback, self.cancel_event = callback, Event()
        self.result, self.error = None, None
    def run(self):
        try: self.result = self.callback(self.cancel_event, self.progress.emit)
        except Exception as exc: self.error = f"{type(exc).__name__}: {exc}"


class MultiRootWorkspace(QWidget):
    """Live analysis controls; exported reports stay separate from action authority."""

    organize_requested = Signal(str)
    analysis_started = Signal()
    analysis_finished = Signal()
    paths_changed = Signal()
    tool_requested = Signal(str)
    history_requested = Signal(str)

    def __init__(self, parent=None, scan_options: dict | None = None):
        super().__init__(parent)
        self.setObjectName("multiRootWorkspace")
        self._options = {key: value for key, value in (scan_options or {}).items()
                         if key in {"exclude_patterns", "min_size", "max_size"}}
        self._worker: _AnalysisWorker | None = None
        self._active_rows: list[int] = []
        self._close_pending = False
        self.result: MultiScanResult | None = None
        self._entries_by_path = {}
        self.session = None
        self.similar_groups = []
        self.last_receipt = None
        self._task_done = None
        self._external_busy = False
        self._action_buttons = []
        self._idle_buttons = []
        self.root_model = _RootModel(self)
        self.file_model = _FilesModel(self)
        self.group_model = _GroupsModel(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        self.ribbon = QTabWidget()
        self.ribbon.setObjectName("workspaceRibbon")
        self.ribbon.setMaximumHeight(130)
        self.ribbon.setDocumentMode(True)
        home = QWidget(); groups = QHBoxLayout(home); groups.setContentsMargins(5, 2, 5, 2)
        def group(title):
            box = QGroupBox(title); row = QHBoxLayout(box); row.setContentsMargins(7, 8, 7, 5); row.setSpacing(4)
            groups.addWidget(box); return row
        def button(row, text, callback, *, action=False, primary=False):
            value = QPushButton(text); value.setToolTip(text); value.clicked.connect(lambda _checked=False: callback())
            value.setMinimumHeight(43)
            compact = {"폴더 추가…": "폴더\n추가…", "분석 시작": "▶ 분석\n시작", "완전 중복": "완전\n중복", "유사 비교": "유사\n비교", "두 파일 비교": "두 파일\n비교", "표시 전체": "표시\n전체", "중복 사본": "중복\n사본", "삭제 보관…": "삭제\n보관…", "규칙 분류…": "규칙\n분류…", "보고서 저장…": "보고서\n저장…", "기록·복원": "기록·\n복원"}
            if text in compact: value.setText(compact[text])
            if primary: value.setObjectName("primary")
            row.addWidget(value)
            (self._action_buttons if action else self._idle_buttons).append(value)
            return value
        search = group("검색")
        self.add_button = button(search, "폴더 추가…", self._pick_folders)
        self.all_button = button(search, "분석 시작", self.start_analysis, primary=True)
        self.cancel_button = button(search, "중지", self.cancel_analysis)
        self._idle_buttons.remove(self.cancel_button); self.cancel_button.setEnabled(False)
        inspect = group("검사")
        self.duplicate_button = button(inspect, "완전 중복", self.show_duplicates, action=True)
        self.similar_button = button(inspect, "유사 비교", self.analyze_similar, action=True)
        self.compare_button = button(inspect, "두 파일 비교", self.compare_current, action=True)
        marking = group("체크")
        self.mark_all_button = button(marking, "표시 전체", self.mark_visible, action=True)
        self.clear_button = button(marking, "해제", self.clear_marks, action=True)
        self.mark_copies_button = button(marking, "중복 사본", self.mark_copies, action=True)
        organize = group("정리")
        self.copy_button = button(organize, "복사…", lambda: self.preview_action("copy"), action=True)
        self.move_button = button(organize, "이동…", lambda: self.preview_action("move"), action=True)
        self.delete_button = button(organize, "삭제 보관…", lambda: self.preview_action("quarantine"), action=True)
        self.rules_button = button(organize, "규칙 분류…", self.show_rules, action=True)
        report = group("결과")
        self.export_button = button(report, "보고서 저장…", self._export)
        self.history_button = button(report, "기록·복원", self.show_history)
        groups.addStretch()
        self.ribbon.addTab(home, "홈")
        more = QWidget(); more_row = QHBoxLayout(more); more_row.setContentsMargins(8, 5, 8, 5)
        self.keep_first_button = button(more_row, "첫 번째 파일만 남기고 나머지 삭제", self.keep_first, action=True, primary=True)
        self.keep_first_button.setToolTip("현재 검증된 그룹의 첫 파일·보호 파일을 남기고 나머지를 원래 폴더의 delete_files에 보관합니다. 추가 전체 검사·확인 창 없이 실행합니다.")
        button(more_row, "날짜별 분류…", lambda: self.show_rules("date"), action=True)
        button(more_row, "확장자별 분류…", lambda: self.show_rules("extension"), action=True)
        button(more_row, "체크 항목을 중복으로 간주…", lambda: self.preview_action("quarantine"), action=True)
        more_row.addStretch(); self.ribbon.addTab(more, "정리 도구")
        tools = QWidget(); tools_row = QHBoxLayout(tools); tools_row.setContentsMargins(8, 5, 8, 5)
        for label, action_name in (("검사·보존 설정", "settings"), ("손상 검사", "health"), ("복구본 만들기", "repair"), ("실행 로그", "logs"), ("도움말", "help")):
            button(tools_row, label, lambda value=action_name: self.tool_requested.emit(value))
        tools_row.addStretch(); self.ribbon.addTab(tools, "도구·도움말")
        layout.addWidget(self.ribbon)
        self.selection_label = QLabel("폴더 추가 → 분석 → 검사·정리")
        self.selection_label.setObjectName("muted"); layout.addWidget(self.selection_label)

        self.panes = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left.setMinimumWidth(230)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 5, 0)
        left_layout.addWidget(QLabel("검색할 폴더"))
        tip = QLabel("체크한 폴더를 함께 비교합니다.\n폴더를 클릭하면 아래에 전체 경로가 표시됩니다.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        left_layout.addWidget(tip)
        self.root_table = self._table(self.root_model)
        self.root_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.root_table.setMinimumHeight(100)
        for column in (2, 3, 4):
            self.root_table.hideColumn(column)
        self.root_table.setColumnWidth(1, 95)
        self.root_table.horizontalHeader().setStretchLastSection(False)
        self.root_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        left_layout.addWidget(self.root_table, 1)
        self.selected_path = QLabel("등록한 폴더가 없습니다.")
        self.selected_path.setObjectName("muted")
        self.selected_path.setWordWrap(True)
        left_layout.addWidget(self.selected_path)
        input_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("폴더 또는 NAS 경로 입력")
        self.path_input.returnPressed.connect(self._add_input)
        input_row.addWidget(self.path_input, 1)
        self.path_button = QPushButton("추가")
        self.path_button.clicked.connect(self._add_input)
        input_row.addWidget(self.path_button)
        left_layout.addLayout(input_row)
        remove_row = QHBoxLayout()
        self.remove_button = QPushButton("목록에서 제거")
        self.remove_button.setToolTip("선택한 폴더를 검색 목록에서만 제거합니다. 파일은 유지됩니다.")
        self.remove_button.clicked.connect(self._remove_selected)
        remove_row.addWidget(self.remove_button)
        self.selected_button = QPushButton("선택만 검사")
        self.selected_button.setToolTip("행으로 선택한 폴더만 이번에 검사합니다.")
        self.selected_button.clicked.connect(lambda: self.start_analysis(selected=True))
        remove_row.addWidget(self.selected_button)
        left_layout.addLayout(remove_row)
        filters = QGroupBox("검사 범위")
        filter_layout = QVBoxLayout(filters)
        self.recursive = QCheckBox("하위 폴더 포함")
        self.recursive.setChecked((scan_options or {}).get("recursive", True))
        filter_layout.addWidget(self.recursive)
        self.hidden = QCheckBox("숨김 파일 포함")
        self.hidden.setChecked((scan_options or {}).get("include_hidden", False))
        filter_layout.addWidget(self.hidden)
        self.exclusions = QLineEdit("; ".join(self._options.get("exclude_patterns", ())))
        self.exclusions.setPlaceholderText("제외 패턴: *.tmp; Backup/**")
        self.exclusions.setToolTip("세미콜론으로 구분합니다. 검사할 때 파일·폴더를 제외합니다.")
        filter_layout.addWidget(self.exclusions)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("최소 크기"))
        self.minimum = QDoubleSpinBox()
        self.minimum.setRange(0, 16_777_216)
        self.minimum.setDecimals(3)
        self.minimum.setSuffix(" MiB")
        self.minimum.setValue(self._options.get("min_size", 0) / 1024 ** 2)
        self._minimum_edited = False
        self.minimum.valueChanged.connect(lambda _value: setattr(self, "_minimum_edited", True))
        size_row.addWidget(self.minimum, 1)
        filter_layout.addLayout(size_row)
        workers_row = QHBoxLayout()
        workers_row.addWidget(QLabel("동시 탐색"))
        self.workers = QSpinBox()
        self.workers.setRange(1, MAX_WORKERS)
        self.workers.setValue(2)
        self.workers.setToolTip("다른 볼륨을 제한된 수로 조회합니다. 같은 볼륨·NAS 서버와 내용 검사는 순차 진행합니다.")
        workers_row.addWidget(self.workers)
        workers_row.addStretch()
        filter_layout.addLayout(workers_row)
        left_layout.addWidget(filters)
        self.organize_button = QPushButton("이 폴더만 분석")
        self.organize_button.setEnabled(False)
        self.organize_button.setToolTip("선택한 폴더 하나를 같은 화면에서 분석합니다. 등록한 다른 폴더는 유지됩니다.")
        self.organize_button.clicked.connect(self._organize_selected)
        left_layout.addWidget(self.organize_button)
        self.panes.addWidget(left)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(3, 0, 3, 0)
        self.summary = QLabel("폴더 추가 → 분석 시작 → 결과 확인")
        self.summary.setObjectName("section")
        self.summary.setWordWrap(True)
        center_layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        self.file_proxy = _FilesProxy(self)
        self.file_proxy.setSourceModel(self.file_model)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("결과에서 파일 이름·전체 경로 찾기")
        self.search_input.textChanged.connect(self.file_proxy.set_query)
        center_layout.addWidget(self.search_input)
        self.file_table = self._table(self.file_proxy)
        self.file_table.setSortingEnabled(True)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.file_table.setColumnWidth(0, 215)
        self.file_table.setColumnWidth(1, 90)
        self.file_table.setColumnWidth(2, 95)
        self.file_table.setColumnWidth(3, 155)
        self.tabs.addTab(self.file_table, "전체 파일")
        self.group_table = self._table(self.group_model)
        self.group_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tabs.addTab(self.group_table, "완전 중복")
        self.similar_model = _RowsModel(("유사 후보", "파일 수", "근거", "첫 파일"), self)
        self.similar_table = self._table(self.similar_model)
        self.tabs.addTab(self.similar_table, "유사 후보")
        center_layout.addWidget(self.tabs, 1)
        self.type_model = _RowsModel(("파일 종류", "파일 수", "전체 용량", "중복 사본", "사본 용량"), self)
        self.type_table = self._table(self.type_model)
        self.type_table.setMaximumHeight(140)
        center_layout.addWidget(QLabel("파일 종류별 요약"))
        center_layout.addWidget(self.type_table)

        self.empty_hint = QLabel("왼쪽에 비교할 폴더를 추가하세요.\n분석 시작을 누르면 파일 목록과 폴더 간 완전 중복을 함께 찾습니다.\n분석만으로 원본 파일은 바뀌지 않습니다.")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        center_layout.addWidget(self.empty_hint)
        self.panes.addWidget(center)

        right = QWidget()
        right.setMinimumWidth(210)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(5, 0, 0, 0)
        right_layout.addWidget(QLabel("파일 정보"))
        self.inspector = QPlainTextEdit()
        self.inspector.setReadOnly(True)
        self.inspector.setPlaceholderText("파일을 클릭하면 이름·용량·날짜·경로를 표시합니다.\n분석 시점의 정보를 사용합니다.")
        right_layout.addWidget(self.inspector, 1)
        right_layout.addWidget(QLabel("그룹 · 분석 안내"))
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("완전 중복 그룹을 클릭하면 근거와 포함된 경로를 표시합니다.")
        right_layout.addWidget(self.details, 1)
        self.scope_hint = QLabel("체크한 폴더를 하나의 작업 공간에서 분석합니다. 정리 전에는 실제 경로를 미리 확인합니다.")
        self.scope_hint.setWordWrap(True); self.scope_hint.setObjectName("muted")
        right_layout.addWidget(self.scope_hint)
        self.panes.addWidget(right)
        self.panes.setStretchFactor(0, 0)
        self.panes.setStretchFactor(1, 1)
        self.panes.setStretchFactor(2, 0)
        self.panes.setSizes([285, 640, 260])
        layout.addWidget(self.panes, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QLabel("폴더 하나 또는 여러 개를 추가하세요. 같은 화면에서 검사·정리할 수 있습니다.")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.group_table.selectionModel().selectionChanged.connect(self._show_group)
        self.file_table.selectionModel().selectionChanged.connect(self._show_file)
        self.root_table.selectionModel().selectionChanged.connect(self._root_selected)
        self.root_model.dataChanged.connect(self._checked_changed)
        self.tabs.currentChanged.connect(lambda index: self._show_file() if index == 0 else self._show_group() if index == 1 else self._show_similar())
        self.similar_table.selectionModel().selectionChanged.connect(self._show_similar)
        self.file_model.checked_changed.connect(self._selection_changed)
        for control in (self.recursive, self.hidden): control.toggled.connect(self._scope_edited)
        self.exclusions.textChanged.connect(self._scope_edited)
        self.minimum.valueChanged.connect(self._scope_edited)
        self._refresh_action_controls()

    def _scope_edited(self, *_args):
        if self.session is not None and not self.is_running():
            self.invalidate_after_change("검사 범위가 바뀌었습니다. 현재 범위로 다시 분석한 뒤 정리하세요.")

    def _actionable(self):
        return self.session is not None and not self.session.consumed and self.result is not None and self.result.complete and not self.result.cancelled and not self._external_busy

    def _refresh_action_controls(self, *, busy=None):
        busy = (self.is_running() if busy is None else busy) or self._external_busy
        active = self._actionable() and not busy
        for button in self._action_buttons:
            button.setEnabled(active)
        self.keep_first_button.setEnabled(active and bool(self.result.groups))
        self.export_button.setEnabled(not busy and self.result is not None)
        self.history_button.setEnabled(not busy and bool(self.root_model.paths))
        if hasattr(self, "selection_label"):
            count = len(self.file_model.checked)
            self.selection_label.setText(f"체크한 파일 {count:,}개 · 원래 폴더별 기록·복원 · " +
                                         ("검사·정리 가능" if active else "분석 중" if busy else "폴더를 분석하세요"))

    def set_external_busy(self, busy, *, cancellable=False):
        self._external_busy = busy
        self._set_busy(busy)
        self.cancel_button.setEnabled(busy and cancellable)
        if busy: self.status.setText(getattr(self.window(), "_task_label", "처리 중…"))

    def _selection_changed(self):
        self._refresh_action_controls()

    def checked_file_paths(self):
        if self.result is None:
            return []
        return [entry.path for entry in self.result.files if entry.path in self.file_model.checked]

    def mark_visible(self):
        if not self._actionable() or self.is_running(): return
        paths = set(self.file_model.checked)
        for row in range(self.file_proxy.rowCount()):
            source = self.file_proxy.mapToSource(self.file_proxy.index(row, 0)).row()
            paths.add(self.result.files[source].path)
        self.file_model.set_checked(paths)

    def clear_marks(self):
        if not self.is_running(): self.file_model.set_checked([])

    def _analysis_options(self):
        parent = self.window()
        return dict(getattr(parent, "analysis_options", {}))

    def mark_copies(self):
        if not self._actionable() or self.is_running(): return
        options, selected = self._analysis_options(), []
        protected = [Path(folder) for folder in options.get("protected_folders", [])]
        preferred = options.get("preferred_folder")
        rule = options.get("retention_rule", "first")
        for group in self.session.groups:
            members = [self._entries_by_path[path] for path in group.paths if path in self._entries_by_path]
            protected_members = [entry for entry in members if any(Path(entry.path).is_relative_to(folder) for folder in protected)]
            pool = protected_members or members
            if not pool: continue
            if rule == "newest": keeper = max(pool, key=lambda entry: entry.modified)
            elif rule == "oldest": keeper = min(pool, key=lambda entry: entry.modified)
            elif rule == "shortest_path": keeper = min(pool, key=lambda entry: len(entry.path))
            elif rule == "preferred_folder" and preferred:
                keeper = next((entry for entry in pool if Path(entry.path).is_relative_to(preferred)), pool[0])
            else: keeper = pool[0]
            retained = {entry.path for entry in protected_members} | {keeper.path}
            selected.extend(entry.path for entry in members if entry.path not in retained)
        self.file_model.set_checked(selected)
        self.tabs.setCurrentIndex(0)
        self.status.setText(f"보존 규칙으로 사본 {len(selected):,}개를 체크했습니다. 추가 내용 검사는 하지 않았으며 정리 전 미리보기에서 변경 여부를 확인합니다.")

    def show_duplicates(self):
        if self.result is None:
            self.status.setText("폴더를 먼저 분석하세요."); return
        self.tabs.setCurrentIndex(1)
        self.status.setText(f"현재 분석에서 검증한 완전 중복 {len(self.result.groups):,}그룹입니다. 추가 전체 검사는 실행하지 않았습니다.")

    def analyze_similar(self):
        if not self._actionable() or self.is_running():
            self.status.setText("현재 폴더를 먼저 분석하세요."); return
        session, options = self.session, self._analysis_options()
        def task(cancel, progress):
            report = {}
            groups = find_similar(list(session.files), methods=tuple(options.get("methods", ("name", "text", "image"))),
                name_threshold=options.get("name_threshold", .82), text_threshold=options.get("text_threshold", .90),
                image_threshold=options.get("image_threshold", .90), verified_duplicates=list(session.groups),
                search_mode=options.get("search_mode", "auto"), max_candidates_per_file=options.get("max_candidates_per_file", 32),
                cancel=cancel, progress_detail=progress, report=report)
            return groups, report
        def done(value):
            self.similar_groups = list(value[0])
            self.similar_model.set_rows([(index+1, len(group.paths), group.reason, Path(group.paths[0]).name)
                                         for index, group in enumerate(self.similar_groups)])
            self.tabs.setTabText(2, f"유사 후보 ({len(self.similar_groups):,})")
            self.tabs.setCurrentIndex(2)
            self.status.setText(f"유사 후보 {len(self.similar_groups):,}그룹 · 완전 중복·자동 삭제 판정이 아닙니다.")
            self.details.setPlainText("유사 후보는 직접 비교할 대상입니다. 높은 점수만으로 같은 내용이나 삭제 가능한 사본이라고 판단하지 않습니다.\n" +
                                      json.dumps(value[1], ensure_ascii=False, indent=2))
        self._start_task("체크한 폴더 전체의 유사 후보를 찾습니다…", task, done)

    def _show_similar(self, *_args):
        rows = self.similar_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.similar_groups): return
        group = self.similar_groups[rows[0].row()]
        first = self._entries_by_path.get(group.paths[0])
        if first: self._inspect_entry(first)
        self.details.setPlainText(f"유사 후보 · {group.reason}\n직접 비교가 필요한 서로 다른 파일입니다.\n\n" + "\n".join(group.paths[:1000]))

    def comparison_paths(self):
        checked = self.checked_file_paths()
        if len(checked) == 2: return checked
        if self.tabs.currentIndex() == 1:
            rows = self.group_table.selectionModel().selectedRows()
            groups = self.result.groups if self.result else []
        elif self.tabs.currentIndex() == 2:
            rows = self.similar_table.selectionModel().selectedRows(); groups = self.similar_groups
        else: return []
        return list(groups[rows[0].row()].paths[:2]) if rows and rows[0].row() < len(groups) else []

    def merge_paths(self):
        checked = self.checked_file_paths()
        if len(checked) >= 2: return checked
        if self.tabs.currentIndex() == 1:
            rows = self.group_table.selectionModel().selectedRows()
            groups = self.result.groups if self.result else []
        elif self.tabs.currentIndex() == 2:
            rows = self.similar_table.selectionModel().selectedRows()
            groups = self.similar_groups
        else: return []
        return list(groups[rows[0].row()].paths) if rows and rows[0].row() < len(groups) else []

    def compare_current(self):
        if self.is_running(): return
        paths = self.comparison_paths()
        if len(paths) != 2:
            self.status.setText("파일 두 개를 체크하거나 완전 중복·유사 후보 그룹을 선택하세요."); return
        parent = self.window()
        if hasattr(parent, "compare_paths"): parent.compare_paths(*paths)

    def _start_task(self, message, task, done):
        if self.is_running(): return
        self._task_done = done
        self._worker = _TaskWorker(task, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._finished)
        self.status.setText(message)
        self._set_busy(True)
        self.analysis_started.emit()
        self._worker.start()

    def preview_action(self, mode, *, destination=None):
        if not self._actionable() or self.is_running():
            self.status.setText("현재 폴더를 먼저 분석하세요. 작업 후에는 새 분석이 필요합니다."); return
        paths = self.checked_file_paths()
        if not paths:
            self.status.setText("파일 이름 앞의 체크박스로 정리할 파일을 선택하세요."); return
        if mode in {"copy", "move"} and destination is None:
            destination = QFileDialog.getExistingDirectory(self, "복사 대상 폴더" if mode == "copy" else "이동 대상 폴더")
            if not destination: return
        session = self.session
        self._start_task("원래 폴더별 작업 경로를 미리 확인합니다…",
            lambda cancel, progress: workspace_actions.build_workspace_plan(session, paths, mode, destination=destination, progress_detail=progress),
            self._show_batch_preview)

    def show_rules(self, preset=None):
        if not self._actionable() or self.is_running():
            self.status.setText("현재 폴더를 먼저 분석하세요."); return
        parent = self.window()
        rules = default_rules(preset) if preset else parent._classification_rules() if hasattr(parent, "_classification_rules") else default_rules("type")
        dialog = ClassificationDialog(rules, self)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        values = dialog.values
        if hasattr(parent, "settings") and not getattr(parent, "_read_only_settings", False):
            parent.settings.setValue("classification/rules", json.dumps(values["rules"], ensure_ascii=False))
        session, options = self.session, self._analysis_options()
        self._start_task("분석한 전체 파일에 규칙을 적용해 경로를 확인합니다…",
            lambda cancel, progress: workspace_actions.build_workspace_plan(session, None, "rules", rules=values["rules"],
                destination=values.get("destination"), action=values["action"], protected_folders=options.get("protected_folders", []), progress_detail=progress),
            self._show_batch_preview)

    def _show_batch_preview(self, plan):
        items = plan.get("items", [])
        if not items:
            self.status.setText("조건에 맞는 변경할 파일이 없습니다."); return
        self._preview_plan = plan
        dialog = QDialog(self); dialog.setWindowTitle("작업 미리보기 · 단일·다중 폴더"); dialog.resize(900, 590)
        layout = QVBoxLayout(dialog)
        mode = {"quarantine": "삭제 보관", "copy": "복사", "move": "이동", "rules": "규칙 분류"}.get(plan.get("mode"), "정리")
        intro = QLabel(f"{mode} · {len(items):,}개 파일 · 원래 폴더 {len(plan.get('plans', [])):,}개\n각 원래 폴더에 복원 기록을 남깁니다. 같은 이름의 파일은 덮어쓰지 않습니다.")
        intro.setWordWrap(True); layout.addWidget(intro)
        if plan.get("mode") == "quarantine":
            note = QLabel("직접 체크한 파일은 중복 여부를 추가 검사하지 않고 delete_files에 보관합니다. 남길 원본이 선택되지 않았는지 확인하세요.")
            note.setWordWrap(True); note.setObjectName("warning"); layout.addWidget(note)
        paths = QPlainTextEdit(); paths.setReadOnly(True)
        lines = []
        for root_plan in plan.get("plans", []):
            lines.append("원래 폴더: " + root_plan["root"])
            for item in root_plan["items"][:1000]: lines.extend(("원본  " + item["source"], "대상  " + item["target"], ""))
            if len(root_plan["items"]) > 1000:
                lines.append(f"이 폴더의 {len(root_plan['items']):,}개 중 처음 1,000개 경로 표시 · 나머지 {len(root_plan['items'])-1000:,}개는 실행 대상에 포함됩니다.")
        paths.setPlainText("\n".join(lines)); layout.addWidget(paths, 1)
        note = QLabel("취소·실패가 발생하면 완료된 항목의 기록은 남고 나머지 폴더를 모두 완료한 것으로 표시하지 않습니다.")
        note.setWordWrap(True); layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(f"위 {len(items):,}개 {mode} 실행")
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.execute_preview(plan)
        self._preview_plan = None

    def execute_preview(self, plan):
        if not self._actionable() or self.is_running(): return
        session = self.session
        self._start_task("원래 폴더별 정리와 복원 기록을 실행합니다…",
            lambda cancel, progress: workspace_actions.execute_workspace_plan(session, plan, cancel=cancel, progress_detail=progress),
            self._operation_done)

    def keep_first(self):
        if not self._actionable() or self.is_running():
            self.status.setText("현재 완전 중복 검사를 먼저 완료하세요."); return
        if not self.result.groups:
            self.status.setText("정리할 완전 중복 그룹이 없습니다."); return
        session, options = self.session, self._analysis_options()
        def task(cancel, progress):
            plan = workspace_actions.build_workspace_keep_first_plan(session, protected_folders=options.get("protected_folders", []), progress_detail=progress)
            return workspace_actions.execute_workspace_plan(session, plan, cancel=cancel, progress_detail=progress)
        self._start_task("현재 그룹의 첫 파일을 남기고 나머지를 delete_files에 보관합니다…", task, self._operation_done)

    def invalidate_after_change(self, message):
        if self.session is not None: self.session.invalidate()
        self.file_model.set_checked([])
        self._refresh_action_controls()
        self.status.setText(message)

    def _operation_done(self, receipt):
        self.last_receipt = receipt
        moved = {item["source"] for item in receipt.get("items", [])
                 if item.get("source_removed") or item.get("state") == "completed" and item.get("action") == "move"}
        if self.result is not None:
            self.result.files = [entry for entry in self.result.files if entry.path not in moved]
            self.result.groups = []
            self._entries_by_path = {entry.path: entry for entry in self.result.files}
            self.file_model.set_result(self.result); self.group_model.set_result(self.result)
            self.summary.setText(f"정리 후 목록 · {len(self.result.files):,}개 · 다음 작업은 새 분석 필요")
            self.tabs.setTabText(0, f"전체 파일 ({len(self.result.files):,})"); self.tabs.setTabText(1, "완전 중복 (사용 완료)")
        if self.result is not None:
            self.result.complete = False
            self.result.notes.append("정리 후 실행 기록으로 갱신한 목록입니다. 다음 작업과 현재 상태의 완전한 보고서는 새 분석이 필요합니다.")
            self.result.duplicate_analysis["complete"] = False
            for root in self.result.roots:
                if root.scan is not None: root.scan.files = [entry for entry in root.scan.files if entry.path not in moved]
        self.similar_groups = []; self.similar_model.set_rows([])
        self._update_type_summary()
        self._refresh_action_controls()
        status = {"completed": "완료", "partial": "일부 처리", "failed": "실패", "cancelled": "취소됨", "unchanged": "변경 없음"}.get(receipt.get("status"), receipt.get("status", "확인 필요"))
        self.progress.setRange(0, 1)
        self.progress.setValue(int(receipt.get("status") == "completed"))
        self.progress.setFormat("정리 " + status)
        self.status.setText(f"정리 {status} · {len(moved):,}개 원래 위치에서 이동 · 전체 재검사 없이 기록으로 목록을 갱신했습니다. 다음 작업은 분석 시작을 누르세요.")
        self.details.setPlainText("원래 폴더별 작업 기록에서 복원할 수 있습니다.\n\n" +
            "\n".join(f"{value.get('root')} · {value.get('id')} · {value.get('status')}" for value in receipt.get("operations", [])) +
            ("\n\n" + json.dumps(receipt.get("errors"), ensure_ascii=False, indent=2) if receipt.get("errors") else ""))

    def _update_type_summary(self):
        totals = defaultdict(lambda: [0, 0, 0, 0])
        if self.result:
            for entry in self.result.files:
                value = totals[entry.suffix or "확장자 없음"]; value[0] += 1; value[1] += entry.size
            for group in self.result.groups:
                for path in group.paths[1:]:
                    entry = self._entries_by_path.get(path)
                    if entry:
                        value = totals[entry.suffix or "확장자 없음"]; value[2] += 1; value[3] += entry.size
        self.type_model.set_rows([(kind, values[0], _size(values[1]), values[2], _size(values[3]))
                                  for kind, values in sorted(totals.items(), key=lambda item: -item[1][1])])

    def show_history(self):
        if self.is_running(): return
        paths = list(dict.fromkeys(self.root_model.paths))
        if not paths: self.status.setText("기록을 확인할 원래 폴더를 추가하세요."); return
        rows = self.root_table.selectionModel().selectedRows()
        root = self.root_model.paths[rows[0].row()] if rows else paths[0]
        if len(paths) > 1:
            from PySide6.QtWidgets import QInputDialog
            root, accepted = QInputDialog.getItem(self, "원래 폴더별 작업 기록", "복원 기록을 확인할 폴더", paths, paths.index(root), False)
            if not accepted: return
        self.history_requested.emit(root)

    def export_csv(self):
        if self.result is None or self.is_running(): return
        path, _ = QFileDialog.getSaveFileName(self, "새 CSV 파일로 목록 저장", "workspace-files.csv", "CSV (*.csv)")
        if not path: return
        visible_entries = tuple(self.result.files[self.file_proxy.mapToSource(self.file_proxy.index(row, 0)).row()]
                                for row in range(self.file_proxy.rowCount()))
        def task(cancel, progress):
            def safe(value):
                value = str(value)
                return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            with open(path, "x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream); writer.writerow(("이름", "경로", "바이트", "수정일", "생성일"))
                for entry in visible_entries:
                    if cancel.is_set(): break
                    writer.writerow((safe(entry.name), safe(entry.path), entry.size, _date(entry.modified), _date(entry.created)))
            return path
        self._start_task("현재 목록을 새 CSV로 저장합니다…", task, lambda value: self.status.setText("CSV 저장: " + value))

    def checked_paths(self):
        return [path for row, path in enumerate(self.root_model.paths) if row in self.root_model.checked_rows]

    def _checked_changed(self, _first, _last, roles=None):
        if roles and Qt.ItemDataRole.CheckStateRole in roles and not self.is_running():
            self._clear_results()
            self.status.setText(f"체크한 {len(self.checked_paths())}개 폴더가 다음 분석의 대상입니다.")
            self.paths_changed.emit()

    def _root_selected(self, *_args):
        rows = self.root_table.selectionModel().selectedRows()
        self.organize_button.setEnabled(bool(rows) and not self.is_running())
        self.selected_path.setText(self.root_model.paths[rows[0].row()] if rows else "폴더를 클릭하면 전체 경로를 확인할 수 있습니다.")

    def _organize_selected(self):
        rows = self.root_table.selectionModel().selectedRows()
        if rows and not self.is_running():
            self.start_analysis(selected=True)

    def selected_snapshot(self):
        if self.result is None:
            return None
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return None
        entry = self.result.files[self.file_proxy.mapToSource(rows[0]).row()]
        return {"name": entry.name, "path": entry.path, "size": entry.size,
                "modified": entry.modified, "created": entry.created, "kind": entry.kind}

    def _show_file(self, *_args):
        if self.result is None:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if rows:
            self._inspect_entry(self.result.files[self.file_proxy.mapToSource(rows[0]).row()])

    def _inspect_entry(self, entry):
        self.inspector.setPlainText(f"{entry.name}\n\n용량  {_size(entry.size)}\n종류  {'응용 프로그램' if entry.kind == 'application' else entry.suffix or '파일'}\n수정  {_date(entry.modified)}\n생성  {_date(entry.created)}\n\n전체 경로\n{entry.path}\n\n분석 시점의 정보입니다.")

    def _table(self, model):
        table = QTableView(self)
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        return table

    def add_paths(self, paths):
        if self.is_running():
            return
        seen = set(self.root_model.paths)
        added = []
        for raw in paths:
            value = str(raw).strip()
            if not value:
                continue
            path = os.path.abspath(os.path.expanduser(value))
            key = path
            if key not in seen:
                seen.add(key)
                added.append(path)
        available = MAX_ROOTS - len(self.root_model.paths)
        dropped = max(0, len(added) - available)
        if len(added) > available:
            self.status.setText(f"최대 {MAX_ROOTS}개 폴더까지 등록할 수 있습니다.")
        added = added[:available]
        if added:
            self._clear_results()
            first = len(self.root_model.paths)
            self.root_model.beginInsertRows(QModelIndex(), first, first + len(added) - 1)
            self.root_model.paths.extend(added)
            self.root_model.checked_rows.update(range(first, first + len(added)))
            self.root_model.endInsertRows()
            self.root_table.selectRow(first)
            self.paths_changed.emit()
            self.status.setText(f"{len(self.root_model.paths)}개 폴더가 등록됐습니다. 결과는 선택한 범위 전체에서 함께 비교합니다.")
            if dropped:
                self.status.setText(self.status.text() + f" 최대 개수 제한으로 {dropped}개는 등록하지 않았습니다.")

    def _add_input(self):
        self.add_paths([self.path_input.text()])
        self.path_input.clear()

    def _pick_folders(self):
        dialog = QFileDialog(self, "여러 폴더 선택")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        for view in [*dialog.findChildren(QListView), *dialog.findChildren(QTreeView)]:
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_paths(dialog.selectedFiles())

    def _remove_selected(self):
        if self.is_running():
            return
        rows = sorted({index.row() for index in self.root_table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        self._clear_results()
        retained = [(path, index in self.root_model.checked_rows) for index, path in enumerate(self.root_model.paths) if index not in rows]
        self.root_model.beginResetModel()
        self.root_model.paths = [path for path, _checked in retained]
        self.root_model.checked_rows = {index for index, (_path, checked) in enumerate(retained) if checked}
        self.root_model.status.clear()
        self.root_model.endResetModel()
        self.paths_changed.emit()
        self._root_selected()

    def is_running(self) -> bool:
        return self._worker is not None

    def _clear_results(self):
        if self.session is not None: self.session.invalidate()
        self.session = None
        self.result = None
        self.similar_groups = []
        self._entries_by_path = {}
        self.similar_model.set_rows([])
        self.type_model.set_rows([])
        self.file_model.set_result(None)
        self.group_model.set_result(None)
        self.tabs.setTabText(0, "전체 파일")
        self.tabs.setTabText(1, "완전 중복")
        self.tabs.setTabText(2, "유사 후보")
        self.export_button.setEnabled(False)
        self.details.clear()
        self.inspector.clear()
        self.summary.setText("폴더 추가 → 분석 시작 → 결과 확인")
        self.empty_hint.show()
        self._refresh_action_controls()

    def _set_busy(self, busy):
        for widget in (self.add_button, self.path_button, self.remove_button, self.path_input,
                       self.recursive, self.hidden, self.workers, self.exclusions, self.minimum,
                       self.root_table, self.selected_button, self.all_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.organize_button.setEnabled(not busy and bool(self.root_table.selectionModel().selectedRows()))
        self.export_button.setEnabled(not busy and self.result is not None)
        for widget in self._idle_buttons: widget.setEnabled(not busy)
        self._refresh_action_controls(busy=busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(int(self.result is not None and self.result.complete))
            self.progress.setFormat("취소됨 · 부분 결과" if self.result and self.result.cancelled else
                                    "분석 완료" if self.result and self.result.complete else
                                    "일부 확인 실패" if self.result else "대기")

    def start_analysis(self, checked=False, *, selected=False):
        if self.is_running():
            return
        rows = sorted(index.row() for index in self.root_table.selectionModel().selectedRows()) if selected else sorted(self.root_model.checked_rows)
        if not rows:
            self.status.setText("검사할 폴더 행을 먼저 선택하세요." if selected else
                                "검사할 폴더를 체크하세요." if self.root_model.paths else "폴더를 먼저 등록하세요.")
            return
        try:
            patterns = list(normalize_exclude_patterns([value.strip() for value in self.exclusions.text().split(";") if value.strip()]))
        except ValueError as exc:
            self.status.setText("제외 패턴을 확인하세요: " + str(exc))
            return
        minimum = round(self.minimum.value() * 1024 ** 2) if self._minimum_edited else self._options.get("min_size", 0)
        maximum = self._options.get("max_size")
        if maximum is not None and minimum > maximum:
            self.status.setText("최소 크기가 저장된 최대 크기보다 큽니다. 검사 설정에서 범위를 조정하세요.")
            return
        self._active_rows = rows
        self._clear_results()
        for row in range(len(self.root_model.paths)):
            if row not in rows:
                self.root_model.update_row(row, state="이번 검사 제외", files=0, bytes=0, warnings=0, error=None, root_id=None)
        options = {**self._options, "exclude_patterns": patterns, "min_size": minimum, "recursive": self.recursive.isChecked(), "include_hidden": self.hidden.isChecked(),
                   "workers": self.workers.value(), "duplicates": True}
        self._worker = _AnalysisWorker([self.root_model.paths[row] for row in rows], options, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._finished)
        for row in rows:
            self.root_model.update_row(row, state="waiting", files=0, bytes=0, warnings=0, error=None, root_id=None)
        self.status.setText("폴더를 조회합니다. 같은 볼륨·NAS 서버의 조회는 순차 진행합니다.")
        self._set_busy(True)
        self.analysis_started.emit()
        self._worker.start()

    def _on_progress(self, event):
        root_id = event.get("root_id")
        if root_id:
            row = next((row for row in self._active_rows if self.root_model.paths[row] == event.get("requested_path")), None)
            if row is not None:
                self.root_model.update_row(row, state=event.get("state", "scanning"), root_id=root_id,
                    files=event.get("completed", 0), bytes=event.get("bytes", 0), warnings=event.get("warnings", 0))
        message = event.get("message", "")
        label = next((Path(self.root_model.paths[row]).name for row in self._active_rows
                      if self.root_model.status.get(row, {}).get("root_id") == root_id), root_id)
        self.status.setText(f"{label}: {message}" if root_id else f"현재 작업: {message}")
        total, done = event.get("total"), event.get("completed", 0)
        if isinstance(total, (int, float)) and total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(min(1000, round(done / total * 1000)))
            self.progress.setFormat(f"현재 단계 {done:,} / {total:,}")
        else:
            self.progress.setRange(0, 0)

    def _finished(self):
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        if isinstance(worker, _TaskWorker):
            callback = self._task_done
            self._task_done = None
            worker.deleteLater()
            self._set_busy(False)
            self.analysis_finished.emit()
            if worker.error:
                self.status.setText("작업 중단: " + worker.error)
                self._refresh_action_controls()
            elif worker.cancel_event.is_set() and callback != self._operation_done:
                self.status.setText("작업을 중단했습니다. 미리보기 실행은 시작하지 않았습니다.")
            elif callback is not None and not self._close_pending and not getattr(self.window(), "_close_requested", False):
                callback(worker.result)
            if self._close_pending: self.close()
            return
        if worker.result is not None:
            self.session = worker.session
            self.result = result = worker.result
            self._entries_by_path = {entry.path: entry for entry in result.files}
            self.file_model.set_result(result)
            self.group_model.set_result(result)
            for root in result.roots:
                row = next((row for row in self._active_rows if self.root_model.paths[row] == root.requested_path), None)
                if row is not None:
                    self.root_model.update_row(row, state=root.state, files=root.file_count, root_id=root.root_id,
                        bytes=root.total_size, warnings=len(root.scan.warnings) if root.scan else int(root.error is not None), error=root.error)
            for alias in result.duplicate_inputs:
                row = next((row for row in self._active_rows if self.root_model.paths[row] == alias["requested_path"]), None)
                if row is not None:
                    self.root_model.update_row(row, state=f"동일 폴더: {alias['root_id']}")
            final = "취소됨" if result.cancelled else "완료" if result.complete else "일부 확인 실패"
            self.summary.setText(f"파일 {len(result.files):,}개 · 완전 중복 {len(result.groups):,}그룹")
            self.tabs.setTabText(0, f"전체 파일 ({len(result.files):,})")
            self.tabs.setTabText(1, f"완전 중복 ({len(result.groups):,})")
            self.empty_hint.setVisible(not result.files)
            self.empty_hint.setText("조건에 맞는 파일이 없습니다. 폴더와 검사 범위를 확인하세요.")
            self.status.setText(f"{final} · 고유 경로 {len(result.files):,}개 · 완전 중복 {len(result.groups):,}그룹 · {result.elapsed_seconds:.2f}초 · 검사·정리 가능" if self._actionable() else f"{final} · 미완료 결과는 정리할 수 없습니다.")
            self._update_type_summary()
            warnings = chain(result.notes, (f"{root.root_id}: {root.error}" for root in result.roots if root.error),
                (f"{root.root_id}: {warning}" for root in result.roots if root.scan for warning in root.scan.warnings),
                (f"{warning.get('phase')}: {warning.get('message')}" for warning in result.duplicate_analysis.get("warnings", [])))
            self.details.setPlainText("\n".join(islice(warnings, 1000)) or "체크한 파일을 복사·이동·삭제 보관하거나 규칙으로 분류할 수 있습니다. 완전 중복은 정리 도구에서 첫 파일을 남기고 정리합니다.")
        elif worker.error:
            self.status.setText("분석 실패: " + worker.error)
        worker.deleteLater()
        self._set_busy(False)
        self.analysis_finished.emit()
        if self._close_pending:
            self.close()

    def _show_group(self, *_args):
        if self.result is None:
            return
        rows = self.group_table.selectionModel().selectedRows()
        if not rows:
            return
        group = self.result.groups[rows[0].row()]
        first = self._entries_by_path.get(group.paths[0])
        if first is not None:
            self._inspect_entry(first)
        paths = group.paths[:1000]
        tail = f"\n나머지 {len(group.paths) - len(paths):,}개 경로는 저장한 보고서에서 확인하세요." if len(paths) < len(group.paths) else ""
        self.details.setPlainText(f"완전 중복 {len(group.paths):,}개 · 파일당 {_size(group.size)}\n첫 파일은 경로순이며 최적 원본을 뜻하지 않습니다.\n정리 도구의 첫 파일 보존 기능은 현재 검증 결과를 사용합니다.\nSHA-256: {group.sha256}\n\n" + "\n".join(paths) + tail)

    def cancel_analysis(self):
        if self._worker is not None:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("취소 요청 중입니다. 진행 중인 파일 읽기가 끝나면 부분 상태를 표시합니다.")
        elif self._external_busy:
            self.window().cancel_task()
            self.cancel_button.setEnabled(False)
            self.status.setText("현재 작업의 중단을 요청했습니다.")

    def _export(self):
        if self.result is None or self.is_running():
            return
        path, _ = QFileDialog.getSaveFileName(self, "읽기 전용 통합 분석 보고서 저장", "multiroot-results.json", "JSON (*.json)")
        if path:
            try:
                write_multiroot_report(self.result, path)
                self.status.setText("보고서를 저장했습니다. 원본 파일은 변경하지 않았습니다.")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "보고서 저장", str(exc))

    def closeEvent(self, event):
        if self.is_running():
            self._close_pending = True
            self.cancel_analysis()
            event.ignore()
        else:
            super().closeEvent(event)


class MultiRootDialog(QDialog):
    """Compatibility wrapper; the normal application embeds the workspace."""

    def __init__(self, parent=None, scan_options=None):
        super().__init__(parent)
        self.setWindowTitle("폴더 분석·검사·정리")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(1180, 760)
        self._close_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace = MultiRootWorkspace(self, scan_options)
        layout.addWidget(self.workspace)
        self.close_button = QPushButton("닫기")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)
        self.workspace.analysis_finished.connect(self._analysis_finished)

    def __getattr__(self, name):
        workspace = self.__dict__.get("workspace")
        if workspace is not None and hasattr(workspace, name):
            return getattr(workspace, name)
        raise AttributeError(name)

    def _analysis_finished(self):
        if self._close_pending:
            self.close()

    def closeEvent(self, event):
        if self.workspace.is_running():
            self._close_pending = True
            self.workspace.cancel_analysis()
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self):
        if self.workspace.is_running():
            self._close_pending = True
            self.workspace.cancel_analysis()
            return
        super().reject()
