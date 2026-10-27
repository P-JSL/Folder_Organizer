"""Rule editing retains ordered decisions and validates before creating a plan."""
from __future__ import annotations

from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from folder_organizer.rule_view import ClassificationDialog, _size_value
from folder_organizer.rules import default_rules, validate_rules


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def test_rule_editor_defaults_to_copy_and_preserves_original_rules(app, tmp_path):
    rules = [{"name": "보고서", "extensions": ["pdf"], "target": "문서/{year}",
              "min_size": 1, "max_size": 1025, "modified_after": "2024-01-01"}]
    original = deepcopy(rules)
    dialog = ClassificationDialog(rules)
    assert dialog.action_combo.currentData() == "copy"
    dialog.destination.setText(str(tmp_path))
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert rules == original
    assert dialog.values["action"] == "copy" and dialog.values["destination"] == str(tmp_path)
    assert dialog.values["rules"][0]["extensions"] == [".pdf"]
    assert dialog.values["rules"][0]["min_size"] == 1
    assert dialog.values["rules"][0]["max_size"] == 1025
    assert dialog.values["rules"][0]["modified_after"] == "2024-01-01"


def test_reordering_rules_keeps_selected_rule_filters(app):
    dialog = ClassificationDialog([
        {"name": "사진", "extensions": ["jpg"], "target": "사진", "folder_pattern": "Downloads/*"},
        {"name": "그 외", "target": "기타", "max_size": 42},
    ])
    dialog.min_size.setText("10 MB")
    dialog.modified_before.setText("2026-01-01")
    dialog._move_rule(1)
    assert dialog.table.currentRow() == 1
    assert dialog.table.item(1, 1).text() == "사진"
    assert dialog.folder_pattern.text() == "Downloads/*"
    dialog._save()
    assert [rule["name"] for rule in dialog.rules_value] == ["그 외", "사진"]
    assert dialog.rules_value[1]["min_size"] == 10 * 1024**2
    assert dialog.rules_value[1]["modified_before"] == "2026-01-01"
    assert dialog.rules_value[0]["max_size"] == 42


def test_invalid_rule_target_stays_in_editor_without_plan(app, monkeypatch):
    dialog = ClassificationDialog(default_rules("type"))
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: errors.append(_args[-1]))
    dialog.table.item(0, 4).setText("../outside")
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert errors and not dialog.values


@pytest.mark.parametrize("preset", ["type", "date", "folder"])
def test_recommended_presets_produce_valid_editable_rules(app, preset):
    dialog = ClassificationDialog(default_rules("type"))
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData(preset))
    dialog._load_preset()
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.rules_value == validate_rules(default_rules(preset))


def test_add_remove_and_disable_rules_are_explicit(app):
    dialog = ClassificationDialog(default_rules("type"))
    original_count = dialog.table.rowCount()
    dialog._add_rule()
    assert dialog.table.rowCount() == original_count + 1
    dialog.table.item(original_count, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.table.setCurrentCell(0, 1)
    dialog._save()
    assert dialog.rules_value[-1]["enabled"] is False
    dialog._remove_rule()
    assert dialog.table.rowCount() == original_count


@pytest.mark.parametrize("text,expected", [("", 0), ("1025 B", 1025), ("10 MB", 10 * 1024**2),
                                            ("1.5KB", 1536), ("2 gb", 2 * 1024**3)])
def test_size_rule_input_uses_readable_units_without_rounding(text, expected):
    assert _size_value(text) == expected


@pytest.mark.parametrize("text", ["-1 MB", "abc", "1.1 B", "1e999", "nan"])
def test_invalid_size_rule_input_is_rejected(text):
    with pytest.raises(ValueError):
        _size_value(text)
