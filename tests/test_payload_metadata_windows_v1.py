"""New native Windows stream guard cases; skipped on other platforms."""
import os
from pathlib import Path

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_payload as payload

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows stream APIs")


def ads(path):
    return str(path) + ":Zone.Identifier"


def test_native_ads_raw_file_survives_compaction_and_undo(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "downloaded.txt"
    raw, extra = b"download content\n" * 4096, b"[ZoneTransfer]\r\nZoneId=3\r\n"
    source.write_bytes(raw)
    with open(ads(source), "wb") as stream:
        stream.write(extra)
    operation = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    assert operation["status"] == "completed"
    target = Path(operation["items"][0]["target"])
    with pytest.raises(payload.PayloadNotBeneficial, match="ADS"):
        payload.compaction_preflight(target)
    result = ops.compact_operation(root, operation["id"])
    assert result["recovery_compaction"]["processed"] == 0
    assert target.read_bytes() == raw
    with open(ads(target), "rb") as stream:
        assert stream.read() == extra
    assert not [p for p in (root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*") if p.is_file()]
    assert ops.undo_operation(root, operation["id"])["status"] == "undone"
    with open(ads(source), "rb") as stream:
        assert stream.read() == extra


def test_native_stream_added_after_capture_is_kept_with_original_file(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "document.txt"
    raw = b"content before stream\n" * 4096
    source.write_bytes(raw)
    operation = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    target = Path(operation["items"][0]["target"])
    rename = ops._rename_anchored
    def add_stream(original, captured):
        rename(original, captured)
        if captured.name.startswith(".compact-"):
            with open(ads(captured), "wb") as stream:
                stream.write(b"late stream must survive")
    monkeypatch.setattr(ops, "_rename_anchored", add_stream)
    result = ops.compact_operation(root, operation["id"])
    assert result["recovery_compaction"]["processed"] == 0
    assert "target_payload" not in result["items"][0]
    assert target.read_bytes() == raw
    with open(ads(target), "rb") as stream:
        assert stream.read() == b"late stream must survive"
    assert ops.undo_operation(root, operation["id"])["status"] == "undone"
    with open(ads(source), "rb") as stream:
        assert stream.read() == b"late stream must survive"


def test_native_raw_copy_is_kept_when_provider_stream_query_is_unavailable(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "document.txt"
    raw = b"provider fallback\n" * 4096
    source.write_bytes(raw)
    operation = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    target = Path(operation["items"][0]["target"])
    def unsupported(_):
        raise OSError(50, "The request is not supported")
    monkeypatch.setattr(payload, "_windows_compaction_metadata", unsupported)
    result = ops.compact_operation(root, operation["id"])
    assert result["recovery_compaction"]["processed"] == 0
    assert target.read_bytes() == raw
    assert not [p for p in (root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*") if p.is_file()]
