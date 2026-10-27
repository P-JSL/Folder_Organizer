from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from folder_organizer import diagnostics
from folder_organizer.progress import ProgressTracker
from folder_organizer.scanner import scan_folder


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = tmp_path / "logs"
    assert diagnostics.configure_logging(folder) == folder
    yield folder
    diagnostics.shutdown_logging()


def test_utf8_records_are_thread_safe_and_filterable(logs):
    def write(number):
        diagnostics.log_event("작업_완료", f"파일 분석 {number}개 완료", operation_id=f"task-{number}",
                              level="WARNING" if number % 2 else "INFO", details={"count": number})
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(60)))
    records = diagnostics.read_logs()
    assert len(records) == 60
    assert {record["details"]["count"] for record in records} == set(range(60))
    assert len(diagnostics.read_logs(level="warning")) == 30
    assert len(diagnostics.read_logs(query="task-47")) == 1
    assert "파일 분석" in (logs / diagnostics.LOG_NAME).read_text(encoding="utf-8")


def test_rotation_bounds_retention_and_reader_returns_newest(logs):
    diagnostics.configure_logging(logs, max_bytes=1024, backup_count=2)
    for number in range(90):
        diagnostics.log_event("analysis", f"완료 {number}", details={"number": number, "reason": "x" * 80})
    files = list(logs.glob("organizer.log*"))
    assert len(files) == 3
    assert all(path.stat().st_size < 1100 for path in files)
    records = diagnostics.read_logs()
    numbers = [record["details"]["number"] for record in records]
    assert numbers[0] == 89
    assert numbers == sorted(numbers, reverse=True)


def test_damaged_and_deep_lines_do_not_hide_valid_logs_or_crash_reader(logs):
    diagnostics.log_event("valid", "정상 기록")
    with (logs / diagnostics.LOG_NAME).open("ab") as stream:
        stream.write(b"not json\n" + b"[" * 4000 + b"0" + b"]" * 4000 + b"\n")
        stream.write(b"x" * (diagnostics.MAX_RECORD_BYTES + 1) + b"\n")
    records = diagnostics.read_logs()
    assert len(records) == 1 and records[0]["event"] == "valid"


def test_reader_has_byte_budget_not_only_record_limit(logs, monkeypatch):
    diagnostics.log_event("old", "이전 기록")
    for number in range(12):
        diagnostics.log_event("new", "x" * 100, details={"number": number})
    monkeypatch.setattr(diagnostics, "MAX_READ_BYTES", 550)
    records = diagnostics.read_logs(limit=1000)
    assert 0 < len(records) <= 2
    assert all(record["event"] == "new" for record in records)
    assert records[0]["details"]["number"] == 11


def test_sensitive_keys_and_common_credential_strings_are_redacted(logs):
    recursive = {}
    recursive["self"] = recursive
    diagnostics.log_event("failure", "token=secretvalue Authorization: Bearer abc.def", level="ERROR",
                          details={"api_key": "never-store-me", "nested": {"password": "never-store-this"}, "recursive": recursive})
    content = (logs / diagnostics.LOG_NAME).read_text(encoding="utf-8")
    assert "secretvalue" not in content and "never-store" not in content and "abc.def" not in content
    assert "[가림]" in content and "세부 항목 생략" in content


def test_logging_failure_is_nonfatal_and_explained(logs, monkeypatch):
    def failed(*_args, **_kwargs):
        raise OSError("no writable storage")
    monkeypatch.setattr(diagnostics, "_NonfatalHandler", failed)
    assert diagnostics.configure_logging(logs / "blocked") is None
    diagnostics.log_event("failure", "앱의 파일 작업은 계속됩니다")
    assert diagnostics.logging_error()


def test_log_export_is_utf8_exclusive_and_matches_filters(logs, tmp_path):
    diagnostics.log_event("scan", "한글 분석 기록", operation_id="analysis-1")
    destination = tmp_path / "export.jsonl"
    diagnostics.export_logs(destination, diagnostics.read_logs(query="analysis-1"))
    record = json.loads(destination.read_text(encoding="utf-8"))
    assert record["message"] == "한글 분석 기록"
    with pytest.raises(FileExistsError):
        diagnostics.export_logs(destination)
    assert json.loads(destination.read_text(encoding="utf-8"))["event"] == "scan"


def test_oversize_export_is_rejected_before_file_creation(logs, tmp_path, monkeypatch):
    destination = tmp_path / "too-large.jsonl"
    monkeypatch.setattr(diagnostics, "MAX_READ_BYTES", 20)
    with pytest.raises(ValueError, match="8 MB"):
        diagnostics.export_logs(destination, [{"message": "x" * 100}])
    assert not destination.exists()


