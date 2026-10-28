"""New deletions use one recoverable folder, with no duplicated raw backups."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest

from folder_organizer import operations as ops


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


def assert_no_old_raw_archives(root):
    assert not (root / ops.STATE_DIRECTORY / "quarantine").exists()
    assert not (root / ops.STATE_DIRECTORY / "retained").exists()


def test_flat_collision_names_move_original_inode_and_restore_without_raw_backups(root, monkeypatch):
    originals = []
    for number in range(3):
        source = root / str(number) / "report.txt"
        source.parent.mkdir()
        source.write_bytes(f"report{number}".encode())
        originals.append((source, source.stat().st_ino, source.read_bytes()))
    monkeypatch.setattr(ops, "_copy_verified", lambda *_a, **_k: pytest.fail("deletion must not copy bytes"))
    monkeypatch.setattr(ops, "_retain_verified", lambda *_a, **_k: pytest.fail("deletion must not create another raw backup"))
    result = ops.execute_plan(ops.build_plan(root, [str(source) for source, _inode, _raw in originals], "quarantine"))
    assert result["status"] == "completed"
    assert len({item["target"] for item in result["items"]}) == 3
    for item, (source, inode, raw) in zip(result["items"], originals):
        target = Path(item["target"])
        assert target.parent == root / "delete_files" and target.read_bytes() == raw
        assert target.stat().st_ino == inode and not source.exists()
        assert "original_backup" not in item and "undo_backup" not in item
    assert_no_old_raw_archives(root)
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert all(source.read_bytes() == raw for source, _inode, raw in originals)
    assert_no_old_raw_archives(root)


def test_destination_collision_after_preview_never_overwrites(root):
    source = root / "file.txt"
    source.write_bytes(b"original")
    plan = ops.build_plan(root, [str(source)], "quarantine")
    target = Path(plan["items"][0]["target"])
    target.parent.mkdir()
    target.write_bytes(b"someone else's file")
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.read_bytes() == b"original" and target.read_bytes() == b"someone else's file"
    assert_no_old_raw_archives(root)


def test_changed_capture_is_returned_without_losing_changed_bytes(root, monkeypatch):
    source = root / "file.txt"
    source.write_bytes(b"original")
    plan = ops.build_plan(root, [str(source)], "quarantine")
    original_move = ops._move_no_replace
    def changed(old, new):
        original_move(old, new)
        if new.parent == root / "delete_files":
            new.write_bytes(b"changed after atomic capture")
    monkeypatch.setattr(ops, "_move_no_replace", changed)
    result = ops.execute_plan(plan)
    assert result["status"] == "failed" and result["items"][0]["deletion_returned"]
    assert source.read_bytes() == b"changed after atomic capture"
    assert not Path(plan["items"][0]["target"]).exists()
    assert_no_old_raw_archives(root)


def test_failed_return_keeps_new_original_and_changed_capture(root, monkeypatch):
    source = root / "file.txt"
    source.write_bytes(b"original")
    plan = ops.build_plan(root, [str(source)], "quarantine")
    original_move = ops._move_no_replace
    def changed(old, new):
        original_move(old, new)
        if new.parent == root / "delete_files":
            source.write_bytes(b"new user file")
            new.write_bytes(b"changed captured file")
    monkeypatch.setattr(ops, "_move_no_replace", changed)
    result = ops.execute_plan(plan)
    assert result["status"] == "failed" and result["items"][0]["source_removed"]
    assert source.read_bytes() == b"new user file"
    target = Path(result["items"][0]["target"])
    assert target.read_bytes() == b"changed captured file"
    assert ops.undo_operation(root, result["id"])["status"] == "partial_undo"
    assert source.read_bytes() == b"new user file" and target.read_bytes() == b"changed captured file"


def test_interrupted_rename_before_capture_checkpoint_remains_restorable(root):
    source = root / "file.txt"
    source.write_bytes(b"durable intent")
    plan = ops.build_plan(root, [str(source)], "quarantine")
    item = {**plan["items"][0], "state": "moving", "target_created": False, "error": None, "source_removed": False}
    state = ops._state(root)
    key = ops._key(state)
    history = state / "history"
    history.mkdir()
    journal_path = history / f"{plan['id']}.json"
    journal = {"version": 1, "id": plan["id"], "root": str(root), "mode": "quarantine",
               "created_at": plan["created_at"], "status": "running", "items": [item],
               "journal_path": str(journal_path), "deletion_storage": "delete_files"}
    ops._write_journal(journal_path, journal, key, initial=True)
    target = Path(item["target"])
    target.parent.mkdir()
    ops._move_no_replace(source, target)  # Process was stopped before the next checkpoint.
    assert ops.undo_operation(root, plan["id"])["status"] == "undone"
    assert source.read_bytes() == b"durable intent" and not target.exists()
    assert_no_old_raw_archives(root)


@pytest.mark.skipif(os.name == "nt", reason="This verifies the POSIX fail-closed fallback")
def test_unsupported_atomic_move_keeps_original_instead_of_link_unlink(root, monkeypatch):
    from types import SimpleNamespace
    source = root / "file.txt"
    source.write_bytes(b"original")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: SimpleNamespace())
    result = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    assert result["status"] == "failed" and source.read_bytes() == b"original"
    assert not Path(result["items"][0]["target"]).exists()


def test_windows_nas_policy_is_observed_once_and_bound_to_the_journal(root, monkeypatch):
    sources = []
    for number in range(4):
        source = root / f"{number}.txt"
        source.write_bytes(b"file")
        sources.append(str(source))
    calls = []
    def policy(path):
        calls.append(path)
        return {"kind": "delete_files", "reason": "NAS 휴지통 정책 미확인", "volume_kind": "remote",
                "nas_recycle_detected": True, "recycle_only_verified": False}
    monkeypatch.setattr(ops.deletion, "deletion_destination", policy)
    result = ops.execute_plan(ops.build_plan(root, sources, "quarantine"))
    assert result["status"] == "completed" and calls == [root]
    assert result["deletion_destination"]["nas_recycle_detected"]
    assert result["deletion_destination"]["recycle_only_verified"] is False
    key = ops._key(root / ops.STATE_DIRECTORY)
    signed = ops._read_journal(Path(result["journal_path"]), root, key)
    assert signed["deletion_destination"] == result["deletion_destination"]
