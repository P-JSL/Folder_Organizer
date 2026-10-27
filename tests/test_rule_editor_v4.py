"""Mapping edits share the planner renderer and retain per-rule date choices."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from folder_organizer import rules
from folder_organizer.rule_view import ClassificationDialog, rule_example


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def test_token_insertion_edits_selected_rule_at_cursor_and_refreshes_preview(app):
    dialog = ClassificationDialog([
        {"name": "이전 규칙", "target": "보관"},
        {"name": "현재 규칙", "target": "자료/"},
    ])
    dialog.table.setCurrentCell(1, 1)
    dialog.target_template.setCursorPosition(len("자료/"))
    dialog.token_combo.setCurrentIndex(dialog.token_combo.findData("{규칙}"))
    dialog._insert_token()
    assert dialog.table.item(1, 4).text() == "자료/{규칙}"
    assert "자료/현재 규칙/예시 파일.jpg" in dialog.example_path.text()
    assert dialog.table.item(0, 4).text() == "보관"
    dialog.table.item(1, 4).setText("원본/{날짜}")
    assert dialog.target_template.text() == "원본/{날짜}"
    assert "원본/2026-10-15/예시 파일.jpg" in dialog.example_path.text()
    assert dialog.values == {}
    dialog.close()


def test_date_basis_follows_rule_through_reordering_and_changes_live_path(app):
    dialog = ClassificationDialog([
        {"name": "날짜 분류", "target": "{연도}/{월}"},
        {"name": "다른 규칙", "target": "그 외"},
    ])
    dialog.date_basis.setCurrentIndex(dialog.date_basis.findData("created"))
    dialog.modified_after.setText("2025-01-01")
    assert "2025/03/예시 파일.jpg" in dialog.example_path.text()
    assert "생성일" in dialog.example_conditions.text()
    dialog._move_rule(1)
    assert dialog.date_basis.currentData() == "created"
    assert dialog.modified_after.text() == "2025-01-01"
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.rules_value[1]["date_basis"] == "created"
    assert dialog.rules_value[0]["date_basis"] == "modified"
    dialog.close()


def test_example_uses_the_actual_renderer_without_building_or_inspecting_files(monkeypatch):
    calls = []
    actual = rules.render_target

    def track(rule, entry, relative):
        calls.append((rule["target"], relative))
        return actual(rule, entry, relative)

    monkeypatch.setattr(rules, "render_target", track)
    monkeypatch.setattr(rules.ops, "build_routed_plan", lambda *_a, **_k: pytest.fail("preview touched filesystem"))
    rule = {"name": "회사: 보고서", "target": "{규칙}/{수정일:%Y/%m/%d}/{created:%Y-%m}/{이름}"}
    example = rule_example(rule)
    entry = SimpleNamespace(name="예시 파일.jpg", suffix=".jpg",
                            modified=datetime(2026, 10, 15, 14, 30).timestamp(),
                            created=datetime(2025, 3, 7, 9, 15).timestamp())
    expected = actual(rules.validate_rules([rule])[0], entry, Path("여행/예시 파일.jpg"))
    assert f"/{expected}/예시 파일.jpg" in example["path"]
    assert (rule["target"], Path("여행/예시 파일.jpg")) in calls


@pytest.mark.parametrize("preset", ["daily", "created_date", "quarter", "rule"])
def test_extended_presets_are_editable_and_return_safe_preview_parameters(app, preset):
    dialog = ClassificationDialog(rules.default_rules())
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData(preset))
    dialog._load_preset()
    assert dialog.target_template.text() == rules.default_rules(preset)[0]["target"]
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values["rules"] == rules.default_rules(preset)
    assert dialog.values["action"] == "copy"
    dialog.close()


def test_invalid_date_formatter_cannot_leave_editor(app, monkeypatch):
    dialog = ClassificationDialog(rules.default_rules())
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    dialog.target_template.setText("{수정일:%Q}")
    assert not dialog.example_path.text()
    dialog._save()
    assert warnings and not dialog.values
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.close()


def test_final_confirmation_stays_visible_when_advanced_editor_needs_scrolling(app):
    dialog = ClassificationDialog(rules.default_rules())
    dialog.resize(1020, 700)
    dialog.advanced_toggle.setChecked(True)
    dialog.show()
    app.processEvents()
    button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    point = button.mapTo(dialog, button.rect().center())
    assert dialog.rect().contains(point)
    assert button.isVisible() and button.isEnabled()
    dialog.close()
