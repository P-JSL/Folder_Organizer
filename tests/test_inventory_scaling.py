"""Inventory work grows with visited entries, without pre-sorting listings."""
from pathlib import Path
from threading import Event

import pytest

from folder_organizer import scanner


def _write_many(root, count):
    root.mkdir()
    for number in range(count):
        (root / f"file-{count-number:04}.txt").write_bytes(str(number).encode())


def test_unsorted_inventory_queries_each_file_once_and_scales_linearly(tmp_path):
    _write_many(tmp_path / "small", 200)
    _write_many(tmp_path / "large", 400)
    small = scanner.scan_folder(tmp_path / "small", sort_results=False)
    large = scanner.scan_folder(tmp_path / "large", sort_results=False)
    assert small.scan_metrics["visited_entries"] == 200
    assert large.scan_metrics["visited_entries"] == 400
    assert small.scan_metrics["directories"] == large.scan_metrics["directories"] == 1
    # Windows needs one extra explicit identity query for each DirEntry.
    per_file = 2 if scanner.os.name == "nt" else 1
    assert small.scan_metrics["metadata_reads"] == 200 * per_file + 2
    assert large.scan_metrics["metadata_reads"] == 400 * per_file + 2
    assert not small.scan_metrics["sorted_results"]
    assert large.scan_metrics["elapsed_seconds"] >= 0


def test_unsorted_inventory_matches_sorted_metadata_without_reading_file_contents(tmp_path, monkeypatch):
    _write_many(tmp_path / "data", 12)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("inventory must not read file contents"))
    fast = scanner.scan_folder(tmp_path / "data", sort_results=False)
    ordered = scanner.scan_folder(tmp_path / "data")
    assert {entry.path: entry for entry in fast.files} == {entry.path: entry for entry in ordered.files}
    assert [entry.relative_path for entry in ordered.files] == sorted(entry.relative_path for entry in ordered.files)
    assert ordered.scan_options["sort_results"] is True


def test_progress_is_bounded_before_callback_and_terminal_counts_are_exact(tmp_path, monkeypatch):
    _write_many(tmp_path / "data", 1000)
    # A deterministic clock proves the count limit rather than machine speed.
    monkeypatch.setattr(scanner.time, "monotonic", lambda: 50.0)
    events = []
    result = scanner.scan_folder(tmp_path / "data", sort_results=False, progress_detail=events.append)
    assert events[0]["completed"] == 0 and events[0]["total"] is None
    assert len(events) <= 22
    assert result.scan_metrics["progress_events"] == len(events)
    assert events[-1]["terminal"] and events[-1]["completed"] == events[-1]["total"] == 1000
    assert events[-1]["bytes"] == result.total_size
    assert all(event["total"] is None for event in events[:-1])


def test_cancel_streams_only_until_first_file_and_closes_directory(tmp_path, monkeypatch):
    _write_many(tmp_path / "data", 50)
    real_scandir = scanner.os.scandir
    observed = {"next": 0, "closed": 0}

    class Stream:
        def __init__(self, path):
            self.stream = real_scandir(path)
        def __iter__(self):
            return self
        def __next__(self):
            observed["next"] += 1
            assert observed["next"] <= 2, "directory entries were materialized before cancellation"
            return next(self.stream)
        def close(self):
            self.stream.close()
            observed["closed"] += 1

    monkeypatch.setattr(scanner.os, "scandir", Stream)
    cancel = Event()
    result = scanner.scan_folder(tmp_path / "data", sort_results=False, cancel=cancel, progress=lambda _path: cancel.set())
    assert result.cancelled and len(result.files) == 1
    assert observed["closed"] == 1 and result.scan_metrics["visited_entries"] <= 2


def test_iterator_error_retains_partial_inventory_and_closes_handle(tmp_path, monkeypatch):
    _write_many(tmp_path / "data", 3)
    original = scanner.os.scandir
    observed = []

    class Failing:
        def __init__(self, path):
            self.stream = original(path)
            self.first = True
        def __iter__(self):
            return self
        def __next__(self):
            if not self.first:
                raise PermissionError("directory changed while enumerating")
            self.first = False
            return next(self.stream)
        def close(self):
            self.stream.close()
            observed.append("closed")

    monkeypatch.setattr(scanner.os, "scandir", Failing)
    result = scanner.scan_folder(tmp_path / "data", sort_results=False)
    assert len(result.files) == 1 and observed == ["closed"]
    assert any("changed while enumerating" in warning for warning in result.warnings)


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_invalid_sort_option_fails_before_inventory(tmp_path, value):
    with pytest.raises(ValueError):
        scanner.scan_folder(tmp_path, sort_results=value)
