from pathlib import Path

import pytest

from folder_organizer.scanner import normalize_exclude_patterns, scan_folder


def write(root: Path, name: str, content=b"1234"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scoped_inventory_skips_folder_before_traversal_and_reports_limits(tmp_path, monkeypatch):
    import folder_organizer.scanner as scanner
    write(tmp_path, "Backup/precious.txt")
    write(tmp_path, "nested/cache/other.txt")
    write(tmp_path, "temp.TMP")
    write(tmp_path, "small.txt", b"a")
    write(tmp_path, "large.txt", b"123456789")
    keep = write(tmp_path, "documents/keep.txt")
    original = scanner.os.scandir

    def guarded(path):
        assert Path(path).name.casefold() not in {"backup", "cache"}
        return original(path)

    monkeypatch.setattr(scanner.os, "scandir", guarded)
    result = scan_folder(tmp_path, exclude_patterns=["backup", "cache", "*.tmp"], min_size=2, max_size=8)
    assert [entry.path for entry in result.files] == [str(keep)]
    assert result.filtered_files == 3
    assert result.excluded_directories == 2
    assert result.scan_options["min_size"] == 2
    assert result.scan_options["exclude_patterns"] == ["backup", "cache", "*.tmp"]
    assert (tmp_path / "Backup/precious.txt").read_bytes() == b"1234"


def test_relative_patterns_and_windows_separators_are_portable(tmp_path):
    write(tmp_path, "reports/private/one.txt")
    write(tmp_path, "reports/public/two.txt")
    write(tmp_path, "private/three.txt")
    result = scan_folder(tmp_path, exclude_patterns=["reports\\private/"])
    assert {entry.relative_path.replace("\\", "/") for entry in result.files} == {"reports/public/two.txt", "private/three.txt"}
    assert result.excluded_directories == 1


@pytest.mark.parametrize("patterns", ["*.tmp", ["../secrets"], ["/absolute"], ["C:\\private"], ["a\0b"], ["a" * 513], [123], ["a"] * 101])
def test_invalid_patterns_are_rejected_before_traversal(tmp_path, patterns):
    with pytest.raises(ValueError):
        scan_folder(tmp_path, exclude_patterns=patterns)


@pytest.mark.parametrize("minimum,maximum", [(-1,None),(10,2),(0,-1),(True,None),(0,2.5)])
def test_invalid_size_ranges_do_not_create_partial_result(tmp_path, minimum, maximum):
    with pytest.raises(ValueError):
        scan_folder(tmp_path, min_size=minimum, max_size=maximum)


def test_internal_state_is_never_treated_as_user_files_even_with_hidden_enabled(tmp_path):
    write(tmp_path, ".FOLDER-ORGANIZER/key.bin")
    write(tmp_path, "keep.txt")
    result = scan_folder(tmp_path, include_hidden=True)
    assert [entry.name for entry in result.files] == ["keep.txt"]
