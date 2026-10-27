"""Independent regressions for concurrent changes around destructive steps."""
from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from folder_organizer import operations as ops


def test_target_symlink_swap_cannot_change_unrelated_metadata(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX mode-bits regression; Windows reparse safety covered elsewhere")
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"original content")
    source.chmod(0o640)
    victim = tmp_path / "unrelated.txt"
    victim.write_bytes(b"unrelated content")
    victim.chmod(0o600)
    os.utime(victim, ns=(1_000_000_000, 1_000_000_000))
    before = victim.stat()
    plan = ops.build_plan(root, [str(source)], "move", str(tmp_path / "output"))
    target = Path(plan["items"][0]["target"])
    original_fsync = os.fsync
    swapped = False

    def replace_target_after_write(fd):
        nonlocal swapped
        original_fsync(fd)
        if not swapped and target.exists() and os.fstat(fd).st_ino == target.lstat().st_ino:
            target.unlink()
            target.symlink_to(victim)
            swapped = True

    monkeypatch.setattr(os, "fsync", replace_target_after_write)
    result = ops.execute_plan(plan)
    assert swapped
    assert result["status"] == "failed"
    assert source.read_bytes() == b"original content"
    assert stat.S_IMODE(victim.stat().st_mode) == stat.S_IMODE(before.st_mode)
    assert victim.stat().st_mtime_ns == before.st_mtime_ns
    assert victim.read_bytes() == b"unrelated content"


def test_move_preserves_source_replaced_during_last_target_verification(tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"original content")
    plan = ops.build_plan(root, [str(source)], "move", str(tmp_path / "output"))
    target = Path(plan["items"][0]["target"])
    original_verify = ops._verify
    target_verifications = 0

    def replace_source_after_target_check(path, item, *, identity=True):
        nonlocal target_verifications
        fingerprint = original_verify(path, item, identity=identity)
        if Path(path) == target:
            target_verifications += 1
            if target_verifications == 2:
                source.unlink()
                source.write_bytes(b"new unsaved user content")
        return fingerprint

    monkeypatch.setattr(ops, "_verify", replace_source_after_target_check)
    result = ops.execute_plan(plan)
    assert target_verifications >= 2
    assert source.exists(), "move deleted the replacement source after its final verification"
    assert source.read_bytes() == b"new unsaved user content"
    assert target.read_bytes() == b"original content"
    assert result["status"] != "completed"


def test_undo_preserves_copy_edited_after_last_content_verification(tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"original content")
    plan = ops.build_plan(root, [str(source)], "copy", str(tmp_path / "output"))
    result = ops.execute_plan(plan)
    target = Path(plan["items"][0]["target"])
    original_verify = ops._verify
    target_verifications = 0

    def edit_copy_after_target_check(path, item, *, identity=True):
        nonlocal target_verifications
        fingerprint = original_verify(path, item, identity=identity)
        if Path(path) == target:
            target_verifications += 1
            if target_verifications == 2:
                target.write_bytes(b"new edited copy")
        return fingerprint

    monkeypatch.setattr(ops, "_verify", edit_copy_after_target_check)
    undone = ops.undo_operation(root, result["id"])
    assert target_verifications >= 2
    assert target.exists(), "undo deleted a copy edited after its last verification"
    assert target.read_bytes() == b"new edited copy"
    assert source.read_bytes() == b"original content"
    assert undone["status"] == "partial_undo"


def test_move_never_discards_last_verified_original_when_target_changes_late(tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "source.txt"
    original = b"the only original file contents"
    source.write_bytes(original)
    plan = ops.build_plan(root, [str(source)], "move", str(tmp_path / "output"))
    target = Path(plan["items"][0]["target"])
    original_verify = ops._verify
    target_verifications = 0

    def edit_target_after_target_check(path, item, *, identity=True):
        nonlocal target_verifications
        fingerprint = original_verify(path, item, identity=identity)
        if Path(path) == target:
            target_verifications += 1
            if target_verifications == 2:
                target.write_bytes(b"newly edited target contents")
        return fingerprint

    monkeypatch.setattr(ops, "_verify", edit_target_after_target_check)
    ops.execute_plan(plan)
    assert target_verifications >= 2
    remaining_contents = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.is_symlink()]
    assert original in remaining_contents, "move discarded the last original after its verified target changed"
