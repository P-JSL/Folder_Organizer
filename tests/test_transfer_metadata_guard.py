"""A primary-byte copy must not silently drop Windows streams/encryption."""
import os
from pathlib import Path

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_payload as payload


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "원본.txt"
    source.write_bytes(b"primary data must survive")
    return root, source, tmp_path / "destination"


def metadata_probe(monkeypatch, metadata):
    monkeypatch.setattr(payload, "_WINDOWS", True)
    def inspect(_path):
        if isinstance(metadata, Exception):
            raise metadata
        return metadata
    monkeypatch.setattr(payload, "_windows_compaction_metadata", inspect)


@pytest.mark.parametrize("mode", ["copy", "move", "rules"])
@pytest.mark.parametrize("metadata", [
    (0x80, ("::$DATA", ":meta:$DATA")),
    (0x4080, ("::$DATA",)),
    OSError(50, "stream enumeration unavailable"),
])
def test_preview_refuses_unrepresented_metadata_without_creating_output(files, monkeypatch, mode, metadata):
    root, source, target = files
    metadata_probe(monkeypatch, metadata)
    with pytest.raises(ops.OperationError):
        if mode == "rules":
            ops.build_routed_plan(root, [{"source": str(source), "relative_target": source.name,
                                         "action": "move"}], destination=target)
        else:
            ops.build_plan(root, [str(source)], mode, str(target))
    assert source.read_bytes() == b"primary data must survive"
    assert not target.exists()
    assert not (root / ops.STATE_DIRECTORY).exists()


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_stream_added_after_preview_does_not_publish_a_primary_only_copy(files, monkeypatch, mode):
    root, source, target = files
    metadata_probe(monkeypatch, (0x80, ("::$DATA",)))
    plan = ops.build_plan(root, [str(source)], mode, str(target))
    metadata_probe(monkeypatch, (0x80, ("::$DATA", ":late:$DATA")))
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.read_bytes() == b"primary data must survive"
    assert not Path(plan["items"][0]["target"]).exists()


def test_stream_added_during_copy_is_rejected_and_partial_output_is_hidden(files, monkeypatch):
    root, source, target = files
    metadata_probe(monkeypatch, (0x80, ("::$DATA",)))
    fingerprint = ops._fingerprint(source)
    output = target / source.name
    def created(_info):
        metadata_probe(monkeypatch, (0x80, ("::$DATA", ":late:$DATA")))
    with pytest.raises(ops.OperationError):
        ops._copy_verified(source, output, fingerprint, created)
    assert source.read_bytes() == b"primary data must survive"
    assert not output.exists()


def test_stream_added_at_move_capture_returns_complete_original(files, monkeypatch):
    root, source, target = files
    metadata_probe(monkeypatch, (0x80, ("::$DATA",)))
    plan = ops.build_plan(root, [str(source)], "move", str(target))
    rename = ops._rename_anchored
    def capture(original, holding):
        rename(original, holding)
        if original == source:
            metadata_probe(monkeypatch, (0x80, ("::$DATA", ":late:$DATA")))
    monkeypatch.setattr(ops, "_rename_anchored", capture)
    result = ops.execute_plan(plan)
    assert result["status"] != "completed"
    assert source.read_bytes() == b"primary data must survive"
    assert result["items"][0].get("recovery_path")


@pytest.mark.parametrize("mode", ["copy", "move"])
@pytest.mark.parametrize("raw", [b"", b"ordinary data"])
def test_representable_files_keep_copy_move_and_undo(files, monkeypatch, mode, raw):
    root, source, target = files
    source.write_bytes(raw)
    metadata_probe(monkeypatch, (0x80, ("::$DATA",) if raw else ()))
    result = ops.execute_plan(ops.build_plan(root, [str(source)], mode, str(target)))
    assert result["status"] == "completed"
    assert (target / source.name).read_bytes() == raw
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert source.read_bytes() == raw


@pytest.mark.skipif(os.name != "nt", reason="Requires actual NTFS streams on Windows")
@pytest.mark.parametrize("mode", ["copy", "move"])
@pytest.mark.parametrize("stream_bytes", [b"", b"private stream data"])
def test_native_named_stream_is_refused_without_losing_either_stream(files, mode, stream_bytes):
    root, source, target = files
    stream = str(source) + ":meta"
    with open(stream, "wb") as output:
        output.write(stream_bytes)
    with pytest.raises(ops.OperationError):
        ops.build_plan(root, [str(source)], mode, str(target))
    assert source.read_bytes() == b"primary data must survive"
    with open(stream, "rb") as original:
        assert original.read() == stream_bytes
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual NTFS streams on Windows")
def test_native_late_stream_with_unchanged_primary_metadata_is_refused(files):
    root, source, target = files
    plan = ops.build_plan(root, [str(source)], "move", str(target))
    before = source.stat()
    with open(str(source) + ":late", "wb") as output:
        output.write(b"late stream must stay at original path")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert source.read_bytes() == b"primary data must survive"
    with open(str(source) + ":late", "rb") as stream:
        assert stream.read() == b"late stream must stay at original path"
    assert not Path(plan["items"][0]["target"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows stream enumeration")
@pytest.mark.parametrize("mode", ["copy", "move"])
@pytest.mark.parametrize("raw", [b"", b"ordinary native Windows data"])
def test_native_representable_copy_move_and_undo(files, mode, raw):
    root, source, target = files
    source.write_bytes(raw)
    operation = ops.execute_plan(ops.build_plan(root, [str(source)], mode, str(target)))
    assert operation["status"] == "completed"
    assert (target / source.name).read_bytes() == raw
    assert ops.undo_operation(root, operation["id"])["status"] == "undone"
    assert source.read_bytes() == raw


@pytest.mark.skipif(os.name != "nt", reason="Requires actual NTFS streams on Windows")
def test_native_stream_added_during_copy_is_preserved_at_source(files):
    root, source, target = files
    fingerprint = ops._fingerprint(source)
    before = source.stat()
    stream_name = str(source) + ":during-copy"
    def created(_info):
        with open(stream_name, "wb") as stream:
            stream.write(b"metadata created while copying")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ops.OperationError):
        ops._copy_verified(source, target / source.name, fingerprint, created)
    assert source.read_bytes() == b"primary data must survive"
    with open(stream_name, "rb") as stream:
        assert stream.read() == b"metadata created while copying"
    assert not (target / source.name).exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual NTFS streams on Windows")
def test_native_stream_added_at_move_capture_returns_complete_original(files, monkeypatch):
    root, source, target = files
    plan = ops.build_plan(root, [str(source)], "move", str(target))
    before = source.stat()
    rename = ops._rename_anchored
    def capture(original, holding):
        rename(original, holding)
        if original == source:
            with open(str(holding) + ":at-capture", "wb") as stream:
                stream.write(b"metadata created at retirement")
            os.utime(holding, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(ops, "_rename_anchored", capture)
    result = ops.execute_plan(plan)
    assert result["status"] != "completed"
    assert source.read_bytes() == b"primary data must survive"
    with open(str(source) + ":at-capture", "rb") as stream:
        assert stream.read() == b"metadata created at retirement"
    assert result["items"][0].get("recovery_path")
