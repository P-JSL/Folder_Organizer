from pathlib import Path
import zipfile

from folder_organizer.matching import find_duplicates
from folder_organizer.operations import build_plan, execute_plan, list_history, undo_operation
from folder_organizer.recovery import inspect_file, repair_file
from folder_organizer.scanner import scan_folder


def test_scan_deduplicate_quarantine_restart_and_restore(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    first, second = root / "report.txt", root / "report copy.txt"
    first.write_bytes(b"important original content")
    second.write_bytes(first.read_bytes())
    files = scan_folder(root).files
    group, = find_duplicates(files)
    keep, quarantine = [Path(path) for path in group.paths]
    plan = build_plan(root, [str(quarantine)], "quarantine")
    assert keep.exists() and quarantine.exists()  # preview does not change files
    operation = execute_plan(plan)
    assert operation["status"] == "completed"
    assert keep.read_bytes() == b"important original content"
    assert not quarantine.exists()
    assert len(scan_folder(root, include_hidden=True).files) == 1
    operation_id = list_history(root)[0]["id"]
    assert undo_operation(root, operation_id)["status"] == "undone"
    assert first.read_bytes() == second.read_bytes()


def test_damaged_archive_recovery_returns_new_verified_partial_file(tmp_path):
    source = tmp_path / "damaged.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("intact.txt", b"precious content")
        archive.writestr("lost.txt", b"lost content")
    data = source.read_bytes()
    index = data.index(b"lost content")
    damaged = data[:index] + b"LOST content" + data[index + len(b"lost content"):]
    source.write_bytes(damaged)
    assert inspect_file(source)["status"] == "damaged"
    output = repair_file(source, tmp_path / "recovered")
    assert output["status"] == "partial"
    assert source.read_bytes() == damaged
    with zipfile.ZipFile(output["output"]) as recovered:
        assert recovered.read("intact.txt") == b"precious content"
        assert "lost.txt" not in recovered.namelist()
