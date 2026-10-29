"""New v1 performance paths retain ratio semantics and responsive progress."""
from difflib import SequenceMatcher
from pathlib import Path
from threading import Event

from folder_organizer import matching
from folder_organizer.models import FileEntry
from folder_organizer.scanner import scan_folder


def test_name_reuse_keeps_ordered_ratios_and_bounded_memory(monkeypatch):
    monkeypatch.setattr(matching, "NAME_INDEX_CACHE_ENTRIES", 3)
    monkeypatch.setattr(matching, "NAME_SCORE_CACHE_ENTRIES", 5)
    comparisons = matching._NameComparisons()
    pairs = [("tide", "diet"), ("diet", "tide"), ("동일 보고서", "동일 보고서"),
             ("a" * 1100, "a" * 1024 + "b"), ("copy alpha", "copy beta")]
    for left, right in pairs * 3:
        assert comparisons.ratio(left, right) == SequenceMatcher(
            None, left[:1024], right[:1024], autojunk=False).ratio()
    assert comparisons.ratio("tide", "diet") != comparisons.ratio("diet", "tide")
    for index in range(25):
        left, right = f"draft {index}", f"final {index}"
        assert comparisons.ratio(left, right) == SequenceMatcher(None, left, right, autojunk=False).ratio()
        assert len(comparisons.indexes) <= 3
        assert len(comparisons.scores) <= 5
    assert comparisons.metrics()["score_hits"] > 0


def test_repeated_normalized_names_skip_sequence_indexes_without_skipping_files(tmp_path, monkeypatch):
    for index in range(18):
        folder = tmp_path / str(index)
        folder.mkdir()
        (folder / "연간 보고서 copy 2.txt").write_bytes(b"x" * (index + 1))
    files = scan_folder(tmp_path).files

    def unnecessary_matcher(*args, **kwargs):
        raise AssertionError("Identical normalized strings need no sequence index")

    monkeypatch.setattr(matching, "SequenceMatcher", unnecessary_matcher)
    report = {}
    groups = matching.find_similar(files, methods=("name",), report=report)
    expected_pairs = len(files) * (len(files) - 1) // 2
    assert len(groups) == report["compared_pairs"] == expected_pairs
    assert all(group.evidence["name_score"] == 1 and group.evidence["byte_identical"] is False
               for group in groups)
    assert report["name_comparison_cache"]["identical_names"] == expected_pairs
    assert report["name_comparison_cache"]["sequence_index_builds"] == 0


def test_similar_results_keep_exact_scores_with_reused_different_names(tmp_path):
    names = ("project report draft.txt", "project report final.txt", "project report notes.txt")
    for index in range(21):
        folder = tmp_path / f"folder-{index:02}"
        folder.mkdir()
        (folder / names[index % len(names)]).write_bytes(b"x" * (index + 1))
    files = scan_folder(tmp_path).files
    report = {}
    groups = matching.find_similar(files, methods=("name",), name_threshold=.5, report=report)
    by_path = {entry.path: Path(entry.name).stem.casefold() for entry in files}
    for group in groups:
        left, right = (by_path[path] for path in group.paths)
        assert group.evidence["name_score"] == SequenceMatcher(None, left, right, autojunk=False).ratio()
    assert report["name_comparison_cache"]["score_hits"] > 100
    assert report["name_comparison_cache"]["sequence_index_builds"] <= 3


def _metadata_only_files(count):
    return [FileEntry(path=f"/metadata-only/{index:06}.bin", relative_path=f"{index:06}.bin",
                      name=f"{index:06}.bin", size=index, modified=1, created=None,
                      created_source="unavailable", kind="file", suffix=".bin",
                      mtime_ns=1, device=1, inode=index + 1) for index in range(count)]


def test_large_index_samples_progress_before_allocating_events(monkeypatch):
    monkeypatch.setattr(matching.time, "monotonic", lambda: 1.0)
    events = []
    assert matching.find_duplicates(_metadata_only_files(10_000), progress_detail=events.append) == []
    indexed = [event for event in events if event["phase"] == "duplicate_index"]
    assert len(indexed) <= 112
    assert {event["completed"] for event in indexed} >= set(range(11)) | {10_000}
    assert events[-1]["phase"] == "duplicate_complete" and events[-1]["terminal"]


def test_sampled_small_phase_can_cancel_before_any_content_reads(monkeypatch):
    cancel = Event()
    events = []

    def detail(event):
        events.append(event)
        if event["phase"] == "duplicate_index" and event["completed"] == 10:
            cancel.set()

    assert matching.find_duplicates(_metadata_only_files(500), cancel=cancel, progress_detail=detail) == []
    assert events[-1]["phase"] == "duplicate_cancelled" and events[-1]["terminal"]
    assert not any(event["phase"] == "duplicate_hash" for event in events)


def test_name_cache_never_bypasses_changed_file_snapshot_check(tmp_path):
    for index in range(5):
        folder = tmp_path / str(index)
        folder.mkdir()
        (folder / "monthly report.txt").write_bytes(b"x" * (index + 1))
    files = scan_folder(tmp_path).files
    changed = Path(files[-1].path)
    mutated = False

    def mutate(message):
        nonlocal mutated
        if not mutated and message.startswith("Comparing "):
            changed.write_bytes(b"a changed file with a different size")
            mutated = True

    report = {}
    groups = matching.find_similar(files, methods=("name",), progress=mutate, report=report)
    assert mutated and report["skipped_count"] > 0 and not report["exhaustive"]
    assert all(str(changed) not in group.paths for group in groups)
