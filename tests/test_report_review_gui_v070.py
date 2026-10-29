"""Only the changed partial-duplicate presentation; prior GUI checks stay skipped."""
from folder_organizer.matching import find_duplicates
from test_ui_v2 import app, window, populate


def test_partial_duplicate_results_show_warning_and_export_actual_failure(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"same", "b.txt": b"same", "missing.txt": b"same"})
    from pathlib import Path
    Path(scan.root, "missing.txt").unlink()
    diagnostics = {}
    groups = find_duplicates(scan.files, report=diagnostics)
    window._load_groups(groups, "duplicate", diagnostics)
    assert "일부 미완료" in window.warning_label.text()
    assert "missing.txt" in window.warning_label.toolTip()
    report = window.match_report_payload()
    assert report["duplicate_analysis"]["complete"] is False
    assert len(report["duplicates"]) == 1
    assert window.keep_first_delete_button.isEnabled()
