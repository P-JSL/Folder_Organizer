"""Read-only productivity tools and cached display work preserve selection scope."""
from dataclasses import replace
from pathlib import Path
import time

from PySide6.QtCore import Qt

from folder_organizer import ui
from folder_organizer.scanner import scan_folder
from test_ui_v2 import app, window, populate, wait


def test_quick_filters_only_change_display_and_preserve_hidden_checks(window, tmp_path):
    scan = populate(window, tmp_path, {"empty.txt": b"", "large.bin": b"x", "old.txt": b"old", "recent.txt": b"new"})
    scan.files = [replace(entry, size=100 * 1024 ** 2) if entry.name == "large.bin" else
                  replace(entry, modified=time.time() - 366 * 86400) if entry.name == "old.txt" else entry
                  for entry in scan.files]
    window.load_scan_result(scan)
    window.model.set_checked([entry.path for entry in scan.files])
    for key, expected in (("empty", {"empty.txt"}), ("large", {"large.bin"}), ("old", {"old.txt"})):
        window.quick_filter.setCurrentIndex(window.quick_filter.findData(key))
        visible = {window.model.entries[window.proxy.mapToSource(window.proxy.index(row, 0)).row()].name
                   for row in range(window.proxy.rowCount())}
        assert visible == expected
        assert len(window.selected_paths()) == 4 and window._hidden_selected_count() == 3
        assert window.scan_complete and len(window.scan_result.files) == 4
    assert all(Path(entry.path).exists() for entry in scan.files)


def test_name_sort_and_search_do_not_format_dates_or_sizes(window, tmp_path, monkeypatch):
    populate(window, tmp_path)
    def expensive(*_args):
        raise AssertionError("Name keys and search must not format unrelated columns")
    monkeypatch.setattr(ui, "format_date", expensive)
    monkeypatch.setattr(ui, "format_size", expensive)
    for row in range(window.model.rowCount()):
        assert window.model.sort_value(row, 1) == window.model.entries[row].name.casefold()
        assert window.model.data(window.model.index(row, 1)) == window.model.entries[row].name
        window.proxy.query = window.model.entries[row].name.casefold()
        assert window.proxy.filterAcceptsRow(row, window.model.index(-1, -1))


def test_cached_date_display_is_bounded_and_reset_with_new_entries(window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    calls = []
    monkeypatch.setattr(ui, "format_date", lambda value: calls.append(value) or f"date:{value}")
    index = window.model.index(0, 4)
    assert window.model.data(index) == window.model.data(index) and len(calls) == 1
    changed = replace(scan.files[0], modified=scan.files[0].modified + 100)
    window.model.set_entries([changed])
    assert window.model.data(window.model.index(0, 4)) == f"date:{changed.modified}"
    assert len(calls) == 2


def test_recent_folder_menu_reopens_with_fresh_unsorted_inventory(app, window, tmp_path):
    folder = tmp_path / "Documents"
    folder.mkdir()
    (folder / "first.txt").write_text("first", encoding="utf-8")
    window._open_folder(str(folder))
    wait(app, window)
    assert window._recent_folders() == [str(folder)]
    assert not window.scan_result.scan_metrics["sorted_results"]
    (folder / "second.txt").write_text("new after previous scan", encoding="utf-8")
    window.recent_menu.actions()[0].trigger()
    wait(app, window)
    assert {entry.name for entry in window.scan_result.files} == {"first.txt", "second.txt"}
    window._clear_recent_folders()
    assert window._recent_folders() == []
    assert (folder / "first.txt").is_file() and (folder / "second.txt").is_file()


def test_overview_selection_replaces_checks_without_mutating_files(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"one", "b.txt": b"two"})
    originals = {entry.path: Path(entry.path).read_bytes() for entry in scan.files}
    window.model.set_checked([scan.files[0].path])
    window.show_overview()
    wait(app, window)
    dialog = window._overview_dialog
    assert dialog is not None
    dialog.files_requested.emit([scan.files[1].path])
    assert window.selected_paths() == [scan.files[1].path]
    assert window.views.currentIndex() == 0
    assert {path: Path(path).read_bytes() for path in originals} == originals
    dialog.close()
    app.processEvents()
    assert window._overview_dialog is None
