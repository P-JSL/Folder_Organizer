"""The help center explains consequences and never performs a file action."""
from __future__ import annotations

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from folder_organizer import operations
from folder_organizer.help_content import TOPICS, get_topic
from folder_organizer.help_view import HelpCenterDialog, LocalHelpBrowser, topic_html


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def test_contextual_navigation_clears_conflicting_search_without_file_work(app, tmp_path, monkeypatch):
    source = tmp_path / "untouched.txt"
    source.write_bytes(b"original contents")
    monkeypatch.setattr(operations, "execute_plan", lambda *_args, **_kwargs: pytest.fail("Help executed a plan"))
    monkeypatch.setattr(operations, "undo_operation", lambda *_args, **_kwargs: pytest.fail("Help restored files"))
    dialog = HelpCenterDialog(topic="keep_first")
    assert dialog.current_topic == "keep_first"
    text = dialog.browser.toPlainText()
    assert "첫" in text and "안전 보관" in text
    dialog.search_input.setText("zzzz-no-such-feature")
    assert dialog.current_topic is None
    assert "맞는 안내가 없습니다" in dialog.browser.toPlainText()
    assert dialog.select_topic("restore")
    assert dialog.search_input.text() == ""
    assert dialog.current_topic == "restore"
    for topic in TOPICS:
        assert dialog.select_topic(topic.key)
        text = dialog.browser.toPlainText()
        assert "언제 쓰나요?" in text
        assert "어떻게 사용하나요?" in text
        assert "실행하면 어떻게 되나요?" in text
    assert source.read_bytes() == b"original contents"
    assert list(tmp_path.iterdir()) == [source]
    assert not dialog.select_topic("unknown-command")
    dialog.close()


def test_search_filters_topics_and_empty_result_can_return_to_all_features(app):
    dialog = HelpCenterDialog()
    dialog.search_input.setText("사진")
    assert "similar" in dialog._items
    assert len(dialog._items) < len(TOPICS)
    dialog.search_input.setText("zzzz-nothing-found")
    assert dialog._items == {}
    assert dialog.current_topic is None
    dialog.clear_search_button.click()
    assert len(dialog._items) == len(TOPICS)
    assert dialog.current_topic in {topic.key for topic in TOPICS}
    dialog.close()


def test_technical_information_is_secondary_and_resets_on_next_topic(app):
    dialog = HelpCenterDialog(topic="duplicates")
    assert not dialog.technical_toggle.isChecked()
    assert dialog.technical_details.isHidden()
    dialog.technical_toggle.setChecked(True)
    assert not dialog.technical_details.isHidden()
    assert dialog.technical_details.toPlainText() == get_topic("duplicates").technical
    dialog.select_topic("similar")
    assert dialog.technical_details.isHidden()
    assert not dialog.technical_toggle.isChecked()
    dialog.close()


def test_help_content_is_escaped_and_browser_cannot_load_local_or_network_resources(app):
    hostile = replace(get_topic("start"), title='<img src="file:///private.txt">',
                      summary='<script>alert("hello")</script>', note='A & B < C')
    html = topic_html(hostile)
    assert '<img src=' not in html and '<script>' not in html
    assert '&lt;img' in html and 'A &amp; B &lt; C' in html
    browser = LocalHelpBrowser()
    browser.setHtml(html)
    assert '<script>alert("hello")</script>' in browser.toPlainText()
    for url in ("https://example.com/file.png", "file:///private.txt", "data:image/png;base64,abcd"):
        assert browser.loadResource(QTextDocument.ResourceType.ImageResource, QUrl(url)) is None
    assert not browser.openExternalLinks() and not browser.openLinks()
    browser.close()


def test_dialog_stays_usable_on_standard_desktop_size(app):
    dialog = HelpCenterDialog(topic="rules")
    dialog.resize(1000, 650)
    dialog.show()
    app.processEvents()
    assert dialog.minimumSizeHint().height() <= dialog.height()
    for widget in (dialog.search_input, dialog.topic_tree, dialog.browser, dialog.technical_toggle):
        origin = widget.mapTo(dialog, widget.rect().topLeft())
        assert origin.x() >= 0 and origin.x() + widget.width() <= dialog.width()
        assert origin.y() >= 0 and origin.y() + widget.height() <= dialog.height()
    dialog.close()
