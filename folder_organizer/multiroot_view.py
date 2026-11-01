"""A separate, read-only multi-folder analysis window."""
from __future__ import annotations

import os
from itertools import chain, islice
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView, QTabWidget,
    QTreeView, QVBoxLayout, QWidget,
)

from .multiroot import MAX_ROOTS, MAX_WORKERS, MultiScanResult, scan_folders, write_multiroot_report

STATUS_LABELS = {"waiting": "대기", "scanning": "파일 조회 중", "complete": "완료",
                 "partial": "일부 확인 실패", "failed": "접근 실패", "cancelled": "취소됨"}


def _size(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return "0 B"


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

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.paths)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.paths):
            return None
        row, column = index.row(), index.column()
        values = self.status.get(row, {})
        if role == Qt.ItemDataRole.ToolTipRole:
            return values.get("error") or self.paths[row]
        if role == Qt.ItemDataRole.DisplayRole:
            label = f"{values['root_id']} · {self.paths[row]}" if values.get("root_id") else self.paths[row]
            return (label, STATUS_LABELS.get(values.get("state", "waiting"), values.get("state", "대기")),
                    f"{values.get('files', 0):,}", _size(values.get("bytes", 0)),
                    str(values.get("warnings", 0)))[column]
        return None

    def update_row(self, row: int, **values):
        if 0 <= row < len(self.paths):
            self.status.setdefault(row, {}).update(values)
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.headers) - 1))


class _FilesModel(_TableModel):
    headers = ("파일 이름", "소속 폴더", "용량", "전체 경로")

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
            return (entry.name, ", ".join(self.result.memberships[entry.path]), _size(entry.size), entry.path)[index.column()]
        return None

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
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
            return "선택하면 아래에서 전체 파일 경로를 확인할 수 있습니다. 이 결과로 파일을 삭제하거나 이동하지 않습니다."
        if role == Qt.ItemDataRole.DisplayRole:
            return (str(index.row() + 1), str(len(group.paths)), _size(group.size),
                    ", ".join(group.root_ids), group.paths[0])[index.column()]
        return None

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
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


