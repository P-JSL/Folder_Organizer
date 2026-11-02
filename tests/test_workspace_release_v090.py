"""Only new 0.9 workspace-release receipt and dispatch boundaries."""
import importlib
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    workspace = importlib.import_module("windows_workspace_v090")
    publisher = importlib.import_module("publish_workspace_release")
finally:
    sys.path.pop(0)


def completed_workspace(roots):
    return {"schema": workspace.SCHEMA, "app_version": "0.9.0", "edition": "Python",
            "main_workspace": True, "action_authority": False, "finished": True,
            "checked_roots": [str(root) for root in roots], "file_rows": 6, "duplicate_groups": 1,
            "scope_mode": "global", "selected_snapshot": {"name": "document.txt",
                "path": str(roots[0] / "document.txt"), "size": 12, "kind": "text"}}


def test_new_workspace_receipt_accepts_checked_folder_snapshot(tmp_path):
    roots, original = workspace.create_workspace_fixture(tmp_path)
    assert len(original) == 6
    workspace.verify_workspace_report(completed_workspace(roots), "Python", roots)


@pytest.mark.parametrize("change", [
    {"main_workspace": False}, {"action_authority": True}, {"checked_roots": []},
    {"duplicate_groups": 0}, {"selected_snapshot": {}}, {"scope_mode": "single"},
])
def test_new_workspace_gate_rejects_popup_stale_scope_or_missing_panels(tmp_path, change):
    roots, _ = workspace.create_workspace_fixture(tmp_path)
    value = {**completed_workspace(roots), **change}
    with pytest.raises(ValueError):
        workspace.verify_workspace_report(value, "Python", roots)


def test_new_workspace_publication_rejects_changed_ui_and_driver_inputs():
    for name in ("folder_organizer/ui.py", "rust-app/src/gui.rs", "scripts/windows_workspace_v090.py",
                 "version.json", "rust-app/Cargo.toml"):
        assert not publisher.publication_only(name)
    assert publisher.publication_only("docs/RELEASE_NOTES_0.9.0.md")


def test_new_workspace_workflow_has_no_old_check_entrypoints():
    value = (SCRIPTS.parent / ".github/workflows/workspace-release.yml").read_text(encoding="utf-8")
    assert "python scripts/windows_workspace_v090.py" in value
    assert "publish_run_id != ''" in value
    for forbidden in ("cargo test", "cargo +1.99.1 test", "pytest", "windows_release_v080.py",
                      "windows_multiroot_v080", "--multiroot-smoke"):
        assert forbidden not in value
