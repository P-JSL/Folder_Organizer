from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QApplication

from folder_organizer import diagnostics, log_view


@pytest.fixture
def viewer(tmp_path):
    app = QApplication.instance() or QApplication([])
    diagnostics.configure_logging(tmp_path / "logs")
    diagnostics.log_event("analysis_start", "한글 분석 시작", operation_id="scan-123")
    diagnostics.log_event("analysis_error", "파일이 변경되었습니다", operation_id="scan-123", level="ERROR")
    widget = log_view.LogViewerDialog()
    yield app, widget
    widget.close()
    widget.deleteLater()
    app.processEvents()
    diagnostics.shutdown_logging()


def test_log_viewer_filters_and_details_use_plain_python_records(viewer):
    app, widget = viewer
    assert widget.model.rowCount() == 2
    assert widget.model.rowCount(widget.model.index(0, 0)) == 0
    assert widget.model.columnCount(widget.model.index(0, 0)) == 0
    assert widget.model.data(QModelIndex()) is None
    widget.level_filter.setCurrentIndex(widget.level_filter.findData("ERROR"))
    app.processEvents()
    assert widget.model.rowCount() == 1
    widget.table.selectRow(0)
    app.processEvents()
    assert "scan-123" in widget.details.toPlainText()
    widget.level_filter.setCurrentIndex(0)
    widget.search_input.setText("한글")
    widget.refresh_logs()
    assert widget.model.rowCount() == 1 and widget.model.records[0]["event"] == "analysis_start"


def test_log_viewer_exports_visible_records_to_exclusive_file(viewer, tmp_path, monkeypatch):
    _app, widget = viewer
    destination = tmp_path / "visible.jsonl"
    monkeypatch.setattr(log_view.QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
    widget.export_visible()
    assert destination.exists() and "한글 분석 시작" in destination.read_text(encoding="utf-8")
    warnings = []
    monkeypatch.setattr(log_view.QMessageBox, "warning", lambda *_args: warnings.append(True))
    widget.export_visible()
    assert warnings
