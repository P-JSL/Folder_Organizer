"""New byte-compaction guards refuse unrepresented Windows file properties."""
from pathlib import Path

import pytest

from folder_organizer import operations as ops
from folder_organizer import recovery_payload as payload


@pytest.mark.parametrize("metadata,reason", [
    ((0, ("::$DATA", ":Zone.Identifier:$DATA")), "ADS"),
    ((0x4000, ()), "EFS"),
])
def test_metadata_preflight_refuses_before_any_payload_publication(tmp_path, monkeypatch, metadata, reason):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "original.txt"
    raw = b"important data\n" * 4096
    source.write_bytes(raw)
    expected = ops._fingerprint(source)
    monkeypatch.setattr(payload, "_WINDOWS", True)
    monkeypatch.setattr(payload, "_windows_compaction_metadata", lambda _: metadata)
    with pytest.raises(payload.PayloadNotBeneficial, match=reason):
        payload.pack_file(root, source, expected)
    assert source.read_bytes() == raw
    assert not (root / ops.STATE_DIRECTORY / "recovery-objects").exists()


def test_unknown_stream_support_falls_back_without_creating_objects(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "original.txt"
    source.write_bytes(b"raw retained" * 4096)
    expected = ops._fingerprint(source)
    def unsupported(_):
        raise OSError("stream enumeration is not supported")
    monkeypatch.setattr(payload, "_WINDOWS", True)
    monkeypatch.setattr(payload, "_windows_compaction_metadata", unsupported)
    with pytest.raises(payload.PayloadNotBeneficial, match="완전히 확인"):
        payload.pack_file(root, source, expected)
    assert source.exists()
    assert not (root / ops.STATE_DIRECTORY / "recovery-objects").exists()


def test_stream_added_while_encoding_refuses_before_publishing_chunks(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "original.txt"
    source.write_bytes(b"raw retained" * 4096)
    calls = []
    def changed(path):
        calls.append(path)
        return (0, ("::$DATA",)) if len(calls) == 1 else (0, ("::$DATA", ":new:$DATA"))
    monkeypatch.setattr(payload, "_WINDOWS", True)
    monkeypatch.setattr(payload, "_windows_compaction_metadata", changed)
    with pytest.raises(payload.PayloadNotBeneficial, match="ADS"):
        payload.pack_file(root, source, ops._fingerprint(source))
    assert len(calls) == 2 and source.exists()
    assert not [p for p in (root / ops.STATE_DIRECTORY / "recovery-objects").rglob("*") if p.is_file()]


def test_capture_rechecks_metadata_before_retiring_raw_copy(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    root.mkdir()
    source = root / "original.txt"
    raw = b"raw retained" * 4096
    source.write_bytes(raw)
    operation = ops.execute_plan(ops.build_plan(root, [str(source)], "quarantine"))
    target = Path(operation["items"][0]["target"])
    calls = []
    def metadata(path):
        path = Path(path)
        calls.append(path)
        return (0, ("::$DATA", ":late:$DATA")) if path.name.startswith(".compact-") else (0, ("::$DATA",))
    monkeypatch.setattr(payload, "_WINDOWS", True)
    monkeypatch.setattr(payload, "_windows_compaction_metadata", metadata)
    result = ops.compact_operation(root, operation["id"])
    assert result["recovery_compaction"]["processed"] == 0
    assert any(path.name.startswith(".compact-") for path in calls)
    assert target.read_bytes() == raw
    assert result["items"][0]["target_packed"] is False
    assert "target_payload" not in result["items"][0]
    assert any("ADS" in warning for warning in result["recovery_compaction"]["warnings"])
    def no_primary_only_fallback(*args, **kwargs):
        raise AssertionError("Metadata refusal must restore the original raw file")
    monkeypatch.setattr(payload, "materialize", no_primary_only_fallback)
    assert ops.undo_operation(root, operation["id"])["status"] == "undone"
    assert source.read_bytes() == raw
