"""Offscreen integration checks for selection, previews, workers and updates."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/folder-organizer-qt-cache")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from folder_organizer import ui
from folder_organizer.models import MatchGroup
from folder_organizer.scanner import scan_folder


@pytest.fixture(scope="session")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
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
    if widget._update_worker is not None:
        widget._update_worker.cancel_event.set()
        widget._update_worker.wait(5000)
        app.processEvents()
    widget.deferred_update_timer.stop()
    widget._pending_update = None
    widget.close()
    widget.deleteLater()
    app.processEvents()


def wait_for_task(app, window, timeout=5):
    deadline = time.monotonic() + timeout
    while window._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert window._worker is None


def populate(window, tmp_path):
    (tmp_path / "small.txt").write_text("x", encoding="utf-8")
    (tmp_path / "large.txt").write_text("a" * 200, encoding="utf-8")
    (tmp_path / "installer.exe").write_bytes(b"MZ" + b"x" * 20)
    result = scan_folder(tmp_path)
    # The settings fixture writes lazily, so exclude it from the product fixture.
    result.files = [entry for entry in result.files if entry.name != "settings.ini"]
    window.load_scan_result(result)
    return result


def test_numeric_sort_filter_and_creation_display(window, tmp_path):
    result = populate(window, tmp_path)
    window.proxy.sort(3, Qt.SortOrder.AscendingOrder)
    sorted_sizes = [window.model.entries[window.proxy.mapToSource(window.proxy.index(i, 0)).row()].size
                    for i in range(window.proxy.rowCount())]
    assert sorted_sizes == [1, 22, 200]
    window.search_input.setText("large")
    assert window.proxy.rowCount() == 1
    window.search_input.clear()
    window.type_filter.setCurrentIndex(window.type_filter.findData("application"))
    assert window.proxy.rowCount() == 1
    item = next(entry for entry in result.files if entry.name == "small.txt")
    row = window.model.path_rows[item.path]
    if item.created is None:
        assert window.model.data(window.model.index(row, 5)) == "제공되지 않음"
    assert "생성일" in window.model.data(window.model.index(row, 5), Qt.ItemDataRole.ToolTipRole)


def test_selection_persists_filter_and_visible_selection_is_explicit(window, tmp_path):
    result = populate(window, tmp_path)
    window.search_input.setText("large")
    window.select_visible()
    assert [Path(path).name for path in window.selected_paths()] == ["large.txt"]
    window.search_input.setText("small")
    assert [Path(path).name for path in window.selected_paths()] == ["large.txt"]
    window.select_visible()
    assert {Path(path).name for path in window.selected_paths()} == {"small.txt", "large.txt"}
    window.clear_selection()
    assert window.selected_paths() == []
    entry = result.files[0]
    index = window.model.index(window.model.path_rows[entry.path], 0)
    assert window.model.setData(index, Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole)
    assert window.selected_paths() == [entry.path]


def test_only_exact_groups_offer_keep_first_selection(window, tmp_path):
    result = populate(window, tmp_path)
    paths = [entry.path for entry in result.files[:2]]
    group = MatchGroup("duplicate", paths, "SHA-256 + byte comparison", 1.0)
    window._load_groups([group], "duplicate")
    assert window.select_duplicates_button.isEnabled()
    window.select_duplicate_candidates()
    assert window.selected_paths() == paths[1:]
    window._load_groups([MatchGroup("similar", paths, "이름 유사 후보", 0.9)], "similar")
    assert not window.select_duplicates_button.isEnabled()
    window.clear_selection()
    window.select_duplicate_candidates()
    assert window.selected_paths() == []


def test_cancelled_scan_blocks_file_actions(window, tmp_path, monkeypatch):
    result = populate(window, tmp_path)
    result.cancelled = True
    window.load_scan_result(result)
    notices = []
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: notices.append(True))
    window.preview_selected()
    assert notices
    assert not window.scan_complete
    assert window._worker is None


def test_assumed_duplicates_require_explicit_confirmation(window, tmp_path, monkeypatch):
    result = populate(window, tmp_path)
    window.model.set_checked([result.files[0].path])
    notices = []
    def reject(*args):
        notices.append(args)
        return QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(QMessageBox, "warning", reject)
    window.assume_duplicates()
    assert notices
    assert "검증하지 않습니다" in notices[0][2]
    assert window._worker is None
    assert Path(result.files[0].path).exists()


def test_rejected_preview_never_executes_plan(window, tmp_path, monkeypatch):
    result = populate(window, tmp_path)
    source = result.files[0].path
    plan = ui.build_plan(result.root, [source], "quarantine")
    monkeypatch.setattr(QDialog, "exec", lambda _self: QDialog.DialogCode.Rejected)
    calls = []
    monkeypatch.setattr(ui, "execute_plan", lambda _plan: calls.append(True))
    window._show_preview(plan)
    assert calls == []
    assert Path(source).exists()
    assert not (tmp_path / ".folder-organizer").exists()


def test_worker_keeps_window_alive_until_operations_complete(app, window):
    release = threading.Event()
    received = []
    window._start_task("테스트 작업", lambda _worker: release.wait(3), received.append)
    assert not window.folder_button.isEnabled()
    assert not window.close()
    assert window._worker is not None
    assert "자동으로 종료" in window.status_label.text()
    release.set()
    wait_for_task(app, window)
    # A requested close waits for the worker, then exits without opening result dialogs.
    assert received == []
    assert window._close_requested


def test_actual_scan_runs_in_worker_and_loads_result(app, window, tmp_path):
    (tmp_path / "scan-me.txt").write_text("data", encoding="utf-8")
    window.root = str(tmp_path)
    window.start_scan()
    wait_for_task(app, window)
    assert window.scan_complete
    assert "scan-me.txt" in [entry.name for entry in window.model.entries]
    assert window._worker is None


def test_update_optout_prevents_network_check(window, monkeypatch):
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: True)
    checks = []
    monkeypatch.setattr(ui.updater, "check_for_update", lambda *_args: checks.append(True))
    window.auto_update_check.setChecked(False)
    window.request_update_check()
    assert checks == []
    assert window._worker is None
    assert not window.settings.value("updates/enabled", True, type=bool)


def test_update_check_deferred_while_filesystem_worker_busy(app, window, monkeypatch):
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: True)
    checks = []
    monkeypatch.setattr(ui.updater, "check_for_update", lambda *_args: checks.append(True))
    release = threading.Event()
    window._start_task("파일 작업", lambda _worker: release.wait(3), lambda _result: None)
    window.request_update_check()
    assert window._update_retry_requested
    assert checks == []
    release.set()
    # Disable automatic updates before completion, including the deferred callback.
    window.auto_update_check.setChecked(False)
    wait_for_task(app, window)
    assert checks == []


def test_staged_update_starts_only_after_idle_close(window, monkeypatch):
    started = []
    plan = {"version": "0.2.0", "plan_path": "prepared.json"}
    window._pending_update = plan
    monkeypatch.setattr(ui.updater, "start_update_helper", lambda value: started.append(value))
    assert window.close()
    assert started == [plan]
    assert window._pending_update is None


def test_optout_cancels_staged_update(window, monkeypatch):
    started = []
    monkeypatch.setattr(ui.updater, "start_update_helper", lambda value: started.append(value))
    window._pending_update = {"version": "0.2.0"}
    window.auto_update_check.setChecked(False)
    assert window._pending_update is None
    assert window.close()
    assert started == []


def test_update_failure_preserves_current_window_and_staged_plan(window, monkeypatch):
    plan = {"version": "0.2.0", "plan_path": "prepared.json"}
    window._pending_update = plan
    def fail(_plan):
        raise RuntimeError("helper not available")
    monkeypatch.setattr(ui.updater, "start_update_helper", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: QMessageBox.StandardButton.Ok)
    assert not window.close()
    assert window._pending_update == plan
    assert "실패" in window.update_status.text()


def test_readable_results_hide_internal_tokens_and_translate_status():
    result = ui.readable_details({"status": "partial", "signature": "internal", "items": [
        {"source": "hello.txt", "target": "backup.txt", "state": "done", "sha256": "digest"}]})
    assert "부분 처리" in result
    assert "원본: hello.txt" in result
    assert "완료" in result
    assert "signature" not in result
    assert "internal" not in result
    assert "digest" not in result


def test_gui_duplicate_preview_quarantine_and_undo_workflow(app, window, tmp_path, monkeypatch):
    """Drive real scan/hash/plan/mutation/undo through the GUI's worker orchestration."""
    first = tmp_path / "original.txt"
    second = tmp_path / "copy.txt"
    first.write_text("keep this content", encoding="utf-8")
    second.write_bytes(first.read_bytes())
    window.load_scan_result(scan_folder(tmp_path))
    window.analyze_duplicates()
    wait_for_task(app, window)
    assert len(window.groups) == 1
    window.select_duplicate_candidates()
    assert len(window.selected_paths()) == 1
    chosen = Path(window.selected_paths()[0])
    results = []
    monkeypatch.setattr(window, "_show_details", lambda _title, result: results.append(result))
    monkeypatch.setattr(QDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    window._build_preview("quarantine")
    wait_for_task(app, window)
    assert results[0]["status"] == "completed"
    assert not chosen.exists()
    assert sum(path.exists() for path in (first, second)) == 1
    assert window.scan_complete
    assert len(window.model.entries) == 1
    operation_id = results[0]["id"]
    window._start_task("실행 취소", lambda _worker: ui.undo_operation(str(tmp_path), operation_id), window._operation_done)
    wait_for_task(app, window)
    assert results[1]["status"] == "undone"
    assert first.read_bytes() == second.read_bytes() == b"keep this content"
    assert window.scan_complete
    assert len(window.model.entries) == 2


def wait_for_update(app, window, timeout=5):
    deadline = time.monotonic() + timeout
    while window._update_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert window._update_worker is None


def test_background_updates_leave_file_controls_and_scan_usable(app, window, tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: True)
    def check(_version, cancel=None):
        entered.set()
        while not release.is_set():
            if cancel.wait(0.005):
                raise RuntimeError("취소")
        return None
    monkeypatch.setattr(ui.updater, "check_for_update", check)
    window.request_update_check()
    assert entered.wait(1)
    assert window._update_worker is not None
    assert window._worker is None
    assert window.folder_button.isEnabled()
    assert not window.update_button.isEnabled()
    (tmp_path / "during-update.txt").write_text("ok", encoding="utf-8")
    window.root = str(tmp_path)
    window.start_scan()
    wait_for_task(app, window)
    assert window.scan_complete
    assert window._update_worker is not None
    release.set()
    wait_for_update(app, window)
    assert window.update_button.isEnabled()


def test_closing_during_update_requests_cancellation_and_then_closes(app, window, monkeypatch):
    entered = threading.Event()
    observed_cancel = threading.Event()
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: True)
    def check(_version, cancel=None):
        entered.set()
        assert cancel.wait(3)
        observed_cancel.set()
        raise RuntimeError("업데이트 확인을 취소했습니다")
    monkeypatch.setattr(ui.updater, "check_for_update", check)
    window.show()
    window.request_update_check()
    assert entered.wait(1)
    assert not window.close()
    assert window._close_requested
    wait_for_update(app, window)
    assert observed_cancel.is_set()
    app.processEvents()
    assert not window.isVisible()
    assert window._pending_update is None


