"""Cross-module regressions found during independent data-safety review."""
from pathlib import Path

import pytest

from folder_organizer.matching import find_duplicates
from folder_organizer.operations import OperationError, build_plan, execute_plan
from folder_organizer.scanner import scan_folder


@pytest.mark.parametrize("mode", ["quarantine", "move", "date", "extension"])
def test_app_bundle_members_can_be_inventoried_but_never_split(tmp_path, mode):
    root = tmp_path / "applications"
    executable = root / "Editor.app" / "Contents" / "MacOS" / "Editor"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"an app executable")
    entries = scan_folder(root).files
    assert len(entries) == 1
    assert entries[0].kind == "application"
    with pytest.raises(OperationError, match="번들"):
        build_plan(root, [entries[0].path], mode, str(tmp_path / "out"))
    assert executable.read_bytes() == b"an app executable"


def test_app_bundle_root_does_not_hide_bundle_ancestor_from_guard(tmp_path):
    root = tmp_path / "Editor.APP" / "Contents"
    root.mkdir(parents=True)
    source = root / "Info.plist"
    source.write_bytes(b"app metadata")
    with pytest.raises(OperationError, match="번들"):
        build_plan(root, [str(source)], "quarantine")


def test_bundle_files_can_be_copied_without_changing_app(tmp_path):
    root = tmp_path / "apps"
    source = root / "Editor.app" / "Contents" / "Info.plist"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"app metadata")
    plan = build_plan(root, [str(source)], "copy", str(tmp_path / "backup"))
    result = execute_plan(plan)
    assert result["status"] == "completed"
    assert source.read_bytes() == b"app metadata"
    assert Path(result["items"][0]["target"]).read_bytes() == b"app metadata"


def test_hardlink_aliases_do_not_displace_separate_duplicate_keeper(tmp_path):
    import os
    root = tmp_path / "files"
    root.mkdir()
    keeper = root / "a.txt"
    alias = root / "b.txt"
    separate_copy = root / "c.txt"
    keeper.write_bytes(b"same document")
    separate_copy.write_bytes(b"same document")
    try:
        os.link(keeper, alias)
    except OSError:
        pytest.skip("Hardlinks unavailable")
    groups = find_duplicates(scan_folder(root).files)
    assert len(groups) == 1
    assert groups[0].paths == [str(keeper), str(separate_copy)]
    result = execute_plan(build_plan(root, groups[0].paths[1:], "quarantine"))
    assert result["status"] == "completed"
    assert keeper.read_bytes() == alias.read_bytes() == b"same document"
    assert not separate_copy.exists()


def test_regular_file_replaced_with_same_size_and_mtime_is_not_duplicate(tmp_path):
    import os
    root = tmp_path / "files"
    root.mkdir()
    first = root / "a.txt"
    second = root / "b.txt"
    first.write_bytes(b"original")
    second.write_bytes(b"original")
    entries = scan_folder(root).files
    timestamp = second.stat().st_mtime_ns
    replacement = root / "replacement"
    replacement.write_bytes(b"original")
    os.utime(replacement, ns=(timestamp, timestamp))
    replacement.replace(second)
    assert find_duplicates(entries) == []


@pytest.mark.parametrize("extension", ["CON", "nul", "PRN", "aux", "com1", "LPT9"])
def test_extension_organization_escapes_windows_reserved_folder_names(tmp_path, extension):
    root = tmp_path / "files"
    root.mkdir()
    source = root / f"report.{extension}"
    source.write_bytes(b"valuable content")
    plan = build_plan(root, [str(source)], "extension")
    assert Path(plan["items"][0]["target"]).relative_to(root).parts[1] == "_" + extension.casefold()
    assert execute_plan(plan)["status"] == "completed"
