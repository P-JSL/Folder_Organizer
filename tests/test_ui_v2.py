"""End-to-end GUI checks for evidence review and conservative copy selection."""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QLabel, QPlainTextEdit, QTabWidget
from PySide6.QtGui import QImage

from folder_organizer import ui
from folder_organizer.comparison import compare_files
from folder_organizer.matching import find_duplicates, find_similar
from folder_organizer.models import MatchGroup
from folder_organizer.scanner import scan_folder


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "ui-preferences.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(ui, "QSettings", lambda *_args: settings)
    monkeypatch.setattr(ui.updater, "is_auto_update_supported", lambda: False)
    widget = ui.MainWindow()
    widget.update_timer.stop()
    widget.initial_update_timer.stop()
    yield widget
    for worker in (widget._worker, widget._update_worker):
        if worker is not None:
            worker.cancel_event.set()
            worker.wait(5000)
    app.processEvents()
    widget.deferred_update_timer.stop()
    widget._pending_update = None
    widget.close()
    widget.deleteLater()
    app.processEvents()


def wait(app, window, timeout=5):
    deadline = time.monotonic() + timeout
    while window._worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    app.processEvents()
    assert window._worker is None


def populate(window, tmp_path, files=None):
    root = tmp_path / "Documents"
    root.mkdir(exist_ok=True)
    for name, content in (files or {"invoice.txt": b"one", "invoice copy.txt": b"two"}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result = scan_folder(root)
    window.load_scan_result(result)
    return result


def test_settings_presets_and_independent_thresholds_preserve_exact_byte_scope(app):
    values = copy.deepcopy(ui.ANALYSIS_DEFAULTS)
    values.update(min_size=123, max_size=987)
    dialog = ui.AnalysisSettingsDialog(values)
    dialog.profile.setCurrentIndex(dialog.profile.findData("photos"))
    assert [method for method, box in dialog.methods.items() if box.isChecked()] == ["image"]
    dialog.thresholds["image"].setValue(.73)
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values["image_threshold"] == .73
    assert dialog.values["name_threshold"] == .82
    # Merely saving a profile must not round existing byte-level scope limits.
    assert (dialog.values["min_size"], dialog.values["max_size"]) == (123, 987)
    dialog.close()


def test_bad_stored_scope_uses_safe_defaults_and_preferences_roundtrip(window):
    window.settings.setValue("analysis/preferences", json.dumps({"exclude_patterns": ["../private"], "methods": ["image"], "image_threshold": .77}))
    parsed = ui.analysis_preferences(window.settings)
    assert parsed["exclude_patterns"] == []
    assert parsed["methods"] == ["image"] and parsed["image_threshold"] == .77
    window.analysis_options = {**parsed, "exclude_patterns": ["Backup", "*.tmp"], "min_size": 10}
    window._persist_analysis_options()
    restored = ui.analysis_preferences(window.settings)
    assert restored["exclude_patterns"] == ["Backup", "*.tmp"]
    assert restored["min_size"] == 10


def test_name_threshold_edit_invalidates_prior_similar_selection(window, tmp_path):
    scan = populate(window, tmp_path)
    groups = find_similar(scan.files, methods=("name",))
    window._load_groups(groups, "similar")
    window.model.set_checked([scan.files[0].path])
    window.similarity_threshold.setValue(.97)
    assert window.groups == [] and window.selected_paths() == []
    assert window.match_kind is None and window.scan_complete
    assert not window.views.isTabVisible(1)
    assert "다시 실행" in window.status_label.text()


def test_recursive_and_hidden_scope_change_requires_rescan_and_is_saved(window, tmp_path):
    scan = populate(window, tmp_path)
    window.model.set_checked([scan.files[0].path])
    window.hidden_check.setChecked(True)
    assert not window.scan_complete and window.selected_paths() == []
    assert window.settings.value("analysis/hidden", False, type=bool)
    window.recursive_check.setChecked(False)
    assert not window.settings.value("analysis/recursive", True, type=bool)


def test_new_scope_settings_rescan_actual_folder(app, window, tmp_path, monkeypatch):
    populate(window, tmp_path, {"keep.txt": b"abc", "private/skip.txt": b"secret", "small.txt": b"x"})
    values = copy.deepcopy(window.analysis_options)
    values.update(exclude_patterns=["private"], min_size=2)

    class AcceptedSettings:
        def __init__(self, *_args):
            self.values = values
        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui, "AnalysisSettingsDialog", AcceptedSettings)
    window.show_analysis_settings()
    wait(app, window)
    assert [entry.name for entry in window.model.entries] == ["keep.txt"]
    assert window.scan_result.filtered_files == 1 and window.scan_result.excluded_directories == 1
    assert window.scan_result.scan_options["exclude_patterns"] == ["private"]