def test_startup_reports_previous_helper_failure_once(window, monkeypatch):
    window.auto_update_check.setChecked(False)
    diagnostic = {"status": "failed", "version": "0.2.0", "details": "파일 교체를 완료하지 못했습니다", "backup": "not-executed.exe"}
    monkeypatch.setattr(ui.updater, "last_update_result", lambda: diagnostic)
    window._initial_update_check()
    assert "지난 업데이트" in window.warning_label.text()
    assert "파일 교체" in window.warning_label.text()
    assert not window.warning_label.isHidden()
    fingerprint = window.settings.value("updates/lastDiagnostic")
    assert fingerprint
    window.warning_label.hide()
    window._initial_update_check()
    assert window.warning_label.isHidden()
    assert window.settings.value("updates/lastDiagnostic") == fingerprint


def test_disabling_updates_cancels_background_download(app, window, monkeypatch):
    entered = threading.Event()
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: True)
    def check(_version, cancel=None):
        entered.set()
        assert cancel.wait(3)
        raise RuntimeError("취소")
    monkeypatch.setattr(ui.updater, "check_for_update", check)
    window.request_update_check()
    assert entered.wait(1)
    window.auto_update_check.setChecked(False)
    wait_for_update(app, window)
    assert window._pending_update is None
    assert "현재 버전 유지" in window.update_status.text()
