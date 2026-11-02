"""Embedded, read-only multi-folder workspace and a compatibility dialog."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from itertools import chain, islice
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView, QTabWidget,
    QTreeView, QVBoxLayout, QWidget,
)

from .scanner import normalize_exclude_patterns
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: MultiScanResult | None = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.result is None else len(self.result.files)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.result is None or not 0 <= index.row() < len(self.result.files):
            return None
        entry = self.result.files[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.path
        if role == Qt.ItemDataRole.DisplayRole:
            return (entry.name, "응용 프로그램" if entry.kind == "application" else entry.suffix or "파일",
                    _size(entry.size), _date(entry.modified),
                    ", ".join(self.root_labels.get(root, root) for root in self.result.memberships[entry.path]))[index.column()]
        return None

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
        self.root_labels = {root.root_id: Path(root.path or root.requested_path).name or root.requested_path
                            for root in result.roots} if result else {}
        self.endResetModel()


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
            return "선택하면 오른쪽에서 전체 파일 경로를 확인할 수 있습니다. 이 결과로 파일을 삭제하거나 이동하지 않습니다."
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
        self.result: MultiScanResult | None = None
        self.error: str | None = None

    def run(self):
        try:
            self.result = scan_folders(self.paths, **self.options, cancel=self.cancel_event,
                                       progress_detail=self.progress.emit)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class MultiRootWorkspace(QWidget):
    """Virtual main-window inventory; its results never authorize file changes."""

    organize_requested = Signal(str)
    analysis_started = Signal()
    analysis_finished = Signal()
    paths_changed = Signal()

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
        self.root_model = _RootModel(self)
        self.file_model = _FilesModel(self)
        self.group_model = _GroupsModel(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("여러 폴더를 한눈에")
        title.setObjectName("section")
        heading.addWidget(title)
        label = QLabel("통합 분석 · 읽기 전용")
        label.setObjectName("muted")
        heading.addWidget(label)
        heading.addStretch()
        self.add_button = QPushButton("폴더 추가…")
        self.add_button.clicked.connect(self._pick_folders)
        heading.addWidget(self.add_button)
        self.all_button = QPushButton("분석 시작")
        self.all_button.setObjectName("primary")
        self.all_button.clicked.connect(self.start_analysis)
        heading.addWidget(self.all_button)
        self.cancel_button = QPushButton("중지")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        heading.addWidget(self.cancel_button)
        layout.addLayout(heading)

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
        self.organize_button = QPushButton("이 폴더에서 정리")
        self.organize_button.setEnabled(False)
        self.organize_button.setToolTip("선택한 폴더 하나를 별도로 분석한 뒤 기존 정리·복원 기능을 사용합니다.")
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
        self.file_table = self._table(self.file_model)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.file_table.setColumnWidth(0, 215)
        self.file_table.setColumnWidth(1, 90)
        self.file_table.setColumnWidth(2, 95)
        self.file_table.setColumnWidth(3, 155)
        self.tabs.addTab(self.file_table, "전체 파일")
        self.group_table = self._table(self.group_model)
        self.group_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tabs.addTab(self.group_table, "완전 중복")
        center_layout.addWidget(self.tabs, 1)
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
        self.export_button = QPushButton("보고서 저장…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        right_layout.addWidget(self.export_button)
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
        self.status = QLabel("폴더를 추가하고 분석을 시작하세요. 통합 결과는 읽기 전용입니다.")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.group_table.selectionModel().selectionChanged.connect(self._show_group)
        self.file_table.selectionModel().selectionChanged.connect(self._show_file)
        self.root_table.selectionModel().selectionChanged.connect(self._root_selected)
        self.root_model.dataChanged.connect(self._checked_changed)
        self.tabs.currentChanged.connect(lambda index: self._show_file() if index == 0 else self._show_group())

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
            self.organize_requested.emit(self.root_model.paths[rows[0].row()])

    def selected_snapshot(self):
        if self.result is None:
            return None
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return None
        entry = self.result.files[rows[0].row()]
        return {"name": entry.name, "path": entry.path, "size": entry.size,
                "modified": entry.modified, "created": entry.created, "kind": entry.kind}

    def _show_file(self, *_args):
        if self.result is None:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if rows:
            self._inspect_entry(self.result.files[rows[0].row()])

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
        self.result = None
        self._entries_by_path = {}
        self.file_model.set_result(None)
        self.group_model.set_result(None)
        self.export_button.setEnabled(False)
        self.details.clear()
        self.inspector.clear()
        self.summary.setText("폴더 추가 → 분석 시작 → 결과 확인")
        self.empty_hint.show()

    def _set_busy(self, busy):
        for widget in (self.add_button, self.path_button, self.remove_button, self.path_input,
                       self.recursive, self.hidden, self.workers, self.exclusions, self.minimum,
                       self.root_table, self.selected_button, self.all_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.organize_button.setEnabled(not busy and bool(self.root_table.selectionModel().selectedRows()))
        self.export_button.setEnabled(not busy and self.result is not None)
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
        self.status.setText(f"{label}: {message}" if root_id else f"전체 폴더 내용 비교: {message}")
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
        if worker.result is not None:
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
            self.status.setText(f"{final} · 고유 경로 {len(result.files):,}개 · 완전 중복 {len(result.groups):,}그룹 · {result.elapsed_seconds:.2f}초 · 읽기 전용")
            warnings = chain(result.notes, (f"{root.root_id}: {root.error}" for root in result.roots if root.error),
                (f"{root.root_id}: {warning}" for root in result.roots if root.scan for warning in root.scan.warnings),
                (f"{warning.get('phase')}: {warning.get('message')}" for warning in result.duplicate_analysis.get("warnings", [])))
            self.details.setPlainText("\n".join(islice(warnings, 1000)) or "분석한 범위의 결과입니다. 이 창에서는 삭제·이동하지 않습니다.")
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
        self.details.setPlainText(f"완전 중복 {len(group.paths):,}개 · 파일당 {_size(group.size)} · 읽기 전용\n첫 파일은 경로순이며 최적 원본을 뜻하지 않습니다.\n정리는 왼쪽의 ‘이 폴더에서 정리’를 사용하세요.\nSHA-256: {group.sha256}\n\n" + "\n".join(paths) + tail)

    def cancel_analysis(self):
        if self._worker is not None:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("취소 요청 중입니다. 진행 중인 파일 읽기가 끝나면 부분 상태를 표시합니다.")

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
        self.setWindowTitle("여러 폴더 통합 분석 · 읽기 전용")
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
