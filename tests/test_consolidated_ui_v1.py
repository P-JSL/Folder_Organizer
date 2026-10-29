"""New unified classification flow; do not rerun the prior GUI suite."""
import pytest

from folder_organizer import ui
from folder_organizer.rule_view import ClassificationDialog
from folder_organizer.rules import default_rules
from test_ui_v2 import app, window


def test_manual_controls_offer_only_distinct_operations(app, window):
    assert [window.action_combo.itemData(i) for i in range(window.action_combo.count())] == [
        ("quarantine", "modified"), ("copy", "modified"), ("move", "modified"),
    ]
    assert window.manual_menu.title() == "체크한 파일 수동 정리"
    assert window.manual_menu.menuAction() in window.organize_menu.actions()
    assert len(window.manual_menu.actions()) == 3
    assert window.classify_button in window.organize_menu.actions()


@pytest.mark.parametrize("mode,basis,preset", [
    ("date", "modified", "date"), ("date", "created", "created_date"),
    ("extension", "modified", "extension"),
])
def test_legacy_classification_shortcuts_open_shared_rules_without_mutating(app, window, monkeypatch, mode, basis, preset):
    opened = []
    monkeypatch.setattr(window, "show_classification", lambda **kwargs: opened.append(kwargs))
    monkeypatch.setattr(window, "preview_selected", lambda: pytest.fail("Legacy shortcut opened manual deletion"))
    window._manual_action(mode, basis)
    assert opened == [{"preset": preset}]


def test_extension_preset_is_available_in_the_common_rule_editor(app):
    rules = default_rules("extension")
    assert rules[0]["target"] == "{ext}/{source_folder}"
    dialog = ClassificationDialog(rules)
    try:
        assert dialog.preset_combo.findData("extension") >= 0
    finally:
        dialog.close()


def test_unknown_manual_mode_cannot_silently_choose_delete(app, window, monkeypatch):
    monkeypatch.setattr(window, "preview_selected", lambda: pytest.fail("Unknown mode selected a deletion"))
    with pytest.raises(ValueError):
        window._manual_action("unsupported", "modified")
