"""Application-wide, accessible native widget themes.

The selected mode is owned by the main window's settings. This module only
normalizes and applies it, including to dialogs that are already open.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


PALETTES = {
    "dark": {
        "background": "#0c111b", "surface": "#121b29", "alternate": "#172233",
        "raised": "#1b293c", "border": "#33445d", "text": "#edf2fb",
        "muted": "#afbdd1", "accent": "#8daeff", "accent_hover": "#acc3ff",
        "accent_text": "#0c1834", "accent_soft": "#233859",
        "selection": "#2d4e80", "selection_text": "#ffffff",
        "warning": "#f6d48c", "warning_background": "#332a1b",
        "danger": "#ffb3ba", "success": "#8be0ba", "disabled": "#8794a8",
        "disabled_background": "#1a2331", "handle": "#536681",
    },
    "light": {
        "background": "#f6f8fc", "surface": "#ffffff", "alternate": "#f0f4fa",
        "raised": "#e9eef7", "border": "#b9c5d8", "text": "#17243a",
        "muted": "#4e6078", "accent": "#315ac5", "accent_hover": "#244aa9",
        "accent_text": "#ffffff", "accent_soft": "#e4edff",
        "selection": "#d4e2ff", "selection_text": "#132d63",
        "warning": "#79500d", "warning_background": "#fff3d8",
        "danger": "#a32b39", "success": "#196442", "disabled": "#64748b",
        "disabled_background": "#e7ecf4", "handle": "#9aaac1",
    },
}


def normalize_mode(mode: object = "dark") -> str:
    return mode if isinstance(mode, str) and mode in PALETTES else "dark"


def current_theme(widget=None) -> str:
    """Return the inherited mode without depending on platform color guesses."""
    current = widget
    while current is not None:
        value = current.property("themeMode")
        if value in PALETTES:
            return value
        current = current.parentWidget() if hasattr(current, "parentWidget") else None
    application = QApplication.instance()
    return normalize_mode(application.property("themeMode") if application else None)


def stylesheet(mode: str = "dark") -> str:
    p = PALETTES[normalize_mode(mode)]
    return """
