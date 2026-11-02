"""New unified-workspace action UI contracts, using synthetic backend sessions.

Only this target is collected for this change; no old regression target or
screenshot driver is executed. Actual filesystem batching is checked separately.
"""
from __future__ import annotations

import os
from pathlib import Path
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from folder_organizer import multiroot_view, ui
from folder_organizer.models import FileEntry, MatchGroup, ScanResult
from folder_organizer.multiroot import MultiScanResult, ReadOnlyDuplicateGroup, RootResult


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "new-action-ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(ui, "QSettings", lambda *_args: settings)
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: False)
    result = ui.MainWindow(read_only_settings=True)
    for timer in (result.initial_update_timer, result.update_timer, result.deferred_update_timer): timer.stop()
    result.show()
    app.processEvents()
    yield result
    if result.workspace.is_running():
        result.workspace.cancel_analysis()
        result.workspace._worker.wait(5000)
        app.processEvents()
    if result._worker:
        result._worker.cancel_event.set(); result._worker.wait(5000); app.processEvents()
    result._pending_update = None
    result.close(); result.deleteLater(); app.processEvents()


def wait(app, predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if predicate(): return
        QTest.qWait(5)
    assert predicate(), "new workspace action did not settle"


class FakeSession:
    def __init__(self, paths):
        self.consumed = False
        self.roots = tuple(paths)
        files, roots, memberships, owners = [], [], {}, {}
        for index, path in enumerate(paths):
            entry = FileEntry(str(Path(path) / f"document-{index+1}.txt"), f"document-{index+1}.txt", f"document-{index+1}.txt",
                              1024, 1700000000.0, 1600000000.0, "birthtime", "file", ".txt", 1700000000000000000, 42, index+1)
            root_id = f"root-{index+1:04d}"
            files.append(entry)
            roots.append(RootResult(root_id, path, path, "complete", scan=ScanResult(path, [entry])))
            memberships[entry.path] = [root_id]; owners[entry.path] = root_id
        self.files = tuple(files)
        group = ReadOnlyDuplicateGroup(tuple(entry.path for entry in files), 1024, "a"*64, tuple(root.root_id for root in roots))
        self.groups = (MatchGroup("duplicate", list(group.paths), "same bytes", evidence={"byte_identical": True, "sha256": "a"*64}),) if len(files)>1 else ()
        self.result = MultiScanResult(roots=roots, files=list(files), memberships=memberships, owners=owners,
                                     groups=[group] if self.groups else [], complete=True)
    def invalidate(self): self.consumed = True


def install_analysis(monkeypatch, calls):
    def analyze(paths, **options):
        calls.append((list(paths), options))
        return FakeSession(paths)
    monkeypatch.setattr(multiroot_view.workspace_actions, "analyze_workspace", analyze)


def analyze(window, app, paths):
    window.workspace.add_paths(paths)
    window.workspace.start_analysis()
    wait(app, lambda: not window.workspace.is_running())
    assert window.workspace.session is not None
    return window.workspace


def plan_for(session, selected, mode="copy"):
    chosen = set(selected)
    plans = []
    for root in session.roots:
        items = [{"source": entry.path, "target": str(Path(root)/"delete_files"/entry.name), "size": entry.size,
                  "action": "copy" if mode=="copy" else "move"}
                 for entry in session.files if entry.path in chosen and Path(entry.path).is_relative_to(root)]
        if items: plans.append({"root": root, "items": items})
    return {"id": "b"*32, "mode": mode, "items": [item for plan in plans for item in plan["items"]], "plans": plans}


def test_next_single_folder_file_menu_uses_the_same_action_workspace(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    folder = str(tmp_path/"one")
    window._open_folder(folder)
    wait(app, lambda: not window.workspace.is_running())
    assert calls[0][0] == [folder]
    assert window.workspace_stack.currentWidget() is window.workspace
    assert window.workspace_mode == "global"
    assert window.workspace.copy_button.isEnabled()
    assert window.analysis_menu.isEnabled() and window.organize_menu.isEnabled()
    assert window.workspace.ribbon.tabText(0) == "홈"
    assert window.workspace.type_model.rows[0][1] == 1


def test_next_multi_folder_inspection_uses_all_active_files_without_reanalysis(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"left"), str(tmp_path/"right")])
    inspected = []
    def similar(files, **options):
        inspected.extend(entry.path for entry in files)
        assert len(options["verified_duplicates"]) == 1
        return [MatchGroup("similar", [entry.path for entry in files], "similar names")]
    monkeypatch.setattr(multiroot_view, "find_similar", similar)
    workspace.analyze_similar()
    wait(app, lambda: not workspace.is_running())
    assert inspected == [entry.path for entry in workspace.session.files]
    assert workspace.tabs.currentIndex() == 2
    assert workspace.similar_model.rowCount() == 1
    window.analyze_duplicates()
    assert workspace.tabs.currentIndex() == 1
    assert len(calls) == 1


