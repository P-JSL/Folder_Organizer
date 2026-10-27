from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QApplication

from folder_organizer import execution_log as logs
from folder_organizer import execution_log_view as view
from folder_organizer import operations


@pytest.fixture
def receipts(tmp_path):
    directory = tmp_path / "logs"
    assert logs.configure_execution_logging(directory) == directory
    yield directory
    logs.shutdown_execution_logging()


def receipt(number, **kwargs):
    logs.record_execution("execution.item", execution_id=f"작업-{number}", action="copy",
                          status="failed" if number % 2 else "completed",
                          message=f"파일 처리 {number}", source=Path(f"파일{number}.txt"), **kwargs)


def test_execution_receipts_thread_safe_utf8_and_filterable(receipts):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(receipt, range(80)))
    records = logs.read_execution_logs()
    assert len(records) == 80
    assert {record["execution_id"] for record in records} == {f"작업-{number}" for number in range(80)}
    assert len(logs.read_execution_logs(status="failed", action="copy")) == 40
    assert logs.read_execution_logs(query="파일47")[0]["source"] == "파일47.txt"
    assert not (receipts / "organizer.log").exists()


def test_rotation_and_read_budget_bound_retention(receipts, monkeypatch):
    logs.configure_execution_logging(receipts, max_bytes=1024, backup_count=2)
    for number in range(60):
        receipt(number)
    files = list(receipts.glob("executions.jsonl*"))
    assert len(files) == 3
    assert all(path.stat().st_size <= 1024 for path in files)
    records = logs.read_execution_logs()
    assert records[0]["execution_id"] == "작업-59"
    assert len(records) < 60
    monkeypatch.setattr(logs, "MAX_READ_BYTES", 800)
    assert 0 < len(logs.read_execution_logs()) <= 3


def test_reader_ignores_corrupt_oversize_and_nested_values(receipts):
    receipt(0)
    altered = dict(logs.read_execution_logs()[0], source={"huge": {"nested": [1]}})
    with (receipts / logs.LOG_NAME).open("ab") as stream:
        stream.write(b"broken\n" + b"[" * 4000 + b"0" + b"]" * 4000 + b"\n")
        stream.write(b"x" * (logs.MAX_RECORD_BYTES + 1) + b"\n")
        stream.write((json.dumps(altered) + "\n").encode())
    records = logs.read_execution_logs()
    assert len(records) == 2
    assert isinstance(records[0]["source"], str)
    assert records[1]["execution_id"] == "작업-0"


def test_receipt_export_exclusive_and_sensitive_error_redacted(receipts, tmp_path):
    receipt(1, error="token=do-not-share Authorization: Bearer abc.def")
    exported = logs.export_execution_logs(tmp_path / "export.jsonl", logs.read_execution_logs(status="failed"))
    text = exported.read_text(encoding="utf-8")
    assert "do-not-share" not in text and "abc.def" not in text
    assert json.loads(text)["execution_id"] == "작업-1"
    with pytest.raises(FileExistsError):
        logs.export_execution_logs(exported)
    assert exported.read_text(encoding="utf-8") == text


def test_export_budget_validated_before_file_creation(receipts, tmp_path, monkeypatch):
    receipt(1)
    records = logs.read_execution_logs()
    monkeypatch.setattr(logs, "MAX_READ_BYTES", 30)
    target = tmp_path / "oversize.jsonl"
    with pytest.raises(ValueError):
        logs.export_execution_logs(target, records)
    assert not target.exists()


