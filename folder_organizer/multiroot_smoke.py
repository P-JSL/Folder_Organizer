"""Opt-in, read-only verification of the bundled multi-folder window.

Only the explicit --multiroot-smoke entry point uses this module. The release
driver supplies disposable roots and an output outside those roots. A capture
acknowledgement keeps the real rendered window alive until its PNG is retained.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QTabWidget

from .multiroot import MAX_ROOTS, write_multiroot_report
from .scanner import normalize_exclude_patterns


def launch() -> int:
    from .ui import MainWindow

    try:
        values = json.loads(os.environ.get("FOLDER_ORGANIZER_MULTI_SMOKE_ROOTS", "null"))
        excludes = list(normalize_exclude_patterns(json.loads(
            os.environ.get("FOLDER_ORGANIZER_MULTI_SMOKE_EXCLUDES", "[]"))))
    except (TypeError, ValueError):
        return 2
    raw_report = os.environ.get("FOLDER_ORGANIZER_MULTI_SMOKE_REPORT", "")
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_ROOTS
            or not all(isinstance(value, str) and value for value in values) or not raw_report):
        return 2
    try:
        roots = [Path(value).resolve(strict=True) for value in values]
        report = Path(raw_report).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return 2
    marker = Path(str(report) + ".capture-done")
    if (report.exists() or marker.exists() or not report.parent.is_dir()
            or any(report.is_relative_to(root) for root in roots)):
        return 2
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Folder Organizer")
    app.setOrganizationName("Folder Organizer")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    for timer in (window.initial_update_timer, window.update_timer, window.deferred_update_timer):
        timer.stop()
    # An explicit disposable-fixture scope makes GUI and CLI contracts comparable
    # without persisting a test preference into the user's settings.
    window.analysis_options.update(exclude_patterns=excludes, min_size=0, max_size=None)
    for checkbox, value in ((window.recursive_check, True), (window.hidden_check, False)):
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(value)
        checkbox.blockSignals(blocked)
    window.show()
    window.show_multiroot()
    dialog = window._multi_root_dialog
    if dialog is None:
        return 2
    dialog.add_paths(roots)
    dialog.start_analysis()
    started = time.monotonic()
    ready_at = None
    failed = False
    timer = QTimer(window)
    timer.setInterval(35)

    def finish(code):
        timer.stop()
        dialog.close()
        window.close()
        app.exit(code)

    def poll():
        nonlocal ready_at, failed
        if time.monotonic() - started > 30:
            failed = True
            dialog.cancel_analysis()
        if dialog.is_running():
            return
        if failed or dialog.result is None or not dialog.result.complete:
            finish(2)
            return
        if ready_at is None:
            try:
                # This is the normal export contract, not a fabricated receipt.
                write_multiroot_report(dialog.result, report)
                tabs = dialog.findChild(QTabWidget)
                if tabs is not None:
                    tabs.setCurrentIndex(1)
                if dialog.result.groups:
                    dialog.group_table.selectRow(0)
                ready_at = time.monotonic()
            except (OSError, ValueError):
                finish(2)
                return
        if marker.exists() and time.monotonic() - ready_at >= .5:
            finish(0)

    timer.timeout.connect(poll)
    timer.start()
    return app.exec()
