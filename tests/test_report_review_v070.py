"""Only the changed diagnostics/interchange paths from the 0.7.0 review."""
from dataclasses import replace
import csv
import json
from threading import Event

import pytest

from folder_organizer import cli, matching
from folder_organizer.models import MatchGroup
from folder_organizer.reporting import build_common_report, build_report, render_html_report, write_common_csv
from folder_organizer.scanner import scan_folder


def test_sample_read_metrics_do_not_certify_identical_samples(tmp_path):
    size = 2 * 1024 * 1024
    for name in ("same-a.bin", "same-b.bin", "different.bin"):
        (tmp_path / name).write_bytes(b"x" * size)
    with (tmp_path / "different.bin").open("r+b") as stream:
        stream.seek(200 * 1024)
        stream.write(b"different unsampled data")
    diagnostics = {}
    groups = matching.find_duplicates(scan_folder(tmp_path).files, report=diagnostics)
    assert {frozenset(group.paths) for group in groups} == {
        frozenset(str(tmp_path / name) for name in ("same-a.bin", "same-b.bin"))}
    assert diagnostics["complete"] and not diagnostics["warnings"]
    assert diagnostics["metrics"]["sample_bytes"] == 3 * 3 * matching.SAMPLE_BYTES
    assert diagnostics["metrics"]["full_hash_bytes"] == 3 * size
    assert diagnostics["metrics"]["byte_comparison_bytes"] > 0


def test_stale_file_is_visible_without_losing_healthy_verified_group(tmp_path):
    for name in ("a.txt", "b.txt", "gone.txt"):
        (tmp_path / name).write_bytes(b"healthy original bytes")
    scan = scan_folder(tmp_path)
    (tmp_path / "gone.txt").unlink()
    diagnostics = {}
    groups = matching.find_duplicates(scan.files, report=diagnostics)
    assert len(groups) == 1 and len(groups[0].paths) == 2
    assert diagnostics["complete"] is False
    assert diagnostics["metrics"]["skipped_files"] == 1
    assert diagnostics["warnings"][0]["path"].endswith("gone.txt")
    report = build_common_report(scan, duplicates=groups, duplicate_analysis=diagnostics)
    assert report["duplicates"]["complete"] is False
    assert report["duplicates"]["warnings"][0]["path"] == "gone.txt"
    html = render_html_report(build_report(scan, duplicates=groups, duplicate_analysis=diagnostics))
    assert "중복 검사 일부를 완료하지 못했습니다" in html and "gone.txt" in html


def test_cancelled_analysis_restores_metrics_context_and_never_reports_complete(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"same")
    scan = scan_folder(tmp_path)
    cancel = Event()
    cancel.set()
    diagnostics = {}
    assert matching.find_duplicates(scan.files, cancel=cancel, report=diagnostics) == []
    assert diagnostics["cancelled"] and not diagnostics["complete"]
    assert getattr(matching._DUPLICATE_READS, "metrics", None) is None
    fresh = {}
    assert len(matching.find_duplicates(scan.files, report=fresh)) == 1
    assert fresh["complete"] and fresh["metrics"]["full_hash_bytes"] == 8


def test_common_report_preserves_exact_dates_unknown_creation_and_csv_data(tmp_path):
    source = tmp_path / "=formula.txt"
    source.write_bytes(b"private original text never exported")
    scan = scan_folder(tmp_path)
    scan.files[0] = replace(scan.files[0], mtime_ns=9_007_199_254_740_993,
                           created_ns=-123_456_789)
    report = build_common_report(scan)
    assert report["schema"] == "folder-organizer.common.v1"
    file = report["inventory"]["files"][0]
    assert file["modified_ns"] == "9007199254740993" and file["created_ns"] == "-123456789"
    assert report["duplicates"]["requested"] is False and report["duplicates"]["complete"] is None
    assert "private original text" not in json.dumps(report)
    output = tmp_path / "new.csv"
    write_common_csv(output, scan)
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["relative_path"] == "'=formula.txt"
    assert rows[0]["modified_ns"] == "9007199254740993"
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_common_csv(output, scan)
    assert output.read_bytes() == before
    scan.files[0] = replace(scan.files[0], created_ns=None)
    assert build_common_report(scan)["inventory"]["files"][0]["created_ns"] is None


def test_common_report_rejects_a_similarity_group_as_exact(tmp_path):
    scan = scan_folder(tmp_path)
    unverified = MatchGroup("similar_name", [], "name only")
    with pytest.raises(ValueError, match="byte equality"):
        build_common_report(scan, duplicates=[unverified])


