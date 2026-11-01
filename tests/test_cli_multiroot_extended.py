"""New command contracts for additive multi-root analysis and session reuse."""
import json
from folder_organizer import cli, matching


def test_multi_cli_cross_folder_groups_are_readonly_and_export_is_exclusive(tmp_path, capsys):
    left, right = tmp_path / "작업", tmp_path / "보관"
    left.mkdir()
    right.mkdir()
    raw = b"actual cross-folder content\n" * 128
    (left / "original.txt").write_bytes(raw)
    (right / "copy.txt").write_bytes(raw)
    report = tmp_path / "multi.json"
    args = [str(left), "--add-folder", str(right), "--duplicates", "--json-multiroot", str(report)]
    assert cli.main(args) == 0
    value = json.loads(report.read_text())
    assert value["schema"] == "folder-organizer.multiroot.v1"
    assert value["complete"] and value["action_authority"] is False
    assert len(value["files"]) == 2
    assert len(value["duplicates"]["groups"]) == 1
    group = value["duplicates"]["groups"][0]
    assert group["group_source_count"] == 2 and group["action_authority"] is False
    assert "_proof" not in json.dumps(value)
    assert all(isinstance(item["modified_ns"], str) for item in value["files"])
    saved = report.read_bytes()
    assert cli.main(args) == 2
    assert report.read_bytes() == saved
    assert (left / "original.txt").read_bytes() == (right / "copy.txt").read_bytes() == raw
    capsys.readouterr()


def test_multi_cli_missing_root_is_partial_and_keeps_verified_healthy_group(tmp_path, capsys):
    root = tmp_path / "healthy"
    root.mkdir()
    for name in ("a.bin", "b.bin"):
        (root / name).write_bytes(b"healthy byte proof")
    report = tmp_path / "partial.json"
    assert cli.main([str(root), "--add-folder", str(tmp_path / "offline"),
                     "--duplicates", "--json-multiroot", str(report)]) == 2
    value = json.loads(report.read_text())
    assert value["complete"] is False
    assert value["duplicates"]["complete"] is False
    assert len(value["duplicates"]["groups"]) == 1
    assert any(root["state"] == "failed" for root in value["roots"])
    capsys.readouterr()


def test_multi_cli_rejects_legacy_output_mix_before_opening_missing_folders(tmp_path, capsys):
    report = tmp_path / "must-not-exist.json"
    assert cli.main([str(tmp_path / "missing"), "--add-folder", str(tmp_path / "other"),
                     "--similar", "--json", str(report)]) == 2
    assert "다중 폴더" in capsys.readouterr().err
    assert not report.exists()


def test_combined_cli_reuses_fresh_nonidentical_hashes_without_pair_rereads(tmp_path, monkeypatch, capsys):
    root = tmp_path / "files"
    root.mkdir()
    for index in range(3):
        (root / f"shared-document-{index}.txt").write_bytes(bytes([65 + index]) * 512)
    comparisons = []
    original = matching._equal
    def counted(left, right, cancel):
        comparisons.append((left.path, right.path))
        return original(left, right, cancel)
    monkeypatch.setattr(matching, "_equal", counted)
    report = tmp_path / "combined.json"
    assert cli.main([str(root), "--duplicates", "--similar", "--similar-method", "name",
                     "--name-threshold", "0.5", "--json", str(report)]) == 0
    value = json.loads(report.read_text())
    assert not value["duplicates"]
    assert len(value["similar"]) == 3
    assert comparisons == []
    capsys.readouterr()
