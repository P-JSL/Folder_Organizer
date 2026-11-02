"""New real-worker UI-to-operation flow; no capture or prior suite is used."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from folder_organizer import matching, operations, workspace_actions
from folder_organizer.multiroot_view import MultiRootWorkspace


def wait_until(app, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        # qWait can retain the GIL while Windows filesystem calls repeatedly
        # release it, starving the real Python worker. Process GUI events, then
        # explicitly yield Python execution without extending the deadline.
        time.sleep(.005)
    assert predicate(), "New workspace worker did not complete"


@pytest.mark.parametrize("root_count", [1, 3], ids=["single-folder", "multiple-folders"])
def test_next_workspace_real_worker_analysis_to_keep_first_and_per_root_restore(tmp_path, monkeypatch, root_count):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    monkeypatch.setattr(operations, "_execution_event", lambda *_a, **_k: None)
    roots = [tmp_path / name for name in ("Documents", "backup", "other")[:root_count]]
    for root in roots:
        root.mkdir()
    content = b"Actual new workspace worker integration\n" * 128
    sources = []
    for index in range(3):
        path = roots[index % root_count] / f"duplicate-{index}.txt"
        path.write_bytes(content)
        sources.append(path)
    unique = roots[0] / "unique.txt"
    unique.write_bytes(b"separate original")
    workspace = MultiRootWorkspace()
    try:
        workspace.add_paths(roots)
        workspace.start_analysis()
        wait_until(app, lambda: not workspace.is_running() and workspace.result is not None)
        assert workspace.session.actionable
        assert workspace.result.complete and not workspace.result.action_authority
        assert len(workspace.result.roots) == root_count
        assert workspace.file_model.rowCount() == 4
        assert len(workspace.result.groups) == 1
        keeper = Path(workspace.result.groups[0].paths[0])
        assert workspace.keep_first_button.isEnabled()

        def forbidden(*_a, **_k):
            # AssertionError is retained by the real QThread worker. pytest's
            # BaseException-based failure would escape its exception receipt.
            raise AssertionError("Keep-first restarted analysis, content hashing or a confirmation")

        with monkeypatch.context() as guard:
            guard.setattr(workspace_actions, "scan_folders", forbidden)
            guard.setattr(matching, "_digest", forbidden)
            guard.setattr(operations, "_fingerprint", forbidden)
            guard.setattr(QMessageBox, "question", forbidden)
            workspace.keep_first_button.click()
            try:
                wait_until(app, lambda: not workspace.is_running())
            except AssertionError as error:
                worker = workspace._worker
                state = {"status": workspace.status.text(), "running": worker.isRunning() if worker else False,
                         "worker_error": getattr(worker, "error", None),
                         "worker_result": repr(getattr(worker, "result", None)),
                         "close_pending": workspace._close_pending,
                         "consumed": workspace.session.consumed}
                frames = "\n".join("".join(traceback.format_stack(frame)) for frame in sys._current_frames().values())
                raise AssertionError(f"New worker state: {state}\n{frames}") from error
            assert workspace.last_receipt is not None, workspace.status.text()
        receipt = workspace.last_receipt
        assert receipt["status"] == "completed", receipt
        assert len(receipt["items"]) == 2
        assert keeper.read_bytes() == content
        assert unique.read_bytes() == b"separate original"
        assert all(not path.exists() for path in sources if path != keeper)
        assert workspace.file_model.rowCount() == 2
        assert not workspace.keep_first_button.isEnabled()
        assert workspace.session.consumed
        for operation in receipt["operations"]:
            root = Path(operation["root"])
            assert root in roots
            for item in operation["items"]:
                assert Path(item["target"]).parent == root / "delete_files"
            restored = operations.undo_operation(root, operation["id"])
            assert restored["status"] == "undone"
        assert all(path.read_bytes() == content for path in sources)
        assert not (tmp_path / ".folder-organizer").exists()
    finally:
        if workspace.is_running():
            workspace.cancel_analysis()
            workspace._worker.wait(15000)
            app.processEvents()
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
