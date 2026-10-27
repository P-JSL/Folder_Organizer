from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from folder_organizer import operations as ops


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    return root


def write(root: Path, name="document.txt", content=b"valuable original") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("mode", ["quarantine", "date", "extension", "move", "copy"])
def test_each_mode_verifies_content_and_supports_repeatable_undo(root, tmp_path, mode):
    source = write(root, "nested/report.txt")
    original = source.read_bytes()
    plan = ops.build_plan(root, [str(source)], mode, str(tmp_path / "output"))
    assert source.read_bytes() == original  # preview is read-only
    target = Path(plan["items"][0]["target"])
    assert not target.exists()
    assert plan["items"][0]["sha256"]
    result = ops.execute_plan(plan)
    assert result["status"] == "completed"
    assert target.read_bytes() == original
    assert source.exists() == (mode == "copy")
    assert ops.list_history(root)[0]["id"] == result["id"]
    undone = ops.undo_operation(root, result["id"])
    assert undone["status"] == "undone"
    assert source.read_bytes() == original
    assert not target.exists()
    assert ops.undo_operation(root, result["id"])["status"] == "undone"


def test_no_overwrite_and_preserves_relative_directories(root, tmp_path):
    source = write(root, "nested/report.txt")
    destination = tmp_path / "destination"
    collision = write(destination, "nested/report.txt", b"existing")
    plan = ops.build_plan(root, [str(source), str(source)], "copy", str(destination))
    assert len(plan["items"]) == 1
    assert Path(plan["items"][0]["target"]).name == "report (1).txt"
    assert ops.execute_plan(plan)["status"] == "completed"
    assert collision.read_bytes() == b"existing"


def test_stale_preview_rejects_modified_file_even_same_size(root):
    source = write(root, content=b"original")
    plan = ops.build_plan(root, [str(source)], "quarantine")
    original_time = source.stat().st_mtime_ns
    source.write_bytes(b"modified")
    os.utime(source, ns=(original_time, original_time))
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.read_bytes() == b"modified"
    assert not Path(plan["items"][0]["target"]).exists()


def test_tampered_plan_never_touches_files(root, tmp_path):
    source = write(root)
    plan = ops.build_plan(root, [str(source)], "quarantine")
    malicious = copy.deepcopy(plan)
    malicious["items"][0]["target"] = str(tmp_path / "unapproved.txt")
    with pytest.raises(ops.OperationError):
        ops.execute_plan(malicious)
    assert source.exists()
    assert not (tmp_path / "unapproved.txt").exists()


def test_plan_cannot_be_executed_twice(root):
    source = write(root)
    plan = ops.build_plan(root, [str(source)], "quarantine")
    ops.execute_plan(plan)
    with pytest.raises(FileExistsError):
        ops.execute_plan(plan)
    assert ops.undo_operation(root, plan["id"])["status"] == "undone"


def test_new_target_created_after_preview_is_preserved(root, tmp_path):
    source = write(root)
    plan = ops.build_plan(root, [str(source)], "move", str(tmp_path / "out"))
    target = Path(plan["items"][0]["target"])
    target.parent.mkdir()
    target.write_bytes(b"racing existing file")
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert target.read_bytes() == b"racing existing file"
    assert source.read_bytes() == b"valuable original"
    assert ops.undo_operation(root, plan["id"])["status"] == "undone"
    assert target.read_bytes() == b"racing existing file"


def test_outside_source_and_internal_state_are_rejected(root, tmp_path):
    outside = write(tmp_path, "outside.txt")
    internal = write(root, ".folder-organizer/journal.txt")
    for path in (outside, internal, root):
        with pytest.raises(ops.OperationError):
            ops.build_plan(root, [str(path)], "quarantine")


def test_source_links_and_linked_parents_are_rejected(root, tmp_path):
    outside = write(tmp_path, "outside.txt")
    link = root / "link.txt"
    parent = root / "linked-folder"
    try:
        link.symlink_to(outside)
        parent.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable")
    for path in (link, parent / "outside.txt"):
        with pytest.raises(ops.OperationError):
            ops.build_plan(root, [str(path)], "quarantine")
    assert outside.read_bytes() == b"valuable original"


