"""Read-only, searchable help for choosing and understanding file actions."""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter,
    QTextBrowser, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from .help_content import HelpTopic, get_topic, topics_for_query
from .themes import PALETTES, current_theme, normalize_mode


def topic_html(topic: HelpTopic, mode: str = "dark") -> str:
    """Escape every content field before rendering a bounded local document."""
    colors = PALETTES[normalize_mode(mode)]
    steps = "".join(f"<li>{escape(step)}</li>" for step in topic.steps)
    return (
        "<html><head><style>"
        f"body {{ color: {colors['text']}; background-color: {colors['surface']}; font-family: 'Segoe UI'; font-size: 13px; margin: 14px; }}"
        f"h1 {{ font-size: 24px; color: {colors['accent']}; margin-bottom: 10px; }}"
        f"h2 {{ font-size: 16px; color: {colors['text']}; margin-top: 13px; margin-bottom: 5px; }}"
        "p { margin-top: 4px; margin-bottom: 7px; } li { margin-bottom: 7px; }"
        f".summary {{ color: {colors['muted']}; font-size: 14px; }}"
        f".note {{ color: {colors['warning']}; }}"
        "</style></head><body>"
        f"<h1>{escape(topic.title)}</h1>"
        f"<p class='summary'>{escape(topic.summary)}</p>"
        f"<h2>언제 쓰나요?</h2><p>{escape(topic.when)}</p>"
        f"<h2>어떻게 사용하나요?</h2><ol>{steps}</ol>"
        f"<h2>실행하면 어떻게 되나요?</h2><p>{escape(topic.result)}</p>"
        f"<p class='note'><b>알아두세요</b><br>{escape(topic.note)}</p>"
        "</body></html>"
    )


