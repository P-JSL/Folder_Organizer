"""Header navigation keeps file mutations behind explicit previews."""
from pathlib import Path
import time
import threading
from types import SimpleNamespace

import pytest

from PySide6.QtWidgets import QFrame, QPushButton, QFileDialog, QMessageBox

from folder_organizer import ui
from folder_organizer.execution_log import configure_execution_logging, read_execution_logs, shutdown_execution_logging
from folder_organizer.matching import find_duplicates
from test_ui_v2 import app, window, populate, wait


def test_header_replaces_sidebar_and_keeps_main_actions_focused(app, window, tmp_path):
    window.show()
    app.processEvents()
    assert window.header_corner.isVisible() and window.help_button.isVisible()
    assert not any(frame.objectName() == "sidebar" for frame in window.findChildren(QFrame))
    assert len([button for button in window.findChildren(QPushButton) if button.isVisible()]) <= 3
    assert not window.merge_action.isEnabled()
    scan = populate(window, tmp_path, {"a.txt": b"same", "b.txt": b"same"})
    assert window.classify_button in window.organize_menu.actions()
    assert window.classify_button.isEnabled() and window.duplicates_button.isEnabled()
    assert not window.selection_panel.isVisible()
    window.model.set_checked([entry.path for entry in scan.files])
    assert window.selection_panel.isVisible() and window.merge_action.isEnabled()
    window.clear_selection()
    assert not window.selection_panel.isVisible() and not window.merge_action.isEnabled()


def test_theme_menu_persists_choice_and_updates_open_help(app, window):
    assert window.theme_mode == "dark" and window.theme_actions["dark"].isChecked()
    window.show_help()
    dialog = window._help_dialog
    window.theme_actions["light"].trigger()
    app.processEvents()
    assert window.theme_mode == "light" and window.theme_actions["light"].isChecked()
    assert window.settings.value("appearance/theme") == "light"
    assert "#17243a" in dialog.browser.toHtml()
    reopened = ui.MainWindow()
    for timer in (reopened.update_timer, reopened.initial_update_timer, reopened.deferred_update_timer):
        timer.stop()
    try:
        assert reopened.theme_mode == "light"
    finally:
        reopened.close()


def test_execution_log_is_read_only_reused_and_closed_with_window(app, window, tmp_path):
    configure_execution_logging(tmp_path / "logs")
    try:
        window.show()
        window.show_execution_logs()
        app.processEvents()
        dialog = window._execution_log_dialog
        window.show_execution_logs()
        assert window._execution_log_dialog is dialog and dialog.isVisible()
        assert read_execution_logs() == [] and window._worker is None
        window.close()
        app.processEvents()
        assert window._execution_log_dialog is None
    finally:
        shutdown_execution_logging()


def test_group_merge_keeps_originals_and_runs_only_after_confirmed_preview(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path, {"report.txt": b"Shared\n\nFirst", "report copy.txt": b"Shared\n\nSecond"})
    # Selecting a comparison group is independent from the manual checkboxes.
    from folder_organizer.models import MatchGroup
    paths = [str(Path(scan.root) / name) for name in ("report.txt", "report copy.txt")]
    window._load_groups([MatchGroup("similar_text", paths, "review", .9)], "similar")
    window.merge_current_group()
    dialog = window._merge_dialog
    assert dialog is not None and not dialog.move_originals.isChecked()
    dialog._preview()
    deadline = time.monotonic() + 10
    while dialog._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert dialog.plan and not Path(dialog.plan["output"]).exists()
    assert all(Path(path).exists() for path in paths) and not window.model.checked
    output = Path(dialog.plan["output"])
    results = []
    monkeypatch.setattr(window, "_show_details", lambda _title, result: results.append(result))
    dialog.confirm.setChecked(True)
    dialog._submit()
    app.processEvents()
    wait(app, window)
    assert results[0]["status"] == "completed"
    assert output.read_text(encoding="utf-8") == "Shared\n\nFirst\n\nSecond\n"
    assert all(Path(path).exists() for path in paths) and window.scan_complete
    assert window._merge_dialog is None