def test_linked_directory_and_backup_are_rejected_nonfatally(receipts, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert logs.configure_execution_logging(linked) is None
    receipt(0)
    assert not list(other.iterdir())
    assert logs.execution_logging_error()
    logs.configure_execution_logging(receipts)
    backup = receipts / f"{logs.LOG_NAME}.1"
    backup.symlink_to(other / "victim")
    receipt(1)
    assert not (other / "victim").exists()
    assert logs.read_execution_logs() == []
    with pytest.raises(OSError):
        logs.export_execution_logs(linked / "export.jsonl", [])


def test_operations_log_start_before_mutation_and_per_file_results(receipts, tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("valuable original", encoding="utf-8")
    plan = operations.build_plan(root, [str(source)], "quarantine")
    assert logs.read_execution_logs() == []  # previews never count as executions
    original = operations._copy_verified
    def copy(*args, **kwargs):
        records = logs.read_execution_logs()
        assert records[-1]["event"] == "execution.started"
        assert source.exists()
        return original(*args, **kwargs)
    monkeypatch.setattr(operations, "_copy_verified", copy)
    result = operations.execute_plan(plan)
    assert result["status"] == "completed"
    records = list(reversed(logs.read_execution_logs()))
    assert [r["event"] for r in records] == ["execution.started", "execution.item", "execution.finished"]
    assert records[1]["source"] == str(source)
    assert records[1]["target"] == result["items"][0]["target"]
    monkeypatch.setattr(operations, "_copy_verified", original)
    assert operations.undo_operation(root, result["id"])["status"] == "undone"
    undo = list(reversed(logs.read_execution_logs(action="undo")))
    assert [r["status"] for r in undo] == ["started", "restored", "undone"]
    assert source.read_text(encoding="utf-8") == "valuable original"


def test_partial_failure_and_rejected_preview_logged_with_originals_safe(receipts, tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    first, second = root / "first.txt", root / "second.txt"
    first.write_bytes(b"original")
    second.write_bytes(b"original")
    plan = operations.build_plan(root, [str(first), str(second)], "copy", str(tmp_path / "output"))
    second.write_bytes(b"changed")
    result = operations.execute_plan(plan)
    assert result["status"] == "partial"
    assert {r["status"] for r in logs.read_execution_logs()} >= {"started", "completed", "failed", "partial"}
    assert second.read_bytes() == b"changed"
    tampered = copy.deepcopy(plan)
    tampered["items"][0]["target"] = str(tmp_path / "outside")
    with pytest.raises(operations.OperationError):
        operations.execute_plan(tampered)
    assert logs.read_execution_logs()[0]["event"] == "execution.failed"
    assert first.read_bytes() == b"original"


def test_logging_backend_exception_does_not_change_operation_or_undo(receipts, tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"original")
    plan = operations.build_plan(root, [str(source)], "quarantine")
    def unavailable(*_args, **_kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(logs, "record_execution", unavailable)
    result = operations.execute_plan(plan)
    assert result["status"] == "completed" and not source.exists()
    assert operations.undo_operation(root, result["id"])["status"] == "undone"
    assert source.read_bytes() == b"original"


def test_log_write_failure_nonfatal_and_visible(receipts, monkeypatch):
    monkeypatch.setattr(logs, "_append", lambda *_args: (_ for _ in ()).throw(OSError("full")))
    receipt(0)
    assert logs.execution_logging_error()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_reader_rejects_nonregular_log_without_blocking(receipts):
    os.mkfifo(receipts / logs.LOG_NAME)
    assert logs.read_execution_logs() == []
    receipt(0)
    assert logs.execution_logging_error()


def test_execution_view_filters_plain_rows_and_readable_details(receipts, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    receipt(0)
    receipt(1, error="파일이 변경되어 보존했습니다")
    widget = view.ExecutionLogDialog()
    try:
        assert widget.model.rowCount() == 2
        assert widget.model.rowCount(widget.model.index(0, 0)) == 0
        assert widget.model.data(QModelIndex()) is None
        widget.status_filter.setCurrentIndex(widget.status_filter.findData("failed"))
        app.processEvents()
        assert widget.model.rowCount() == 1
        assert "파일이 변경되어 보존" in widget.details.toPlainText()
        assert "작업 기록 · 복원" in widget.details.toPlainText()
        widget.status_filter.setCurrentIndex(0)
        widget.search_input.setText("완료")
        widget.refresh_logs()
        assert widget.model.rowCount() == 1 and widget.model.records[0]["status"] == "completed"
        destination = tmp_path / "visible.jsonl"
        monkeypatch.setattr(view.QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
        widget.export_visible()
        assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "completed"
        widget.search_input.setText("no matching file")
        widget.refresh_logs()
        assert widget.model.rowCount() == 0 and not widget.export_button.isEnabled()
        assert "검색어와 필터" in widget.details.toPlainText()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
