"""Lossless shared recovery data never depends on surviving user originals."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import stat
import threading
import zlib

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_payload as payload


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "Documents"
    directory.mkdir()
    return directory


def source_file(root, name="saved.bin", data=b"recovery bytes"):
    path = root / ops.STATE_DIRECTORY / "retained" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, ops._fingerprint(path)


def objects(root):
    return list((root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*.raw")) + list(
        (root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*.zlib"))


def object_path(root, chunk):
    return root / ops.STATE_DIRECTORY / "recovery-objects" / "v1" / chunk["sha256"][:2] / (chunk["sha256"] + "." + chunk["codec"])


def resign(root, descriptor):
    descriptor["signature"] = ops._sign(descriptor, ops._key(root / ops.STATE_DIRECTORY))
    return descriptor


def test_repeated_chunks_compress_once_and_restore_without_any_raw_original(root):
    raw = b"a" * payload.CHUNK_SIZE * 4 + b"last paragraph\n" * 40
    source, fingerprint = source_file(root, data=raw)
    descriptor = payload.pack_file(root, source, fingerprint)
    assert descriptor["stats"]["chunk_count"] == 5
    assert descriptor["stats"]["unique_chunks"] == 2
    assert descriptor["stats"]["compressed_chunks"] == 2
    assert descriptor["stats"]["stored_bytes"] < len(raw) // 100
    assert len(objects(root)) == 2 and source.read_bytes() == raw
    source.unlink()
    output = root / "Restored" / "original.bin"
    result = payload.materialize(root, descriptor, output)
    assert result["sha256"] == fingerprint["sha256"] and output.read_bytes() == raw
    assert result["mtime_ns"] == fingerprint["mtime_ns"]
    assert payload.verify_payload(root, descriptor)["verified_bytes"] == len(raw)


def test_high_entropy_duplicate_files_share_one_uncompressed_object(root):
    raw = os.urandom(payload.CHUNK_SIZE)
    first, info1 = source_file(root, "first.bin", raw)
    second, info2 = source_file(root, "second.bin", raw)
    first_plan = payload.pack_file(root, first, info1)
    second_plan = payload.pack_file(root, second, info2)
    assert first_plan["chunks"][0]["codec"] == "raw"
    assert first_plan["stats"]["new_bytes"] == len(raw)
    assert second_plan["stats"]["new_bytes"] == 0
    assert second_plan["stats"]["reused_bytes"] == len(raw)
    assert len(objects(root)) == 1
    first.unlink(); second.unlink()
    payload.materialize(root, first_plan, root / "first-restored.bin")
    payload.materialize(root, second_plan, root / "second-restored.bin")
    assert (root / "first-restored.bin").read_bytes() == (root / "second-restored.bin").read_bytes() == raw


def test_unhelpful_unique_data_falls_back_before_publishing_objects(root):
    raw = os.urandom(payload.CHUNK_SIZE + 99)
    source, fingerprint = source_file(root, data=raw)
    with pytest.raises(payload.PayloadNotBeneficial):
        payload.pack_file(root, source, fingerprint, min_savings_ratio=.1)
    assert source.read_bytes() == raw and objects(root) == []


def test_cancelled_or_timed_out_encoding_keeps_raw_and_publishes_no_objects(root, monkeypatch):
    source, fingerprint = source_file(root, data=b"a" * payload.CHUNK_SIZE * 3)
    cancel = threading.Event()
    def progress(detail):
        if detail["completed"]:
            cancel.set()
    with pytest.raises(payload.PayloadLimitError):
        payload.pack_file(root, source, fingerprint, cancel=cancel, progress_detail=progress)
    assert source.is_file() and objects(root) == []
    values = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(payload.time, "monotonic", lambda: next(values, 2.0))
    with pytest.raises(payload.PayloadLimitError):
        payload.pack_file(root, source, fingerprint, time_budget=1)
    assert source.is_file() and objects(root) == []


def test_same_size_edit_with_restored_mtime_is_rejected(root):
    original = b"a" * payload.CHUNK_SIZE * 2
    source, fingerprint = source_file(root, data=original)
    changed = False
    def progress(detail):
        nonlocal changed
        if detail["completed"] and not changed:
            changed = True
            with source.open("r+b") as stream:
                stream.write(b"CHANGED")
            os.utime(source, ns=(fingerprint["mtime_ns"], fingerprint["mtime_ns"]))
    with pytest.raises(ValueError, match="변경"):
        payload.pack_file(root, source, fingerprint, progress_detail=progress)
    assert source.read_bytes().startswith(b"CHANGED") and objects(root) == []


def test_short_regular_file_reads_do_not_change_chunk_boundaries(root, monkeypatch):
    raw = os.urandom(payload.CHUNK_SIZE + 4097)
    source, fingerprint = source_file(root, data=raw)
    original_read = os.read
    monkeypatch.setattr(payload.os, "read", lambda fd, amount: original_read(fd, min(amount, 117_123)))
    descriptor = payload.pack_file(root, source, fingerprint)
    assert [chunk["size"] for chunk in descriptor["chunks"]] == [payload.CHUNK_SIZE, 4097]
    output = root / "restored.bin"
    payload.materialize(root, descriptor, output)
    assert output.read_bytes() == raw


def test_existing_object_is_verified_never_overwritten(root):
    source, fingerprint = source_file(root, data=b"test" * 1000)
    descriptor = payload.pack_file(root, source, fingerprint)
    path = object_path(root, descriptor["chunks"][0])
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        payload.pack_file(root, source, fingerprint)
    assert path.read_bytes() == b"corrupted"
    output = root / "must-not-exist.bin"
    with pytest.raises(ValueError):
        payload.materialize(root, descriptor, output)
    assert not output.exists() and source.is_file()


def test_corrupt_last_object_is_rejected_before_creating_output(root):
    source, fingerprint = source_file(root, data=b"a" * payload.CHUNK_SIZE + b"tail")
    descriptor = payload.pack_file(root, source, fingerprint)
    object_path(root, descriptor["chunks"][-1]).write_bytes(b"bad")
    with pytest.raises(ValueError):
        payload.materialize(root, descriptor, root / "restored.bin")
    assert not (root / "restored.bin").exists()


def test_existing_output_and_signed_recipe_are_never_silently_replaced(root):
    source, fingerprint = source_file(root, data=b"private restore")
    descriptor = payload.pack_file(root, source, fingerprint)
    output = root / "restored.bin"
    output.write_bytes(b"a new user file")
    with pytest.raises(FileExistsError):
        payload.materialize(root, descriptor, output)
    assert output.read_bytes() == b"a new user file"
    edited = copy.deepcopy(descriptor)
    edited["mtime_ns"] += 1
    with pytest.raises(ValueError):
        payload.verify_payload(root, edited)
    other_root = root.parent / "Other"
    other_root.mkdir()
    with pytest.raises(ValueError, match="폴더"):
        payload.verify_payload(other_root, descriptor)


@pytest.mark.parametrize("change", [
    lambda d: d.update(chunks={"not": "a list"}),
    lambda d: d["chunks"][0].update(sha256="../../outside"),
    lambda d: d["chunks"][0].update(codec="pickle"),
    lambda d: d["chunks"][0].update(size=payload.CHUNK_SIZE + 1),
    lambda d: d["chunks"][0].update(stored_size=payload.MAX_STORED_CHUNK + 1),
    lambda d: d.update(stats={"nested": {"a": [1]}}),
    lambda d: d.update(extra="unexpected"),
])
def test_even_authenticated_malformed_recipes_are_rejected(root, change):
    source, fingerprint = source_file(root, data=b"a" * 1024)
    descriptor = payload.pack_file(root, source, fingerprint)
    change(descriptor)
    resign(root, descriptor)
    with pytest.raises(ValueError):
        payload.verify_payload(root, descriptor)


def test_bounded_decompressor_rejects_an_authenticated_compression_bomb(root):
    source, fingerprint = source_file(root, data=b"legitimate")
    descriptor = payload.pack_file(root, source, fingerprint)
    expanded = b"B" * payload.CHUNK_SIZE * 4
    stored = zlib.compress(expanded, level=1)
    chunk = {"sha256": hashlib.sha256(expanded).hexdigest(), "size": 1024,
             "codec": "zlib", "stored_size": len(stored), "stored_sha256": hashlib.sha256(stored).hexdigest()}
    target = object_path(root, chunk)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stored)
    descriptor["chunks"], descriptor["size"] = [chunk], 1024
    resign(root, descriptor)
    with pytest.raises(ValueError, match="제한"):
        payload.verify_payload(root, descriptor)


def test_empty_file_needs_no_payload_object(root):
    source, fingerprint = source_file(root, data=b"")
    descriptor = payload.pack_file(root, source, fingerprint)
    assert descriptor["chunks"] == [] and objects(root) == []
    result = payload.materialize(root, descriptor, root / "empty-restored.bin")
    assert result["size"] == 0 and result["sha256"] == hashlib.sha256(b"").hexdigest()


def test_symlink_objects_and_output_parent_links_are_refused(root):
    source, fingerprint = source_file(root, data=b"a" * 4096)
    descriptor = payload.pack_file(root, source, fingerprint)
    target = object_path(root, descriptor["chunks"][0])
    actual = root / "actual"
    target.rename(actual)
    try:
        target.symlink_to(actual)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        payload.verify_payload(root, descriptor)
    target.unlink(); actual.rename(target)
    outside = root.parent / "Outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        payload.materialize(root, descriptor, root / "linked" / "new.bin")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_fifo_sources_and_objects_are_rejected_without_blocking(root):
    source, fingerprint = source_file(root, data=b"a" * 4096)
    descriptor = payload.pack_file(root, source, fingerprint)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        payload.pack_file(root, fifo, fingerprint)
    target = object_path(root, descriptor["chunks"][0])
    target.unlink(); os.mkfifo(target)
    with pytest.raises(ValueError):
        payload.verify_payload(root, descriptor)


def test_disk_write_failure_keeps_original_data_and_existing_outputs(root, monkeypatch):
    raw = b"important" * 1000
    source, fingerprint = source_file(root, data=raw)
    monkeypatch.setattr(payload, "_write_object", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        payload.pack_file(root, source, fingerprint)
    assert source.read_bytes() == raw