class LocalHelpBrowser(QTextBrowser):
    """Help never fetches remote images, documents or other resources."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setObjectName("helpDocument")
        self.setAccessibleName("선택한 기능의 사용 안내")

    def loadResource(self, resource_type, name: QUrl):
        # Content consists only of escaped text and inline formatting.
        # Refuse file:// as well as network URLs, even if help text is edited.
        return None


class HelpCenterDialog(QDialog):
    """Choose a goal, read a short example and understand its effect.

    This dialog contains no file-operation callback. Contextual callers may
    choose a topic while retaining the user's current search/navigation state.
    """
    def __init__(self, parent=None, topic: str = "start"):
        super().__init__(parent)
        self.setWindowTitle("기능 안내 · 무엇을 하고 싶으세요?")
        self.resize(1040, 660)
        self.setMinimumSize(780, 500)
        self.current_topic: str | None = None
        self._items: dict[str, QTreeWidgetItem] = {}
        outer = QVBoxLayout(self)
        heading = QLabel("무엇을 하고 싶으세요?")
        heading.setObjectName("section")
        outer.addWidget(heading)
        intro = QLabel("기능을 고르면 쓰는 상황, 사용 순서, 실행 결과를 예시로 설명합니다. 이 창에서는 파일을 변경하지 않습니다.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        outer.addWidget(intro)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("기능이나 궁금한 내용 검색 · 예: 사진 비교, 첫 번째, 복원, 날짜")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaxLength(256)
        self.search_input.setAccessibleName("기능 안내 검색")
        self.search_input.setToolTip("기능 이름과 설명에서 검색합니다. 여러 단어를 입력하면 모든 단어가 포함된 안내를 찾습니다.")
        outer.addWidget(self.search_input)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.topic_tree = QTreeWidget()
        self.topic_tree.setHeaderHidden(True)
        self.topic_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.topic_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.topic_tree.setMinimumWidth(210)
        self.topic_tree.setAccessibleName("기능별 안내 목록")
        splitter.addWidget(self.topic_tree)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self.browser = LocalHelpBrowser()
        panel_layout.addWidget(self.browser, 1)
        self.technical_toggle = QToolButton()
        self.technical_toggle.setText("자세한 기준과 지원 범위 보기")
        self.technical_toggle.setCheckable(True)
        self.technical_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.technical_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.technical_toggle.setAccessibleName("자세한 기준 펼치기")
        panel_layout.addWidget(self.technical_toggle)
        self.technical_details = QPlainTextEdit()
        self.technical_details.setReadOnly(True)
        self.technical_details.setMaximumHeight(140)
        self.technical_details.setMaximumBlockCount(80)
        self.technical_details.setAccessibleName("자세한 판정 기준과 지원 범위")
        self.technical_details.hide()
        panel_layout.addWidget(self.technical_details)
        splitter.addWidget(panel)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes((250, 740))
        outer.addWidget(splitter, 1)
        footer = QHBoxLayout()
        self.result_label = QLabel()
        self.result_label.setObjectName("muted")
        self.result_label.setWordWrap(True)
        footer.addWidget(self.result_label, 1)
        self.clear_search_button = QPushButton("전체 기능 보기")
        self.clear_search_button.clicked.connect(self.search_input.clear)
        footer.addWidget(self.clear_search_button)
        outer.addLayout(footer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("안내 닫기")
        buttons.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.search_input.textChanged.connect(self.apply_search)
        self.search_input.returnPressed.connect(self.topic_tree.setFocus)
        self.topic_tree.currentItemChanged.connect(self._topic_changed)
        self.technical_toggle.toggled.connect(self._show_technical)
        self.apply_search()
        self.select_topic(topic)

    def apply_search(self, *_args) -> None:
        """Filter navigation without running a filesystem job or event loop."""
        topics = topics_for_query(self.search_input.text())
        previous = self.current_topic
        with QSignalBlocker(self.topic_tree):
            self.topic_tree.clear()
            self._items.clear()
            groups: dict[str, QTreeWidgetItem] = {}
            for topic in topics:
                if topic.group not in groups:
                    group = QTreeWidgetItem((topic.group,))
                    group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self.topic_tree.addTopLevelItem(group)
                    group.setExpanded(True)
                    groups[topic.group] = group
                item = QTreeWidgetItem((topic.title,))
                item.setData(0, Qt.ItemDataRole.UserRole, topic.key)
                item.setToolTip(0, topic.summary)
                groups[topic.group].addChild(item)
                self._items[topic.key] = item
        count = len(topics)
        query = self.search_input.text().strip()
        self.result_label.setText(f"검색 결과 {count}개 · 검색어를 바꾸거나 전체 기능을 확인하세요."
                                  if query else f"기능 안내 {count}개 · 왼쪽에서 궁금한 기능을 선택하세요.")
        self.clear_search_button.setVisible(bool(query))
        if not topics:
            self.current_topic = None
            self.browser.setPlainText("검색한 내용과 맞는 안내가 없습니다.\n\n‘중복’, ‘사진’, ‘규칙’, ‘안전 보관’, ‘복원’처럼 짧은 말로 다시 검색해 보세요.\n아래의 ‘전체 기능 보기’를 누르면 모든 안내가 나타납니다.")
            self.technical_toggle.hide()
            self.technical_details.hide()
            return
        key = previous if previous in self._items else topics[0].key
        self.topic_tree.setCurrentItem(self._items[key])
        # Rebuilding may leave Qt with an equivalent current item and no signal.
        self._display_topic(key)

    def select_topic(self, topic: str) -> bool:
        """Open a known contextual topic, clearing a conflicting search."""
        if get_topic(topic) is None:
            return False
        if topic not in self._items:
            self.search_input.clear()
        item = self._items.get(topic)
        if item is None:
            return False
        self.topic_tree.setCurrentItem(item)
        self.topic_tree.scrollToItem(item)
        self._display_topic(topic)
        return True

    def _topic_changed(self, current, _previous):
        if current is None:
            return
        key = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, str):
            self._display_topic(key)

    def _display_topic(self, key: str) -> None:
        topic = get_topic(key)
        if topic is None:
            return
        self.current_topic = topic.key
        self.browser.setHtml(topic_html(topic, current_theme(self)))
        self.browser.moveCursor(QTextCursor.MoveOperation.Start)
        self.technical_toggle.setChecked(False)
        self.technical_toggle.setVisible(bool(topic.technical))
        self.technical_details.setPlainText(topic.technical)
        self.technical_details.hide()

    def refresh_theme(self) -> None:
        """Recolor an open document while keeping navigation and detail state."""
        topic = get_topic(self.current_topic) if self.current_topic else None
        if topic is None:
            return
        position = self.browser.verticalScrollBar().value()
        self.browser.setHtml(topic_html(topic, current_theme(self)))
        self.browser.verticalScrollBar().setValue(position)

    def _show_technical(self, checked: bool):
        self.technical_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.technical_details.setVisible(checked)