def test_evidence_filter_preserves_selection_and_all_metric_scores(window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"one", "b.txt": b"two", "c.txt": b"three"})
    paths = [entry.path for entry in scan.files]
    named = MatchGroup("similar_name", paths[:2], "이름만", 1.0,
                       {"byte_identical": False, "matched_signals": ["name"], "name_score": 1.0})
    content = MatchGroup("similar_text", paths[1:], "내용도 비교", .93,
                         {"byte_identical": False, "matched_signals": ["name", "text"], "name_score": 1.0, "text_score": .93})
    window._load_groups([named, content], "similar", {"compared_pairs": 3})
    window.model.set_checked([paths[0]])
    window.match_filter.setCurrentIndex(window.match_filter.findData("content"))
    assert window.match_tree.topLevelItemCount() == 1
    assert window.selected_paths() == [paths[0]]
    row = window.match_tree.topLevelItem(0)
    assert "이름 1.00" in row.text(2) and "텍스트 0.93" in row.text(2)
    assert "확률이 아닙니다" in row.toolTip(2)
    assert row.data(0, Qt.ItemDataRole.UserRole + 1) == 1
    assert not window.retention_button.isEnabled()
    assert not window.select_duplicates_button.isEnabled()


def test_similarity_limits_remain_visible_in_screen_and_report(window, tmp_path):
    scan = populate(window, tmp_path)
    report = {"compared_pairs": 1, "max_pairs": 1, "limit_reached": True,
              "skipped_count": 2, "warnings": ["읽지 못한 파일"]}
    window._load_groups(find_similar(scan.files, methods=("name",)), "similar", report)
    assert "한도 1쌍" in window.warning_label.text()
    assert "2건" in window.warning_label.text()
    assert "읽지 못한 파일" in window.warning_label.toolTip()
    assert window.match_report_payload()["comparison_report"] == report


def test_first_file_shortcut_revalidates_and_respects_protected_folder(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a/source.txt": b"same", "z/protected.txt": b"same"})
    window.analysis_options["protected_folders"] = [str(Path(scan.root) / "z")]
    window._load_groups(find_duplicates(scan.files), "duplicate")
    window.select_duplicate_candidates()
    wait(app, window)
    assert [Path(path).parent.name for path in window.selected_paths()] == ["a"]
    assert len(window.last_retention["kept"]) == 1
    assert all(Path(entry.path).read_bytes() == b"same" for entry in scan.files)


def test_content_changed_after_duplicate_check_is_not_auto_selected(app, window, tmp_path):
    scan = populate(window, tmp_path, {"a.txt": b"same", "b.txt": b"same"})
    window._load_groups(find_duplicates(scan.files), "duplicate")
    changed = Path(scan.files[1].path)
    original = changed.stat()
    changed.write_bytes(b"edit")
    os.utime(changed, ns=(original.st_atime_ns, original.st_mtime_ns))
    window.select_duplicate_candidates()
    wait(app, window)
    assert window.selected_paths() == []
    assert window.last_retention["warnings"]
    assert changed.read_bytes() == b"edit"


def test_pair_comparison_shows_full_hash_text_difference_and_no_file_mutation(window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path, {"one.txt": b"original words\n", "two.txt": b"changed words\n"})
    compared = compare_files(*scan.files)
    seen = {}

    def inspect_dialog(dialog):
        seen["tabs"] = [tab.tabText(i) for tab in dialog.findChildren(QTabWidget) for i in range(tab.count())]
        seen["text"] = "\n".join(edit.toPlainText() for edit in dialog.findChildren(QPlainTextEdit))
        seen["labels"] = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    window._show_pair_comparison(compared)
    assert "파일 정보·전체 해시" in seen["tabs"] and "텍스트 차이" in seen["tabs"]
    assert compared["left"]["sha256"] in seen["text"] and compared["right"]["sha256"] in seen["text"]
    assert "-original words" in seen["text"] and "+changed words" in seen["text"]
    assert "동일할 확률이 아닙니다" in seen["labels"]
    assert Path(scan.files[0].path).read_bytes() == b"original words\n"


def test_pair_comparison_image_preview_and_metadata(window, tmp_path, monkeypatch):
    from PIL import Image
    root = tmp_path / "Photos"
    root.mkdir()
    Image.new("RGB", (60, 40), "blue").save(root / "photo.png")
    Image.new("RGB", (120, 80), "blue").save(root / "resized.png")
    scan = scan_folder(root)
    window.load_scan_result(scan)
    compared = compare_files(*scan.files)
    seen = {}

    def inspect_dialog(dialog):
        seen["tabs"] = [tab.tabText(i) for tab in dialog.findChildren(QTabWidget) for i in range(tab.count())]
        seen["pixmaps"] = [label.pixmap() for label in dialog.findChildren(QLabel) if label.pixmap() and not label.pixmap().isNull()]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    window._show_pair_comparison(compared)
    assert "이미지 나란히 보기" in seen["tabs"]
    assert len(seen["pixmaps"]) == 2
    assert all(pixmap.width() <= 450 and pixmap.height() <= 450 for pixmap in seen["pixmaps"])


def test_changed_pair_reports_rescan_instead_of_displaying_old_proof(app, window, tmp_path, monkeypatch):
    scan = populate(window, tmp_path)
    Path(scan.files[0].path).write_bytes(b"changed after scan")
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args))
    window.compare_paths(scan.files[0].path, scan.files[1].path)
    wait(app, window)
    assert errors and not hasattr(window, "last_pair_result")


