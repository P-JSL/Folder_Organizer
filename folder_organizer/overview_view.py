"""Read-only inventory overview; selecting a bucket only returns file paths.

The scan is the sole data source. This dialog never stats, opens, deletes or
moves its files. Complete FileEntry objects stay on the Python side; Qt models
expose only display strings and bounded percentage integers.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QStyle, QStyledItemDelegate,
    QStyleOptionProgressBar, QTableView, QTabWidget, QVBoxLayout, QWidget,
)

from .inventory_summary import inventory_category, inventory_folder, summarize_inventory


def _size(value: int) -> str:
    value = max(0, value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return "0 B"


def _date(value) -> str:
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError, TypeError):
        return "날짜 확인 불가"


class OverviewTableModel(QAbstractTableModel):
    """Bounded flat cells; no nested data or filesystem identifiers in QVariant."""
    def __init__(self, headers, rows, parent=None, *, shares=None):
        super().__init__(parent)
        self.headers, self.rows = tuple(headers), list(rows)
        self.shares = list(shares or [])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section] if 0 <= section < len(self.headers) else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows) or not 0 <= index.column() < len(self.headers):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return str(self.rows[index.row()][index.column()])
        if role == Qt.ItemDataRole.UserRole and self.shares and index.column() == len(self.headers) - 1:
            return min(1000, max(0, int(self.shares[index.row()] * 1000)))
        return None


class ShareDelegate(QStyledItemDelegate):
    """Native palette-aware bars work in both themes without creating widgets."""
    def paint(self, painter, option, index):
        value = index.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return super().paint(painter, option, index)
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(6, 7, -6, -7)
        bar.palette = option.palette
        bar.state = option.state
        bar.minimum, bar.maximum, bar.progress = 0, 1000, value
        bar.text, bar.textVisible = index.data(), True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter, option.widget)


class InventoryOverviewDialog(QDialog):
    """Show existing scan metadata and request checkbox selection explicitly."""
    files_requested = Signal(object)

    def __init__(self, scan_result, parent=None, *, summary=None):
        super().__init__(parent)
        self.setWindowTitle("용량·정리 요약 · 파일을 바꾸지 않고 살펴보기")
        self.resize(1040, 700)
        self.scan_result = scan_result
        self.entries = scan_result.files
        self.summary = summarize_inventory(self.entries, scan_result.root, top_limit=20) if summary is None else summary
        self._complete = not scan_result.cancelled
        outer = QVBoxLayout(self)
        title = QLabel("어디부터 정리하면 좋을까요?")
        title.setObjectName("section")
        outer.addWidget(title)
        intro = QLabel("1. 용량이 큰 곳 확인  →  2. 목록에서 파일 체크  →  3. 정리 미리보기에서 결정\n"
                       "이미 분석한 목록만 사용합니다. 요약을 보거나 체크하는 동안 파일은 변경되지 않습니다.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        folder = QLabel(str(scan_result.root))
        folder.setTextFormat(Qt.TextFormat.PlainText)
        folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        folder.setWordWrap(True)
        outer.addWidget(folder)
        cards = QHBoxLayout()
        for label, value in (("분석 범위의 파일", f"{self.summary['total_files']:,}개"),
                             ("파일 크기 합계", _size(self.summary["total_bytes"])),
                             ("내용이 비어 있는 파일", f"{self.summary['zero_byte_files']:,}개")):
            card = QFrame()
            card.setObjectName("card")
            box = QVBoxLayout(card)
            caption, number = QLabel(label), QLabel(value)
            caption.setObjectName("muted")
            number.setObjectName("cardValue")
            box.addWidget(caption)
            box.addWidget(number)
            cards.addWidget(card, 1)
        outer.addLayout(cards)
        self.scope_notice = QLabel("파일 크기의 합계이며 실제 디스크 점유 공간·여유 공간과 다를 수 있습니다. 검사에서 제외한 항목은 포함하지 않습니다.")
        self.scope_notice.setObjectName("muted")
        self.scope_notice.setWordWrap(True)
        outer.addWidget(self.scope_notice)
        if not self._complete:
            self.scope_notice.setText("분석이 중단되어 일부 파일만 표시합니다. 폴더를 다시 분석한 뒤 목록에서 체크할 수 있습니다.")
            self.scope_notice.setObjectName("warning")
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        self.folder_records = self.summary["by_folder"]
        self.folder_table, self.folder_select_button = self._bucket_page(
            "폴더별 용량", "선택 폴더 바로 아래 폴더별 합계입니다. 하위 폴더의 파일도 합산합니다.",
            self.folder_records, "폴더", self._select_folder)
        if self.summary.get("folders_truncated"):
            others = self.summary["folder_others"]
            note = QLabel(f"큰 폴더 {len(self.folder_records):,}개를 표시합니다. 나머지 {others['folders']:,}개 폴더 · {_size(others['bytes'])} · {others['count']:,}개 파일은 전체 합계에 포함됩니다.")
            note.setObjectName("muted")
            note.setWordWrap(True)
            self.tabs.widget(0).layout().addWidget(note)

        self.type_records = self.summary["by_type"]
        self.type_table, self.type_select_button = self._bucket_page(
            "종류별 용량", "확장자를 기준으로 나눕니다. 파일 내용이나 응용 프로그램의 필요성을 판단하지 않습니다.",
            self.type_records, "종류", self._select_type)

        large_page = QWidget()
        large_layout = QVBoxLayout(large_page)
        large_note = QLabel("큰 파일부터 확인하면 정리할 위치를 찾기 쉽습니다. 크기가 크다고 불필요한 파일은 아닙니다.\n"
                            f"현재 분석 범위에서 큰 파일 {len(self.summary['largest_files']):,}개까지 표시합니다. Ctrl·Shift로 여러 행을 고를 수 있습니다.")
        large_note.setObjectName("muted")
        large_note.setWordWrap(True)
        large_layout.addWidget(large_note)
        self.largest_records = self.summary["largest_files"]
        large_rows = [(item["relative_path"], _size(item["size"]), _date(item["modified"])) for item in self.largest_records]
        self.largest_table = self._table(("파일 · 상대 경로", "크기", "마지막 수정"), large_rows)
        self.largest_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.largest_table.setColumnWidth(1, 130)
        self.largest_table.setColumnWidth(2, 170)
        large_layout.addWidget(self.largest_table, 1)
        self.largest_select_button = QPushButton("고른 파일을 목록에서 체크")
        self.largest_select_button.clicked.connect(self._select_largest)
        self.largest_table.selectionModel().selectionChanged.connect(self._largest_selection_changed)
        self.largest_select_button.setEnabled(False)
        large_layout.addWidget(self.largest_select_button)
        self.tabs.addTab(large_page, "큰 파일부터")

        self.date_records = self.summary["date_buckets"]
        self.date_table, self.date_select_button = self._bucket_page(
            "수정 시기·빈 파일", "마지막 수정 시기를 기준으로 나눕니다. 마지막으로 열어 본 시기나 파일의 중요도를 뜻하지 않습니다.",
            self.date_records, "마지막 수정", self._select_date)
        empty_note = QLabel("0바이트 파일도 프로그램의 표시·설정 용도로 필요할 수 있습니다. 자동으로 삭제하지 않습니다.")
        empty_note.setWordWrap(True)
        empty_note.setObjectName("muted")
        self.tabs.widget(3).layout().addWidget(empty_note)
        self.empty_select_button = QPushButton(f"0바이트 파일 {self.summary['zero_byte_files']:,}개를 목록에서 체크")
        self.empty_select_button.setEnabled(self._complete and self.summary["zero_byte_files"] > 0)
        self.empty_select_button.clicked.connect(lambda: self._request([entry.path for entry in self.entries if entry.size == 0]))
        self.tabs.widget(3).layout().addWidget(self.empty_select_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _table(self, headers, rows, *, shares=None):
        table = QTableView()
        table.setModel(OverviewTableModel(headers, rows, table, shares=shares))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(35)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(headers)):
            table.setColumnWidth(column, 145)
        if shares is not None:
            table.setItemDelegateForColumn(len(headers) - 1, ShareDelegate(table))
        return table

    def _bucket_page(self, title, text, records, column, callback):
        page, layout = QWidget(), QVBoxLayout()
        page.setLayout(layout)
        note = QLabel(text)
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        total = self.summary["total_bytes"]
        shares = [record["bytes"] / total if total else 0 for record in records]
        rows = [(record["label"], f"{record['count']:,}개", _size(record["bytes"]), f"{share:.1%}")
                for record, share in zip(records, shares)]
        table = self._table((column, "파일 수", "크기 합계", "전체 크기 중 비중"), rows, shares=shares)
        layout.addWidget(table, 1)
        button = QPushButton("선택한 분류의 파일을 목록에서 체크")
        button.setEnabled(False)
        button.clicked.connect(callback)

        def current_changed(current, _previous):
            record = records[current.row()] if current.isValid() and 0 <= current.row() < len(records) else None
            count = record["count"] if record else 0
            button.setText(f"선택한 분류의 파일 {count:,}개를 목록에서 체크" if record else "선택한 분류의 파일을 목록에서 체크")
            button.setEnabled(self._complete and count > 0)

        table.selectionModel().currentRowChanged.connect(current_changed)
        layout.addWidget(button)
        self.tabs.addTab(page, title)
        return table, button

    def _selected_record(self, table, records):
        index = table.currentIndex()
        return records[index.row()] if index.isValid() and 0 <= index.row() < len(records) else None

    def _select_folder(self):
        record = self._selected_record(self.folder_table, self.folder_records)
        if record:
            self._request([entry.path for entry in self.entries if inventory_folder(entry, self.scan_result.root) == record["key"]])

    def _select_type(self):
        record = self._selected_record(self.type_table, self.type_records)
        if record:
            self._request([entry.path for entry in self.entries if inventory_category(entry) == record["key"]])

    def _largest_selection_changed(self, *_args):
        count = len(self.largest_table.selectionModel().selectedRows())
        self.largest_select_button.setText(f"고른 파일 {count:,}개를 목록에서 체크" if count else "고른 파일을 목록에서 체크")
        self.largest_select_button.setEnabled(self._complete and count > 0)

    def _select_largest(self):
        rows = sorted(index.row() for index in self.largest_table.selectionModel().selectedRows())
        self._request([self.largest_records[row]["path"] for row in rows if 0 <= row < len(self.largest_records)])

    def _select_date(self):
        record = self._selected_record(self.date_table, self.date_records)
        if not record:
            return
        # The summary records include the fixed reference used for age buckets.
        # Reuse its classifier instead of letting clock movement change selection.
        from .inventory_summary import inventory_date_bucket
        now = self.summary.get("reference_time")
        self._request([entry.path for entry in self.entries if inventory_date_bucket(entry, now=now) == record["key"]])

    def _request(self, paths):
        if self._complete and paths:
            self.files_requested.emit(list(paths))
            self.accept()