def test_linked_log_paths_and_export_parents_are_rejected(logs, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Symbolic links are not available")
    assert diagnostics.configure_logging(link) is None
    with pytest.raises(OSError):
        diagnostics.export_logs(link / "export.jsonl", [])
    assert not (real / "export.jsonl").exists()


def test_exception_hooks_log_then_chain_and_restore(logs, monkeypatch):
    called = []
    previous_sys = lambda *_args: called.append("sys")
    previous_thread = lambda _args: called.append("thread")
    monkeypatch.setattr(sys, "excepthook", previous_sys)
    monkeypatch.setattr(threading, "excepthook", previous_thread)
    restore = diagnostics.install_exception_hook()
    try:
        try:
            raise ValueError("설명 가능한 오류")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
            threading.excepthook(SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=exc.__traceback__))
        assert called == ["sys", "thread"]
        records = diagnostics.read_logs(level="ERROR")
        assert len(records) == 2 and records[0]["details"]["exception_type"] == "ValueError"
        assert records[0]["details"]["frames"]
    finally:
        restore()
    assert sys.excepthook is previous_sys and threading.excepthook is previous_thread


def test_crash_logging_preserves_existing_faulthandler(logs, monkeypatch):
    monkeypatch.setattr(diagnostics.faulthandler, "is_enabled", lambda: True)
    monkeypatch.setattr(diagnostics.faulthandler, "enable", lambda **_kwargs: pytest.fail("existing handler replaced"))
    assert diagnostics.enable_crash_logging() is None
    assert not list(logs.glob("crash-*.log"))


def test_progress_unknown_total_is_indeterminate_then_completes_honestly():
    clock = [10.0]
    tracker = ProgressTracker(lambda: clock[0])
    start = tracker.update({"phase": "scan", "completed": 0, "total": None, "unit": "파일"})
    assert start.fraction is None and start.eta_seconds is None
    clock[0] = 12.0
    scanning = tracker.update({"phase": "scan", "completed": 30, "total": None})
    assert scanning.fraction is None and scanning.completed == 30 and scanning.elapsed == 2
    finish = tracker.update({"phase": "scan", "completed": 30, "total": 30, "terminal": True})
    assert finish.fraction == 1 and finish.eta_seconds == 0


def test_progress_eta_counters_and_phase_reset_are_measured():
    clock = [0.0]
    tracker = ProgressTracker(lambda: clock[0])
    tracker.update({"phase": "hash", "completed": 0, "total": 10})
    clock[0] = 2.0
    hashed = tracker.update({"phase": "hash", "completed": 2, "total": 10})
    assert hashed.fraction == .2 and hashed.eta_seconds == 8
    assert tracker.update({"phase": "hash", "completed": 1, "total": 10}).completed == 2
    verified = tracker.update({"phase": "verify", "completed": 1, "total": 3})
    assert verified.completed == 1 and verified.elapsed == 2 and verified.eta_seconds is None
    invalid = tracker.update({"phase": "verify", "completed": -9, "total": float("nan")})
    assert invalid.total is None and invalid.completed == 0


def test_cancelled_progress_never_claims_completion():
    tracker = ProgressTracker()
    result = tracker.update({"phase": "scan", "completed": 2, "total": None, "terminal": True, "cancelled": True})
    assert result.cancelled and result.terminal and result.fraction is None


def test_scanner_reports_counts_and_bytes_without_a_fake_discovery_total(tmp_path):
    (tmp_path / "one.txt").write_bytes(b"123")
    (tmp_path / "two.txt").write_bytes(b"12345")
    (tmp_path / "skip.tmp").write_bytes(b"no")
    events = []
    result = scan_folder(tmp_path, exclude_patterns=["*.tmp"], progress_detail=events.append)
    assert all(event["total"] is None for event in events[:-1])
    assert [event["completed"] for event in events] == [0, 1, 2, 2]
    final = events[-1]
    assert final["total"] == 2 and final["bytes"] == result.total_size == 8
    assert final["terminal"] and not final["cancelled"] and final["filtered_files"] == 1


def test_scanner_cancelled_final_event_does_not_report_a_known_total(tmp_path):
    (tmp_path / "one.txt").write_bytes(b"1")
    cancel = threading.Event()
    cancel.set()
    events = []
    result = scan_folder(tmp_path, cancel=cancel, progress_detail=events.append)
    assert result.cancelled and events[-1]["cancelled"]
    assert events[-1]["total"] is None and events[-1]["completed"] == 0
