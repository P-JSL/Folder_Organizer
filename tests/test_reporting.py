import json

import pytest

from folder_organizer.matching import find_duplicates
from folder_organizer.models import MatchGroup
from folder_organizer.reporting import build_report, render_html_report, write_report
from folder_organizer.scanner import scan_folder


def test_report_records_verified_evidence_and_scope_without_file_contents(tmp_path):
    (tmp_path / "one.txt").write_text("secret original content")
    (tmp_path / "two.txt").write_text("secret original content")
    (tmp_path / "ignored.tmp").write_text("excluded")
    scan = scan_folder(tmp_path, exclude_patterns=["*.tmp"])
    groups = find_duplicates(scan.files)
    report = build_report(scan, duplicates=groups)
    assert report["schema_version"] == 2
    assert report["scope"]["exclude_patterns"] == ["*.tmp"]
    assert report["filtered_files"] == 1
    assert report["duplicates"][0]["evidence"]["byte_identical"] is True
    assert "secret original content" not in json.dumps(report)
    output = tmp_path / "new-report.json"
    write_report(output, report)
    assert json.loads(output.read_text())["root"] == str(tmp_path)
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_report(output, report)
    assert output.read_bytes() == before


def test_html_escapes_untrusted_filenames_and_explains_score_not_probability(tmp_path):
    scan = scan_folder(tmp_path)
    group = MatchGroup("similar_name", ["<img src=x onerror=alert(1)>", "other.txt"], "<script>alert(1)</script>", 1.0,
                       evidence={"byte_identical": False, "name_score": 1.0, "limitations": ["<unsafe>"]})
    report = build_report(scan, similar=[group], comparison_report={"limit_reached": True})
    html = render_html_report(report)
    assert "<script>" not in html and "<img" not in html
    assert "&lt;img" in html and "&lt;unsafe&gt;" in html
    assert "100.0%" in html and "점수는 동일 확률이 아니며" in html
    assert "Content-Security-Policy" in html
    assert "비교 한도에 도달했습니다" in html
    assert "limit_reached" not in html
    output = tmp_path / "report.html"
    write_report(output, report)
    assert output.read_text().startswith("<!doctype html>")


def test_readable_report_discloses_display_limits(tmp_path):
    report = build_report(scan_folder(tmp_path))
    report["files"] = [{"relative_path": f"file-{i}", "size": i, "kind": "file"} for i in range(2001)]
    assert "앞 2,000개 파일만 표시" in render_html_report(report)
