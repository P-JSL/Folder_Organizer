import csv
import json

from folder_organizer.cli import main


def test_cli_reports_inventory_duplicates_and_protects_csv(tmp_path, capsys):
    root = tmp_path / "input"
    root.mkdir()
    (root / "first.txt").write_text("same", encoding="utf-8")
    (root / "second.txt").write_text("same", encoding="utf-8")
    (root / "=formula.txt").write_text("different", encoding="utf-8")
    csv_path = tmp_path / "inventory.csv"
    assert main([str(root), "--duplicates", "--csv", str(csv_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["files"]) == 3
    assert len(result["duplicates"]) == 1
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert "'=formula.txt" in [row["relative_path"] for row in rows]
    previous = csv_path.read_bytes()
    assert main([str(root), "--csv", str(csv_path)]) == 2
    assert csv_path.read_bytes() == previous
    assert all(p.exists() for p in (root / "first.txt", root / "second.txt"))


def test_invalid_folder_reports_failure(tmp_path, capsys):
    assert main([str(tmp_path / "missing")]) == 2
    assert "오류" in capsys.readouterr().err


def test_json_report_supports_windowed_binary_without_stdout(tmp_path, monkeypatch):
    import sys
    root = tmp_path / "files"
    root.mkdir()
    (root / "one.txt").write_bytes(b"same")
    (root / "two.txt").write_bytes(b"same")
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "stdout", None)
    assert main([str(root), "--duplicates", "--json", str(output)]) == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))["duplicates"]) == 1
    assert main([str(root), "--json", str(output)]) == 2


def test_cli_scope_modes_and_readable_evidence_report(tmp_path, capsys):
    root = tmp_path / "files"
    root.mkdir()
    (root / "invoice.txt").write_text("one")
    (root / "invoice copy.txt").write_text("two")
    (root / "exclude.tmp").write_text("private")
    output = tmp_path / "comparison.html"
    assert main([str(root), "--similar", "--similar-method", "name", "--exclude", "*.tmp", "--min-size", "2", "--html", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 2 and report["filtered_files"] == 1
    assert report["comparison_report"]["methods"] == ["name"]
    assert len(report["similar"]) == 1
    assert report["similar"][0]["evidence"]["matched_signals"] == ["name"]
    assert report["similar"][0]["evidence"]["byte_identical"] is False
    assert "유사 후보" in output.read_text(encoding="utf-8")
    assert (root / "exclude.tmp").read_text() == "private"


def test_cli_rejects_invalid_scope_without_modifying_folder(tmp_path, capsys):
    (tmp_path / "original.txt").write_bytes(b"original")
    assert main([str(tmp_path), "--exclude", "../outside"]) == 2
    assert "오류" in capsys.readouterr().err
    assert [path.name for path in tmp_path.iterdir()] == ["original.txt"]


def test_cli_preserves_unicode_json_in_legacy_windows_pipe(tmp_path, monkeypatch):
    import io
    import sys
    (tmp_path / "one.txt").write_bytes(b"same")
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", output)
    assert main([str(tmp_path)]) == 0
    output.flush()
    report = json.loads(buffer.getvalue().decode("cp1252"))
    assert "완전 중복" in report["definitions"]["duplicate"]
    assert len(report["files"]) == 1


def test_cli_errors_do_not_crash_with_legacy_windows_stderr(tmp_path, monkeypatch):
    import io
    import sys
    buffer = io.BytesIO()
    errors = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stderr", errors)
    assert main([str(tmp_path / "missing")]) == 2
    errors.flush()
    assert "missing" in buffer.getvalue().decode("cp1252")
