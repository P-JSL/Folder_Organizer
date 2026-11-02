"""New 0.9 main-workspace contracts; no previous release cases are collected.

The analysis engine is replaced by synthetic snapshots here. These checks cover
only the newly embedded workspace, scope routing, and worker/UI lifecycle.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Event
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from folder_organizer import multiroot_view, ui
from folder_organizer.models import FileEntry, ScanResult
from folder_organizer.multiroot import MultiScanResult, ReadOnlyDuplicateGroup, RootResult


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "workspace-settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(ui, "QSettings", lambda *_args: settings)
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: False)
    widget = ui.MainWindow()
    for timer in (widget.initial_update_timer, widget.update_timer, widget.deferred_update_timer):
        timer.stop()
    widget.show()
    app.processEvents()
    yield widget
    workspace = widget.workspace
    if workspace._worker is not None:
        workspace.cancel_analysis()
        workspace._worker.wait(5000)
        app.processEvents()
    if widget._worker is not None:
        widget._worker.cancel_event.set()
        widget._worker.wait(5000)
        app.processEvents()
    widget._pending_update = None
    widget.close()
    widget.deleteLater()
    app.processEvents()


def wait_for(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    assert predicate(), "new 0.9 workspace operation did not settle"


def entry(path, *, size=321):
    return FileEntry(str(path), Path(path).name, Path(path).name, size,
                     1_700_000_000.0, 1_600_000_000.0, "birthtime", "file", ".txt",
                     1_700_000_000_000_000_000, 123, 456)


def snapshot(paths, *, cancelled=False):
    roots = []
    files = []
    memberships = {}
    owners = {}
    for index, path in enumerate(paths):
        root_id = f"root-{index + 1:04d}"
        item = entry(Path(path) / f"document-{index + 1}.txt")
        scan = ScanResult(str(path), [item], cancelled=cancelled)
        roots.append(RootResult(root_id, str(path), str(path),
                                "cancelled" if cancelled else "complete", scan=scan))
        files.append(item)
        memberships[item.path] = [root_id]
        owners[item.path] = root_id
    return MultiScanResult(roots=roots, files=files, memberships=memberships, owners=owners,
                           cancelled=cancelled, complete=not cancelled)


def publish_snapshot(app, workspace, result, monkeypatch):
    monkeypatch.setattr(multiroot_view, "scan_folders", lambda _paths, **_options: result)
    workspace.start_analysis()
    wait_for(app, lambda: not workspace.is_running())
    assert workspace.result is result


def test_v090_default_is_embedded_main_workspace_and_menu_stays_in_main(window, app):
    workspace = window.workspace
    assert window.workspace_mode == "global"
    assert not isinstance(workspace, QDialog)
    assert window.isAncestorOf(workspace)
    assert workspace.isVisible()
    assert window._multi_root_dialog is None
    window.show_multiroot()
    app.processEvents()
    assert workspace.isVisible()
    assert window._multi_root_dialog is None
    assert window.theme_mode == "dark"


def test_v090_checked_roots_are_the_only_worker_inputs(window, app, tmp_path, monkeypatch):
    workspace = window.workspace
    roots = [str(tmp_path / name) for name in ("documents", "pictures", "excluded")]
    workspace.add_paths(roots)
    index = workspace.root_model.index(2, 0)
    assert workspace.root_model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert workspace.checked_paths() == roots[:2]
    calls = []

    def capture(paths, **options):
        calls.append((list(paths), options))
        return snapshot(paths)

    monkeypatch.setattr(multiroot_view, "scan_folders", capture)
    workspace.start_analysis()
    wait_for(app, lambda: not workspace.is_running())
    assert len(calls) == 1
    assert calls[0][0] == roots[:2]
    assert calls[0][1]["duplicates"] is True
    assert workspace.file_model.rowCount() == 2
    assert not workspace.result.action_authority
    assert window.scan_result is None
    assert not window.groups


def test_v090_no_checked_roots_does_not_start_any_worker(window, tmp_path, monkeypatch):
    workspace = window.workspace
    workspace.add_paths([str(tmp_path / "unchecked")])
    workspace.root_model.setData(workspace.root_model.index(0, 0), Qt.CheckState.Unchecked,
                                 Qt.ItemDataRole.CheckStateRole)
    monkeypatch.setattr(multiroot_view, "scan_folders", lambda *_a, **_k: pytest.fail("unchecked root was scanned"))
    workspace.start_analysis()
    assert not workspace.is_running()
    assert workspace.result is None
    assert workspace.checked_paths() == []


def test_v090_remove_registered_root_invalidates_displayed_snapshot(window, app, tmp_path, monkeypatch):
    workspace = window.workspace
    roots = [str(tmp_path / name) for name in ("left", "right")]
    workspace.add_paths(roots)
    publish_snapshot(app, workspace, snapshot(roots), monkeypatch)
    workspace.root_table.selectRow(0)
    workspace._remove_selected()
    assert workspace.root_model.paths == roots[1:]
    assert workspace.checked_paths() == roots[1:]
    assert workspace.result is None
    assert workspace.file_model.rowCount() == 0
    assert workspace.group_model.rowCount() == 0
    assert not workspace.export_button.isEnabled()


def test_v090_file_inspector_uses_snapshot_even_when_source_cannot_be_read(window, app, tmp_path, monkeypatch):
    workspace = window.workspace
    root = str(tmp_path / "offline-nas")
    workspace.add_paths([root])
    result = snapshot([root])
    publish_snapshot(app, workspace, result, monkeypatch)

    def forbidden(*_args, **_kwargs):
        pytest.fail("inspector tried to query the filesystem instead of the scan snapshot")

    with monkeypatch.context() as context:
        context.setattr(Path, "stat", forbidden)
        context.setattr(Path, "open", forbidden)
        workspace.file_table.selectRow(0)
        workspace._show_file()
        app.processEvents()
        text = workspace.inspector.toPlainText()
    assert result.files[0].name in text
    assert result.files[0].path in text
    assert "321" in text
    assert "2023" in text
    assert "2020" in text


def test_v090_global_scope_rejects_stale_single_folder_operation(window, tmp_path, monkeypatch):
    root = str(tmp_path / "old-single")
    item = entry(Path(root) / "previous.txt")
    window.root = root
    window.scan_result = ScanResult(root, [item])
    window.scan_complete = True
    window.model.set_entries([item])
    messages = []
    monkeypatch.setattr(ui.QMessageBox, "information", lambda *_args: messages.append(_args))
    assert window.workspace_mode == "global"
    assert not window._require_scan()
    assert messages
    assert window.workspace_mode == "global"
    assert window._worker is None


def test_v090_organize_selected_folder_explicitly_enters_single_scope(window, tmp_path, monkeypatch):
    root = str(tmp_path / "explicit")
    window.workspace.add_paths([root])
    calls = []
    monkeypatch.setattr(window, "_start_task", lambda *args, **kwargs: calls.append((args, kwargs)))
    window._open_workspace_folder(root)
    assert window.workspace_mode == "single"
    assert window.root == root
    assert window.scan_result is None
    assert not window.scan_complete
    assert not window.workspace.isVisible()
    assert calls, "explicit single scope should use its existing authenticated scan workflow"
    window.show_multiroot()
    assert window.workspace_mode == "global"
    assert window.workspace.isVisible()
    assert window.workspace.checked_paths() == [root]
    assert not window.model.checked


def test_v090_main_close_cancels_embedded_worker_and_keeps_partial_snapshot(window, app, tmp_path, monkeypatch):
    workspace = window.workspace
    roots = [str(tmp_path / "cancelled")]
    workspace.add_paths(roots)
    entered = Event()
    cancelled = Event()

    def hold(paths, *, cancel, **_options):
        entered.set()
        if cancel.wait(3):
            cancelled.set()
        return snapshot(paths, cancelled=cancel.is_set())

    monkeypatch.setattr(multiroot_view, "scan_folders", hold)
    workspace.start_analysis()
    wait_for(app, entered.is_set)
    window.close()
    wait_for(app, lambda: not workspace.is_running())
    wait_for(app, lambda: not window.isVisible())
    assert cancelled.is_set()
    assert workspace.result.cancelled
    assert not workspace.result.complete
    assert workspace.file_model.rowCount() == 1
    assert not workspace.result.action_authority
