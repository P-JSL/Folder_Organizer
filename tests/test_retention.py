from __future__ import annotations

import os
from pathlib import Path

import pytest

from folder_organizer.matching import find_duplicates
from folder_organizer.models import MatchGroup
from folder_organizer.retention import choose_duplicate_copies
from folder_organizer.scanner import scan_folder


def write(root: Path, name: str, timestamp: int = 10) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"verified original bytes")
    os.utime(path, ns=(timestamp * 1_000_000_000, timestamp * 1_000_000_000))
    return path


def inventory(root: Path):
    files = scan_folder(root).files
    return files, find_duplicates(files)


@pytest.mark.parametrize("rule,expected", [("first", "a.txt"), ("oldest", "b.txt"), ("newest", "c.txt"), ("shortest_path", "a.txt")])
def test_each_rule_keeps_one_verified_file_without_mutation(tmp_path, rule, expected):
    paths = [write(tmp_path, "a.txt", 20), write(tmp_path, "b.txt", 10), write(tmp_path, "c.txt", 30)]
    files, groups = inventory(tmp_path)
    result = choose_duplicate_copies(groups, files, rule)
    assert [Path(path).name for path in result["kept"]] == [expected]
    assert len(result["selected"]) == 2
    assert result["decisions"][0]["keeper"] in result["kept"]
    assert all(path.read_bytes() == b"verified original bytes" for path in paths)


def test_timestamp_ties_are_deterministic_and_order_independent(tmp_path):
    write(tmp_path, "z.txt")
    write(tmp_path, "a.txt")
    files, groups = inventory(tmp_path)
    groups[0].paths.reverse()
    result = choose_duplicate_copies(groups, list(reversed(files)), "newest")
    assert Path(result["kept"][0]).name == "a.txt"


def test_preferred_folder_is_component_containment_not_prefix(tmp_path):
    preferred = write(tmp_path, "originals/keep.txt")
    other = write(tmp_path, "originals-extra/copy.txt")
    files, groups = inventory(tmp_path)
    result = choose_duplicate_copies(groups, files, "preferred_folder", str(preferred.parent))
    assert result["kept"] == [str(preferred)]
    assert result["selected"] == [str(other)]


def test_all_protected_members_are_kept_even_with_newer_unprotected_copy(tmp_path):
    one = write(tmp_path, "protected/one.txt", 10)
    two = write(tmp_path, "protected/nested/two.txt", 20)
    copy = write(tmp_path, "other/copy.txt", 30)
    files, groups = inventory(tmp_path)
    result = choose_duplicate_copies(groups, files, "newest", protected_folders=[str(tmp_path / "protected")])
    assert set(result["kept"]) == {str(one), str(two)}
    assert result["selected"] == [str(copy)]


def test_similar_and_explicitly_unverified_groups_are_not_selected(tmp_path):
    one, two = write(tmp_path, "one.txt"), write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    similar = MatchGroup("similar_text", [str(one), str(two)], "near only")
    assert choose_duplicate_copies([similar], files)["selected"] == []
    assert choose_duplicate_copies([similar], files)["warnings"]
    # Exercise the evidence field without depending on which task adds it first.
    groups[0].evidence = {"byte_identical": False}
    assert choose_duplicate_copies(groups, files)["selected"] == []


def test_legacy_verified_group_without_evidence_is_supported(tmp_path):
    one, two = write(tmp_path, "one.txt"), write(tmp_path, "two.txt")
    files, _ = inventory(tmp_path)
    group = MatchGroup("duplicate", [str(one), str(two)], "legacy byte verification")
    assert len(choose_duplicate_copies([group], files)["selected"]) == 1


def test_overlapping_groups_are_all_skipped(tmp_path):
    paths = [write(tmp_path, name) for name in ("one.txt", "two.txt", "three.txt")]
    files, _ = inventory(tmp_path)
    groups = [MatchGroup("duplicate", [str(paths[0]), str(paths[1])], "verified"),
              MatchGroup("duplicate", [str(paths[1]), str(paths[2])], "verified")]
    result = choose_duplicate_copies(groups, files)
    assert result["selected"] == []
    assert len(result["kept"]) == 3
    assert all(decision["status"] == "skipped" for decision in result["decisions"])


@pytest.mark.parametrize("change", ["edit", "missing", "symlink"])
def test_stale_or_unavailable_member_skips_the_entire_group(tmp_path, change):
    one, two = write(tmp_path, "one.txt"), write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    if change == "edit":
        two.write_bytes(b"changed since matching")
    else:
        two.unlink()
        if change == "symlink":
            try:
                two.symlink_to(one)
            except OSError:
                pytest.skip("symlinks unavailable")
    result = choose_duplicate_copies(groups, files)
    assert result["selected"] == []
    assert result["warnings"]
    assert result["decisions"][0]["status"] == "skipped"


def test_unknown_group_member_and_invalid_rule_are_rejected(tmp_path):
    write(tmp_path, "one.txt")
    write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    groups[0].paths.append(str(tmp_path / "not-scanned.txt"))
    assert choose_duplicate_copies(groups, files)["selected"] == []
    with pytest.raises(ValueError):
        choose_duplicate_copies([], files, "unverified quality")
    with pytest.raises(ValueError):
        choose_duplicate_copies([], files, "preferred_folder")


def test_shortest_path_and_missing_preferred_folder_explain_fallback(tmp_path):
    short = write(tmp_path, "x.txt")
    write(tmp_path, "long/folder/path/copy.txt")
    files, groups = inventory(tmp_path)
    assert choose_duplicate_copies(groups, files, "shortest_path")["kept"] == [str(short)]
    result = choose_duplicate_copies(groups, files, "preferred_folder", str(tmp_path / "absent"))
    assert len(result["kept"]) == 1
    assert result["warnings"]


def test_same_size_edit_preserving_timestamp_is_rejected_by_verified_digest(tmp_path):
    first, second = write(tmp_path, "one.txt"), write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    before = second.stat()
    second.write_bytes(b"X" * before.st_size)
    os.utime(second, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = choose_duplicate_copies(groups, files)
    assert result["selected"] == []
    assert len(result["kept"]) == 2
    assert any("내용이 변경" in warning for warning in result["warnings"])
    assert first.read_bytes() != second.read_bytes()


def test_invalid_digest_is_not_treated_as_legacy_and_uppercase_digest_is_accepted(tmp_path):
    write(tmp_path, "one.txt")
    write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    valid = groups[0].evidence["sha256"]
    groups[0].evidence["sha256"] = "bad digest"
    assert choose_duplicate_copies(groups, files)["selected"] == []
    groups[0].evidence["sha256"] = valid.upper()
    assert len(choose_duplicate_copies(groups, files)["selected"]) == 1


def test_cancelled_selection_assistant_returns_no_partial_selection(tmp_path):
    from threading import Event
    write(tmp_path, "one.txt")
    write(tmp_path, "two.txt")
    files, groups = inventory(tmp_path)
    event = Event()
    with pytest.raises(ValueError, match="중단"):
        choose_duplicate_copies(groups, files, cancel=event, progress=lambda message: event.set())
    assert all(Path(file.path).exists() for file in files)
