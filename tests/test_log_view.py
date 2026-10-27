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


def test_plain_explanation_is_default_and_technical_fields_need_opt_in(viewer):
    app, widget = viewer
    diagnostics.log_event("operation.result", "파일 작업 결과", level="WARNING",
                          operation_id="operation-456", details={"status": "partial", "items": 3})
    widget.refresh_logs()
    widget.table.selectRow(0)
    app.processEvents()
    assert "파일 정리 결과" in widget.details.toPlainText()
    assert "일부 처리" in widget.details.toPlainText()
    assert "파일 수: 3" in widget.details.toPlainText()
    assert '"operation_id"' not in widget.details.toPlainText()
    assert not widget.technical_toggle.isChecked()
    assert widget.technical_details.isHidden()
    widget.technical_toggle.setChecked(True)
    assert not widget.technical_details.isHidden()
    assert '"operation_id": "operation-456"' in widget.technical_details.toPlainText()
    widget.technical_toggle.setChecked(False)
    assert widget.technical_details.isHidden() and not widget.technical_details.toPlainText()


def test_search_understands_translated_visible_activity_labels(viewer):
    _app, widget = viewer
    diagnostics.log_event("task.started", "폴더 확인", operation_id="translated-search")
    widget.search_input.setText("작업 시작")
    widget.refresh_logs()
    assert widget.model.rowCount() == 1
    assert widget.model.records[0]["event"] == "task.started"
    assert widget.model.data(widget.model.index(0, 2)) == "작업 시작"


def test_empty_filtered_log_explains_how_to_continue(viewer):
    _app, widget = viewer
    widget.search_input.setText("no-such-activity")
    widget.refresh_logs()
    assert widget.model.rowCount() == 0
    assert "검색어와 구분 필터" in widget.details.toPlainText()
    assert not widget.export_button.isEnabled()
