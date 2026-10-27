"""Capture the real desktop UI with disposable example files and preferences.

No personal folder, installed preference or live update is used. Run after
installing project dependencies, with QT_QPA_PLATFORM=offscreen when headless.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from folder_organizer import diagnostics, ui
from folder_organizer.help_view import HelpCenterDialog
from folder_organizer.history_view import HistoryDialog
from folder_organizer.log_view import LogViewerDialog
from folder_organizer.matching import find_duplicates
from folder_organizer.operations import build_plan, execute_plan, list_history
from folder_organizer.rule_view import ClassificationDialog
from folder_organizer.rules import default_rules
from folder_organizer.scanner import scan_folder


def capture(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    with tempfile.TemporaryDirectory(prefix="organizer-ui-examples-") as temporary:
        scratch = Path(temporary)
        root = scratch / "Downloads"
        root.mkdir()
        for folder in ("보고서", "사진", "설치 파일", "업무/청구서"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        paragraph = b"A local organizer keeps the original document safe for a careful review before applying a classification rule.\n"
        for name in ("업무 보고서.md", "업무 보고서 copy.md"):
            (root / "보고서" / name).write_bytes(paragraph)
        (root / "보고서/10월 매출.csv").write_bytes("항목,금액\n매출,1000\n".encode("utf-8"))
        (root / "업무/청구서/10월 청구서.pdf").write_bytes(b"%PDF-1.4\n% example fixture")
        (root / "설치 파일/PhotoEditor-Setup.exe").write_bytes(b"MZ" + b"fixture-not-executed" * 80)
        Image.new("RGB", (480, 320), "#317b80").save(root / "사진/여행 사진.png")
        (root / "사진/여행 사진 copy.png").write_bytes((root / "사진/여행 사진.png").read_bytes())
        preferences = QSettings(str(scratch / "preferences.ini"), QSettings.Format.IniFormat)
        diagnostics.configure_logging(scratch / "logs")

        def shot(widget, filename):
            widget.show()
            app.processEvents()
            if not widget.grab().save(str(destination / filename)):
                raise RuntimeError(f"Screenshot failed: {filename}")

        with patch.object(ui, "QSettings", lambda *_args: preferences), patch.object(ui.updater, "is_auto_update_supported", lambda: False):
            window = ui.MainWindow()
            for timer in (window.update_timer, window.initial_update_timer, window.deferred_update_timer):
                timer.stop()
            window.resize(1306, 700)
            shot(window, "welcome.png")
            diagnostics.log_event("task.started", "예시 폴더를 분석합니다", operation_id="example-scan")
            scan = scan_folder(root)
            window.load_scan_result(scan)
            window.folder_input.setText("예시 폴더 / Downloads")
            diagnostics.log_event("task.completed", "예시 파일 분석 완료", operation_id="example-scan", details={"files": len(scan.files)})
            shot(window, "screenshot.png")
            window._load_groups(find_duplicates(scan.files), "duplicate")
            shot(window, "comparison.png")
            help_window = HelpCenterDialog(window, topic="duplicates")
            help_window.resize(1040, 660)
            shot(help_window, "help.png")
            help_window.close()
            rule_window = ClassificationDialog(default_rules("type"), window)
            rule_window.resize(1020, 680)
            shot(rule_window, "classification.png")
            rule_window.close()
            copied = execute_plan(build_plan(root, [str(root / "보고서/업무 보고서.md")], "copy", destination=str(scratch / "Copies")))
            diagnostics.log_event("operation.result", "예시 문서의 복사 작업을 완료했습니다", operation_id=copied["id"], details={"status": copied["status"], "items": 1})
            history_window = HistoryDialog(list_history(root), window)
            history_window.resize(1000, 620)
            shot(history_window, "history.png")
            history_window.close()
            log_window = LogViewerDialog(window)
            log_window.resize(1000, 650)
            shot(log_window, "logs.png")
            log_window.close()
            window.close()
            app.processEvents()
        diagnostics.shutdown_logging()
    print(f"Saved seven actual Qt application captures to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs")
    capture(parser.parse_args().output)
