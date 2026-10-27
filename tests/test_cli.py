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
    assert len(json.loads(output.read_text())["duplicates"]) == 1
    assert main([str(root), "--json", str(output)]) == 2
