"""Explicit byte compaction restores independently of deleted live files."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_payload as payload


def removed(tmp_path, contents):
    root = tmp_path / "Documents"
    root.mkdir()
    sources = []
    for number, raw in enumerate(contents):
        source = root / f"file-{number}.bin"
        source.write_bytes(raw)
        sources.append(source)
    result = ops.execute_plan(ops.build_plan(root, list(map(str, sources)), "quarantine"))
    assert result["status"] == "completed"
    return root, sources, result


def object_files(root):
    return [path for path in (root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*") if path.is_file()]


def test_new_delete_files_content_compacts_and_restores_with_no_raw_archives(tmp_path):
    original = b"important contents\n" * 80_000
    root, sources, result = removed(tmp_path, [original])
    target = Path(result["items"][0]["target"])
    before = {str(path) for path in root.rglob("*")}
    plan = ops.compaction_preview(root, result["id"])
    assert plan["candidate_count"] == 1 and plan["raw_bytes"] == len(original)
    assert {str(path) for path in root.rglob("*")} == before  # Preview changed no paths.
    compact = ops.compact_operation(root, result["id"], plan=plan)
    assert compact["recovery_compaction"]["status"] == "completed"
    assert compact["recovery_compaction"]["saved_bytes"] > len(original) * .9
    assert not target.exists() and not sources[0].exists()
    assert not (root / ops.STATE_DIRECTORY / "quarantine").exists()
    assert not (root / ops.STATE_DIRECTORY / "retained").exists()
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == original
    assert not (root / ops.STATE_DIRECTORY / "retained").exists()


def test_identical_incompressible_copies_store_one_independent_payload(tmp_path):
    raw = os.urandom(payload.CHUNK_SIZE)
    root, sources, result = removed(tmp_path, [raw, raw])
    plan = ops.compaction_preview(root, result["id"])
    assert plan["duplicate_bytes"] == len(raw)
    compact = ops.compact_operation(root, result["id"], plan=plan)
    assert compact["recovery_compaction"]["processed"] == 2
    assert len(object_files(root)) == 1
    assert compact["recovery_compaction"]["saved_bytes"] == len(raw)
    assert all(not source.exists() for source in sources)
    assert all(not Path(item["target"]).exists() for item in result["items"])
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert all(source.read_bytes() == raw for source in sources)


def test_unique_incompressible_file_keeps_raw_without_committed_objects(tmp_path):
    raw = os.urandom(payload.CHUNK_SIZE)
    root, sources, result = removed(tmp_path, [raw])
    compact = ops.compact_operation(root, result["id"])
    assert compact["recovery_compaction"]["status"] == "unchanged"
    assert Path(result["items"][0]["target"]).read_bytes() == raw
    assert object_files(root) == []
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == raw


def test_packed_undo_conflict_preserves_user_file_and_independent_payload(tmp_path):
    raw = b"packed original" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    compact = ops.compact_operation(root, result["id"])
    sources[0].write_bytes(b"new user file")
    assert ops.undo_operation(root, result["id"])["status"] == "partial_undo"
    assert sources[0].read_bytes() == b"new user file"
    descriptor = compact["items"][0]["target_payload"]
    assert payload.verify_payload(root, descriptor)["verified_bytes"] == len(raw)


def test_corrupt_object_never_creates_visible_partial_restore(tmp_path):
    raw = b"packed original" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    ops.compact_operation(root, result["id"])
    object_files(root)[0].write_bytes(b"corrupt")
    restored = ops.undo_operation(root, result["id"])
    assert restored["status"] == "partial_undo" and not sources[0].exists()
    assert restored["items"][0]["state"] == "undo_failed"


def test_changed_private_capture_is_preserved_instead_of_retired(tmp_path, monkeypatch):
    raw = b"original" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    original_rename = ops._rename_anchored
    def change_after_capture(source, target):
        original_rename(source, target)
        if target.name.startswith(".compact-"):
            target.write_bytes(b"unexpected changed bytes")
    monkeypatch.setattr(ops, "_rename_anchored", change_after_capture)
    compact = ops.compact_operation(root, result["id"])
    assert compact["recovery_compaction"]["status"] == "unchanged"
    assert Path(result["items"][0]["target"]).read_bytes() == b"unexpected changed bytes"
    assert compact["items"][0]["target_packed"] is False
    assert payload.verify_payload(root, compact["items"][0]["target_payload"])["verified_bytes"] == len(raw)


def test_payload_reference_and_retirement_intent_are_durable_before_raw_retirement(tmp_path, monkeypatch):
    root, _sources, result = removed(tmp_path, [b"data" * 300_000])
    original = ops._remove_verified_private
    inspected = []
    def inspect(path, fingerprint, *, compaction=False):
        assert compaction is True
        key = ops._key(root / ops.STATE_DIRECTORY)
        record = ops._read_journal(Path(result["journal_path"]), root, key)
        item = record["items"][0]
        assert item["target_packed"] is True
        assert item["target_compaction"]["state"] == "retire_pending"
        payload.verify_payload(root, item["target_payload"])
        inspected.append(path)
        return original(path, fingerprint, compaction=compaction)
    monkeypatch.setattr(ops, "_remove_verified_private", inspect)
    assert ops.compact_operation(root, result["id"])["recovery_compaction"]["status"] == "completed"
    assert len(inspected) == 1


def test_slow_or_unsupported_packing_falls_back_to_existing_raw_restore(tmp_path, monkeypatch):
    raw = b"original" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    def fail(*_args, **_kwargs):
        raise payload.PayloadLimitError("time budget")
    monkeypatch.setattr(payload, "pack_file", fail)
    compact = ops.compact_operation(root, result["id"])
    assert compact["recovery_compaction"]["status"] == "unchanged"
    assert Path(result["items"][0]["target"]).read_bytes() == raw
    assert object_files(root) == []
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == raw


def test_legacy_quarantine_journal_keeps_compaction_and_restore_compatibility(tmp_path):
    root = tmp_path / "Legacy"
    root.mkdir()
    source = root / "original.txt"
    raw = b"legacy source" * 100_000
    source.write_bytes(raw)
    plan = ops.build_plan(root, [str(source)], "quarantine")
    plan.pop("deletion_storage")
    plan["items"][0].pop("deletion_storage")
    plan["items"][0]["target"] = str(root / ops.STATE_DIRECTORY / "quarantine" / plan["id"] / source.name)
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    legacy = ops.execute_plan(plan)
    compact = ops.compact_operation(root, legacy["id"])
    assert compact["recovery_compaction"]["processed"] == 2
    assert ops.undo_operation(root, legacy["id"])["status"] == "undone"
    assert source.read_bytes() == raw


def test_small_and_stale_compaction_previews_keep_existing_files(tmp_path):
    root, sources, result = removed(tmp_path, [b"small data"])
    plan = ops.compaction_preview(root, result["id"])
    compact = ops.compact_operation(root, result["id"], plan=plan)
    assert compact["recovery_compaction"]["status"] == "unchanged" and object_files(root) == []
    with pytest.raises(ops.OperationError):
        ops.compact_operation(root, result["id"], plan=plan)
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == b"small data"


def test_failed_payload_reference_checkpoint_publishes_no_objects(tmp_path, monkeypatch):
    raw = b"important original bytes" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    original_write = ops._write_journal
    failed = []
    def fail_first_prepared(path, journal, key, **kwargs):
        if not failed and journal["items"][0].get("target_compaction", {}).get("state") == "packing":
            failed.append(True)
            raise OSError("checkpoint storage unavailable")
        return original_write(path, journal, key, **kwargs)
    monkeypatch.setattr(ops, "_write_journal", fail_first_prepared)
    compact = ops.compact_operation(root, result["id"])
    assert failed and compact["recovery_compaction"]["status"] == "unchanged"
    assert object_files(root) == []
    assert Path(result["items"][0]["target"]).read_bytes() == raw
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == raw


def test_interrupted_packed_restore_after_atomic_move_resumes_without_overwrite(tmp_path):
    raw = b"original byte sequence" * 100_000
    root, sources, result = removed(tmp_path, [raw])
    ops.compact_operation(root, result["id"])
    key = ops._key(root / ops.STATE_DIRECTORY)
    path = Path(result["journal_path"])
    record = ops._read_journal(path, root, key)
    item = record["items"][0]
    temporary = root / ops.STATE_DIRECTORY / "materialized" / result["id"] / "interrupted"
    temporary.parent.mkdir(parents=True)
    fingerprint = payload.materialize(root, item["target_payload"], temporary)
    item.update(restore_state="moving_back_packed", restore_fingerprint=fingerprint)
    ops._write_journal(path, record, key)
    ops._move_no_replace(temporary, sources[0])  # Stopped before the result checkpoint.
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert sources[0].read_bytes() == raw
    assert not (root / ops.STATE_DIRECTORY / "retained").exists()
