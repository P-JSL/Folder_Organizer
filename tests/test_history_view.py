"""History stays flat at the Qt boundary, including large file journals."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication

from folder_organizer.history_view import (
    HistoryDialog, HistoryTableModel, MAX_DETAIL_ITEMS, MAX_HISTORY_ROWS,
    history_details,
    restore_effect,
)
from folder_organizer.operations import build_plan, execute_plan, list_history, undo_operation


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def record(number=1, **updates):
    return {"id": f"{number:032x}", "mode": "quarantine", "status": "completed",
            "created_at": "2026-10-27T10:00:00+00:00", "root": "/fixture",
            "items": [{"source": "계약서.txt", "target": "안전 보관/계약서.txt",
                       "state": "completed", "inode": 2**100}], **updates}


def test_real_journal_selection_emits_only_id_and_preserves_backend_restore(app, tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "계약서.txt"
    source.write_bytes(b"retained original")
    journal = execute_plan(build_plan(root, [str(source)], "quarantine"))
    assert not source.exists()
    dialog = HistoryDialog(list_history(root))
    requested = []
    dialog.undo_requested.connect(requested.append)
    dialog.show()
    app.processEvents()
    assert dialog.selected_operation_id() == journal["id"]
    assert dialog.undo_button.isEnabled()
    assert "계약서.txt" in dialog.details.toPlainText()
    assert "delete_files" in dialog.details.toPlainText()
    assert "original_backup" not in journal["items"][0]
    dialog.undo_button.click()
    assert requested == [journal["id"]]
    # Viewing/requesting a restore never performs filesystem work itself.
    assert not source.exists()
    assert undo_operation(root, requested[0])["status"] == "undone"
    assert source.read_bytes() == b"retained original"
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_journal_containers_never_cross_qt_model_boundary(app):
    history = [record(1), record(2, status="partial_undo")]
    # An unexpectedly nested/cyclic extension is irrelevant to displayed fields.
    history[0]["extension_metadata"] = history
    model = HistoryTableModel(history)
    assert model.rowCount() == 2 and model.columnCount() == 5
    child = model.index(0, 0)
    assert model.rowCount(child) == 0 and model.columnCount(child) == 0
    for row in range(model.rowCount()):
        for column in range(model.columnCount()):
            index = model.index(row, column)
            assert isinstance(model.data(index, Qt.ItemDataRole.DisplayRole), str)
            assert isinstance(model.data(index, Qt.ItemDataRole.ToolTipRole), str)
            assert model.data(index, Qt.ItemDataRole.UserRole) is None
    assert model.data(QModelIndex()) is None


def test_large_details_are_bounded_and_do_not_recurse(app):
    nested = record()
    nested["items"] = [nested]  # Not a normal backend journal; rendering must stay safe.
    assert "지원하지 않는 값" not in history_details(nested)
    many = record(items=[{"source": "x" * 10000, "target": "y", "state": "completed"}]
                  * (MAX_DETAIL_ITEMS + 20))
    text = history_details(many)
    assert f"처음 {MAX_DETAIL_ITEMS:,}개" in text
    assert "표시 생략" in text and len(text) < 600_000
    model = HistoryTableModel([record(i) for i in range(1, MAX_HISTORY_ROWS + 3)])
    assert model.rowCount() == MAX_HISTORY_ROWS
    assert model.total_records == MAX_HISTORY_ROWS + 2


def test_copy_and_move_restore_explain_different_file_effects(app):
    copied = record(mode="copy", items=[{"action": "copy", "source": "원본.txt", "target": "사본.txt"}])
    moved = record(mode="rules", items=[{"action": "move", "source": "원래.txt", "target": "분류됨.txt"}])
    assert "원본은 그대로" in restore_effect(copied)
    assert "앱이 만든 사본" in history_details(copied)
    assert "원래 위치로 돌려" in restore_effect(moved)
    assert "일부 파일만 처리" in history_details(record(status="partial"))
    assert "충돌하는 파일은 보존" in history_details(record(status="partial_undo"))


def test_disabled_restore_explains_why_and_empty_history_explains_first_step(app):
    dialog = HistoryDialog([record(status="undone"), record(2, items=[])])
    assert not dialog.undo_button.isEnabled()
    assert "이미 되돌린" in dialog.restore_hint.text()
    dialog.table.selectRow(1)
    app.processEvents()
    assert "파일 목록이 없는" in dialog.restore_hint.text()
    dialog.close()
    empty = HistoryDialog([])
    assert "분석만으로는 기록이 생기지" in empty.summary_label.text()
    assert not empty.undo_button.isEnabled()
    empty.close()


@pytest.mark.parametrize("history", [
    [], [None], [record(id="../bad")], [record(items=None)],
    [record(status="undone")], [record(), record()],
])
def test_empty_malformed_or_completed_restores_do_not_enable_restore(app, history):
    dialog = HistoryDialog(history)
    dialog.show()
    app.processEvents()
    assert not dialog.undo_button.isEnabled()
    assert dialog.selected_operation_id() is None
    requested = []
    dialog.undo_requested.connect(requested.append)
    dialog._request_undo()
    assert requested == []
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_large_history_repeated_open_and_selection_does_not_crash_native_qt(tmp_path):
    # A subprocess makes a native stack overflow a failing test instead of
    # terminating the entire suite, and runs on Windows CI with the real Qt.
    script = """
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from folder_organizer.history_view import HistoryDialog
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
items = [{'source': '한글/file.txt', 'target': 'archive/file.txt', 'state': 'completed',
          'inode': 2**100, 'extra': {'nested': [1, 2, 3]}} for _ in range(300)]
history = [{'id': f'{i:032x}', 'mode': 'quarantine', 'status': 'completed',
            'created_at': '2026-10-27T10:00:00+00:00', 'items': items} for i in range(1, 1201)]
for repeat in range(15):
    dialog = HistoryDialog(history)
    dialog.show()
    for row in (0, 1199, 500, 2):
        dialog.table.selectRow(row)
        app.processEvents()
        assert dialog.selected_operation_id() == history[row]['id']
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
print('history native stress passed')
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run([sys.executable, "-c", script], env=environment,
                               capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "history native stress passed" in completed.stdout
