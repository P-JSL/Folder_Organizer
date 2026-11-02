"""New live multi-root action integration only; no previous suites collected."""
from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest

from folder_organizer import matching, operations as ops, workspace_actions as actions
from folder_organizer.rules import default_rules


@pytest.fixture(autouse=True)
def local_logs(monkeypatch):
    monkeypatch.setattr(ops, "_execution_event", lambda *_args, **_kwargs: None)


def write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def analyze(*roots):
    session = actions.analyze_workspace(roots)
    assert session.actionable
    assert session.result.action_authority is False
    return session


def test_next_workspace_global_first_matches_display_and_retains_one_across_roots_without_reread(tmp_path, monkeypatch):
    # Raw lexical ordering and casefold ordering deliberately disagree.
    roots = [tmp_path / "backup", tmp_path / "Documents", tmp_path / "z-last"]
    sources = [write(root / "same.txt", b"global verified duplicate\n" * 32) for root in roots]
    session = analyze(*roots)
    displayed_first = session.result.groups[0].paths[0]
    assert session.groups[0].paths[0] == displayed_first
    assert displayed_first == str(tmp_path / "Documents" / "same.txt")

    def no_content(*_args, **_kwargs):
        pytest.fail("keep-first repeated a scan or content read")

    monkeypatch.setattr(actions, "scan_folders", no_content)
    monkeypatch.setattr(matching, "_digest", no_content)
    monkeypatch.setattr(ops, "_fingerprint", no_content)
    plan = actions.build_workspace_keep_first_plan(session)
    assert {item["source"] for item in plan["items"]} == set(sources) - {displayed_first}
    result = actions.execute_workspace_plan(session, plan)
    assert result["status"] == "completed"
    assert session.consumed
    assert Path(displayed_first).exists()
    for operation in result["operations"]:
        assert operation["root"] in [str(root) for root in roots]
        for item in operation["items"]:
            assert Path(item["target"]).parent == Path(operation["root"]) / "delete_files"
            assert Path(item["target"]).read_bytes() == b"global verified duplicate\n" * 32
            assert not Path(item["source"]).exists()
    monkeypatch.undo()
    for operation in result["operations"]:
        restored = ops.undo_operation(operation["root"], operation["id"])
        assert restored["status"] == "undone"
    assert all(Path(path).read_bytes() == b"global verified duplicate\n" * 32 for path in sources)


def test_next_workspace_shared_copy_destination_is_reserved_before_preview(tmp_path):
    roots = [tmp_path / "left", tmp_path / "right"]
    sources = [write(roots[0] / "same.txt", b"left"), write(roots[1] / "same.txt", b"right")]
    destination = tmp_path / "output"
    existing = write(destination / "same.txt", b"unrelated")
    session = analyze(*roots)
    plan = actions.build_workspace_plan(session, sources, "copy", str(destination))
    targets = [item["target"] for item in plan["items"]]
    assert len(set(targets)) == 2
    assert existing not in targets
    result = actions.execute_workspace_plan(session, plan)
    assert result["status"] == "completed"
    assert {item["target"] for item in result["items"]} == set(targets)
    assert Path(existing).read_bytes() == b"unrelated"
    for item in result["items"]:
        assert Path(item["source"]).read_bytes() == Path(item["target"]).read_bytes()
    first, second = result["operations"]
    ops.undo_operation(first["root"], first["id"])
    assert all(not Path(item["target"]).exists() for item in first["items"])
    assert all(Path(item["target"]).exists() for item in second["items"])