def test_keep_first_removal_is_enabled_only_for_complete_exact_results(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"same", "b.txt": b"same"})
    window.show()
    groups = find_duplicates(scan.files)
    window._load_groups(groups, "duplicate")
    app.processEvents()
    assert window.keep_first_delete_button.isVisible() and window.keep_first_hint.isVisible()
    assert window.keep_first_delete_action.isEnabled()
    window.set_busy(True)
    assert not window.keep_first_delete_action.isEnabled()
    window.set_busy(False)
    window._load_groups([], "similar")
    assert not window.keep_first_delete_action.isEnabled()
    assert not window.keep_first_delete_button.isVisible()
    assert all(Path(entry.path).exists() for entry in scan.files)


@pytest.mark.parametrize("outcome,overall", [("repaired", "completed"), ("partial", "partial"), ("unsupported", "failed")])
def test_recovery_execution_log_keeps_partial_results_and_readable_errors(window, tmp_path, monkeypatch, outcome, overall):
    scan = populate(window, tmp_path, {"a.png": b"fixture"})
    window.model.set_checked([entry.path for entry in scan.files])
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path / "Recovered"))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(ui, "repair_file", lambda *_args: {"status": outcome, "output": "copy.png", "details": ["읽지 못한 항목", "원본 유지"]})
    receipts = []
    monkeypatch.setattr(ui, "record_execution", lambda event, **kwargs: receipts.append((event, kwargs)))
    monkeypatch.setattr(window, "_start_task", lambda _label, task, _done: task(SimpleNamespace(report=lambda *_args: None)))
    window.repair_selected()
    assert [event for event, _record in receipts] == ["execution.started", "execution.item", "execution.finished"]
    assert receipts[-1][1]["status"] == overall
    assert receipts[1][1]["source"] == scan.files[0].path
    if outcome != "repaired":
        assert receipts[1][1]["error"] == "읽지 못한 항목; 원본 유지"
    assert Path(scan.files[0].path).read_bytes() == b"fixture"


def test_main_close_waits_for_merge_preview_without_destroying_thread(app, window, tmp_path, monkeypatch):
    from folder_organizer import merge_view
    scan = populate(window, tmp_path, {"a.txt": b"Shared\n\nFirst", "b.txt": b"Shared\n\nSecond"})
    original_preview = merge_view.build_merge_preview
    release = threading.Event()
    def delayed(*args, **kwargs):
        release.wait(3)
        return original_preview(*args, **kwargs)
    monkeypatch.setattr(merge_view, "build_merge_preview", delayed)
    window.show()
    window.show_merge([entry.path for entry in scan.files])
    dialog = window._merge_dialog
    dialog._preview()
    try:
        window.close()
        app.processEvents()
        assert window.isVisible() and window._close_requested
        assert dialog._worker is not None and not dialog.isEnabled()
    finally:
        release.set()
        deadline = time.monotonic() + 10
        while window.isVisible() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
    assert not window.isVisible() and window._merge_dialog is None
    assert all(Path(entry.path).exists() for entry in scan.files)


def test_exit_waits_for_confirmed_merge_queued_after_dialog_callback(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"Shared\n\nFirst", "b.txt": b"Shared\n\nSecond"})
    window.show()
    window.show_merge([entry.path for entry in scan.files])
    dialog = window._merge_dialog
    dialog._preview()
    deadline = time.monotonic() + 10
    while dialog._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    output = Path(dialog.plan["output"])
    window._cancellable = True  # A completed inventory job used cancellation.
    dialog.confirm.setChecked(True)
    dialog._submit()
    assert window._merge_execute_pending
    window.close()  # Before the zero-delay worker callback has run.
    assert window.isVisible() and not output.exists()
    deadline = time.monotonic() + 10
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert not window.isVisible() and output.is_file()
    assert all(Path(entry.path).exists() for entry in scan.files)
