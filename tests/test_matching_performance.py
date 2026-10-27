"""Read-reduction regressions retain full-byte proof and current snapshots."""
from contextlib import contextmanager
import os
from pathlib import Path
from threading import Event

import pytest

from folder_organizer import matching
from folder_organizer.scanner import scan_folder


def test_large_different_samples_avoid_full_hashing_but_real_copies_are_verified(tmp_path, monkeypatch):
    size = 2 * 1024 * 1024
    for index in range(12):
        (tmp_path / f"unique-{index:02}.bin").write_bytes(bytes([index]) * size)
    duplicate = tmp_path / "real-copy.bin"
    duplicate.write_bytes((tmp_path / "unique-00.bin").read_bytes())
    real_digest, real_equal = matching._digest, matching._equal
    full_hashes, byte_checks = [], []

    def digest(entry, cancel):
        full_hashes.append(entry.name)
        return real_digest(entry, cancel)

    def equal(first, second, cancel):
        byte_checks.append((first.name, second.name))
        return real_equal(first, second, cancel)

    monkeypatch.setattr(matching, "_digest", digest)
    monkeypatch.setattr(matching, "_equal", equal)
    group, = matching.find_duplicates(scan_folder(tmp_path).files)
    assert {Path(path).name for path in group.paths} == {"unique-00.bin", "real-copy.bin"}
    assert set(full_hashes) == {"unique-00.bin", "real-copy.bin"}
    assert len(byte_checks) == 1 and group.evidence["byte_identical"] is True


def test_equal_samples_and_fabricated_full_hash_cannot_prove_duplicate(tmp_path, monkeypatch):
    payload = bytearray(2 * 1024 * 1024)
    (tmp_path / "first.bin").write_bytes(payload)
    payload[400_000] = 1  # Outside first/middle/last 64KiB samples.
    (tmp_path / "second.bin").write_bytes(payload)
    files = scan_folder(tmp_path).files
    assert matching._sample_digest(files[0], None) == matching._sample_digest(files[1], None)
    assert matching.find_duplicates(files) == []
    monkeypatch.setattr(matching, "_digest", lambda *_args: "fabricated full-hash collision")
    assert matching.find_duplicates(files) == []


def test_sampling_read_budget_is_three_small_reads_per_large_file(tmp_path, monkeypatch):
    size = 2 * 1024 * 1024
    for index in range(8):
        (tmp_path / f"different-{index}.bin").write_bytes(bytes([index]) * size)
    real_open = matching._open_snapshot
    read_bytes = 0

    class CountReads:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, attribute):
            return getattr(self.stream, attribute)

        def read(self, *args):
            nonlocal read_bytes
            data = self.stream.read(*args)
            read_bytes += len(data)
            return data

    @contextmanager
    def measured_open(entry):
        with real_open(entry) as stream:
            yield CountReads(stream)

    monkeypatch.setattr(matching, "_open_snapshot", measured_open)
    assert matching.find_duplicates(scan_folder(tmp_path).files) == []
    assert read_bytes == 8 * 3 * matching.SAMPLE_BYTES
    assert read_bytes < 0.1 * 8 * size


def text_fixture(tmp_path):
    first = "This shared document has enough words and characters for safe text similarity comparison. alpha beta gamma " * 3
    (tmp_path / "apple.txt").write_bytes(first.encode())
    (tmp_path / "zebra.txt").write_bytes((first + " newer revision").encode())
    return scan_folder(tmp_path).files


def test_session_cache_reuses_text_features_but_never_duplicate_hashes(tmp_path, monkeypatch):
    files = text_fixture(tmp_path)
    cache = matching.AnalysisCache()
    calls = []
    real_signature = matching._text_signature

    def signature(entry, limit):
        calls.append(entry.path)
        return real_signature(entry, limit)

    monkeypatch.setattr(matching, "_text_signature", signature)
    first_report, repeated_report = {}, {}
    first = matching.find_similar(files, methods=("text",), cache=cache, report=first_report)
    second = matching.find_similar(files, methods=("text",), cache=cache, report=repeated_report)
    assert first and [group.to_dict() for group in first] == [group.to_dict() for group in second]
    assert len(calls) == 2
    assert first_report["feature_cache"]["misses"] == 2
    assert repeated_report["feature_cache"]["misses"] == 0
    assert repeated_report["feature_cache"]["hits"] == 2
    assert cache.bytes_used <= cache.max_bytes


