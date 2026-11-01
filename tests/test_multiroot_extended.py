"""New read-only multi-folder contracts; existing single-root cases are skipped."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Barrier, Event, Lock
import time

import pytest

from folder_organizer import multiroot
from folder_organizer.matching import validate_duplicate_proof


def test_global_exact_result_has_no_action_proof_and_preserves_sources(tmp_path):
    left, right = tmp_path / "문서 A", tmp_path / "문서 B"
    left.mkdir(); right.mkdir()
    content = b"shared actual bytes\n" * 120
    original, copy = left / "original.txt", right / "copy.txt"
    original.write_bytes(content); copy.write_bytes(content)
    (right / "different.txt").write_bytes(b"another unrelated file")
    result = multiroot.scan_folders([left, right])
    assert result.complete and result.inventory_complete and not result.cancelled
    assert len(result.files) == 3 and len(result.groups) == 1
    group = result.groups[0]
    assert set(group.paths) == {str(original), str(copy)}
    assert set(group.root_ids) == {"root-0001", "root-0002"}
    assert group.action_authority is False
    with pytest.raises(ValueError):
        validate_duplicate_proof(group)
    assert original.read_bytes() == copy.read_bytes() == content
    assert not list(tmp_path.rglob(".folder-organizer*"))


def test_overlapping_and_duplicate_roots_keep_membership_without_rehashing_paths(tmp_path):
    parent, child = tmp_path / "parent", tmp_path / "parent" / "child"
    child.mkdir(parents=True)
    original, copy = parent / "original.bin", child / "copy.bin"
    original.write_bytes(b"same"); copy.write_bytes(b"same")
    result = multiroot.scan_folders([parent, parent / ".", child])
    assert result.complete and len(result.roots) == 2
    assert [root.root_id for root in result.roots] == ["root-0001", "root-0002"]
    assert result.duplicate_inputs == [{"requested_path": str(parent / "."), "root_id": "root-0001"}]
    assert len(result.files) == 2 and len(result.groups) == 1
    assert result.memberships[str(copy)] == ["root-0001", "root-0002"]
    assert result.owners[str(copy)] == "root-0001"
    assert result.duplicate_analysis["metrics"]["full_hash_files"] == 2
    assert result.notes


def test_unavailable_root_is_partial_while_healthy_verified_group_is_retained(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    for name in ("a", "b"):
        (good / name).write_bytes(b"verified safe fixture")
    result = multiroot.scan_folders([good, tmp_path / "offline"], include_hidden=True,
                                    exclude_patterns=["*.skip"])
    report = multiroot.build_multiroot_report(result)
    assert len(result.groups) == 1
    assert not result.complete and not result.inventory_complete
    assert result.duplicate_analysis["complete"] is False
    assert report["roots"][1]["state"] == "failed"
    assert report["roots"][1]["scan_options"]["include_hidden"] is True
    assert report["roots"][1]["scan_options"]["exclude_patterns"] == ["*.skip"]
    assert report["duplicates"]["warning_count"] >= 1
    assert report["duplicates"]["warnings"][0]["phase"] == "inventory"
    assert report["schema"] == "folder-organizer.multiroot.v1"
    assert report["action_authority"] is False
    assert all(isinstance(entry["modified_ns"], str) for entry in report["files"])


def test_hardlinks_are_logical_paths_but_do_not_create_a_false_duplicate(tmp_path):
    left, right = tmp_path / "one", tmp_path / "two"
    left.mkdir(); right.mkdir()
    original, alias = left / "real.bin", right / "alias.bin"
    original.write_bytes(b"same physical file")
    try:
        os.link(original, alias)
    except OSError as exc:
        pytest.skip(f"Fixture filesystem does not support hardlinks: {exc}")
    result = multiroot.scan_folders([left, right])
    assert result.complete and len(result.files) == 2 and result.groups == []
    assert result.duplicate_analysis["metrics"]["hardlink_aliases"] == 1
    assert original.read_bytes() == alias.read_bytes() == b"same physical file"


def test_cancelled_before_scan_is_not_complete_or_empty_success(tmp_path):
    cancel = Event(); cancel.set()
    result = multiroot.scan_folders([tmp_path], cancel=cancel)
    assert result.cancelled and not result.complete and not result.inventory_complete
    assert result.roots[0].state == "cancelled"
    assert result.duplicate_analysis["complete"] is False
    assert result.duplicate_analysis["cancelled"] is True


def test_cancelled_during_exact_analysis_preserves_completed_inventory(tmp_path):
    for name in ("a.bin", "b.bin"):
        (tmp_path / name).write_bytes(b"same bytes")
    cancel = Event()

    def cancel_exact(event):
        if event.get("root_id") is None and event.get("phase", "").startswith("duplicate_"):
            cancel.set()

    result = multiroot.scan_folders([tmp_path], cancel=cancel, progress_detail=cancel_exact)
    assert result.inventory_complete and result.roots[0].state == "complete"
    assert result.cancelled and not result.complete and result.groups == []
    assert result.duplicate_analysis["complete"] is False
    assert result.duplicate_analysis["cancelled"] is True
    assert all((tmp_path / name).read_bytes() == b"same bytes" for name in ("a.bin", "b.bin"))


def test_conflicting_overlapping_snapshots_are_not_silently_coalesced(tmp_path, monkeypatch):
    parent, child = tmp_path / "parent", tmp_path / "parent" / "child"
    child.mkdir(parents=True)
    victim = child / "changing.bin"
    victim.write_bytes(b"old data")
    actual_scan = multiroot.scan_folder
    calls = 0

    def scan_then_replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"new data")
            os.replace(replacement, victim)
        return actual_scan(*args, **kwargs)

    monkeypatch.setattr(multiroot, "scan_folder", scan_then_replace)
    result = multiroot.scan_folders([parent, child], workers=1)
    assert not result.complete and not result.inventory_complete
    assert result.duplicate_analysis["complete"] is False
    assert result.files == [] and result.groups == []
    assert all(root.state == "partial" for root in result.roots)
    assert victim.read_bytes() == b"new data"
    assert result.duplicate_analysis["warning_count"] == 2


def test_scheduler_serializes_volume_groups_and_bounds_independent_work(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ("a0", "a1", "b0")]
    for root in roots:
        root.mkdir()
        (root / "file").write_bytes(root.name.encode())
    monkeypatch.setattr(multiroot, "_volume_group", lambda path, _info: path.name[0])
    actual_scan = multiroot.scan_folder
    lock, rendezvous = Lock(), Barrier(2)
    active, peaks, seen = {}, {}, set()
    global_peak = 0

    def observed_scan(path, *args, **kwargs):
        nonlocal global_peak
        group = Path(path).name[0]
        with lock:
            active[group] = active.get(group, 0) + 1
            peaks[group] = max(peaks.get(group, 0), active[group])
            global_peak = max(global_peak, sum(active.values()))
            first_in_group = group not in seen
            seen.add(group)
        try:
            if first_in_group:
                rendezvous.wait(timeout=3)
            return actual_scan(path, *args, **kwargs)
        finally:
            with lock:
                active[group] -= 1

    monkeypatch.setattr(multiroot, "scan_folder", observed_scan)
    result = multiroot.scan_folders(roots, workers=2)
    assert result.complete and len(result.files) == 3
    assert peaks == {"a": 1, "b": 1} and global_peak == 2


def test_report_is_new_only_and_outside_read_only_scopes(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    original = root / "file"
    original.write_bytes(b"unchanged")
    result = multiroot.scan_folders([root], duplicates=False)
    with pytest.raises(ValueError):
        multiroot.write_multiroot_report(result, root / "new-report.json")
    target = tmp_path / "report.json"
    multiroot.write_multiroot_report(result, target)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["duplicates"]["requested"] is False and document["duplicates"]["complete"] is None
    with pytest.raises(FileExistsError):
        multiroot.write_multiroot_report(result, target)
    assert original.read_bytes() == b"unchanged" and list(root.iterdir()) == [original]


def test_case_distinct_paths_are_not_folded_together(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    upper, lower = root / "A.txt", root / "a.txt"
    upper.write_bytes(b"upper")
    lower.write_bytes(b"lower")
    if os.path.samefile(upper, lower):
        pytest.skip("Fixture directory is case-insensitive")
    result = multiroot.scan_folders([root])
    assert result.complete and len(result.files) == 2
    assert {entry.path for entry in result.files} == {str(upper), str(lower)}


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    instance = QApplication.instance() or QApplication([])
    yield instance


def _finish_events(app, dialog, timeout=5):
    deadline = time.monotonic() + timeout
    while dialog.is_running() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    app.processEvents()
    assert not dialog.is_running(), "New multi-folder worker did not finish"


def test_multifolder_dialog_displays_read_only_results_without_item_per_file(app, tmp_path):
    from folder_organizer.multiroot_view import MultiRootDialog
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir(); right.mkdir()
    (left / "source").write_bytes(b"global duplicate")
    (right / "copy").write_bytes(b"global duplicate")
    dialog = MultiRootDialog()
    closed = []
    dialog.finished.connect(closed.append)
    dialog.show()
    dialog.add_paths([left, right])
    dialog.start_analysis()
    _finish_events(app, dialog)
    assert dialog.root_model.rowCount() == 2
    assert dialog.file_model.rowCount() == 2 and dialog.group_model.rowCount() == 1
    assert dialog.result.complete and dialog.export_button.isEnabled()
    assert "읽기 전용" in dialog.status.text()
    dialog.add_paths([tmp_path / "new-scope"])
    assert dialog.result is None and dialog.file_model.rowCount() == 0
    assert not dialog.export_button.isEnabled()
    dialog.close()
    assert len(closed) == 1


@pytest.mark.parametrize("duplicates", [False, True])
def test_terminal_progress_cancellation_cannot_report_complete(tmp_path, duplicates):
    for name in ("a.bin", "b.bin"):
        (tmp_path / name).write_bytes(b"already read exact bytes")
    cancel = Event()
    def cancel_at_terminal(event):
        if ((duplicates and event.get("phase") == "duplicate_complete")
                or (not duplicates and event.get("state") == "complete" and event.get("terminal"))):
            cancel.set()
    result = multiroot.scan_folders([tmp_path], duplicates=duplicates, cancel=cancel,
                                    progress_detail=cancel_at_terminal)
    assert cancel.is_set()
    assert result.inventory_complete
    assert result.cancelled and not result.complete
    assert (tmp_path / "a.bin").read_bytes() == b"already read exact bytes"


def test_closing_running_dialog_requests_cancel_and_defers_thread_destruction(app, tmp_path, monkeypatch):
    from folder_organizer import multiroot_view
    started = Event()

    def delayed_scan(roots, *, cancel, **_options):
        started.set()
        assert cancel.wait(timeout=3)
        return multiroot.MultiScanResult(cancelled=True, complete=False)

    monkeypatch.setattr(multiroot_view, "scan_folders", delayed_scan)
    dialog = multiroot_view.MultiRootDialog()
    closed = []
    dialog.finished.connect(closed.append)
    dialog.add_paths([tmp_path])
    dialog.show()
    dialog.start_analysis()
    assert started.wait(timeout=3)
    dialog.close()
    assert dialog.is_running() and dialog._close_pending
    _finish_events(app, dialog)
    assert not dialog.isVisible() and dialog.result.cancelled
    assert len(closed) == 1