def test_next_visible_checkbox_selection_preview_and_execution_are_one_scope(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"left"), str(tmp_path/"right")])
    workspace.search_input.setText("document-2")
    workspace.mark_visible()
    selected = [workspace.session.files[1].path]
    assert workspace.checked_file_paths() == selected
    built, previews = [], []
    def build(session, paths, mode, **options):
        built.append((session, paths, mode, options)); return plan_for(session, paths, mode)
    monkeypatch.setattr(multiroot_view.workspace_actions, "build_workspace_plan", build)
    monkeypatch.setattr(workspace, "_show_batch_preview", previews.append)
    workspace.preview_action("move", destination=str(tmp_path/"destination"))
    wait(app, lambda: not workspace.is_running())
    assert built[0][1] == selected and built[0][2] == "move"
    assert built[0][3]["destination"] == str(tmp_path/"destination")
    assert previews[0]["items"][0]["source"] == selected[0]
    def execute(session, plan, **options):
        session.invalidate()
        return {"status": "completed", "items": [{**plan["items"][0], "state": "completed", "source_removed": True}], "operations": []}
    monkeypatch.setattr(multiroot_view.workspace_actions, "execute_workspace_plan", execute)
    workspace.execute_preview(previews[0])
    wait(app, lambda: not workspace.is_running())
    assert selected[0] not in [entry.path for entry in workspace.result.files]
    assert not workspace.copy_button.isEnabled() and not workspace.keep_first_button.isEnabled()
    assert len(calls) == 1


def test_next_keep_first_uses_the_displayed_global_group_without_confirmation_or_rescan(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"z"), str(tmp_path/"A")])
    displayed = workspace.result.groups[0].paths[0]
    built = []
    def build(session, **options):
        assert session.groups[0].paths[0] == displayed
        built.append(session)
        return plan_for(session, session.groups[0].paths[1:], "duplicate_auto")
    def execute(session, plan, **options):
        session.invalidate()
        return {"status": "completed", "items": [{**item, "state": "completed", "source_removed": True} for item in plan["items"]], "operations": []}
    monkeypatch.setattr(multiroot_view.workspace_actions, "build_workspace_keep_first_plan", build)
    monkeypatch.setattr(multiroot_view.workspace_actions, "execute_workspace_plan", execute)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: pytest.fail("extra confirmation"))
    workspace.keep_first()
    wait(app, lambda: not workspace.is_running())
    assert len(built) == 1 and len(calls) == 1
    assert [entry.path for entry in workspace.result.files] == [displayed]
    assert workspace.session.consumed


def test_next_scope_edits_revoke_actions_but_snapshot_inspection_remains_readonly(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"offline")])
    workspace.file_table.selectRow(0)
    def forbidden(*_args, **_kwargs): pytest.fail("snapshot inspector read filesystem")
    with monkeypatch.context() as context:
        context.setattr(Path, "stat", forbidden); context.setattr(Path, "open", forbidden)
        workspace._show_file()
    assert "document-1.txt" in workspace.inspector.toPlainText()
    workspace.exclusions.setText("*.tmp")
    assert workspace.session.consumed and not workspace.move_button.isEnabled()
    assert workspace.file_model.rowCount() == 1


def test_next_close_during_batch_cancels_the_new_action_worker(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"A"), str(tmp_path/"B")])
    started = []
    def execute(session, plan, *, cancel, progress_detail):
        started.append(True)
        while not cancel.is_set(): time.sleep(.005)
        session.invalidate()
        return {"status": "cancelled", "items": [], "operations": []}
    monkeypatch.setattr(multiroot_view.workspace_actions, "execute_workspace_plan", execute)
    plan = plan_for(workspace.session, [entry.path for entry in workspace.session.files])
    workspace.execute_preview(plan)
    wait(app, lambda: bool(started))
    window.close()
    wait(app, lambda: not workspace.is_running())
    assert workspace.session.consumed
    assert window._close_requested


