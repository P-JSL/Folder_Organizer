"""Compressed recipes retain authenticated checkpoints and real restore bytes."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_metadata as metadata


@pytest.fixture
def completed(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    sources = []
    for number in range(4):
        path = root / f"report-{number}.txt"
        path.write_bytes((f"Report {number} ".encode()) * 100)
        sources.append(path)
    result = ops.execute_plan(ops.build_plan(root, list(map(str, sources)), "quarantine"))
    assert result["status"] == "completed"
    path = Path(result["journal_path"])
    assert result["recovery_metadata"]["format"] == "gzip"
    return root, sources, result, path


def test_gzip_is_preferred_without_contaminating_signed_journal(completed):
    root, sources, result, path = completed
    info = result["recovery_metadata"]
    assert info["compressed_bytes"] < info["uncompressed_bytes"]
    assert info["metadata_only"] and info["requires_retained_data"]
    key = ops._key(root / ops.STATE_DIRECTORY)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "recovery_metadata" not in raw
    ops._check_signature(raw, key)
    listed = ops.list_history(root)
    assert listed[0]["recovery_metadata"]["format"] == "gzip"
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert all(source.exists() for source in sources)


def test_active_compressed_recipe_restores_when_json_checkpoint_is_missing(completed):
    root, sources, result, path = completed
    path.unlink()
    assert ops.list_history(root)[0]["recovery_metadata"]["format"] == "gzip"
    restored = ops.undo_operation(root, result["id"])
    assert restored["status"] == "undone" and path.exists()
    assert all(source.exists() for source in sources)


@pytest.mark.parametrize("part", ["gzip", "receipt"])
def test_corrupt_compact_data_falls_back_to_valid_json(completed, part):
    root, sources, result, path = completed
    gzip_path, receipt = metadata.paths(path)
    (gzip_path if part == "gzip" else receipt).write_bytes(b"corrupt")
    listed = ops.list_history(root)[0]
    assert listed["recovery_metadata"]["format"] == "legacy"
    assert listed["recovery_metadata_warning"]
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert all(source.exists() for source in sources)


def test_existing_tampered_json_never_uses_valid_gzip_to_bypass_authentication(completed):
    root, _sources, result, path = completed
    record = json.loads(path.read_text(encoding="utf-8"))
    record["items"][0]["source"] = str(root / "outside-change.txt")
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ops.OperationError):
        ops.list_history(root)
    with pytest.raises(ops.OperationError):
        ops.undo_operation(root, result["id"])


def test_inactive_recipe_does_not_restore_after_a_working_checkpoint_is_lost(completed):
    root, _sources, result, path = completed
    key = ops._key(root / ops.STATE_DIRECTORY)
    record = ops._read_journal(path, root, key)
    record["status"] = "partial_undo"
    ops._write_journal(path, record, key)
    path.unlink()
    with pytest.raises(ops.OperationError, match="활성 압축"):
        ops.undo_operation(root, result["id"])


def test_missing_receipt_uses_legacy_and_explains_fallback(completed):
    root, _sources, _result, path = completed
    metadata.paths(path)[1].unlink()
    listed = ops.list_history(root)[0]
    assert listed["recovery_metadata"]["format"] == "legacy" and listed["recovery_metadata_warning"]


@pytest.mark.parametrize("problem", ["error", "slow", "large", "unhelpful"])
def test_compression_failure_budget_or_size_keeps_legacy_undo(tmp_path, monkeypatch, problem):
    if problem == "error":
        monkeypatch.setattr(metadata.gzip, "compress", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("compressor unavailable")))
    elif problem == "slow":
        monkeypatch.setattr(metadata, "COMPRESSION_BUDGET_SECONDS", -1)
    elif problem == "large":
        monkeypatch.setattr(metadata, "MAX_RAW_BYTES", 1)
    else:
        monkeypatch.setattr(metadata.gzip, "compress", lambda raw, **_k: raw)
    root = tmp_path / "Folder"
    root.mkdir()
    sources = [root / f"file-{number}.txt" for number in range(3)]
    for source in sources:
        source.write_bytes(b"valuable bytes")
    result = ops.execute_plan(ops.build_plan(root, list(map(str, sources)), "quarantine"))
    assert result["recovery_metadata"]["format"] == "legacy"
    assert result["recovery_metadata_warning"]
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert all(source.read_bytes() == b"valuable bytes" for source in sources)


def test_gzip_bomb_and_excessive_nesting_are_bounded(completed):
    root, _sources, _result, path = completed
    key = ops._key(root / ops.STATE_DIRECTORY)
    gzip_path, _receipt_path = metadata.paths(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    oversized = gzip.compress(b"x" * (metadata.MAX_RAW_BYTES + 1), mtime=0)
    gzip_path.write_bytes(oversized)
    metadata._write_receipt(path, root, key, "final", codec="gzip", json_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            gzip_sha256=hashlib.sha256(oversized).hexdigest(), journal_signature=record["signature"],
                            uncompressed_bytes=metadata.MAX_RAW_BYTES + 1, compressed_bytes=len(oversized))
    assert ops.list_history(root)[0]["recovery_metadata"]["format"] == "legacy"
    with pytest.raises(metadata.MetadataError, match="중첩"):
        metadata._document(b'{"value":' + b"[" * 65 + b"0" + b"]" * 65 + b"}")


def test_compact_recipe_cannot_recover_missing_file_contents(completed):
    root, _sources, result, _path = completed
    for item in result["items"]:
        Path(item["target"]).unlink()
    restored = ops.undo_operation(root, result["id"])
    assert restored["status"] == "partial_undo"
    assert all(item["state"] == "undo_failed" for item in restored["items"])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is POSIX only")
def test_fifo_compact_file_does_not_block_or_change_legacy(completed):
    root, _sources, _result, path = completed
    gzip_path, _receipt = metadata.paths(path)
    gzip_path.unlink()
    os.mkfifo(gzip_path)
    started = time.monotonic()
    assert ops.list_history(root)[0]["recovery_metadata"]["format"] == "legacy"
    assert time.monotonic() - started < 2


def test_symlink_compact_file_is_not_followed(completed, tmp_path):
    root, _sources, _result, path = completed
    gzip_path, _receipt = metadata.paths(path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"leave unchanged")
    gzip_path.unlink()
    try:
        gzip_path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert ops.list_history(root)[0]["recovery_metadata"]["format"] == "legacy"
    assert outside.read_bytes() == b"leave unchanged"
