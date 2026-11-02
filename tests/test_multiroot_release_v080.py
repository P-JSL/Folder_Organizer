"""The new Escape route must wait for the live read-only worker."""
import os
import json
import subprocess
import sys
import time
from threading import Event
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from folder_organizer import multiroot_view
from folder_organizer.multiroot import MultiScanResult


def test_escape_requests_cancellation_before_rejecting_live_multiroot_dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    entered, cancelled = Event(), Event()
    def wait_for_cancel(roots, *, cancel, **options):
        entered.set()
        assert cancel.wait(3)
        cancelled.set()
        return MultiScanResult(cancelled=True, complete=False)
    monkeypatch.setattr(multiroot_view, "scan_folders", wait_for_cancel)
    dialog = multiroot_view.MultiRootDialog()
    finished = []
    dialog.finished.connect(finished.append)
    dialog.add_paths([tmp_path])
    dialog.show()
    dialog.start_analysis()
    deadline = time.monotonic() + 3
    while not entered.is_set() and time.monotonic() < deadline:
        app.processEvents(); QTest.qWait(5)
    assert entered.is_set()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert finished == []
    assert dialog._close_pending
    while dialog.is_running() and time.monotonic() < deadline:
        app.processEvents(); QTest.qWait(5)
    app.processEvents()
    assert cancelled.is_set() and not dialog.is_running()
    assert len(finished) == 1 and not dialog.isVisible()


def test_new_opt_in_smoke_exports_real_results_and_waits_for_capture_ack(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir(); right.mkdir()
    raw = b"readonly smoke bytes\n" * 30
    (left / "a.bin").write_bytes(raw); (right / "b.bin").write_bytes(raw)
    report = tmp_path / "gui.json"
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen",
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "FOLDER_ORGANIZER_MULTI_SMOKE_ROOTS": json.dumps([str(left), str(right)]),
        "FOLDER_ORGANIZER_MULTI_SMOKE_REPORT": str(report)}
    process = subprocess.Popen([sys.executable, "-m", "folder_organizer", "--multiroot-smoke"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not report.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.03)
        assert report.exists(), process.communicate(timeout=1)
        value = json.loads(report.read_text())
        assert value["complete"] and value["action_authority"] is False
        assert len(value["duplicates"]["groups"]) == 1
        assert process.poll() is None
        Path(str(report) + ".capture-done").write_text("captured")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert (left / "a.bin").read_bytes() == (right / "b.bin").read_bytes() == raw
    finally:
        if process.poll() is None:
            process.kill(); process.communicate()
