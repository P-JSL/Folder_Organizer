"""New multi-folder window integration and deferred application close."""
import os
import time
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from folder_organizer import ui, multiroot_view
from folder_organizer.scanner import scan_folder


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    assert predicate(), "new multi-folder worker did not finish"


def make_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window = ui.MainWindow()
    for timer in (window.initial_update_timer, window.update_timer, window.deferred_update_timer):
        timer.stop()
    window.show()
    return app, window


def test_menu_multiroot_worker_keeps_single_root_context_and_can_reopen(tmp_path, monkeypatch):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir(); right.mkdir()
    raw = b"same actual bytes across selected folders"
    (left / "a.bin").write_bytes(raw); (right / "b.bin").write_bytes(raw)
    app, window = make_window(tmp_path, monkeypatch)
    window.load_scan_result(scan_folder(left))
    original_scan = window.scan_result
    original_entries = list(window.model.entries)
    action = next(action for action in window.file_menu.actions() if action.text() == "여러 폴더 통합 분석…")
    action.trigger()
    dialog = window._multi_root_dialog
    dialog.add_paths([right])
    dialog.start_analysis()
    wait_for(lambda: not dialog.is_running())
    assert dialog.result.complete and len(dialog.result.groups) == 1
    assert window.scan_result is original_scan and window.model.entries == original_entries
    assert not window.groups
    dialog.close()
    wait_for(lambda: window._multi_root_dialog is None)
    action.trigger()
    assert window._multi_root_dialog is not None and window._multi_root_dialog.isVisible()
    window._multi_root_dialog.close()
    window.close()
    app.processEvents()


def test_application_close_waits_for_new_readonly_worker_cancellation(tmp_path, monkeypatch):
    root = tmp_path / "files"; root.mkdir(); (root / "a.txt").write_text("kept")
    entered = Event()
    original = multiroot_view.scan_folders
    def delayed(paths, *, cancel, **options):
        entered.set()
        assert cancel.wait(3), "window close failed to request cancellation"
        return original(paths, cancel=cancel, **options)
    monkeypatch.setattr(multiroot_view, "scan_folders", delayed)
    app, window = make_window(tmp_path, monkeypatch)
    window.show_multiroot()
    dialog = window._multi_root_dialog
    dialog.add_paths([root])
    dialog.start_analysis()
    wait_for(entered.is_set)
    window.close()
    assert window.isVisible()
    wait_for(lambda: not window.isVisible())
    assert window._multi_root_dialog is None
    assert (root / "a.txt").read_text() == "kept"
    app.processEvents()