def test_user_cancelling_comparison_does_not_display_failure_dialog(app, window, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args))

    def cancelled(worker):
        worker.cancel_event.wait(2)
        raise ValueError("파일 비교를 중단했습니다.")

    callbacks = []
    window._start_task("비교", cancelled, callbacks.append, cancellable=True)
    window.cancel_task()
    wait(app, window)
    assert not errors and not callbacks
    assert "중단했습니다" in window.status_label.text()


def test_compact_window_keeps_comparison_and_all_operation_categories_accessible(app, window, tmp_path):
    scan = populate(window, tmp_path)
    window._load_groups(find_similar(scan.files, methods=("name",)), "similar", {"compared_pairs": 1})
    window.resize(1306, 730)
    window.show()
    app.processEvents()
    assert window.minimumSizeHint().height() <= window.height()
    for control in (window.match_tree, window.preview_button, window.assume_duplicates_button,
                    window.recovery_menu_button, window.export_menu_button):
        origin = control.mapTo(window, control.rect().topLeft())
        assert 0 <= origin.y() and origin.y() + control.height() <= window.height()
    assert {action.text() for action in window.recovery_menu_button.menu().actions()} >= {
        "선택 파일 손상 검사", "원본을 보존하고 복구본 만들기", "작업 기록 · 실행 취소"}
    assert {action.text() for action in window.export_menu_button.menu().actions()} == {
        "표시된 파일 목록 · CSV", "분석 보고서 · HTML / JSON"}


def test_image_preview_fits_resized_panel_without_cropping(app):
    image = QImage(512, 256, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.blue)
    preview = ui.ImagePreviewLabel(image)
    preview.resize(180, 120)
    preview.show()
    app.processEvents()
    assert preview.pixmap().width() <= preview.width()
    assert preview.pixmap().height() <= preview.height()
    assert preview.pixmap().width() / preview.pixmap().height() == pytest.approx(2, abs=.05)
    preview.close()


@pytest.mark.parametrize("extension,selected_filter", [("html", "HTML 보고서 (*.html)"), ("json", "JSON 보고서 (*.json)")])
def test_export_report_contains_evidence_and_scope_without_raw_contents(app, window, tmp_path, monkeypatch, extension, selected_filter):
    scan = populate(window, tmp_path, {"invoice.txt": b"PRIVATE_ALPHA_FILE_CONTENT", "invoice copy.txt": b"PRIVATE_BETA_FILE_CONTENT"})
    window._load_groups(find_similar(scan.files, methods=("name",)), "similar", {"compared_pairs": 1})
    output = tmp_path / ("analysis-" + extension)
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *_args: (str(output), selected_filter))
    window.export_match_report()
    wait(app, window)
    saved = output.with_suffix("." + extension)
    assert saved.exists()
    text = saved.read_text(encoding="utf-8")
    assert "유사" in text or "similar" in text
    assert "byte_identical" in text or "바이트" in text
    assert "PRIVATE_ALPHA_FILE_CONTENT" not in text and "PRIVATE_BETA_FILE_CONTENT" not in text
