from pathlib import Path
from threading import Event

import pytest

from folder_organizer.scanner import scan_folder


def write(root: Path, name: str, value: bytes = b"hello") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def test_inventory_has_real_metadata_and_recursive_controls(tmp_path):
    source = write(tmp_path, "report.txt", b"1234")
    write(tmp_path, "sub/installer.EXE", b"executable")
    result = scan_folder(tmp_path)
    assert len(result.files) == 2
    entry = next(item for item in result.files if item.name == "report.txt")
    assert entry.path == str(source)
    assert entry.size == 4
    assert entry.modified == source.stat().st_mtime
    assert entry.mtime_ns == source.stat().st_mtime_ns
    assert next(item for item in result.files if item.name == "installer.EXE").kind == "application"
    assert result.total_size == 14
    assert len(scan_folder(tmp_path, recursive=False).files) == 1


def test_no_unix_metadata_change_time_is_fabricated_as_creation(tmp_path):
    source = write(tmp_path, "file.txt")
    entry = scan_folder(tmp_path).files[0]
    if hasattr(source.stat(), "st_birthtime"):
        assert entry.created_source == "birthtime"
    else:
        import os
        if os.name == "nt":
            assert entry.created_source == "windows_creation_time"
        else:
            assert entry.created is None
            assert entry.created_source == "unavailable"


def test_hidden_internal_and_symlinks_are_excluded(tmp_path):
    visible = write(tmp_path, "visible.txt")
    write(tmp_path, ".hidden.txt")
    write(tmp_path, ".hidden/secret.txt")
    write(tmp_path, ".folder-organizer/history.json")
    try:
        (tmp_path / "alias.txt").symlink_to(visible)
        (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    result = scan_folder(tmp_path)
    assert [item.name for item in result.files] == ["visible.txt"]
    included = scan_folder(tmp_path, include_hidden=True)
    assert {item.name for item in included.files} == {"visible.txt", ".hidden.txt", "secret.txt"}


def test_cancelled_scan_is_explicit_and_partial(tmp_path):
    write(tmp_path, "a.txt")
    write(tmp_path, "b.txt")
    event = Event()
    result = scan_folder(tmp_path, cancel=event, progress=lambda _: event.set())
    assert result.cancelled is True
    assert len(result.files) == 1
    event.set()
    assert scan_folder(tmp_path, cancel=event).files == []


def test_invalid_roots_raise_clear_error(tmp_path):
    with pytest.raises(ValueError, match="Cannot access"):
        scan_folder(tmp_path / "missing")
    source = write(tmp_path, "file.txt")
    with pytest.raises(ValueError, match="not a folder"):
        scan_folder(source)


def test_app_bundle_contents_are_classified_without_mutation(tmp_path):
    file = write(tmp_path, "Editor.app/Contents/MacOS/editor", b"binary")
    result = scan_folder(tmp_path)
    assert len(result.files) == 1
    assert result.files[0].kind == "application"
    assert file.read_bytes() == b"binary"


def test_folder_permission_failure_is_reported(tmp_path, monkeypatch):
    import folder_organizer.scanner as scanner
    original = scanner.os.scandir
    write(tmp_path, "blocked/file.txt")
    write(tmp_path, "visible.txt")

    def denied(path):
        if Path(path).name == "blocked":
            raise PermissionError("denied in test")
        return original(path)

    monkeypatch.setattr(scanner.os, "scandir", denied)
    result = scan_folder(tmp_path)
    assert [item.name for item in result.files] == ["visible.txt"]
    assert any("denied in test" in message for message in result.warnings)
