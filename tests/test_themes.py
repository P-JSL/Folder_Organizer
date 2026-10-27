"""Theme changes keep native controls and existing help documents readable."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QWidget

from folder_organizer.help_content import get_topic
from folder_organizer.help_view import HelpCenterDialog, topic_html
from folder_organizer.themes import PALETTES, apply_theme, current_theme


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    yield application
    apply_theme(None, "dark")


def contrast(first: str, second: str) -> float:
    def luminance(value):
        rgb = QColor(value).getRgbF()[:3]
        linear = tuple(channel / 12.92 if channel <= .04045 else
                       ((channel + .055) / 1.055) ** 2.4 for channel in rgb)
        return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_theme_text_has_readable_contrast(mode):
    colors = PALETTES[mode]
    for foreground, background in (
        ("text", "background"), ("text", "surface"), ("muted", "surface"),
        ("accent", "surface"), ("accent_text", "accent"),
        ("selection_text", "selection"), ("warning", "warning_background"),
    ):
        assert contrast(colors[foreground], colors[background]) >= 4.5, (mode, foreground, background)


def test_invalid_mode_defaults_dark_and_new_dialogs_inherit_palette(app):
    root = QWidget()
    root.setStyleSheet("QWidget { background: red; }")
    assert apply_theme(root, "system-or-invalid") == "dark"
    assert root.styleSheet() == ""
    assert current_theme(root) == "dark"
    assert current_theme() == "dark"
    dialog = QDialog(root)
    field = QLineEdit(dialog)
    assert current_theme(field) == "dark"
    assert app.palette().color(QPalette.ColorRole.Window).name() == PALETTES["dark"]["background"]
    assert apply_theme(root, "light") == "light"
    assert current_theme(field) == "light"
    new_dialog = QDialog()
    assert current_theme(new_dialog) == "light"
    assert new_dialog.palette().color(QPalette.ColorRole.Text).name() == PALETTES["light"]["text"]
    for widget in (dialog, new_dialog, root):
        widget.close()


def test_switching_theme_recolors_open_help_without_losing_state(app):
    root = QWidget()
    apply_theme(root, "dark")
    help_dialog = HelpCenterDialog(root, topic="duplicates")
    help_dialog.search_input.setText("중복")
    help_dialog.select_topic("duplicates")
    help_dialog.technical_toggle.setChecked(True)
    original_text = help_dialog.browser.toPlainText()
    assert help_dialog.browser.document().find(get_topic("duplicates").title).charFormat().foreground().color().name() == PALETTES["dark"]["accent"]
    apply_theme(root, "light")
    assert help_dialog.current_topic == "duplicates"
    assert help_dialog.search_input.text() == "중복"
    assert help_dialog.technical_toggle.isChecked()
    assert not help_dialog.technical_details.isHidden()
    assert help_dialog.browser.toPlainText() == original_text
    assert help_dialog.browser.document().find(get_topic("duplicates").title).charFormat().foreground().color().name() == PALETTES["light"]["accent"]
    assert PALETTES["light"]["text"] in topic_html(get_topic("duplicates"), "light")
    help_dialog.close()
    root.close()