QWidget { background: %(background)s; color: %(text)s; font-size: 13px; }
QMainWindow, QDialog { background: %(background)s; }
QLabel { background: transparent; }
QLabel#brand { color: %(accent)s; font-size: 14px; font-weight: 700; }
QLabel#title { font-size: 23px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: %(muted)s; }
QLabel#section { font-size: 15px; font-weight: 600; }
QLabel#cardValue { color: %(text)s; font-size: 22px; font-weight: 700; }
QLabel#warning { color: %(warning)s; background: %(warning_background)s; padding: 8px; border-radius: 6px; }
QFrame#card, QFrame#actionCard { background: %(surface)s; border: 1px solid %(border)s; border-radius: 12px; }
QFrame#header, QWidget#header { background: %(surface)s; border-bottom: 1px solid %(border)s; }
QFrame#sidebar { background: %(surface)s; border-right: 1px solid %(border)s; }
QPushButton, QToolButton { background: %(raised)s; border: 1px solid %(border)s; border-radius: 7px; padding: 8px 12px; }
QPushButton:hover, QToolButton:hover { background: %(accent_soft)s; border-color: %(accent)s; }
QPushButton:pressed, QToolButton:pressed, QToolButton:checked { background: %(selection)s; color: %(selection_text)s; }
QPushButton:focus, QToolButton:focus { border: 2px solid %(accent)s; }
QPushButton:disabled, QToolButton:disabled { background: %(disabled_background)s; color: %(disabled)s; border-color: %(border)s; }
QPushButton#primary, QToolButton#primary { background: %(accent)s; color: %(accent_text)s; border-color: %(accent)s; font-weight: 700; }
QPushButton#primary:hover, QToolButton#primary:hover { background: %(accent_hover)s; border-color: %(accent_hover)s; }
QPushButton#primary:disabled, QToolButton#primary:disabled { background: %(disabled_background)s; color: %(disabled)s; border-color: %(border)s; }
QPushButton#caution, QToolButton#caution { color: %(warning)s; background: %(warning_background)s; border-color: %(warning)s; }
QPushButton#nav { text-align: left; border: 0; background: transparent; }
QPushButton#nav:hover { background: %(accent_soft)s; }
QLineEdit, QComboBox, QAbstractSpinBox { background: %(surface)s; border: 1px solid %(border)s; border-radius: 7px; padding: 7px; selection-background-color: %(selection)s; selection-color: %(selection_text)s; }
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus { border-color: %(accent)s; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled { color: %(disabled)s; background: %(disabled_background)s; }
QComboBox::drop-down { border: 0; width: 23px; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(selection)s; selection-color: %(selection_text)s; }
QCheckBox, QRadioButton { background: transparent; spacing: 7px; }
QCheckBox:disabled, QRadioButton:disabled { color: %(disabled)s; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox::indicator:unchecked, QTableView::indicator:unchecked, QTreeView::indicator:unchecked { background: %(surface)s; border: 1px solid %(muted)s; border-radius: 3px; }
QCheckBox::indicator:unchecked:hover, QTableView::indicator:unchecked:hover, QTreeView::indicator:unchecked:hover { border-color: %(accent)s; background: %(accent_soft)s; }
QCheckBox::indicator:unchecked:disabled, QTableView::indicator:unchecked:disabled, QTreeView::indicator:unchecked:disabled { border-color: %(border)s; background: %(disabled_background)s; }
QCheckBox:focus, QRadioButton:focus { color: %(accent)s; }
QTableView, QTreeView, QListView, QPlainTextEdit, QTextEdit, QTextBrowser { background: %(surface)s; alternate-background-color: %(alternate)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 8px; selection-background-color: %(selection)s; selection-color: %(selection_text)s; }
QTableView::item, QTreeView::item, QListView::item { padding: 7px; border: 0; }
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected { background: %(selection)s; color: %(selection_text)s; }
QTableView::item:disabled, QTreeView::item:disabled { color: %(disabled)s; }
QHeaderView::section { padding: 9px 7px; background: %(raised)s; color: %(muted)s; border: 0; border-bottom: 1px solid %(border)s; }
QTableCornerButton::section { background: %(raised)s; border: 0; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: %(background)s; color: %(muted)s; padding: 8px 15px; border-bottom: 2px solid %(border)s; }
QTabBar::tab:selected { background: %(surface)s; color: %(accent)s; border-bottom-color: %(accent)s; }
QTabBar::tab:hover { background: %(accent_soft)s; }
QProgressBar { background: %(raised)s; color: %(text)s; border: 0; border-radius: 4px; min-height: 17px; max-height: 17px; text-align: center; font-size: 11px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
QMenuBar { background: %(surface)s; color: %(text)s; border-bottom: 1px solid %(border)s; padding: 3px; }
QMenuBar::item { background: transparent; padding: 7px 12px; border-radius: 5px; }
QMenuBar::item:selected { background: %(accent_soft)s; color: %(text)s; }
QMenuBar::item:disabled { color: %(disabled)s; }
QMenu { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; padding: 5px; }
QMenu::item { padding: 8px 30px 8px 12px; border-radius: 4px; }
QMenu::item:selected { background: %(selection)s; color: %(selection_text)s; }
QMenu::item:disabled { color: %(disabled)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 5px 6px; }
QToolTip { background: %(raised)s; color: %(text)s; border: 1px solid %(border)s; padding: 6px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: %(background)s; border: 0; }
QScrollBar:vertical { background: %(background)s; width: 11px; margin: 0; }
QScrollBar::handle:vertical { background: %(handle)s; min-height: 25px; border-radius: 5px; }
QScrollBar:horizontal { background: %(background)s; height: 11px; margin: 0; }
QScrollBar::handle:horizontal { background: %(handle)s; min-width: 25px; border-radius: 5px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: %(border)s; }
QGroupBox { border: 1px solid %(border)s; border-radius: 8px; margin-top: 12px; padding: 12px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: %(muted)s; }
QStatusBar { background: %(surface)s; color: %(muted)s; }
""" % p


def apply_theme(window, mode: str = "dark") -> str:
    """Apply one mode to the app, existing dialogs, and future native widgets."""
    mode = normalize_mode(mode)
    p = PALETTES[mode]
    application = QApplication.instance()
    if application is None:
        raise RuntimeError("테마를 적용하려면 QApplication이 필요합니다.")
    application.setProperty("themeMode", mode)
    if window is not None:
        window.setProperty("themeMode", mode)
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "background", QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "surface", QPalette.ColorRole.AlternateBase: "alternate",
        QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "raised",
        QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.Highlight: "selection",
        QPalette.ColorRole.HighlightedText: "selection_text", QPalette.ColorRole.ToolTipBase: "raised",
        QPalette.ColorRole.ToolTipText: "text", QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.LinkVisited: "accent", QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.Light: "surface", QPalette.ColorRole.Mid: "border",
        QPalette.ColorRole.Dark: "border", QPalette.ColorRole.Shadow: "background",
    }
    for role, key in roles.items():
        palette.setColor(role, QColor(p[key]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(p["disabled"]))
    application.setPalette(palette)
    application.setStyleSheet(stylesheet(mode))
    # An older root stylesheet would override application colors on descendants.
    if window is not None:
        window.setStyleSheet("")
    for widget in application.allWidgets():
        refresh = getattr(widget, "refresh_theme", None)
        if callable(refresh):
            refresh()
        widget.update()
    return mode
