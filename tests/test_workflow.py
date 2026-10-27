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


def test_protected_photo_review_selection_report_and_undo_work_together(tmp_path):
    from PIL import Image
    from folder_organizer.comparison import compare_files
    from folder_organizer.matching import find_similar
    from folder_organizer.reporting import build_report, write_report
    from folder_organizer.retention import choose_duplicate_copies

    root = tmp_path / "Personal"
    protected, downloads, ignored = root / "Originals", root / "Downloads", root / "Cache"
    for directory in (protected, downloads, ignored):
        directory.mkdir(parents=True)
    original, copy, resized = protected / "photo.png", downloads / "photo copy.png", downloads / "resized.jpg"
    image = Image.new("RGB", (48, 48))
    image.putdata([(x * 5, y * 5, 80) for y in range(48) for x in range(48)])
    image.save(original)
    copy.write_bytes(original.read_bytes())
    image.resize((96, 96)).save(resized, quality=95)
    image.close()
    (ignored / "not-scanned.txt").write_bytes(b"excluded private data")

    scan = scan_folder(root, exclude_patterns=["Cache"])
    duplicates = find_duplicates(scan.files)
    assert len(duplicates) == 1
    diagnostics = {}
    similar = find_similar(scan.files, methods=("image",), image_threshold=.9, report=diagnostics)
    assert similar and all(group.kind == "similar_image" for group in similar)
    assert all(group.evidence["byte_identical"] is False for group in similar)
    assert diagnostics["exact_pairs_excluded"] == 1
    entries = {entry.path: entry for entry in scan.files}
    comparison = compare_files(entries[str(original)], entries[str(resized)])
    assert comparison["byte_identical"] is False
    assert comparison["left_preview"] and comparison["right_preview"]
    selection = choose_duplicate_copies(duplicates, scan.files, rule="newest", protected_folders=[str(protected)])
    assert selection["selected"] == [str(copy)] and str(original) in selection["kept"]
    report = build_report(scan, duplicates=duplicates, similar=similar, comparison_report=diagnostics, selection=selection)
    write_report(tmp_path / "review.html", report)
    assert "동일 확률이 아니며" in (tmp_path / "review.html").read_text(encoding="utf-8")
    snapshot = original.read_bytes()
    journal = execute_plan(build_plan(root, selection["selected"], "quarantine"))
    assert journal["status"] == "completed" and original.read_bytes() == snapshot and not copy.exists()
    assert undo_operation(root, journal["id"])["status"] == "undone"
    assert copy.read_bytes() == original.read_bytes() == snapshot
    assert (ignored / "not-scanned.txt").read_bytes() == b"excluded private data"