def test_destination_parent_link_added_after_preview_is_rejected(root, tmp_path):
    source = write(root)
    destination = tmp_path / "output"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    plan = ops.build_plan(root, [str(source)], "move", str(destination))
    try:
        destination.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable")
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.exists()
    assert list(elsewhere.iterdir()) == []


def test_undo_never_overwrites_new_original_and_can_retry(root):
    source = write(root)
    result = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    target = Path(result["items"][0]["target"])
    source.write_bytes(b"new document")
    undone = ops.undo_operation(root, result["id"])
    assert undone["status"] == "partial_undo"
    assert source.read_bytes() == b"new document"
    assert target.read_bytes() == b"valuable original"
    source.unlink()
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert source.read_bytes() == b"valuable original"


def test_undo_preserves_edited_destination(root, tmp_path):
    source = write(root)
    result = ops.execute_plan(ops.build_plan(root, [str(source)], "copy", str(tmp_path / "output")))
    target = Path(result["items"][0]["target"])
    target.write_bytes(b"user edited this copy")
    assert ops.undo_operation(root, result["id"])["status"] == "partial_undo"
    assert source.read_bytes() == b"valuable original"
    assert target.read_bytes() == b"user edited this copy"


def test_undo_copy_does_not_remove_last_remaining_copy(root, tmp_path):
    source = write(root)
    result = ops.execute_plan(ops.build_plan(root, [str(source)], "copy", str(tmp_path / "out")))
    source.unlink()
    assert ops.undo_operation(root, result["id"])["status"] == "partial_undo"
    assert Path(result["items"][0]["target"]).read_bytes() == b"valuable original"


def test_tampered_journal_and_path_traversal_rejected(root, tmp_path):
    source = write(root)
    result = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    path = Path(result["journal_path"])
    journal = json.loads(path.read_text(encoding="utf-8"))
    journal["items"][0]["source"] = str(tmp_path / "unauthorized.txt")
    path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(ops.OperationError):
        ops.undo_operation(root, result["id"])
    with pytest.raises(ops.OperationError):
        ops.list_history(root)
    with pytest.raises(ops.OperationError):
        ops.undo_operation(root, "../../outside")
    assert not (tmp_path / "unauthorized.txt").exists()


def test_failed_copy_retains_original_and_cleans_owned_partial(root, tmp_path, monkeypatch):
    source = write(root, content=b"long enough to copy")
    plan = ops.build_plan(root, [str(source)], "move", str(tmp_path / "out"))
    original_write = os.write

    def fail_data_write(fd, data):
        if bytes(data) == b"long enough to copy":
            raise OSError("simulated disk full")
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", fail_data_write)
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.read_bytes() == b"long enough to copy"
    assert not Path(plan["items"][0]["target"]).exists()


def test_partial_batch_keeps_failed_source_and_undoes_success(root):
    first = write(root, "first.txt")
    second = write(root, "second.txt")
    plan = ops.build_plan(root, [str(first), str(second)], "quarantine")
    second.write_bytes(b"changed after preview")
    result = ops.execute_plan(plan)
    assert result["status"] == "partial"
    assert not first.exists()
    assert second.read_bytes() == b"changed after preview"
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert first.read_bytes() == b"valuable original"
    assert second.read_bytes() == b"changed after preview"


def test_creation_date_never_uses_linux_metadata_change_time(root):
    source = write(root)
    info = source.stat()
    if os.name != "nt" and not hasattr(info, "st_birthtime"):
        plan = ops.build_plan(root, [str(source)], "date", date_basis="created")
        assert plan["items"] == []
        assert plan["warnings"]


def test_already_organized_file_does_not_nest_outputs(root):
    source = write(root, "정리됨/txt/first.txt")
    plan = ops.build_plan(root, [str(source)], "extension")
    assert plan["items"] == []
    assert plan["warnings"]


def test_history_is_empty_without_creating_state(root):
    assert ops.list_history(root) == []
    assert not (root / ops.STATE_DIRECTORY).exists()
