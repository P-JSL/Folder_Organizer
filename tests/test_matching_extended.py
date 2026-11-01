"""Fresh comparison reads can reject differences, but never establish equality."""
from pathlib import Path
from threading import Event
import hashlib
import json
import os

import pytest

from folder_organizer import matching
from folder_organizer.scanner import scan_folder


def same_size_revisions(root: Path, count=3):
    shared = (b"Shared paragraph words are enough to compare content and safely keep originals. " * 3)[:147]
    for index in range(count):
        (root / f"report ({index + 1}).txt").write_bytes(shared + bytes((65 + index,)))
    files = scan_folder(root).files
    assert all(entry.size == 148 for entry in files)
    return files


def contents(files):
    return {entry.path: hashlib.sha256(Path(entry.path).read_bytes()).hexdigest() for entry in files}


def test_current_hash_differences_remove_all_repeated_pair_reads(tmp_path, monkeypatch):
    files = same_size_revisions(tmp_path)
    before = contents(files)
    byte_pairs = []
    original = matching._equal

    def measured(first, second, cancel):
        byte_pairs.append((first.path, second.path))
        return original(first, second, cancel)

    monkeypatch.setattr(matching, "_equal", measured)
    report = {}
    groups = matching.find_similar(files, methods=("name", "text"), report=report)
    assert len(groups) == 3
    assert byte_pairs == []
    assert report["read_hint_files"] == report["exact_difference_hints"] == 3
    assert report["byte_comparison_pairs"] == report["byte_comparison_bytes"] == 0
    assert all(group.evidence["byte_identical"] is False for group in groups)
    exported = json.dumps({"groups": [group.to_dict() for group in groups], "report": report})
    assert "sha256" not in exported and "signature" not in exported and "snapshot_ctime" not in exported
    assert contents(files) == before


@pytest.mark.parametrize("share_session", [True, False])
def test_combined_exact_and_similar_reuse_is_opt_in_and_missing_hints_read_bytes(tmp_path, share_session):
    files = same_size_revisions(tmp_path)
    session = matching.AnalysisSession()
    exact = matching.find_duplicates(files, session=session)
    assert exact == []
    report = {}
    groups = matching.find_similar(files, methods=("name",), verified_duplicates=exact,
                                   session=session if share_session else None, report=report)
    assert len(groups) == 3
    assert report["exact_results_reused"] is True
    assert report["exact_difference_hints"] == (3 if share_session else 0)
    assert report["byte_comparison_pairs"] == (0 if share_session else 3)
    assert report["byte_comparison_bytes"] == (0 if share_session else 888)


def test_same_size_restored_mtime_change_rejects_the_old_difference_hint(tmp_path):
    files = same_size_revisions(tmp_path)
    victim = files[1]
    original = Path(victim.path).stat()
    changed = False

    def mutate(message):
        nonlocal changed
        if message.startswith("Comparing ") and not changed:
            changed = True
            Path(victim.path).write_bytes(Path(files[0].path).read_bytes())
            os.utime(victim.path, ns=(original.st_atime_ns, original.st_mtime_ns))

    report = {}
    groups = matching.find_similar(files, methods=("name",), progress=mutate, report=report)
    assert changed
    assert not any(victim.path in group.paths for group in groups)
    assert report["skipped_count"] >= 1 and not report["exhaustive"]
    assert any("exact-analysis read" in warning for warning in report["warnings"])
    assert len(groups) == 1


def test_equal_hash_hints_still_require_actual_bytes(tmp_path, monkeypatch):
    files = same_size_revisions(tmp_path, count=2)
    original = matching._digest

    def colliding_digest(entry, cancel):
        original(entry, cancel)
        matching._DUPLICATE_READS.sink[entry.path]["sha256"] = "0" * 64
        return "0" * 64

    monkeypatch.setattr(matching, "_digest", colliding_digest)
    report = {}
    groups = matching.find_similar(files, methods=("name",), report=report)
    assert len(groups) == 1 and groups[0].evidence["byte_identical"] is False
    assert report["exact_difference_hints"] == 0
    assert report["byte_comparison_pairs"] == 1
    assert report["byte_comparison_bytes"] == 296
    assert report["exact_pairs_excluded"] == 0


def test_different_large_samples_only_reject_identity_without_full_rereads(tmp_path, monkeypatch):
    for index, value in enumerate((17, 93), 1):
        (tmp_path / f"report ({index}).bin").write_bytes(bytes((value,)) * (2 * 1024 * 1024))
    files = scan_folder(tmp_path).files
    before = contents(files)

    def unnecessary_read(*_args):
        raise AssertionError("Distinct current samples already rule out byte identity")

    monkeypatch.setattr(matching, "_digest", unnecessary_read)
    monkeypatch.setattr(matching, "_equal", unnecessary_read)
    report = {}
    groups = matching.find_similar(files, methods=("name",), report=report)
    assert len(groups) == 1 and groups[0].evidence["byte_identical"] is False
    assert report["exact_difference_hints"] == 1
    assert report["byte_comparison_pairs"] == report["byte_comparison_bytes"] == 0
    assert contents(files) == before


def test_edited_session_records_cannot_supply_difference_hints(tmp_path):
    files = same_size_revisions(tmp_path, count=2)
    session = matching.AnalysisSession()
    exact = matching.find_duplicates(files, session=session)
    session._snapshots[files[0].path]["sha256"] = "forged digest"
    with pytest.raises(ValueError, match="session was changed"):
        matching.find_similar(files, methods=("name",), verified_duplicates=exact, session=session)


def test_cancelled_exact_pass_clears_previously_captured_session(tmp_path):
    files = same_size_revisions(tmp_path, count=2)
    session = matching.AnalysisSession()
    matching.find_duplicates(files, session=session)
    cancelled = Event()
    cancelled.set()
    assert matching.find_duplicates(files, session=session, cancel=cancelled) == []
    report = {}
    groups = matching.find_similar(files, methods=("name",), verified_duplicates=[], session=session, report=report)
    assert len(groups) == 1
    assert report["read_hint_files"] == report["exact_difference_hints"] == 0
    assert report["byte_comparison_pairs"] == 1


def test_session_hint_bound_falls_back_without_changing_results(tmp_path, monkeypatch):
    files = same_size_revisions(tmp_path)
    monkeypatch.setattr(matching, "MAX_READ_HINT_FILES", 2)
    report = {}
    groups = matching.find_similar(files, methods=("name",), report=report)
    assert len(groups) == 3
    assert report["read_hint_files"] == report["read_hint_limit"] == 2
    assert report["exact_difference_hints"] == 1
    assert report["byte_comparison_pairs"] == 2
    assert report["byte_comparison_bytes"] == 592
