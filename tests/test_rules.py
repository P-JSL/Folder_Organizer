from __future__ import annotations

import copy
from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from folder_organizer import operations as ops, rules
from folder_organizer.matching import find_duplicates, find_similar
from folder_organizer.models import MatchGroup
from folder_organizer.scanner import scan_folder


@pytest.fixture
def root(tmp_path):
    value = tmp_path / "Inbox"
    value.mkdir()
    return value


def write(root, name, content=b"valuable data"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_ordered_conditions_preserve_folder_structure_and_copy_is_default(root):
    wanted = write(root, "업무/청구서/2026/report.PDF", b"invoice")
    write(root, "업무/청구서/2026/short.PDF", b"x")
    write(root, "개인/report.PDF", b"invoice")
    stamp = datetime(2025, 6, 4, 12).timestamp()
    os.utime(wanted, (stamp, stamp))
    scan = scan_folder(root)
    spec = [{"name": "회사 청구서", "extensions": ["pdf"], "folder_pattern": "업무/청구서",
             "name_pattern": "report*", "min_size": 5, "max_size": 10,
             "modified_after": "2025-06-04", "modified_before": "2025-06-04",
             "target": "청구서/{year}/{month}/{source_folder}"},
            {"name": "나머지 PDF", "extensions": [".pdf"], "target": "기타/{source_folder}"}]
    plan = rules.build_classification_plan(scan, spec)
    item = next(item for item in plan["items"] if item["source"] == str(wanted))
    assert Path(item["target"]).relative_to(root).as_posix() == "정리됨/청구서/2025/06/업무/청구서/2026/report.PDF"
    assert item["rule"] == "회사 청구서" and item["action"] == "copy"
    assert wanted.exists() and not Path(item["target"]).exists()
    result = ops.execute_plan(plan)
    assert result["status"] == "completed" and wanted.read_bytes() == b"invoice"
    assert Path(item["target"]).read_bytes() == b"invoice"
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert wanted.read_bytes() == b"invoice" and not Path(item["target"]).exists()


@pytest.mark.parametrize("preset", ["type", "date", "folder"])
def test_presets_are_valid_and_return_independent_rule_lists(preset):
    first, second = rules.default_rules(preset), rules.default_rules(preset)
    assert first == rules.validate_rules(first)
    first[0]["enabled"] = False
    assert second[0]["enabled"]


@pytest.mark.parametrize("target", ["../escape", "/outside", "C:/outside", ".FOLDER-ORGANIZER/data",
                                     "safe/CON", "NUL.txt", "dir/name.", "{unknown}", "{year.__class__}", "{year!r}"])
def test_invalid_or_reserved_destinations_rejected_without_mutation(root, target):
    source = write(root, "document.txt")
    with pytest.raises(ValueError):
        rules.build_classification_plan(scan_folder(root), [{"name": "unsafe", "target": target}])
    assert source.exists() and not (root / ops.STATE_DIRECTORY).exists()


@pytest.mark.parametrize("rule", [
    {"extensions": "pdf"}, {"extensions": ["../pdf"]}, {"enabled": "yes"},
    {"min_size": -1}, {"min_size": 10, "max_size": 5}, {"modified_after": "yesterday"},
    {"modified_before": []}, {"modified_after": "2026-01-01", "modified_before": "2025-01-01"},
    {"name_pattern": "../*"}, {"folder_pattern": "../*"},
])
def test_invalid_conditions_have_actionable_validation(rule):
    with pytest.raises(ValueError):
        rules.validate_rules([{"name": "규칙", **rule}])


def test_unmatched_protected_app_and_existing_outputs_are_skipped(root):
    write(root, "protected/private.txt")
    write(root, "Editor.APP/data.txt")
    write(root, "정리됨/문서/already.txt")
    write(root, "skip.jpg")
    included = write(root, "work.txt")
    plan = rules.build_classification_plan(scan_folder(root), [{"name": "문서", "extensions": ["txt"]}], protected_folders=[str(root / "protected")])
    assert [item["source"] for item in plan["items"]] == [str(included)]
    assert plan["classification_summary"]["skipped"] == 4
    assert len(plan["warnings"]) == 3


def test_protected_target_folder_is_not_written(root, tmp_path):
    write(root, "document.txt")
    destination = tmp_path / "protected-output"
    destination.mkdir()
    plan = rules.build_classification_plan(scan_folder(root), rules.default_rules(), destination=destination, protected_folders=[str(destination)])
    assert plan["items"] == [] and plan["warnings"]
    assert list(destination.iterdir()) == []


def test_repeat_run_never_nests_output_tree_or_overwrites(root):
    source = write(root, "nested/a.txt")
    first = rules.build_classification_plan(scan_folder(root), rules.default_rules())
    ops.execute_plan(first)
    original_target = Path(first["items"][0]["target"])
    repeated = rules.build_classification_plan(scan_folder(root), rules.default_rules())
    assert len(repeated["items"]) == 1
    assert repeated["items"][0]["source"] == str(source)
    assert Path(repeated["items"][0]["target"]).name == "a (1).txt"
    assert original_target.read_bytes() == b"valuable data"


def test_move_rules_keep_original_backup_and_undo(root):
    source = write(root, "project/data.txt")
    plan = rules.build_classification_plan(scan_folder(root), rules.default_rules("folder"), action="move")
    result = ops.execute_plan(plan)
    assert result["status"] == "completed" and not source.exists()
    assert Path(result["items"][0]["original_backup"]).read_bytes() == b"valuable data"
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert source.read_bytes() == b"valuable data"


def test_stale_classification_preview_and_tampering_reject_before_move(root):
    source = write(root, "file.txt")
    plan = rules.build_classification_plan(scan_folder(root), rules.default_rules(), action="move")
    changed = copy.deepcopy(plan)
    changed["items"][0]["target"] = str(root / "unauthorized.txt")
    with pytest.raises(ops.OperationError):
        ops.execute_plan(changed)
    source.write_bytes(b"updated bytes")
    result = ops.execute_plan(plan)
    assert result["status"] == "failed" and source.read_bytes() == b"updated bytes"
    assert not Path(plan["items"][0]["target"]).exists()


def test_insufficient_space_prevents_preview_and_execution(root, monkeypatch):
    source = write(root, "large.txt")
    scan = scan_folder(root)
    plan = rules.build_classification_plan(scan, rules.default_rules())
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda *_: SimpleNamespace(free=0))
    with pytest.raises(ops.OperationError, match="공간"):
        rules.build_classification_plan(scan, rules.default_rules())
    with pytest.raises(ops.OperationError, match="공간"):
        ops.execute_plan(plan)
    assert source.exists() and not (root / ops.STATE_DIRECTORY).exists()


