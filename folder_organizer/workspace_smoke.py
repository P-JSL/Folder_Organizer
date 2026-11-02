"""Explicit 0.9 main-workspace capture entry point for disposable fixtures.

This uses the normal main-window controls and worker. It does not run a previous
release's validation driver, change file contents, or persist GUI preferences.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog

from . import __version__
from .multiroot import MAX_ROOTS


def launch() -> int:
    from .ui import MainWindow

    try:
        values = json.loads(os.environ.get("FOLDER_ORGANIZER_WORKSPACE_SMOKE_ROOTS", "null"))
        raw_report = os.environ.get("FOLDER_ORGANIZER_WORKSPACE_SMOKE_REPORT", "")
        if (not isinstance(values, list) or not 2 <= len(values) <= MAX_ROOTS
                or not all(isinstance(value, str) and value for value in values) or not raw_report):
            return 2
        roots = [Path(value).resolve(strict=True) for value in values]
        if not all(root.is_dir() for root in roots):
            return 2
        report = Path(raw_report).resolve(strict=False)
        marker = Path(str(report) + ".capture-done")
        if (report.exists() or marker.exists() or not report.parent.is_dir()
                or any(report.is_relative_to(root) for root in roots)):
            return 2
    except (OSError, ValueError, TypeError, RuntimeError):
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Folder Organizer")
    app.setOrganizationName("Folder Organizer")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow(read_only_settings=True)
    for timer in (window.initial_update_timer, window.update_timer, window.deferred_update_timer):
        timer.stop()
    window.set_theme("dark", persist=False)
    window.resize(1320, 820)
    workspace = window.workspace
    workspace.add_paths(roots)
    workspace.recursive.setChecked(True)
    workspace.hidden.setChecked(False)
    workspace.exclusions.clear()
    workspace.minimum.setValue(0)
    workspace._options.update(min_size=0, max_size=None, exclude_patterns=[])
    window.show()
    workspace.all_button.click()
    started = time.monotonic()
    ready_at = None
    timed_out = False
    timer = QTimer(window)
    timer.setInterval(35)

    def finish(code):
        timer.stop()
        window.close()
        app.exit(code)

    def poll():
        nonlocal ready_at, timed_out
        if time.monotonic() - started > 30:
            timed_out = True
            workspace.cancel_analysis()
        if workspace.is_running():
            return
        result = workspace.result
        if timed_out or result is None or not result.complete:
            finish(2)
            return
        if ready_at is None:
            workspace.tabs.setCurrentIndex(0)
            if result.groups:
                workspace.group_table.selectRow(0)
                # Show the whole inventory while retaining selected-file metadata
                # and the exact group's explanation beside it.
                workspace.tabs.setCurrentIndex(0)
            if result.files:
                workspace.file_table.selectRow(0)
                workspace._show_file()
            snapshot = workspace.selected_snapshot()
            receipt = {
                "schema": "folder-organizer-workspace-smoke-v1",
                "app_version": __version__, "edition": "python",
                "main_workspace": bool(window.isAncestorOf(workspace)
                                       and not isinstance(workspace, QDialog)
                                       and window.workspace_stack.currentWidget() is workspace),
                "scope_mode": window.workspace_mode,
                "checked_roots": workspace.checked_paths(),
                "file_rows": workspace.file_model.rowCount(),
                "duplicate_groups": workspace.group_model.rowCount(),
                "action_authority": result.action_authority,
                "finished": not workspace.is_running() and result.complete,
                "selected_snapshot": snapshot,
                "header_controls": [workspace.add_button.text(), workspace.all_button.text(), workspace.cancel_button.text()],
                "main_window_title": window.windowTitle(),
            }
            try:
                with report.open("x", encoding="utf-8") as handle:
                    json.dump(receipt, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except (OSError, ValueError):
                finish(2)
                return
            ready_at = time.monotonic()
        if marker.exists() and time.monotonic() - ready_at >= .5:
            finish(0)

    timer.timeout.connect(poll)
    timer.start()
    return app.exec()