def test_cli_reuses_live_exact_proofs_for_similarity_and_shared_outputs(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "input"
    folder.mkdir()
    for name in ("first.txt", "second.txt"):
        (folder / name).write_text("a live byte verified duplicate for this contract", encoding="utf-8")
    calls = 0
    original = matching.find_duplicates
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(matching, "find_duplicates", counted)
    output, csv_path = tmp_path / "common.json", tmp_path / "common.csv"
    assert cli.main([str(folder), "--duplicates", "--similar", "--similar-method", "name",
                     "--json-common", str(output), "--csv-common", str(csv_path)]) == 0
    assert calls == 1
    report = json.loads(output.read_text())
    assert report["duplicates"]["complete"] is True and len(report["duplicates"]["groups"]) == 1
    assert report["similarity"]["pairs"] == []
    assert capsys.readouterr().err == ""


def test_imported_duplicate_report_cannot_authorize_exact_result_reuse(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"same bytes")
    files = scan_folder(tmp_path).files
    groups = matching.find_duplicates(files)
    imported = [MatchGroup(**json.loads(json.dumps(groups[0].to_dict())))]
    with pytest.raises(ValueError, match="중복 결과"):
        matching.find_similar(files, verified_duplicates=imported)


def test_common_unrequested_container_types_and_extension_are_stable(tmp_path):
    (tmp_path / "file.TXT").write_bytes(b"metadata")
    report = build_common_report(scan_folder(tmp_path))
    assert report["action_authority"] is False
    assert report["inventory"]["files"][0]["extension"] == ".txt"
    assert report["duplicates"]["metrics"] == {}
    assert report["similarity"]["thresholds"] == {}
    assert report["similarity"]["warnings"] == []
    assert report["similarity"]["candidate_index"] == {}


def test_common_similar_only_marks_exact_section_not_requested(tmp_path, capsys):
    folder = tmp_path / "input"
    folder.mkdir()
    for name in ("a.txt", "b.txt"):
        (folder / name).write_bytes(b"same original bytes")
    output = tmp_path / "similar-common.json"
    assert cli.main([str(folder), "--similar", "--similar-method", "name",
                     "--json-common", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["duplicates"]["requested"] is False
    assert report["duplicates"]["groups"] == []
    assert report["duplicates"]["metrics"] == {}
    assert report["duplicates"]["complete"] is None
    assert report["similarity"]["pairs"] == []
    capsys.readouterr()


def test_post_sample_change_marks_python_result_partial_without_rehashing(tmp_path):
    import os
    size = 2 * 1024 * 1024
    first, second = tmp_path / "a.bin", tmp_path / "b.bin"
    first.write_bytes(b"a" * size)
    second.write_bytes(b"b" * size)
    scan = scan_folder(tmp_path)
    before = first.stat()
    changed = False
    def mutate(event):
        nonlocal changed
        if not changed and event["phase"] == "duplicate_hash":
            changed = True
            first.write_bytes(b"b" * size)
            os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns))
    diagnostics = {}
    assert matching.find_duplicates(scan.files, report=diagnostics, progress_detail=mutate) == []
    assert changed and not diagnostics["complete"]
    assert diagnostics["metrics"]["full_hash_bytes"] == 0
    assert any(value["phase"] == "final_snapshot" for value in diagnostics["warnings"])


def test_cli_partial_exit_and_report_preserve_healthy_group(tmp_path, monkeypatch, capsys):
    from folder_organizer import scanner
    folder = tmp_path / "input"
    folder.mkdir()
    for name in ("a.txt", "b.txt", "missing.txt"):
        (folder / name).write_bytes(b"healthy exact content")
    original = scanner.scan_folder
    def scan_then_remove(*args, **kwargs):
        scan = original(*args, **kwargs)
        (folder / "missing.txt").unlink()
        return scan
    monkeypatch.setattr(scanner, "scan_folder", scan_then_remove)
    output = tmp_path / "partial.json"
    assert cli.main([str(folder), "--duplicates", "--json-common", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["duplicates"]["complete"] is False
    assert len(report["duplicates"]["groups"]) == 1
    assert report["duplicates"]["warnings"][0]["path"] == "missing.txt"
    capsys.readouterr()


def test_sample_to_hash_same_mtime_change_cannot_move_between_buckets_silently(tmp_path):
    import os
    size = 2 * 1024 * 1024
    for name, value in (("a.bin", b"a"), ("b.bin", b"a"), ("c.bin", b"c"), ("d.bin", b"c")):
        (tmp_path / name).write_bytes(value * size)
    scan = scan_folder(tmp_path)
    victim = tmp_path / "a.bin"
    before = victim.stat()
    changed = False
    def mutate(event):
        nonlocal changed
        if not changed and event["phase"] == "duplicate_hash":
            changed = True
            victim.write_bytes(b"c" * size)
            os.utime(victim, ns=(before.st_atime_ns, before.st_mtime_ns))
    diagnostics = {}
    groups = matching.find_duplicates(scan.files, report=diagnostics, progress_detail=mutate)
    assert changed and not diagnostics["complete"]
    assert {frozenset(group.paths) for group in groups} == {
        frozenset(str(tmp_path / name) for name in ("c.bin", "d.bin"))}
    assert diagnostics["metrics"]["skipped_files"] == 1
    assert diagnostics["metrics"]["full_hash_files"] == 3
    assert any(value["phase"] == "hash" and value["path"].endswith("a.bin")
               for value in diagnostics["warnings"])
