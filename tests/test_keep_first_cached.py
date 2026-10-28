"""One-click keep-first reuses only authenticated, unchanged live byte proof."""
import copy
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from folder_organizer import matching, operations as ops, rules
from folder_organizer.models import MatchGroup
from folder_organizer.scanner import scan_folder


def verified(tmp_path, count=3):
    for index in range(count):
        (tmp_path / f"report-{index}.txt").write_bytes(b"original duplicate content")
    scan = scan_folder(tmp_path)
    groups = matching.find_duplicates(scan.files)
    assert len(groups) == 1
    return scan, groups


def no_content_reads(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("The existing-result plan reread file content or reanalyzed")
    for target, name in ((matching, "find_duplicates"), (matching, "_digest"), (matching, "_equal"),
                         (ops, "_fingerprint"), (ops, "build_plan"), (ops, "build_routed_plan"),
                         (rules, "choose_duplicate_copies")):
        monkeypatch.setattr(target, name, denied)
    real_open = matching._open_snapshot
    class MetadataOnly:
        def __init__(self, stream): self.stream = stream
        def __getattr__(self, key): return getattr(self.stream, key)
        read = denied
    @contextmanager
    def metadata_open(entry):
        with real_open(entry) as stream:
            yield MetadataOnly(stream)
    monkeypatch.setattr(matching, "_open_snapshot", metadata_open)


def test_existing_result_plan_does_not_read_content_rescan_or_hash(tmp_path, monkeypatch):
    scan, groups = verified(tmp_path)
    first = groups[0].paths[0]
    no_content_reads(monkeypatch)
    plan = rules.build_existing_duplicate_plan(scan, groups)
    assert len(plan["items"]) == 2 and plan["reuse_verified_results"]
    assert plan["destination"] == str(tmp_path / "delete_files")
    assert all(Path(item["target"]).parent == tmp_path / "delete_files" and item["deletion_storage"] == "delete_files" for item in plan["items"])
    assert all(item["snapshot_reuse"] and item["duplicate_keeper"]["path"] == first for item in plan["items"])
    assert first not in {item["source"] for item in plan["items"]}
    assert all(item["sha256"] == groups[0].evidence["sha256"] and type(item["snapshot_ctime_ns"]) is int for item in plan["items"])
    ops._check_signature(plan, ops._PLAN_KEY)
    assert not (tmp_path / ops.STATE_DIRECTORY).exists()


def test_first_displayed_member_and_all_protected_members_are_kept(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"same")
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/b.txt").write_bytes(b"same")
    (tmp_path / "z.txt").write_bytes(b"same")
    scan = scan_folder(tmp_path)
    groups = matching.find_duplicates(scan.files)
    scan.files.reverse()  # Inventory display/model ordering cannot change the group keeper.
    plan = rules.build_existing_duplicate_plan(scan, groups, protected_folders=[str(tmp_path / "protected")])
    assert [Path(item["source"]).name for item in plan["items"]] == ["z.txt"]
    assert set(plan["decisions"][0]["kept"]) == {str(tmp_path / "a.txt"), str(tmp_path / "protected/b.txt")}


def test_all_protected_result_produces_no_mutations(tmp_path):
    scan, groups = verified(tmp_path)
    plan = rules.build_existing_duplicate_plan(scan, groups, protected_folders=[str(tmp_path)])
    assert not plan["items"] and plan["classification_summary"]["kept"] == 3


@pytest.mark.parametrize("change", ["evidence", "paths", "proof", "imported", "old_process"])
def test_forged_modified_imported_or_other_process_result_is_refused(tmp_path, monkeypatch, change):
    scan, groups = verified(tmp_path)
    group = groups[0]
    if change == "evidence": group.evidence["sha256"] = "0" * 64
    elif change == "paths": group.paths.reverse()
    elif change == "proof": group._snapshot_proof["snapshots"][0]["snapshot_ctime_ns"] += 1
    elif change == "imported": groups[0] = MatchGroup(**group.to_dict())
    else: monkeypatch.setattr(matching, "_DUPLICATE_PROOF_KEY", b"a different process key")
    with pytest.raises(ValueError, match="다시"):
        rules.build_existing_duplicate_plan(scan, groups)
    assert all(Path(entry.path).exists() for entry in scan.files)
    assert not (tmp_path / ops.STATE_DIRECTORY).exists()


def test_same_size_restored_mtime_edit_is_refused_without_rehash(tmp_path, monkeypatch):
    scan, groups = verified(tmp_path)
    path = Path(groups[0].paths[-1])
    before = path.stat()
    path.write_bytes(b"x" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    no_content_reads(monkeypatch)
    with pytest.raises(ValueError, match="변경"):
        rules.build_existing_duplicate_plan(scan, groups)
    assert path.read_bytes() == b"x" * before.st_size


def test_replaced_or_missing_keeper_is_refused(tmp_path):
    scan, groups = verified(tmp_path)
    Path(groups[0].paths[0]).unlink()
    with pytest.raises((OSError, ValueError)):
        rules.build_existing_duplicate_plan(scan, groups)
    assert all(Path(path).exists() for path in groups[0].paths[1:])


def test_overlapping_authenticated_groups_are_refused_as_a_whole(tmp_path):
    scan, groups = verified(tmp_path)
    with pytest.raises(ValueError, match="겹치"):
        rules.build_existing_duplicate_plan(scan, groups + [copy.deepcopy(groups[0])])
    assert not (tmp_path / ops.STATE_DIRECTORY).exists()


@pytest.mark.parametrize("phase", ["hash", "byte_compare"])
def test_proof_never_captures_a_new_ctime_after_changed_read_gap(tmp_path, monkeypatch, phase):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"old")
    scan = scan_folder(tmp_path)
    originals = {entry.path: Path(entry.path).stat() for entry in scan.files}
    def mutate():
        for path, info in originals.items():
            Path(path).write_bytes(b"new")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    if phase == "hash":
        real_digest = matching._digest
        count = 0
        def digest(entry, cancel):
            nonlocal count
            result = real_digest(entry, cancel)
            count += 1
            if count == 2: mutate()
            return result
        monkeypatch.setattr(matching, "_digest", digest)
    else:
        real_equal = matching._equal
        def equal(first, second, cancel):
            result = real_equal(first, second, cancel)
            mutate()
            return result
        monkeypatch.setattr(matching, "_equal", equal)
    assert matching.find_duplicates(scan.files) == []


def test_new_target_collision_is_preserved_and_reserved_name_is_unique(tmp_path, monkeypatch):
    scan, groups = verified(tmp_path)
    identifier = "a" * 32
    monkeypatch.setattr(rules.uuid, "uuid4", lambda: SimpleNamespace(hex=identifier))
    occupied = tmp_path / "delete_files" / Path(groups[0].paths[1]).name
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"preexisting user bytes")
    plan = rules.build_existing_duplicate_plan(scan, groups)
    assert plan["items"][0]["target"] != str(occupied)
    assert occupied.read_bytes() == b"preexisting user bytes"


