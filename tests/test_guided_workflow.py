"""The next-step UI explains scope without silently changing original files."""
from pathlib import Path

from PySide6.QtWidgets import QDialog, QLabel

from folder_organizer import ui
from folder_organizer.operations import build_plan
from folder_organizer.models import MatchGroup
from test_ui_v2 import app, window, populate, wait


def test_guided_duplicate_flow_stops_at_review_until_user_confirms(app, window, tmp_path, monkeypatch):
    assert not window.duplicates_button.isEnabled()
    assert not window.preview_button.isEnabled()
    scan = populate(window, tmp_path, {"a.txt": b"same original", "b.txt": b"same original", "other.txt": b"different"})
    original = {entry.path: Path(entry.path).read_bytes() for entry in scan.files}
    assert window._guidance_action == "duplicates"
    window.next_step_button.click()
    wait(app, window)
    assert len(window.groups) == 1 and window._guidance_action == "retention"
    window.next_step_button.click()
    wait(app, window)
    assert len(window.selected_paths()) == 1 and window._guidance_action == "preview"
    previews = []
    def reject(dialog):
        previews.append("\n".join(label.text() for label in dialog.findChildren(QLabel)))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", reject)
    window.next_step_button.click()
    wait(app, window)
    assert previews and "실행 결과" in previews[0] and "안전 보관" in previews[0]
    assert {path: Path(path).read_bytes() for path in original} == original
    assert not (Path(scan.root) / ".folder-organizer").exists()


def test_hidden_checks_remain_visible_in_hint_and_actual_copy_preview(window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path, {"a.txt": b"first", "b.txt": b"second", "c.txt": b"visible"})
    paths = [entry.path for entry in scan.files if entry.name != "c.txt"]
    window.model.set_checked(paths)
    window.search_input.setText("c.txt")
    assert window.proxy.rowCount() == 1
    assert "숨겨진 2개 포함" in window.selection_hint.text()
    assert "2개" in window.preview_button.text()
    copy_index = next(index for index in range(window.action_combo.count())
                      if window.action_combo.itemData(index)[0] == "copy")
    window.action_combo.setCurrentIndex(copy_index)
    assert "원본을 남기고" in window.selection_hint.text()
    output = tmp_path / "Copies"
    plan = build_plan(scan.root, window.selected_paths(), "copy", destination=str(output))
    previews = []
    def reject(dialog):
        previews.append("\n".join(label.text() for label in dialog.findChildren(QLabel)))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", reject)
    window._show_preview(plan)
    assert len(plan["items"]) == 2
    assert "숨겨진 파일 2개" in previews[0] and "원본은 그대로" in previews[0]
    assert not output.exists() and all(Path(entry.path).exists() for entry in scan.files)


def test_context_help_reuses_read_only_window_and_clears_conflicting_search(app, window, tmp_path):
    scan = populate(window, tmp_path)
    original = {entry.path: Path(entry.path).read_bytes() for entry in scan.files}
    window.help_button.click()
    app.processEvents()
    dialog = window._help_dialog
    try:
        assert dialog is not None and dialog.isVisible() and dialog.current_topic == "start"
        dialog.search_input.setText("복원")
        window.show_comparison_guide()
        app.processEvents()
        assert window._help_dialog is dialog and dialog.current_topic == "duplicates"
        assert not dialog.search_input.text()
        assert window._worker is None
        assert {path: Path(path).read_bytes() for path in original} == original
    finally:
        dialog.close()
        app.processEvents()
    assert window._help_dialog is None


def test_empty_results_and_incomplete_scans_get_appropriate_next_action(window, tmp_path):
    scan = populate(window, tmp_path)
    window._load_groups([], "duplicate")
    assert window._guidance_action == "rules" and not window.keep_first_delete_button.isEnabled()
    scan.cancelled = True
    window.load_scan_result(scan)
    assert window._guidance_action == "scan" and window.next_step_button.isEnabled()
    assert not window.duplicates_button.isEnabled() and not window.preview_button.isEnabled()


def test_closing_main_window_also_closes_modeless_feature_help(app, window):
    window.show()
    window.help_button.click()
    app.processEvents()
    assert window._help_dialog is not None and window._help_dialog.isVisible()
    window.close()
    app.processEvents()
    assert window._help_dialog is None and not window.isVisible()


def test_compare_next_step_opens_first_visible_candidate_and_recovers_empty_filter(window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path, {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c", "d.txt": b"d"})
    paths = [entry.path for entry in scan.files]
    groups = [MatchGroup("similar_text", paths[:2], "text review", .9),
              MatchGroup("similar_text", paths[2:], "text review", .9)]
    window._load_groups(groups, "similar")
    window.views.setCurrentIndex(0)
    comparisons = []
    monkeypatch.setattr(window, "compare_paths", lambda left, right: comparisons.append((left, right)))
    window.next_step_button.click()
    assert window.views.currentIndex() == 1
    assert comparisons == [tuple(paths[:2])]
    window.match_filter.setCurrentIndex(window.match_filter.findData("image"))
    assert window.match_tree.topLevelItemCount() == 0 and window._guidance_action == "results"
    window.next_step_button.click()
    assert window.match_tree.topLevelItemCount() == 2
    assert comparisons == [tuple(paths[:2])] and not window.model.checked