def test_next_csv_export_snapshots_visible_rows_on_gui_thread_before_worker(window, app, tmp_path, monkeypatch):
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QFileDialog
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"left"), str(tmp_path/"right")])
    workspace.search_input.setText("document-2")
    output = tmp_path/"new-visible.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(output), "CSV"))
    original = workspace.file_proxy.mapToSource
    threads = []
    def map_on_gui(index):
        assert QThread.currentThread() == app.thread(), "Qt model read from CSV worker"
        threads.append(True)
        return original(index)
    monkeypatch.setattr(workspace.file_proxy, "mapToSource", map_on_gui)
    workspace.export_csv()
    wait(app, lambda: not workspace.is_running())
    assert threads
    assert "document-2.txt" in output.read_text(encoding="utf-8-sig")
    assert "document-1.txt" not in output.read_text(encoding="utf-8-sig")
    assert not workspace.session.consumed


def test_next_cross_root_merge_routes_preview_and_execution_through_captured_session(window, app, tmp_path, monkeypatch):
    from folder_organizer import merge_view
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"A"), str(tmp_path/"B")])
    session = workspace.session
    paths = [entry.path for entry in session.files]
    workspace.file_model.set_checked(paths)
    routed = []
    def build(selected_session, selected_paths, **options):
        assert selected_session is session
        routed.append((list(selected_paths), options))
        return {"move_originals": False, "source_count": 2, "retained_units": 2, "removed_units": 1,
                "unit": "문단", "output": str(tmp_path/"merged.txt"), "output_size": 14, "content": "one\n\ntwo\n", "schema": "workspace-merge"}
    def execute(selected_session, plan, **options):
        assert selected_session is session and plan["schema"] == "workspace-merge"
        selected_session.invalidate()
        return {"id": "c"*32, "status": "completed", "items": [], "operations": [], "output_created": True}
    monkeypatch.setattr(multiroot_view.workspace_actions, "build_workspace_merge_preview", build, raising=False)
    monkeypatch.setattr(multiroot_view.workspace_actions, "execute_workspace_merge", execute, raising=False)
    monkeypatch.setattr(window, "_show_details", lambda *_a, **_k: None)
    window.show_merge()
    dialog = window._merge_dialog
    assert isinstance(dialog, merge_view.MergeDialog)
    assert str(Path(paths[0]).parent) == dialog.root
    dialog._preview()
    wait(app, lambda: dialog._worker is None)
    assert routed[0][0] == paths
    assert dialog.plan["schema"] == "workspace-merge"
    dialog.confirm.setChecked(True)
    dialog._submit()
    wait(app, lambda: session.consumed and window._worker is None)
    assert not workspace.copy_button.isEnabled()
    assert len(calls) == 1


def test_next_merge_group_passes_every_member_while_pair_comparison_stays_bounded(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/name) for name in ("A", "B", "C")])
    workspace.tabs.setCurrentIndex(1)
    workspace.group_table.selectRow(0)
    routed = []
    monkeypatch.setattr(window, "show_merge", lambda paths=None: routed.append(paths))
    window.merge_current_group()
    assert routed == [list(workspace.result.groups[0].paths)]
    assert len(routed[0]) == 3
    assert len(workspace.comparison_paths()) == 2


def test_next_readonly_tool_worker_progress_and_stop_are_in_visible_workspace(window, app, tmp_path, monkeypatch):
    calls = []; install_analysis(monkeypatch, calls)
    workspace = analyze(window, app, [str(tmp_path/"A")])
    started = []
    def inspect(worker):
        started.append(True)
        worker.report_detail({"phase": "health", "completed": 1, "total": 3, "message": "new tool reading"})
        while not worker.cancel_event.is_set(): time.sleep(.005)
        return {"cancelled": True}
    window._start_task("new tool inspection", inspect, lambda _value: None, cancellable=True)
    wait(app, lambda: bool(started))
    assert workspace._external_busy and workspace.cancel_button.isEnabled()
    assert not workspace.copy_button.isEnabled() and not workspace.add_button.isEnabled()
    workspace.cancel_button.click()
    wait(app, lambda: window._worker is None)
    assert not workspace._external_busy
    assert workspace.copy_button.isEnabled()
    assert not workspace.session.consumed
