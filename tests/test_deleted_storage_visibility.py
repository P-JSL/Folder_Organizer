"""Deleted-storage folders are excluded; observing NAS trash never deletes."""
from pathlib import Path

from folder_organizer.deletion import nas_recycle_observation, is_network_location
from folder_organizer.scanner import scan_folder


def test_deleted_items_do_not_reappear_even_with_hidden_files_included(tmp_path):
    (tmp_path / "active.txt").write_bytes(b"live")
    for name in ("delete_files", "#recycle", "@Recycle", "$RECYCLE.BIN", ".folder-organizer"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "deleted.txt").write_bytes(b"previously deleted")
    for hidden in (False, True):
        scan = scan_folder(tmp_path, include_hidden=hidden)
        assert [entry.name for entry in scan.files] == ["active.txt"]
    assert all(path.is_file() for path in tmp_path.glob("*/deleted.txt"))


def test_nas_folder_observation_is_read_only_and_does_not_prove_recycling(tmp_path):
    recycle = tmp_path / "#recycle"
    recycle.mkdir()
    sentinel = recycle / "old.txt"
    sentinel.write_bytes(b"previously recycled")
    result = nas_recycle_observation(tmp_path)
    assert result["nas_recycle_detected"]
    assert result["nas_recycle_path"] == str(recycle)
    assert not result["nas_recycle_policy_verified"]
    assert sentinel.read_bytes() == b"previously recycled"
    assert set(tmp_path.iterdir()) == {recycle}


def test_symlink_is_not_accepted_as_a_nas_recycle_folder(tmp_path):
    import pytest
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    try:
        (tmp_path / "#recycle").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable")
    assert not nas_recycle_observation(tmp_path)["nas_recycle_detected"]


def test_unc_and_mapped_nas_are_recognized_without_assuming_recycle_support(monkeypatch):
    from folder_organizer import deletion
    assert is_network_location(r"\\nas\share\folder")
    monkeypatch.setattr(deletion, "_is_windows", lambda: True)
    monkeypatch.setattr(deletion, "_drive_type", lambda _root: 4)
    assert is_network_location("Z:\\")
    monkeypatch.setattr(deletion, "_drive_type", lambda _root: 3)
    assert not is_network_location("C:\\")


def test_native_network_root_policy_keeps_fixed_drive_guard(monkeypatch, tmp_path):
    import os
    import pytest
    from folder_organizer import operations, deletion
    if os.name != "nt":
        pytest.skip("Windows drive-root validation")
    anchor = Path(tmp_path.anchor)
    monkeypatch.setattr(deletion, "is_network_location", lambda _path: False)
    with pytest.raises(operations.OperationError):
        operations._root(anchor)
    monkeypatch.setattr(deletion, "is_network_location", lambda _path: True)
    assert operations._root(anchor) == anchor  # Read-only validation only.