def test_cancelled_scan_and_output_equal_input_cannot_classify(root):
    write(root, "file.txt")
    scan = scan_folder(root)
    with pytest.raises(ValueError, match="다른"):
        rules.build_classification_plan(scan, rules.default_rules(), destination=root)
    scan.cancelled = True
    with pytest.raises(ValueError):
        rules.build_classification_plan(scan, rules.default_rules())


def test_exact_duplicate_auto_classification_retains_protected_original(root):
    keeper = write(root, "originals/photo.jpg")
    extra = write(root, "copies/photo copy.jpg")
    write(root, "unrelated.txt", b"different")
    scan = scan_folder(root)
    plan = rules.build_duplicate_plan(scan, find_duplicates(scan.files), protected_folders=[str(keeper.parent)])
    assert [item["source"] for item in plan["items"]] == [str(extra)]
    assert plan["items"][0]["duplicate_keeper"]["path"] == str(keeper)
    result = ops.execute_plan(plan)
    assert result["status"] == "completed" and keeper.exists() and not extra.exists()
    assert Path(result["items"][0]["target"]).read_bytes() == keeper.read_bytes()
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert extra.exists() and keeper.exists()


def test_keep_first_also_preserves_later_protected_member(root):
    first = write(root, "a.txt")
    extra = write(root, "b.txt")
    protected = write(root, "protected/z.txt")
    scan = scan_folder(root)
    groups = find_duplicates(scan.files)
    assert groups[0].paths[0] == str(first)
    plan = rules.build_duplicate_plan(scan, groups, protected_folders=[str(protected.parent)], preserve_first=True)
    assert plan["preserve_first"] is True
    assert [item["source"] for item in plan["items"]] == [str(extra)]
    assert plan["items"][0]["duplicate_keeper"]["path"] == str(first)
    assert set(plan["decisions"][0]["kept"]) == {str(first), str(protected)}
    assert plan["classification_summary"]["kept"] == 2
    result = ops.execute_plan(plan)
    assert result["status"] == "completed"
    assert first.exists() and protected.exists() and not extra.exists()
    assert ops.undo_operation(root, result["id"])["status"] == "undone"
    assert first.exists() and extra.exists() and protected.exists()


