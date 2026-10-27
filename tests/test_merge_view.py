"""The merge dialog is read-only until confirmed plans go to the main worker."""
from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from folder_organizer.merge_view import MergeDialog
from folder_organizer.merging import build_merge_preview


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    paths = [root / "보고서.txt", root / "보고서 수정.txt"]
    paths[0].write_text("공통\n\n처음 내용", encoding="utf-8")
    paths[1].write_text("공통\n\n추가 내용", encoding="utf-8")
    return root, list(map(str, paths))


def wait_preview(app, dialog):
    deadline = time.monotonic() + 10
    while dialog._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert dialog._worker is None


def test_background_preview_requires_confirmation_and_emits_python_plan_only(app, sources):
    root, paths = sources
    dialog = MergeDialog(root, paths)
    emitted = []
    dialog.plan_ready.connect(emitted.append)
    assert not dialog.move_originals.isChecked() and not dialog.execute_button.isEnabled()
    dialog._preview()
    assert not dialog.execute_button.isEnabled()
    wait_preview(app, dialog)
    assert dialog.plan is not None and not Path(dialog.plan["output"]).exists()
    assert "추가 내용" in dialog.content.toPlainText() and dialog.plan["removed_units"] == 1
    dialog._submit()
    assert not emitted and dialog.result() != QDialog.DialogCode.Accepted
    dialog.confirm.setChecked(True)
    assert dialog.execute_button.isEnabled()
    dialog._submit()
    assert emitted == [dialog.plan] and emitted[0] is dialog.plan
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert all(Path(path).exists() for path in paths)
    assert not Path(dialog.plan["output"]).exists()


def test_config_edit_invalidates_old_confirmed_preview(app, sources):
    root, paths = sources
    dialog = MergeDialog(root, paths)
    dialog._preview()
    wait_preview(app, dialog)
    dialog.confirm.setChecked(True)
    dialog.move_originals.setChecked(True)
    assert dialog.plan is None and not dialog.confirm.isChecked() and not dialog.execute_button.isEnabled()
    dialog._preview()
    wait_preview(app, dialog)
    assert dialog.plan["move_originals"] and not dialog.execute_button.isEnabled()
    assert "검증 후 원본" in dialog.summary.text()
    dialog.output_name.setText("새이름.txt")
    assert dialog.plan is None and not dialog.execute_button.isEnabled()
    dialog.close()


def test_failed_preview_never_enables_execution_or_mutates_files(app, sources):
    root, paths = sources
    dialog = MergeDialog(root, paths)
    dialog.output_name.setText("../unsafe.txt")
    dialog._preview()
    wait_preview(app, dialog)
    assert dialog.plan is None and not dialog.execute_button.isEnabled() and not dialog.confirm.isEnabled()
    assert "미리보기를 만들지 못했습니다" in dialog.summary.text()
    assert len(list(root.iterdir())) == 2
    dialog.close()


def test_large_inode_plan_crosses_object_signal_without_qvariant_conversion(app, sources):
    root, paths = sources
    plan = build_merge_preview(root, paths)
    plan["sources"][0]["inode"] = 2**100
    dialog = MergeDialog(root, paths)
    dialog._preview_ready(plan)
    dialog._set_busy(False)
    dialog.confirm.setChecked(True)
    emitted = []
    dialog.plan_ready.connect(emitted.append)
    dialog._submit()
    assert emitted[0] is plan and emitted[0]["sources"][0]["inode"] == 2**100
