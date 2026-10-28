"""Storage summaries are readable, bounded and return selection without mutation."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QDialog

from folder_organizer import inventory_summary, operations
from folder_organizer.inventory_summary import summarize_inventory
from folder_organizer.models import FileEntry, ScanResult
from folder_organizer.overview_view import InventoryOverviewDialog
from folder_organizer.scanner import scan_folder
from folder_organizer.themes import apply_theme


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def scanned(tmp_path):
    for relative, data in {"Photos/photo.png": b"image" * 50,
                           "Docs/report.txt": b"report" * 10,
                           "Docs/empty.txt": b"", "setup.exe": b"MZ" * 20}.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return scan_folder(tmp_path)


def test_dialog_uses_existing_inventory_without_opening_or_stating_files(app, scanned, monkeypatch):
    originals = {entry.path: Path(entry.path).read_bytes() for entry in scanned.files}
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: pytest.fail("Overview queried filesystem metadata"))
    monkeypatch.setattr(operations, "execute_plan", lambda *_args, **_kwargs: pytest.fail("Overview changed files"))
    dialog = InventoryOverviewDialog(scanned)
    assert dialog.summary["entry_visits"] == len(scanned.files)
    assert dialog.summary["total_bytes"] == sum(map(len, originals.values()))
    assert len(dialog.largest_records) == len(scanned.files)
    assert dialog.tabs.count() == 4
    assert "실제 디스크" in dialog.scope_notice.text()
    dialog.close()
    assert {path: Path(path).read_bytes() for path in originals} == originals


@pytest.mark.parametrize("kind,key", [("folder", "Docs"), ("type", "documents")])
def test_bucket_selection_emits_only_matching_paths_then_returns_to_file_list(app, scanned, kind, key):
    dialog = InventoryOverviewDialog(scanned)
    requested = []
    dialog.files_requested.connect(requested.append)
    records = getattr(dialog, kind + "_records")
    table = getattr(dialog, kind + "_table")
    index = next(index for index, record in enumerate(records) if record["key"] == key)
    table.selectRow(index)
    getattr(dialog, kind + "_select_button").click()
    assert requested == [[entry.path for entry in scanned.files if entry.relative_path.replace("\\", "/").startswith("Docs/")]]
    assert all(isinstance(path, str) for path in requested[0])
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert all(Path(entry.path).is_file() for entry in scanned.files)


def test_largest_files_preserve_user_row_selection_without_mutating_sources(app, scanned):
    dialog = InventoryOverviewDialog(scanned)
    requested = []
    dialog.files_requested.connect(requested.append)
    table = dialog.largest_table
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    table.selectionModel().select(table.model().index(0, 0), flags)
    table.selectionModel().select(table.model().index(2, 0), flags)
    assert dialog.largest_select_button.isEnabled()
    dialog.largest_select_button.click()
    assert requested == [[dialog.largest_records[index]["path"] for index in (0, 2)]]
    assert len(list(Path(scanned.root).rglob("*"))) == 6


def _entry(name, modified):
    return FileEntry(path="/offline/" + name, relative_path=name, name=name, size=10,
                     modified=modified, created=None, created_source="unavailable", kind="file",
                     suffix=".txt", mtime_ns=0, device=2**100, inode=2**100)


def test_date_selection_uses_frozen_summary_reference_at_boundaries(app, monkeypatch):
    reference = 1_700_000_000
    scan = ScanResult("/offline", files=[_entry("recent.txt", reference - 30 * 86400),
                                         _entry("year.txt", reference - 365 * 86400),
                                         _entry("older.txt", reference - 366 * 86400)])
    summary = summarize_inventory(scan.files, scan.root, now=reference)
    monkeypatch.setattr(inventory_summary.time, "time", lambda: reference + 800 * 86400)
    dialog = InventoryOverviewDialog(scan, summary=summary)
    requested = []
    dialog.files_requested.connect(requested.append)
    row = next(index for index, record in enumerate(dialog.date_records) if record["key"] == "older")
    dialog.date_table.selectRow(row)
    dialog.date_select_button.click()
    assert requested == [["/offline/older.txt"]]


def test_empty_file_selection_is_explicit_and_partial_scan_cannot_select(app, scanned):
    dialog = InventoryOverviewDialog(scanned)
    requested = []
    dialog.files_requested.connect(requested.append)
    assert not requested and dialog.empty_select_button.isEnabled()
    dialog.empty_select_button.click()
    assert requested == [[entry.path for entry in scanned.files if entry.size == 0]]
    partial = InventoryOverviewDialog(replace(scanned, cancelled=True))
    partial.files_requested.connect(requested.append)
    partial.folder_table.selectRow(0)
    partial.largest_table.selectRow(0)
    assert not partial.folder_select_button.isEnabled()
    assert not partial.largest_select_button.isEnabled()
    assert not partial.empty_select_button.isEnabled()
    partial._request([scanned.files[0].path])
    assert len(requested) == 1 and "분석이 중단" in partial.scope_notice.text()
    partial.close()


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_standard_window_keeps_all_tab_actions_visible_and_qt_cells_scalar(app, mode):
    scan = ScanResult("/offline", files=[_entry("long-file-" + "a" * 200 + ".txt", 1_700_000_000)])
    dialog = InventoryOverviewDialog(scan)
    apply_theme(dialog, mode)
    dialog.resize(1000, 650)
    dialog.show()
    for tab, table, button in ((0, dialog.folder_table, dialog.folder_select_button),
                               (1, dialog.type_table, dialog.type_select_button),
                               (2, dialog.largest_table, dialog.largest_select_button),
                               (3, dialog.date_table, dialog.date_select_button)):
        dialog.tabs.setCurrentIndex(tab)
        app.processEvents()
        assert dialog.minimumSizeHint().height() <= dialog.height()
        for widget in (table, button):
            origin = widget.mapTo(dialog, widget.rect().topLeft())
            assert origin.y() >= 0 and origin.y() + widget.height() <= dialog.height()
        model = table.model()
        assert model.rowCount(QModelIndex()) < 21
        for row in range(model.rowCount()):
            for column in range(model.columnCount()):
                index = model.index(row, column)
                assert isinstance(model.data(index), str)
                assert model.data(index, Qt.ItemDataRole.UserRole) is None or type(model.data(index, Qt.ItemDataRole.UserRole)) is int
    dialog.close()
    apply_theme(None, "dark")