def test_keep_first_follows_group_order_and_revalidates_that_first(root):
    a = write(root, "a.txt")
    b = write(root, "b.txt")
    scan = scan_folder(root)
    groups = find_duplicates(scan.files)
    groups[0].paths.reverse()
    assert groups[0].paths[0] == str(b)
    plan = rules.build_duplicate_plan(scan, groups, preserve_first=True)
    # Generic retention's path order would pick a, while keep-first must retain
    # the first member in the actual displayed group, b.
    assert plan["decisions"][0]["kept"] == [str(b)]
    assert [item["source"] for item in plan["items"]] == [str(a)]
    assert plan["items"][0]["duplicate_keeper"]["path"] == str(b)
    b.unlink()
    with pytest.raises((OSError, ops.OperationError)):
        ops.execute_plan(plan)
    assert a.exists() and not (root / ops.STATE_DIRECTORY).exists()


@pytest.mark.parametrize("change", ["remove", "edit_same_size_restore_mtime"])
def test_changed_keeper_blocks_entire_batch_before_any_copy_or_journal(root, change):
    write(root, "a/original.txt", b"first group")
    write(root, "a/copy.txt", b"first group")
    write(root, "b/original.txt", b"second group")
    write(root, "b/copy.txt", b"second group")
    scan = scan_folder(root)
    plan = rules.build_duplicate_plan(scan, find_duplicates(scan.files))
    assert len(plan["items"]) == 2
    keeper = Path(plan["items"][-1]["duplicate_keeper"]["path"])
    if change == "remove":
        keeper.unlink()
    else:
        timestamp = keeper.stat().st_mtime_ns
        keeper.write_bytes(b"X" * keeper.stat().st_size)
        os.utime(keeper, ns=(timestamp, timestamp))
    with pytest.raises((OSError, ops.OperationError)):
        ops.execute_plan(plan)
    assert all(Path(item["source"]).exists() for item in plan["items"])
    assert not (root / ops.STATE_DIRECTORY).exists()


def test_keeper_change_during_atomic_move_returns_selected_source(root, monkeypatch):
    write(root, "first.txt")
    write(root, "second.txt")
    scan = scan_folder(root)
    plan = rules.build_duplicate_plan(scan, find_duplicates(scan.files))
    item = plan["items"][0]
    original_move = ops._move_no_replace

    def changed_keeper(source, target):
        result = original_move(source, target)
        if target == Path(item["target"]):
            Path(item["duplicate_keeper"]["path"]).unlink()
        return result

    monkeypatch.setattr(ops, "_move_no_replace", changed_keeper)
    result = ops.execute_plan(plan)
    assert result["status"] == "failed"
    assert Path(item["source"]).read_bytes() == b"valuable data"
    assert not Path(item["target"]).exists()
    assert result["items"][0]["state"] == "failed"


def test_similar_or_legacy_unproven_groups_never_auto_quarantine(root):
    first = write(root, "document.txt", b"first document words")
    second = write(root, "document copy.txt", b"second document words")
    scan = scan_folder(root)
    groups = find_similar(scan.files, methods=("name",))
    groups.append(MatchGroup("duplicate", [str(first), str(second)], "legacy"))
    plan = rules.build_duplicate_plan(scan, groups)
    assert not plan["items"] and plan["warnings"]
    assert first.exists() and second.exists()


def test_progress_is_counted_for_preview_execute_history_and_undo(root):
    write(root, "one.txt")
    write(root, "two.txt")
    events = []
    plan = rules.build_classification_plan(scan_folder(root), rules.default_rules(), progress_detail=events.append)
    assert any(event["phase"] == "rules" and event["total"] == 2 for event in events)
    assert events[-1]["completed"] == events[-1]["total"] == 2
    result = ops.execute_plan(plan, progress_detail=events.append)
    assert events[-1]["phase"] == "execute" and events[-1]["completed"] == 2
    ops.list_history(root, progress_detail=events.append)
    assert events[-1]["phase"] == "history" and events[-1]["completed"] == events[-1]["total"] == 1
    ops.undo_operation(root, result["id"], progress_detail=events.append)
    assert events[-1]["phase"] == "undo" and events[-1]["completed"] == events[-1]["total"] == 2
