"""Rule explanations follow edits without inspecting or changing user files."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from folder_organizer import rules
from folder_organizer.rule_view import ClassificationDialog, rule_example


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def test_example_explains_destination_and_date_source_without_building_a_plan(monkeypatch):
    monkeypatch.setattr(rules.ops, "build_routed_plan", lambda *_args, **_kwargs: pytest.fail("example inspected files"))
    example = rule_example({"name": "청구서", "extensions": ["pdf"], "name_pattern": "invoice*",
                            "target": "청구서/{year}/{month}/{source_folder}"})
    assert "invoice*" in example["conditions"] and ".pdf" in example["conditions"]
    assert "선택 폴더/정리됨/청구서/2026/10/여행/예시 파일.pdf" in example["path"]
    assert "수정일" in example["note"] and "촬영일" in example["note"]
    assert "가상" in example["note"] and "실제" in example["note"]


def test_preset_target_edit_and_output_action_refresh_the_explanation(app):
    dialog = ClassificationDialog(rules.default_rules("type"))
    assert "사진/여행" in dialog.example_path.text()
    dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData("date"))
    dialog._load_preset()
    assert "2026/10/여행" in dialog.example_path.text()
    dialog.table.item(0, 4).setText("보관/{ext}")
    dialog.destination.setText("사용자 분류 폴더")
    assert "사용자 분류 폴더/보관/jpg" in dialog.example_path.text()
    dialog.action_combo.setCurrentIndex(dialog.action_combo.findData("move"))
    assert "원래 위치에서 파일을 옮기고" in dialog.safety_label.text()
    assert not dialog.values
    dialog.close()


def test_reordered_rule_keeps_current_example_and_multiple_condition_summary(app):
    dialog = ClassificationDialog([
        {"name": "사진", "extensions": ["jpg"], "target": "사진", "folder_pattern": "업무"},
        {"name": "문서", "extensions": ["pdf"], "target": "문서"},
    ])
    assert "원래 폴더 업무" in dialog.example_conditions.text()
    dialog._move_rule(1)
    assert dialog.table.currentRow() == 1
    assert "사진/예시 파일.jpg" in dialog.example_path.text()
    dialog.table.setCurrentCell(0, 1)
    assert "문서/예시 파일.pdf" in dialog.example_path.text()
    assert ".pdf" in dialog.example_conditions.text()
    dialog.close()


def test_collapsing_optional_conditions_preserves_their_saved_values(app):
    dialog = ClassificationDialog([{"name": "문서", "target": "문서", "min_size": 1025,
                                    "max_size": 2048, "modified_after": "2025-01-01",
                                    "folder_pattern": "업무"}])
    assert dialog.advanced_toggle.isChecked()
    dialog.advanced_toggle.click()
    assert dialog.advanced_panel.isHidden()
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    saved = dialog.values["rules"][0]
    assert saved["min_size"] == 1025 and saved["max_size"] == 2048
    assert saved["modified_after"] == "2025-01-01" and saved["folder_pattern"] == "업무"


def test_incomplete_template_uses_inline_guidance_until_valid_again(app):
    dialog = ClassificationDialog(rules.default_rules("type"))
    dialog.table.item(0, 4).setText("사진/{year")
    assert not dialog.example_path.text()
    assert "입력 중" in dialog.example_conditions.text()
    dialog.table.item(0, 4).setText("사진/{year}")
    assert "사진/2026" in dialog.example_path.text()
    assert not dialog.values
    dialog.close()