class MultiRootDialog(QDialog):
    """Manage selected folders; global results never enter single-root actions."""

    def __init__(self, parent=None, scan_options: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("여러 폴더 통합 분석 · 읽기 전용")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(1080, 760)
        self._options = {key: value for key, value in (scan_options or {}).items()
                         if key in {"exclude_patterns", "min_size", "max_size"}}
        self._worker: _AnalysisWorker | None = None
        self._active_rows: list[int] = []
        self._close_pending = False
        self.result: MultiScanResult | None = None
        self.root_model = _RootModel(self)
        self.file_model = _FilesModel(self)
        self.group_model = _GroupsModel(self)
        layout = QVBoxLayout(self)
        lead = QLabel("여러 폴더에서 파일을 조회하고 폴더 간 완전 중복을 함께 찾습니다.")
        lead.setWordWrap(True)
        layout.addWidget(lead)
        caution = QLabel("읽기 전용: 이 창에서는 파일을 삭제·이동하지 않습니다. 각 폴더의 기존 작업 기록은 별도로 유지됩니다.")
        caution.setWordWrap(True)
        layout.addWidget(caution)
        inputs = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("폴더 경로를 입력하거나 ‘폴더 추가’를 누르세요. NAS 경로도 입력할 수 있습니다.")
        self.path_input.returnPressed.connect(self._add_input)
        inputs.addWidget(self.path_input, 1)
        self.add_button = QPushButton("폴더 추가…")
        self.add_button.clicked.connect(self._pick_folders)
        inputs.addWidget(self.add_button)
        self.path_button = QPushButton("경로 등록")
        self.path_button.clicked.connect(self._add_input)
        inputs.addWidget(self.path_button)
        self.remove_button = QPushButton("선택 제거")
        self.remove_button.clicked.connect(self._remove_selected)
        inputs.addWidget(self.remove_button)
        layout.addLayout(inputs)
        self.root_table = self._table(self.root_model)
        self.root_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.root_table.setMinimumHeight(150)
        layout.addWidget(self.root_table)
        controls = QHBoxLayout()
        self.recursive = QCheckBox("하위 폴더 포함")
        self.recursive.setChecked((scan_options or {}).get("recursive", True))
        controls.addWidget(self.recursive)
        self.hidden = QCheckBox("숨김 파일 포함")
        self.hidden.setChecked((scan_options or {}).get("include_hidden", False))
        controls.addWidget(self.hidden)
        controls.addWidget(QLabel("동시 탐색 수"))
        self.workers = QSpinBox()
        self.workers.setRange(1, MAX_WORKERS)
        self.workers.setValue(2)
        self.workers.setToolTip("서로 다른 볼륨을 제한된 수로 조회합니다. 같은 볼륨·NAS 서버는 순차 조회하며, 내용 검사는 순차 실행합니다.")
        controls.addWidget(self.workers)
        controls.addStretch(1)
        self.selected_button = QPushButton("선택 폴더 검사")
        self.selected_button.clicked.connect(lambda: self.start_analysis(selected=True))
        controls.addWidget(self.selected_button)
        self.all_button = QPushButton("전체 폴더 검사")
        self.all_button.clicked.connect(self.start_analysis)
        controls.addWidget(self.all_button)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QLabel("폴더를 추가하고 검사할 범위를 선택하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        tabs = QTabWidget()
        tabs.addTab(self._table(self.file_model), "통합 파일 목록")
        groups_page = QWidget()
        group_layout = QVBoxLayout(groups_page)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.addWidget(QLabel("첫 파일은 경로 순서이며 최적 원본을 뜻하지 않습니다. 그룹을 선택해 파일 경로를 확인하세요."))
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.group_table = self._table(self.group_model)
        splitter.addWidget(self.group_table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("중복 그룹의 전체 경로 또는 분석 경고를 표시합니다.")
        splitter.addWidget(self.details)
        group_layout.addWidget(splitter)
        tabs.addTab(groups_page, "폴더 간 완전 중복")
        layout.addWidget(tabs, 1)
        self.group_table.selectionModel().selectionChanged.connect(self._show_group)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.export_button = QPushButton("분석 보고서 저장…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        bottom.addWidget(self.export_button)
        self.close_button = QPushButton("닫기")
        self.close_button.clicked.connect(self.close)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

    def _table(self, model):
        table = QTableView(self)
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
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
            self.root_model.endInsertRows()
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
        rows = sorted({index.row() for index in self.root_table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        self._clear_results()
        self.root_model.beginResetModel()
        self.root_model.paths = [path for index, path in enumerate(self.root_model.paths) if index not in rows]
        self.root_model.status.clear()
        self.root_model.endResetModel()

    def is_running(self) -> bool:
        return self._worker is not None

    def _clear_results(self):
        self.result = None
        self.file_model.set_result(None)
        self.group_model.set_result(None)
        self.export_button.setEnabled(False)
        self.details.clear()

    def _set_busy(self, busy):
        for widget in (self.add_button, self.path_button, self.remove_button, self.path_input,
                       self.recursive, self.hidden, self.workers, self.selected_button, self.all_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.export_button.setEnabled(not busy and self.result is not None)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(int(self.result is not None and self.result.complete))

    def start_analysis(self, checked=False, *, selected=False):
        if self.is_running():
            return
        rows = sorted(index.row() for index in self.root_table.selectionModel().selectedRows()) if selected else list(range(len(self.root_model.paths)))
        if not rows:
            self.status.setText("검사할 폴더를 먼저 선택하세요." if selected else "폴더를 먼저 등록하세요.")
            return
        self._active_rows = rows
        self._clear_results()
        for row in range(len(self.root_model.paths)):
            if row not in rows:
                self.root_model.update_row(row, state="이번 검사 제외", files=0, bytes=0, warnings=0, error=None, root_id=None)
        options = {**self._options, "recursive": self.recursive.isChecked(), "include_hidden": self.hidden.isChecked(),
                   "workers": self.workers.value(), "duplicates": True}
        self._worker = _AnalysisWorker([self.root_model.paths[row] for row in rows], options, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._finished)
        for row in rows:
            self.root_model.update_row(row, state="waiting", files=0, bytes=0, warnings=0, error=None, root_id=None)
        self.status.setText("폴더를 조회합니다. 같은 볼륨·NAS 서버의 조회는 순차 진행합니다.")
        self._set_busy(True)
        self._worker.start()

    def _on_progress(self, event):
        root_id = event.get("root_id")
        if root_id:
            row = next((row for row in self._active_rows if self.root_model.paths[row] == event.get("requested_path")), None)
            if row is not None:
                self.root_model.update_row(row, state=event.get("state", "scanning"), root_id=root_id,
                    files=event.get("completed", 0), bytes=event.get("bytes", 0), warnings=event.get("warnings", 0))
        message = event.get("message", "")
        self.status.setText(f"{root_id}: {message}" if root_id else f"전체 폴더 내용 비교: {message}")

    def _finished(self):
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        if worker.result is not None:
            self.result = result = worker.result
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
            self.status.setText(f"{final} · 고유 경로 {len(result.files):,}개 · 완전 중복 {len(result.groups):,}그룹 · {result.elapsed_seconds:.2f}초 · 읽기 전용")
            warnings = chain(result.notes, (f"{root.root_id}: {root.error}" for root in result.roots if root.error),
                (f"{root.root_id}: {warning}" for root in result.roots if root.scan for warning in root.scan.warnings),
                (f"{warning.get('phase')}: {warning.get('message')}" for warning in result.duplicate_analysis.get("warnings", [])))
            self.details.setPlainText("\n".join(islice(warnings, 1000)) or "분석한 범위의 결과입니다. 이 창에서는 삭제·이동하지 않습니다.")
        elif worker.error:
            self.status.setText("분석 실패: " + worker.error)
        worker.deleteLater()
        self._set_busy(False)
        if self._close_pending:
            self.close()

    def _show_group(self):
        if self.result is None:
            return
        rows = self.group_table.selectionModel().selectedRows()
        if not rows:
            return
        group = self.result.groups[rows[0].row()]
        paths = group.paths[:1000]
        tail = f"\n나머지 {len(group.paths) - len(paths):,}개 경로는 저장한 보고서에서 확인하세요." if len(paths) < len(group.paths) else ""
        self.details.setPlainText(f"완전 중복 {len(group.paths):,}개 · 파일당 {_size(group.size)} · 읽기 전용\nSHA-256: {group.sha256}\n\n" + "\n".join(paths) + tail)

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
            # QDialog's implementation emits rejected/finished. Merely accepting
            # the QWidget event would hide the window without notifying the
            # parent, preventing its menu entry or deferred close from resuming.
            super().closeEvent(event)
