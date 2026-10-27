"""UI checks for history restoration, measured progress and safe classification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QMessageBox

from folder_organizer import ui
from folder_organizer.matching import find_duplicates
from folder_organizer.operations import build_plan, execute_plan
from folder_organizer.scanner import scan_folder


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(ui, "QSettings", lambda *_args: settings)
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: False)
    widget = ui.MainWindow()
    widget.update_timer.stop()
    widget.initial_update_timer.stop()
    yield widget
    if widget._worker is not None:
        widget._worker.cancel_event.set()
        widget._worker.wait(5000)
        app.processEvents()
    for child in (widget._history_dialog, widget._log_dialog):
        if child is not None:
            child.reject()
    widget._pending_update = None
    widget.close()
    widget.deleteLater()
    app.processEvents()


def wait(app, window):
    deadline = time.monotonic() + 5
    while window._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.003)
    app.processEvents()
    assert window._worker is None


def populate(window, tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_bytes(b"The same original contents")
    result = scan_folder(root)
    window.load_scan_result(result)
    return result


def test_history_uses_modeless_scalar_model_for_large_file_ids(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    journal = execute_plan(build_plan(scan.root, [scan.files[0].path], "quarantine"))
    journal["items"][0]["inode"] = 2 ** 100
    monkeypatch.setattr(QDialog, "exec", lambda _self: pytest.fail("history must not nest exec"))
    window._show_history([journal])
    app.processEvents()
    dialog = window._history_dialog
    assert dialog is not None and dialog.isVisible()
    assert dialog.model.rowCount() == 1
    assert isinstance(dialog.model.data(dialog.model.index(0, 3)), str)
    assert dialog.model.data(dialog.model.index(0, 0), Qt.ItemDataRole.UserRole) is None
    dialog.reject()
    app.processEvents()
    assert window._history_dialog is None


def test_history_restore_starts_after_dialog_closes(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    journal = execute_plan(build_plan(scan.root, [scan.files[0].path], "quarantine"))
    called, results = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(ui, "undo_operation", lambda root, identifier, **_kwargs: called.append((root, identifier)) or journal)
    monkeypatch.setattr(window, "_operation_done", results.append)
    window._show_history([journal])
    dialog = window._history_dialog
    dialog.table.selectRow(0)
    app.processEvents()
    dialog.undo_button.click()
    assert window._worker is None
    assert window._history_dialog is None
    app.processEvents()
    wait(app, window)
    assert called == [(scan.root, journal["id"])] and results


def test_numeric_phase_progress_and_unknown_discovery_are_distinct(app, window):
    release = threading.Event()
    window._start_task("검사", lambda _worker: release.wait(3), lambda _result: None)
    try:
        window._on_progress_detail({"phase": "duplicate_hash", "completed": 2, "total": 4, "unit": "files", "message": "sample.txt"})
        assert window.progress_bar.value() == 500
        assert "전체 내용 검증" in window.progress_bar.format()
        assert "2 / 4" in window.progress_bar.format()
        assert "50%" in window.progress_bar.format()
        window._on_progress_detail({"phase": "scan", "completed": 7, "total": None, "unit": "files"})
        assert window.progress_bar.minimum() == window.progress_bar.maximum() == 0
        assert "7 파일 확인" in window.progress_bar.format()
        assert "%" not in window.progress_bar.format()
        assert "폴더 탐색" in window.progress_time_label.text()
        assert "7 파일 확인" in window.progress_time_label.text()
    finally:
        release.set()
        wait(app, window)


def test_task_errors_are_logged_with_operation_id(app, window, monkeypatch):
    events, warnings = [], []
    monkeypatch.setattr(ui, "log_event", lambda event, message="", **kwargs: events.append((event, message, kwargs)))
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(True))
    def fail(_worker):
        raise OSError("sample failure")
    window._start_task("검증", fail, lambda _result: None)
    wait(app, window)
    assert [event[0] for event in events] == ["task.started", "task.failed"]
    assert events[0][2]["operation_id"] == events[1][2]["operation_id"]
    assert events[1][2]["level"] == "ERROR" and warnings


def test_keep_first_delete_only_offers_preview_for_exact_results(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    assert not window.keep_first_delete_button.isEnabled()
    groups = find_duplicates(scan.files)
    window._load_groups(groups, "similar")
    assert not window.keep_first_delete_button.isEnabled()
    window._load_groups(groups, "duplicate")
    assert window.keep_first_delete_button.isEnabled()
    previews = []
    monkeypatch.setattr(window, "_show_preview", lambda plan, **kwargs: previews.append((plan, kwargs)))
    window.keep_first_delete()
    wait(app, window)
    plan, options = previews[0]
    assert options == {"require_confirmation": True, "first_only": True}
    assert len(plan["items"]) == 2
    assert groups[0].paths[0] not in {item["source"] for item in plan["items"]}
    assert all(Path(entry.path).exists() for entry in scan.files)
    assert not (Path(scan.root) / ".folder-organizer").exists()


def test_classification_cannot_execute_without_explicit_path_confirmation(window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    plan = ui.build_classification_plan(scan, ui.default_rules("type"))
    calls = []
    monkeypatch.setattr(QDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ui, "execute_plan", lambda *_args, **_kwargs: calls.append(True))
    window._show_preview(plan, require_confirmation=True)
    assert not calls
    assert all(Path(entry.path).exists() for entry in scan.files)


def test_rule_editor_values_persist_and_only_create_preview(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    rules = ui.default_rules("date")
    class Editor:
        values = {"rules": rules, "action": "copy", "destination": None}
        def __init__(self, *_args):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui, "ClassificationDialog", Editor)
    previews = []
    monkeypatch.setattr(window, "_show_preview", lambda plan, **kwargs: previews.append((plan, kwargs)))
    window.show_classification()
    wait(app, window)
    assert json.loads(window.settings.value("classification/rules")) == rules
    assert previews[0][1]["require_confirmation"]
    assert all(item["action"] == "copy" for item in previews[0][0]["items"])
    assert all(Path(entry.path).exists() for entry in scan.files)


def test_busy_compact_window_keeps_progress_and_actions_inside(app, window, tmp_path):
    scan = populate(window, tmp_path)
    window._load_groups(find_duplicates(scan.files), "duplicate")
    window.resize(1306, 700)
    window.show()
    release = threading.Event()
    window._start_task("분석", lambda _worker: release.wait(3), lambda _result: None)
    try:
        window._on_progress_detail({"phase": "scan", "completed": 17000, "total": None, "unit": "files"})
        app.processEvents()
        assert window.minimumSizeHint().height() <= window.height()
        for control in (window.keep_first_delete_button, window.progress_bar, window.progress_time_label, window.menuBar()):
            origin = control.mapTo(window, control.rect().topLeft())
            assert 0 <= origin.x() and origin.x() + control.width() <= window.width()
            assert 0 <= origin.y() and origin.y() + control.height() <= window.height()
        assert window.classify_button in window.organize_menu.actions()
        assert not window.classify_button.isEnabled()
    finally:
        release.set()
        wait(app, window)


@pytest.mark.parametrize("status,level", [("failed", "ERROR"), ("partial", "WARNING"), ("partial_undo", "WARNING"), ("completed", "INFO")])
def test_operation_status_controls_log_level(window, monkeypatch, status, level):
    events = []
    monkeypatch.setattr(ui, "log_event", lambda event, message="", **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(window, "_show_details", lambda *_args: None)
    monkeypatch.setattr(window, "start_scan", lambda: None)
    window._operation_done({"id": "operation-id", "status": status, "items": []})
    assert events == [("operation.result", {"operation_id": "operation-id", "level": level, "details": {"status": status, "items": 0}})]