def test_private_proof_is_not_exported_or_shared_between_groups(tmp_path):
    scan, groups = verified(tmp_path)
    assert "snapshot_proof" not in str(groups[0].to_dict())
    clone = MatchGroup(**groups[0].to_dict())
    with pytest.raises(ValueError): matching.validate_duplicate_proof(clone)
    proof = matching.validate_duplicate_proof(groups[0])
    proof[groups[0].paths[0]]["sha256"] = "0" * 64
    assert matching.validate_duplicate_proof(groups[0])[groups[0].paths[0]]["sha256"] == groups[0].evidence["sha256"]


def test_fast_execution_moves_to_single_delete_folder_without_rereading_keeper_or_sources(tmp_path, monkeypatch):
    scan, groups = verified(tmp_path)
    first = groups[0].paths[0]
    plan = rules.build_existing_duplicate_plan(scan, groups)
    def no_hash(*_args, **_kwargs):
        raise AssertionError("Fast deletion reread payload bytes")
    with monkeypatch.context() as guarded:
        guarded.setattr(ops, "_fingerprint", no_hash)
        guarded.setattr(ops, "_copy_verified", no_hash)
        guarded.setattr(matching, "find_duplicates", no_hash)
        result = ops.execute_plan(plan)
    assert result["status"] == "completed"
    assert Path(first).read_bytes() == b"original duplicate content"
    assert all(not Path(item["source"]).exists() and Path(item["target"]).parent == tmp_path / "delete_files" for item in result["items"])
    assert len(list((tmp_path / "delete_files").iterdir())) == 2
    assert not (tmp_path / ops.STATE_DIRECTORY / "quarantine").exists()
    assert not (tmp_path / ops.STATE_DIRECTORY / "retained").exists()
    restored = ops.undo_operation(tmp_path, result["id"])
    assert restored["status"] == "undone"
    assert all(Path(entry.path).read_bytes() == b"original duplicate content" for entry in scan.files)


def test_fast_execution_rejects_changed_keeper_before_creating_destination(tmp_path):
    scan, groups = verified(tmp_path)
    plan = rules.build_existing_duplicate_plan(scan, groups)
    keeper = Path(groups[0].paths[0])
    old = keeper.stat()
    keeper.write_bytes(b"x" * old.st_size)
    os.utime(keeper, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(ValueError, match="변경"):
        ops.execute_plan(plan)
    assert not (tmp_path / "delete_files").exists()
    assert not (tmp_path / ops.STATE_DIRECTORY).exists()


def test_same_basename_flat_targets_do_not_repeat_quadratic_collision_probes(tmp_path, monkeypatch):
    for index in range(80):
        folder = tmp_path / f"folder-{index:03}"
        folder.mkdir()
        (folder / "report.txt").write_bytes(b"duplicate")
    scan = scan_folder(tmp_path)
    groups = matching.find_duplicates(scan.files)
    real_exists, probes = Path.exists, 0
    def measured(path):
        nonlocal probes
        probes += 1
        return real_exists(path)
    monkeypatch.setattr(Path, "exists", measured)
    plan = rules.build_existing_duplicate_plan(scan, groups)
    assert len(plan["items"]) == 79
    assert len({item["target"] for item in plan["items"]}) == 79
    assert probes < 80 * 10  # A reset-to-zero naming loop performs >3,000 probes.


def test_keeper_edit_during_atomic_move_rolls_back_selected_source_without_overwriting_edit(tmp_path, monkeypatch):
    scan, groups = verified(tmp_path, 2)
    plan = rules.build_existing_duplicate_plan(scan, groups)
    item = plan["items"][0]
    keeper = Path(item["duplicate_keeper"]["path"])
    real_move = ops._move_no_replace
    def changed_keeper(source, target):
        real_move(source, target)
        if target == Path(item["target"]):
            before = keeper.stat()
            keeper.write_bytes(b"x" * before.st_size)
            os.utime(keeper, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(ops, "_move_no_replace", changed_keeper)
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert Path(item["source"]).read_bytes() == b"original duplicate content"
    assert keeper.read_bytes() == b"x" * len(b"original duplicate content")
    assert not Path(item["target"]).exists()
