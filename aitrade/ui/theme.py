"""Colours and the look of the window (dark, calm, large numbers)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0e1318"
SURFACE = "#161d26"
FIELD = "#1d2633"
BORDER = "#2b3646"
TEXT = "#e7edf4"
MUTED = "#8e9bae"
DISABLED = "#566273"
ACCENT = "#3d8bfd"
ACCENT_HOVER = "#5c9eff"
BUY = "#2ecc71"
SELL = "#ff6b6b"
WARN = "#f5a524"
LOG_BG = "#0a0e13"

TONE_COLORS = {"muted": MUTED, "info": ACCENT_HOVER, "good": BUY, "warn": WARN, "bad": SELL}
SIDE_COLORS = {"buy": BUY, "sell": SELL}

STYLESHEET = f"""
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QWidget#page {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#header {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="small"] {{ color: {MUTED}; font-size: 9pt; }}
QLabel[role="field"] {{ color: {MUTED}; font-size: 9pt; font-weight: 600; }}
QLabel[role="brand"] {{ font-size: 15pt; font-weight: 700; }}
QLabel[role="title"] {{ font-size: 16pt; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="clock"] {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 15pt; font-weight: 600; }}
QLabel[role="big"] {{ font-size: 24pt; font-weight: 700; }}
QLabel[role="state"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="error"] {{ color: {SELL}; background: rgba(255, 107, 107, 0.10);
    border: 1px solid rgba(255, 107, 107, 0.45); border-radius: 8px; padding: 10px 12px; }}
QLabel[role="notice"] {{ color: {WARN}; background: rgba(245, 165, 36, 0.10);
    border: 1px solid rgba(245, 165, 36, 0.40); border-radius: 8px; padding: 10px 12px; }}
QLabel[role="step"] {{ color: {MUTED}; padding: 6px 12px; border-radius: 14px; border: 1px solid {BORDER}; }}
QLabel[role="step-current"] {{ color: white; padding: 6px 12px; border-radius: 14px;
    background: {ACCENT}; border: 1px solid {ACCENT}; font-weight: 600; }}
QLabel[role="step-done"] {{ color: {BUY}; padding: 6px 12px; border-radius: 14px;
    border: 1px solid rgba(46, 204, 113, 0.5); }}
QLineEdit {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 7px 9px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ color: {DISABLED}; }}
QAbstractSpinBox {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 8px;
    min-height: 20px; }}
QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
QAbstractSpinBox:disabled {{ color: {DISABLED}; }}
QAbstractSpinBox QLineEdit {{ background: transparent; border: none; padding: 0px; }}
QComboBox {{ min-height: 30px; padding-left: 6px; }}
QPushButton {{ background: {FIELD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 8px 16px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {DISABLED}; border-color: {BORDER}; background: {SURFACE}; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:disabled {{ background: #2a3d5c; border-color: #2a3d5c; color: #8ea5c7; }}
QPushButton[kind="run"] {{ background: {BUY}; border-color: {BUY}; color: #06220f; font-size: 13pt;
    font-weight: 700; padding: 12px 16px; }}
QPushButton[kind="run"]:hover {{ background: #48dd88; border-color: #48dd88; }}
QPushButton[kind="run"]:disabled {{ background: #1f4d33; border-color: #1f4d33; color: #7fa890; }}
QPushButton[kind="stop"] {{ background: {SELL}; border-color: {SELL}; color: #2b0707; font-size: 13pt;
    font-weight: 700; padding: 12px 16px; }}
QPushButton[kind="stop"]:hover {{ background: #ff8585; border-color: #ff8585; }}
QPushButton[kind="segment"] {{ padding: 7px 14px; }}
QPushButton[kind="segment"]:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white;
    font-weight: 600; }}
QPushButton[kind="segment-danger"] {{ padding: 7px 14px; }}
QPushButton[kind="segment-danger"]:checked {{ background: {SELL}; border-color: {SELL}; color: #2b0707;
    font-weight: 600; }}
QPushButton[kind="link"] {{ background: transparent; border: none; color: {ACCENT_HOVER}; padding: 4px 2px; }}
QPushButton[kind="link"]:hover {{ color: white; }}
QRadioButton, QCheckBox {{ background: transparent; spacing: 8px; }}
QPlainTextEdit {{ background: {LOG_BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
    font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt; }}
QScrollArea {{ background: transparent; border: none; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""


def apply(app: QApplication) -> None:
    """Fusion style with a dark palette (so spin boxes, calendars and menus are dark too)."""
    app.setStyle("Fusion")
    pal = QPalette()
    roles = QPalette.ColorRole
    for role, color in (
        (roles.Window, BG), (roles.WindowText, TEXT), (roles.Base, FIELD), (roles.AlternateBase, SURFACE),
        (roles.Text, TEXT), (roles.Button, FIELD), (roles.ButtonText, TEXT), (roles.BrightText, "#ffffff"),
        (roles.Highlight, ACCENT), (roles.HighlightedText, "#ffffff"), (roles.ToolTipBase, SURFACE),
        (roles.ToolTipText, TEXT), (roles.PlaceholderText, DISABLED), (roles.Link, ACCENT_HOVER),
        (roles.Light, BORDER), (roles.Midlight, BORDER), (roles.Mid, BORDER), (roles.Dark, "#0a0d11"),
        (roles.Shadow, "#000000"),
    ):
        pal.setColor(role, QColor(color))
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED))
    app.setPalette(pal)
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)