def test_next_workspace_overlap_assigns_one_real_root_and_one_journal(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    source = write(child / "file.txt", b"one source")
    session = analyze(parent, child, parent)
    assert len(session.files) == 1
    plan = actions.build_workspace_plan(session, [source], "quarantine")
    assert len(plan["plans"]) == 1
    root = plan["plans"][0]["root"]
    assert root in (str(parent), str(child))
    assert Path(source).is_relative_to(root)
    result = actions.execute_workspace_plan(session, plan)
    assert len(result["operations"]) == 1
    assert result["operations"][0]["root"] == root
    assert not Path(source).exists()


def test_next_workspace_stale_later_root_aborts_before_first_root_mutation(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    sources = [write(roots[0] / "a.txt", b"a"), write(roots[1] / "b.txt", b"b")]
    session = analyze(*roots)
    plan = actions.build_workspace_plan(session, sources, "move", str(tmp_path / "output"))
    Path(sources[1]).write_bytes(b"externally modified after preview")
    with pytest.raises(ValueError, match="변경"):
        actions.execute_workspace_plan(session, plan)
    assert session.consumed
    assert all(Path(path).exists() for path in sources)
    assert not (tmp_path / "output").exists()


def test_next_workspace_stale_cross_root_keeper_consumes_all_plans_without_writes(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B", tmp_path / "C"]
    sources = [write(root / "same.txt", b"same bytes") for root in roots]
    session = analyze(*roots)
    plan = actions.build_workspace_keep_first_plan(session)
    Path(session.groups[0].paths[0]).write_bytes(b"keeper changed")
    with pytest.raises(ValueError, match="변경"):
        actions.execute_workspace_plan(session, plan)
    assert all(Path(path).exists() for path in sources)
    assert all(not (root / "delete_files").exists() for root in roots)
    assert session.consumed


def test_next_workspace_cancel_before_execution_and_between_roots_are_honest(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    sources = [write(roots[0] / "a.txt", b"a"), write(roots[1] / "b.txt", b"b")]
    session = analyze(*roots)
    plan = actions.build_workspace_plan(session, sources, "quarantine")
    cancel = Event()
    cancel.set()
    result = actions.execute_workspace_plan(session, plan, cancel=cancel)
    assert result["status"] == "cancelled"
    assert result["operations"] == []
    assert session.consumed and all(Path(path).exists() for path in sources)
    with pytest.raises(ValueError):
        actions.execute_workspace_plan(session, plan)

    session = analyze(*roots)
    plan = actions.build_workspace_plan(session, sources, "quarantine")
    cancel.clear()

    def cancel_after_one(event):
        if event["phase"] == "execute" and event["completed"] == 1:
            cancel.set()

    result = actions.execute_workspace_plan(session, plan, cancel=cancel, progress_detail=cancel_after_one)
    assert result["status"] == "cancelled"
    assert len(result["operations"]) == 1
    assert not Path(sources[0]).exists() and Path(sources[1]).exists()
    assert result["operations"][0]["status"] == "completed"


def test_next_workspace_single_root_uses_same_session_and_rules(tmp_path):
    root = tmp_path / "one"
    source = write(root / "memo.txt", b"one root uses same workspace")
    session = analyze(root)
    plan = actions.build_workspace_plan(session, [source], "rules", str(tmp_path / "output"),
                                        rules=default_rules("extension"))
    assert len(plan["plans"]) == 1 and plan["plans"][0]["root"] == str(root)
    result = actions.execute_workspace_plan(session, plan)
    assert result["status"] == "completed"
    assert Path(source).exists()
    assert Path(result["items"][0]["target"]).read_bytes() == Path(source).read_bytes()


def test_next_workspace_public_reports_and_mutated_incomplete_flags_never_grant_authority(tmp_path):
    root = tmp_path / "root"
    source = write(root / "file.txt", b"data")
    session = analyze(root)
    with pytest.raises(ValueError):
        actions.build_workspace_plan(session.result, [source], "quarantine")
    with pytest.raises(ValueError):
        actions.WorkspaceSession(session.result, [])
    partial = actions.analyze_workspace([root, tmp_path / "missing"])
    assert not partial.actionable
    partial.result.complete = True
    partial.result.cancelled = False
    with pytest.raises(ValueError):
        actions.build_workspace_plan(partial, [source], "quarantine")
    assert Path(source).exists()


def test_next_workspace_tampered_batch_and_foreign_source_are_rejected(tmp_path):
    root = tmp_path / "root"
    source = write(root / "file.txt", b"data")
    foreign = write(tmp_path / "outside.txt", b"outside")
    session = analyze(root)
    with pytest.raises(ValueError):
        actions.build_workspace_plan(session, [source, foreign], "quarantine")
    plan = actions.build_workspace_plan(session, [source], "quarantine")
    plan["plans"][0]["items"][0]["target"] = str(tmp_path / "forged.txt")
    with pytest.raises(ValueError):
        actions.execute_workspace_plan(session, plan)
    assert session.consumed
    assert Path(source).exists() and not (tmp_path / "forged.txt").exists()


def test_next_workspace_union_preflight_rejects_keeper_selected_by_another_root(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    sources = [write(root / "same.txt", b"same") for root in roots]
    session = analyze(*roots)
    duplicate = actions.build_workspace_keep_first_plan(session)
    manual = actions.build_workspace_plan(session, [session.groups[0].paths[0]], "quarantine")
    combined = actions._base(session, "quarantine", duplicate["plans"] + manual["plans"])
    with pytest.raises(ValueError, match="보존 원본"):
        actions.execute_workspace_plan(session, combined)
    assert session.consumed
    assert all(Path(path).exists() for path in sources)


def test_next_workspace_union_rejects_keeper_hardlink_in_another_root(tmp_path):
    roots = [tmp_path / name for name in ("A", "B", "C")]
    a = write(roots[0] / "original.txt", b"same bytes")
    b = write(roots[1] / "copy.txt", b"same bytes")
    roots[2].mkdir()
    alias = str(roots[2] / "keeper-hardlink.txt")
    os.link(a, alias)
    session = analyze(*roots)
    assert session.groups[0].paths[0] == a
    duplicate = actions.build_workspace_keep_first_plan(session)
    manual = actions.build_workspace_plan(session, [alias], "quarantine")
    combined = actions._base(session, "quarantine", duplicate["plans"] + manual["plans"])
    with pytest.raises(ValueError, match="하드링크"):
        actions.execute_workspace_plan(session, combined)
    assert all(Path(path).read_bytes() == b"same bytes" for path in (a, b, alias))


def test_next_workspace_inventory_only_root_never_receives_operation_metadata(tmp_path, monkeypatch):
    roots = [tmp_path / "selected", tmp_path / "inventory-only"]
    source = write(roots[0] / "selected.txt", b"selected")
    write(roots[1] / "unselected.txt", b"unselected")
    session = analyze(*roots)
    plan = actions.build_workspace_plan(session, [source], "copy", str(tmp_path / "output"))
    original_state = ops._state

    def selected_state_only(root):
        assert Path(root) != roots[1], "inventory-only root unexpectedly requires write access"
        return original_state(root)

    monkeypatch.setattr(ops, "_state", selected_state_only)
    result = actions.execute_workspace_plan(session, plan)
    assert result["status"] == "completed"
    assert not (roots[1] / ops.STATE_DIRECTORY).exists()
    assert (roots[1] / "unselected.txt").read_bytes() == b"unselected"


def test_next_workspace_protected_all_keep_first_is_noop_without_metadata(tmp_path, monkeypatch):
    roots = [tmp_path / "A", tmp_path / "B"]
    sources = [write(root / "same.txt", b"same bytes") for root in roots]
    session = analyze(*roots)
    plan = actions.build_workspace_keep_first_plan(session, protected_folders=[str(root) for root in roots])
    assert plan["items"] == []

    def no_metadata(*_args, **_kwargs):
        pytest.fail("a protected-all no-op created operation metadata")

    monkeypatch.setattr(ops, "_state", no_metadata)
    result = actions.execute_workspace_plan(session, plan)
    assert result["status"] == "unchanged" and result["operations"] == []
    assert session.consumed
    assert all(Path(path).read_bytes() == b"same bytes" for path in sources)


def test_next_workspace_cancel_within_root_keeps_pending_sources_and_undo_restores_only_completed(tmp_path):
    root = tmp_path / "root"
    sources = [write(root / f"{index}.txt", f"bytes-{index}".encode()) for index in range(3)]
    session = analyze(root)
    plan = actions.build_workspace_plan(session, sources, "quarantine")
    cancel = Event()

    def stop_after_one(event):
        if event["phase"] == "execute" and event["completed"] == 1:
            cancel.set()

    result = actions.execute_workspace_plan(session, plan, cancel=cancel, progress_detail=stop_after_one)
    assert result["status"] == "cancelled"
    assert len(result["operations"]) == 1
    receipt = result["operations"][0]
    assert receipt["status"] == "cancelled"
    assert [item["state"] for item in receipt["items"]] == ["completed", "pending", "pending"]
    assert not Path(sources[0]).exists()
    assert all(Path(path).exists() for path in sources[1:])
    restored = ops.undo_operation(root, receipt["id"])
    assert restored["status"] == "undone"
    assert all(Path(path).read_bytes() == f"bytes-{index}".encode() for index, path in enumerate(sources))


def test_next_workspace_cross_root_merge_preserves_content_and_optionally_retires_real_roots(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    a = write(roots[0] / "notes.txt", b"shared paragraph\n\nleft details\n")
    b = write(roots[1] / "notes.txt", b"shared paragraph\n\nright details\n")
    output = tmp_path / "merged"
    output.mkdir()
    session = analyze(*roots)
    preview = actions.build_workspace_merge_preview(session, [a, b], destination=str(output), output_name="preserved.txt")
    assert preview["removed_units"] == 1
    assert {item["root"] for item in preview["sources"]} == {str(root) for root in roots}
    assert not (output / "preserved.txt").exists()
    result = actions.execute_workspace_merge(session, preview)
    assert result["status"] == "completed", result["error"]
    assert result["output_verified"] and result["operations"] == []
    expected = "shared paragraph\n\nleft details\n\nright details\n"
    assert Path(result["output"]).read_text() == expected
    assert Path(a).exists() and Path(b).exists()
    assert all(not (root / ops.STATE_DIRECTORY).exists() for root in roots)
    assert session.consumed

    session = analyze(*roots)
    preview = actions.build_workspace_merge_preview(session, [a, b], destination=str(output), output_name="retired.txt", move_originals=True)
    result = actions.execute_workspace_merge(session, preview)
    assert result["status"] == "completed", result["error"]
    assert Path(result["output"]).read_text() == expected
    assert len(result["operations"]) == 2
    assert not Path(a).exists() and not Path(b).exists()
    for receipt in result["operations"]:
        assert Path(receipt["items"][0]["target"]).parent == Path(receipt["root"]) / "delete_files"
        assert ops.undo_operation(receipt["root"], receipt["id"])["status"] == "undone"
    assert Path(a).read_bytes() == b"shared paragraph\n\nleft details\n"
    assert Path(b).read_bytes() == b"shared paragraph\n\nright details\n"
    assert Path(result["output"]).read_text() == expected


def test_next_workspace_merge_changed_later_source_aborts_without_output_or_retirement(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    a = write(roots[0] / "notes.txt", b"shared\n\nfirst\n")
    b = write(roots[1] / "notes.txt", b"shared\n\nsecond\n")
    output = tmp_path / "merged"
    output.mkdir()
    session = analyze(*roots)
    preview = actions.build_workspace_merge_preview(session, [a, b], destination=str(output), move_originals=True)
    Path(b).write_bytes(b"changed after preview")
    result = actions.execute_workspace_merge(session, preview)
    assert result["status"] == "failed"
    assert result["error"] and not result["output_created"]
    assert not Path(result["output"]).exists()
    assert Path(a).read_bytes() == b"shared\n\nfirst\n"
    assert Path(b).read_bytes() == b"changed after preview"
    assert all(not (root / "delete_files").exists() for root in roots)
    assert session.consumed
