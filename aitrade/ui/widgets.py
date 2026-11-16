"""Small building blocks shared by the wizard and the run screen."""

from __future__ import annotations

import html
from typing import Iterable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def set_role(widget: QWidget, role: str) -> None:
    """Pick the look from the stylesheet (and repaint if it changed)."""
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text: str = "", role: Optional[str] = None, *, wrap: bool = False, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def button(text: str, kind: Optional[str] = None, *, tip: str = "") -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setProperty("kind", kind)
    if tip:
        btn.setToolTip(tip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def set_kind(btn: QPushButton, kind: str) -> None:
    if btn.property("kind") == kind:
        return
    btn.setProperty("kind", kind)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


class Card(QFrame):
    """A rounded panel with its own vertical layout (`.body`)."""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            self.body.addWidget(label(title, "h2"))


def field(title: str, widget: QWidget, hint: str = "") -> QWidget:
    """A caption above an input, with an optional explanation under it."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    caption = label(title, "field")
    caption.setBuddy(widget)
    lay.addWidget(caption)
    lay.addWidget(widget)
    if hint:
        lay.addWidget(label(hint, "small", wrap=True))
    lay.addStretch(1)  # keep the caption at the top when the neighbour is taller
    return box


def row(*widgets: QWidget, spacing: int = 12, stretch: Optional[Iterable[int]] = None) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    stretches = list(stretch) if stretch is not None else [1] * len(widgets)
    for w, s in zip(widgets, stretches):
        lay.addWidget(w, s)
    return box


class MessageBox(QLabel):
    """A coloured box for problems ('error') or warnings ('notice'). Hidden while empty."""

    def __init__(self, role: str = "error", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setProperty("role", role)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hide()

    def show_lines(self, lines: Iterable[str]) -> None:
        items = [html.escape(str(x)) for x in lines if str(x).strip()]
        if not items:
            self.clear_lines()
            return
        self.setText("<br>".join(items) if len(items) == 1 else "<br>".join("• " + i for i in items))
        self.show()

    def clear_lines(self) -> None:
        self.setText("")
        self.hide()


class PasswordEdit(QWidget):
    """Password input with a Show/Hide button."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.toggle = button("Show")
        self.toggle.setCheckable(True)
        self.toggle.setFixedWidth(70)
        self.toggle.toggled.connect(self._toggled)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.toggle)
        self.setFocusProxy(self.edit)

    def _toggled(self, on: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.toggle.setText("Hide" if on else "Show")

    def text(self) -> str:
        return self.edit.text()

    def clear(self) -> None:
        self.edit.clear()
        self.toggle.setChecked(False)

    def set_saved(self, saved: bool) -> None:
        self.edit.setPlaceholderText("Saved – leave empty to keep it" if saved else "")


class Pill(QLabel):
    """Small coloured tag, e.g. BUY / SELL."""

    def __init__(self, text: str, color: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 9px; padding: 2px 10px;"
            "font-weight: 700; font-size: 9pt;"
        )
