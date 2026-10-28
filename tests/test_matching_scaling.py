"""Bounded candidate work still spans late files and retains byte proof."""
from pathlib import Path
from threading import Event

import pytest

from folder_organizer import matching
from folder_organizer.scanner import scan_folder


def inventory(tmp_path, count):
    for index in range(count):
        # Normalized names agree, while distinct sizes avoid duplicate hashing.
        (tmp_path / f"report ({index:04}).txt").write_bytes(b"x" * (index + 1))
    return scan_folder(tmp_path).files


def test_auto_keeps_small_complete_mode_and_reports_capped_coverage(tmp_path):
    files = inventory(tmp_path, 5)
    report = {}
    matching.find_similar(files, methods=("name",), max_pairs=2, report=report)
    assert report["effective_search_mode"] == "complete"
    assert report["potential_pairs"] == 10
    assert report["compared_pairs"] == 2
    assert report["limit_reached"] and not report["exhaustive"]


def test_explicit_complete_mode_preserves_sorted_pair_order_without_mutating_input(tmp_path):
    files = inventory(tmp_path, 4)
    reversed_files = list(reversed(files))
    report = {}
    groups = matching.find_similar(reversed_files, methods=("name",), search_mode="complete", max_pairs=2, report=report)
    assert [group.paths for group in groups] == [[files[0].path, files[1].path], [files[0].path, files[2].path]]
    assert reversed_files == list(reversed(files))
    assert report["effective_search_mode"] == "complete" and report["candidate_files_visited"] == 0


def test_small_complete_result_reports_actual_exhaustive_coverage(tmp_path):
    files = inventory(tmp_path, 3)
    report = {}
    matching.find_similar(files, methods=("name",), report=report)
    assert report["exhaustive"] and report["compared_pairs"] == report["potential_pairs"] == 3


def test_auto_indexes_entire_large_inventory_and_samples_late_pairs(tmp_path):
    files = inventory(tmp_path, 320)
    report = {}
    first = matching.find_similar(files, methods=("name",), max_pairs=50, report=report)
    repeated = matching.find_similar(files, methods=("name",), max_pairs=50)
    assert [group.to_dict() for group in first] == [group.to_dict() for group in repeated]
    assert len(first) == report["compared_pairs"] == report["candidate_pairs_selected"] == 50
    assert report["effective_search_mode"] == "indexed" and not report["exhaustive"]
    assert report["candidate_files_visited"] == len(files)
    assert report["candidate_pairs"] <= len(files) * 32
    assert report["candidate_pairs"] > 50 and report["limit_reached"]
    late = {entry.path for entry in files[250:]}
    assert any(late.intersection(group.paths) for group in first)
    assert all(len(group.paths) == 2 and group.evidence["byte_identical"] is False for group in first)
    assert any("모든 쌍" in warning for warning in report["warnings"])
    assert all(any("빠른 후보" in text for text in group.evidence["limitations"]) for group in first)


def test_candidate_work_is_bounded_per_file_when_input_doubles(tmp_path):
    reports = []
    for count in (300, 600):
        root = tmp_path / str(count)
        root.mkdir()
        files = inventory(root, count)
        report = {}
        matching.find_similar(files, methods=("name",), max_pairs=1,
                              max_candidates_per_file=4, report=report)
        assert report["candidate_files_visited"] == count
        assert report["candidate_lookups"] <= 9 * count
        assert report["index_postings_scanned"] <= 32 * report["candidate_lookups"]
        assert report["candidate_pairs"] <= 4 * count
        reports.append(report)
    assert reports[1]["candidate_lookups"] == 2 * reports[0]["candidate_lookups"]
    assert reports[1]["index_postings_scanned"] < 2.2 * reports[0]["index_postings_scanned"]


def test_content_index_finds_similar_text_with_unrelated_titles_near_end(tmp_path):
    for index in range(280):
        (tmp_path / f"document-{index:04}.txt").write_text(
            (f"unique{index} words{index} details{index} records{index} " * 3), encoding="utf-8")
    common = "A careful document review preserves every original and compares the shared wording before changing a file. " * 8
    first, second = tmp_path / "zebra.txt", tmp_path / "zzz-other-title.txt"
    first.write_text(common, encoding="utf-8")
    second.write_text(common + "A new ending.", encoding="utf-8")
    report = {}
    groups = matching.find_similar(scan_folder(tmp_path).files, methods=("text",),
                                   max_pairs=500, report=report)
    assert any(set(group.paths) == {str(first), str(second)} for group in groups)
    assert report["candidate_files_visited"] == 282
    assert report["compared_pairs"] < 282


def test_indexed_image_retrieval_does_not_require_matching_filename(tmp_path):
    from PIL import Image
    first, second = tmp_path / "apple.png", tmp_path / "zebra.jpg"
    Image.new("RGB", (20, 20), (40, 80, 160)).save(first)
    Image.new("RGB", (40, 40), (40, 80, 160)).save(second, quality=100)
    report = {}
    group, = matching.find_similar(scan_folder(tmp_path).files, methods=("image",),
                                  search_mode="indexed", report=report)
    assert set(group.paths) == {str(first), str(second)}
    assert group.kind == "similar_image" and not group.evidence["byte_identical"]
    assert report["candidate_files_visited"] == 2


def test_index_never_relabels_identical_bytes_as_similar(tmp_path):
    (tmp_path / "report.txt").write_bytes(b"same original bytes")
    (tmp_path / "report copy.txt").write_bytes(b"same original bytes")
    report = {}
    assert matching.find_similar(scan_folder(tmp_path).files, search_mode="indexed", report=report) == []
    assert report["exact_pairs_excluded"] == 1
    group, = matching.find_duplicates(scan_folder(tmp_path).files)
    assert group.evidence["byte_identical"] is True


def test_index_retains_bounded_key_memory_and_admits_late_files(tmp_path, monkeypatch):
    monkeypatch.setattr(matching, "MAX_INDEX_KEYS", 4)
    files = inventory(tmp_path, 50)
    report = {}
    matching.find_similar(files, methods=("name",), search_mode="indexed", report=report)
    assert report["candidate_files_visited"] == 50
    assert report["index_keys_retained_peak"] <= 4
    assert report["index_evictions"] > 0 and report["candidate_limit_reached"]


def test_cancel_during_index_discards_partial_suggestions(tmp_path):
    files = inventory(tmp_path, 30)
    event, report = Event(), {}
    def progress(detail):
        if detail["phase"] == "similar_index" and detail["completed"] == 10:
            event.set()
    assert matching.find_similar(files, methods=("name",), search_mode="indexed", cancel=event,
                                 progress_detail=progress, report=report) == []
    assert report["cancelled"] and not report["exhaustive"]
    assert report["candidate_files_visited"] == 10


def test_file_change_after_candidate_index_is_still_rejected(tmp_path):
    files = inventory(tmp_path, 2)
    report = {}
    def mutate(message):
        if message.startswith("Comparing "):
            Path(files[1].path).write_bytes(b"changed during comparison")
    assert matching.find_similar(files, methods=("name",), search_mode="indexed",
                                 progress=mutate, report=report) == []
    assert report["skipped_count"] >= 1 and not report["exhaustive"]


@pytest.mark.parametrize("mode", [None, "fast", [], True])
def test_search_mode_validation(mode):
    with pytest.raises(ValueError):
        matching.find_similar([], search_mode=mode)


@pytest.mark.parametrize("count", [0, -1, 129, True, 3.5])
def test_per_file_candidate_bound_validation(count):
    with pytest.raises(ValueError):
        matching.find_similar([], max_candidates_per_file=count)