def test_repeated_same_size_analysis_still_streams_full_hashes(tmp_path, monkeypatch):
    first = "This shared paragraph is long enough and has sufficiently many distinct words for comparison. " * 3
    (tmp_path / "apple.txt").write_bytes(first.encode())
    (tmp_path / "zebra.txt").write_bytes((first[:-6] + "other ").encode())
    files = scan_folder(tmp_path).files
    assert files[0].size == files[1].size
    cache, calls = matching.AnalysisCache(), []
    real_digest = matching._digest

    def digest(entry, cancel):
        calls.append(entry.path)
        return real_digest(entry, cancel)

    monkeypatch.setattr(matching, "_digest", digest)
    for _ in range(2):
        assert matching.find_similar(files, methods=("text",), cache=cache)
    assert len(calls) == 4


def test_content_only_profile_does_not_hash_or_pair_unsupported_files(tmp_path, monkeypatch):
    for index in range(20):
        (tmp_path / f"program-{index}.exe").write_bytes(b"x" * 2_000)
    report = {}

    def should_not_hash(*_args):
        raise AssertionError("Content-only image profile hashed an executable")

    monkeypatch.setattr(matching, "_digest", should_not_hash)
    assert matching.find_similar(scan_folder(tmp_path).files, methods=("image",), report=report) == []
    assert report["compared_pairs"] == 0 and report["unsupported_files"] == 20
    assert any("not hashed or paired" in message for message in report["warnings"])


def test_same_size_restored_mtime_edit_invalidates_cached_feature(tmp_path, monkeypatch):
    files = text_fixture(tmp_path)
    cache = matching.AnalysisCache()
    original = files[0]
    calls = []

    def loader(entry):
        calls.append(entry.path)
        return Path(entry.path).read_bytes()

    old = cache.get(original, "test", loader)
    old_info = Path(original.path).stat()
    Path(original.path).write_bytes(b"X" * len(old))
    os.utime(original.path, ns=(old_info.st_atime_ns, old_info.st_mtime_ns))
    new = cache.get(original, "test", loader)
    assert old != new and len(calls) == 2


def test_feature_cache_remains_bounded_and_can_be_cleared(tmp_path):
    files = text_fixture(tmp_path)
    cache = matching.AnalysisCache(max_bytes=2_000, max_entries=1)
    for entry in files:
        cache.get(entry, "fixture", lambda _entry: (1, 2, 3))
        assert cache.bytes_used <= 2_000 and len(cache._values) <= 1
    cache.clear()
    assert cache.bytes_used == 0 and cache.hits == cache.misses == 0


def test_more_than_old_lru_limit_decodes_each_small_image_once(tmp_path, monkeypatch):
    from PIL import Image
    # Different file lengths avoid the exact-byte pass being the measured work.
    for index in range(135):
        filename = tmp_path / f"photo-{index:03}.png"
        Image.new("RGB", (4, 4), (index, 10, 20)).save(filename)
        with filename.open("ab") as output:
            output.write(b"\0" * index)
    calls = 0
    real_signature = matching._image_signature

    def signature(entry):
        nonlocal calls
        calls += 1
        return real_signature(entry)

    monkeypatch.setattr(matching, "_image_signature", signature)
    matching.find_similar(scan_folder(tmp_path).files, methods=("image",), max_pairs=10_000)
    assert calls == 135


def test_structured_progress_has_real_phase_totals_and_terminal_event(tmp_path):
    files = text_fixture(tmp_path)
    events = []
    report = {}
    matching.find_similar(files, methods=("text",), progress_detail=events.append, report=report)
    assert {"duplicate_index", "duplicate_hash", "duplicate_verify", "similar_inventory", "similar_pairs"} <= {event["phase"] for event in events}
    for event in events:
        assert set(event) >= {"phase", "completed", "total", "unit", "message", "terminal"}
        assert event["completed"] >= 0
        assert event["total"] is None or event["completed"] <= event["total"]
    assert events[-1]["terminal"] and events[-1]["phase"] == "similar_complete"
    assert events[-1]["completed"] == events[-1]["total"] == 1


def test_structured_progress_can_cancel_sampling_without_partial_groups(tmp_path):
    for name in ("first.bin", "second.bin"):
        (tmp_path / name).write_bytes(b"x" * (2 * 1024 * 1024))
    event, details = Event(), []

    def callback(detail):
        details.append(detail)
        if detail["phase"] == "duplicate_sample" and detail["completed"]:
            event.set()

    assert matching.find_duplicates(scan_folder(tmp_path).files, cancel=event, progress_detail=callback) == []
    assert details[-1]["phase"] == "duplicate_cancelled" and details[-1]["terminal"]


@pytest.mark.parametrize("max_bytes,max_entries", [(0, 1), (1, 0), (True, 1)])
def test_cache_rejects_invalid_bounds(max_bytes, max_entries):
    with pytest.raises(ValueError):
        matching.AnalysisCache(max_bytes=max_bytes, max_entries=max_entries)
