"""Capture the real desktop UI with disposable example files and preferences.

No personal folder, installed preference or live update is used. Run after
installing project dependencies, with QT_QPA_PLATFORM=offscreen when headless.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog

from folder_organizer import diagnostics, ui
from folder_organizer.execution_log import configure_execution_logging, shutdown_execution_logging
from folder_organizer.execution_log_view import ExecutionLogDialog
from folder_organizer.merge_view import MergeDialog
from folder_organizer.merging import execute_merge
from folder_organizer.help_view import HelpCenterDialog
from folder_organizer.history_view import HistoryDialog
from folder_organizer.log_view import LogViewerDialog
from folder_organizer.overview_view import InventoryOverviewDialog
from folder_organizer.matching import find_duplicates
from folder_organizer.operations import build_plan, execute_plan, list_history, compaction_preview, compact_operation
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
        configure_execution_logging(scratch / "logs")

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
            window.set_theme("light")
            shot(window, "screenshot-light.png")
            window.set_theme("dark")
            overview_window = InventoryOverviewDialog(scan, window)
            overview_window.resize(1040, 680)
            shot(overview_window, "overview.png")
            overview_window.close()
            window._load_groups(find_duplicates(scan.files), "duplicate")
            shot(window, "comparison.png")
            window.set_theme("light")
            shot(window, "comparison-light.png")
            window.set_theme("dark")
            shot(window.organize_menu, "menu.png")
            window.organize_menu.close()
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
            # Real preview and real merge in disposable files; no personal files.
            first = root / "보고서/업무 보고서.md"
            second = root / "보고서/업무 보고서 수정.md"
            first.write_text("# 10월 업무 보고\n\n공통: 제품 검토를 완료했습니다.\n\n첫 문서: 사용자 피드백을 수집했습니다.\n", encoding="utf-8")
            second.write_text("# 10월 업무 보고\n\n공통: 제품 검토를 완료했습니다.\n\n추가 문서: 다음 달 개선 일정을 확정했습니다.\n", encoding="utf-8")
            merge_window = MergeDialog(root, [str(first), str(second)], window)
            merge_window.resize(1040, 700)
            merge_window._preview()
            deadline = time.monotonic() + 10
            while merge_window._worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.005)
            if merge_window._worker is not None or merge_window.plan is None:
                raise RuntimeError("Merge preview did not finish")
            shot(merge_window, "merge.png")
            execute_merge(merge_window.plan)
            merge_window.close()
            # Exercise the real immediate keep-first path, with no modal
            # confirmation or new folder traversal after pressing the button.
            cleanup_root = scratch / "CleanupExample"
            cleanup_root.mkdir()
            payload = ("복원 검증용 원본 문서입니다.\n" * 50_000).encode("utf-8")
            original = cleanup_root / "01 남길 원본.txt"
            copy = cleanup_root / "02 삭제할 사본.txt"
            original.write_bytes(payload)
            copy.write_bytes(payload)
            cleanup_scan = scan_folder(cleanup_root)
            window.load_scan_result(cleanup_scan)
            window.folder_input.setText("예시 폴더 / CleanupExample")
            window._load_groups(find_duplicates(cleanup_scan.files), "duplicate")
            window.keep_first_delete()
            deadline = time.monotonic() + 10
            while window._worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.005)
            if window._worker is not None or not original.exists() or copy.exists():
                raise RuntimeError("Immediate duplicate cleanup did not finish safely")
            shot(window, "cleanup-result.png")
            record = list_history(cleanup_root)[0]
            compact_plan = compaction_preview(cleanup_root, record["id"])
            def preview_only(dialog):
                shot(dialog, "compaction.png")
                return QDialog.DialogCode.Rejected
            with patch.object(QDialog, "exec", preview_only):
                window._show_compaction_preview(compact_plan)
            optimized = compact_operation(cleanup_root, record["id"], plan=compact_plan)
            if not optimized["recovery_compaction"]["processed"]:
                raise RuntimeError("Recovery payload example was not compacted")
            recovery_history = HistoryDialog(list_history(cleanup_root), window)
            recovery_history.resize(1040, 680)
            shot(recovery_history, "recovery-history.png")
            recovery_history.close()
            execution_window = ExecutionLogDialog(window)
            execution_window.resize(1040, 680)
            shot(execution_window, "execution-logs.png")
            execution_window.close()
            window.close()
            app.processEvents()
        diagnostics.shutdown_logging()
        shutdown_execution_logging()
    print(f"Saved actual Qt application captures to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs")
    capture(parser.parse_args().output)
