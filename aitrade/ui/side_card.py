"""The BUY and SELL panels: who the account is and what that side is doing right now."""

from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QWidget

from .. import describe
from ..runner import SideState
from ..settings import SIDE_TITLES, Account
from . import theme
from .widgets import Card, Pill, label


def account_caption(account: Account) -> str:
    parts = [account.broker or account.server or "No account yet"]
    if account.login > 0:
        parts.append(str(account.login))
    if account.server or account.login > 0:
        parts.append("Real money" if account.real else "Demo")
    return " · ".join(parts)


class SideCard(Card):
    def __init__(self, side: str, parent: Optional[QWidget] = None):
        super().__init__("", parent)
        self.side = side
        self.pill = Pill(SIDE_TITLES[side], theme.SIDE_COLORS[side])
        self.caption = label("", "muted")
        self.caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.pill)
        head.addWidget(self.caption, 1)
        self.body.addLayout(head)
        self.state = label("Not started", "state")
        self.details = label("", wrap=True, selectable=True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.notes = label("", wrap=True, selectable=True)
        self.notes.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(self.state)
        self.body.addWidget(self.details)
        self.body.addWidget(self.notes)
        self.body.addStretch(1)
        self.set_account(Account())
        self.render(SideState(side))

    def set_account(self, account: Account) -> None:
        self.caption.setText(account_caption(account))
        self.caption.setStyleSheet(f"color: {theme.SELL if account.real else theme.MUTED};")

    def render(self, st: SideState) -> None:
        text, tone = describe.headline(st)
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {theme.TONE_COLORS[tone]};")
        lines = describe.detail_lines(st)
        problem_color = theme.SELL if tone == describe.BAD else theme.WARN if st.is_problem else None
        if problem_color:
            body = "<br>".join(html.escape(x) for x in lines)
            self.details.setText(f'<span style="color:{problem_color}">{body}</span>')
        else:
            self.details.setText("<br>".join(html.escape(x) for x in lines))
        self.details.setVisible(bool(lines))
        notes = [n for n in st.notes if n not in lines and n != st.message]
        warning = st.snapshot.get("margin_warning") if st.snapshot else None
        if warning:
            notes.insert(0, str(warning))
        self.notes.setText("<br>".join(
            f'<span style="color:{theme.WARN}">⚠ {html.escape(n)}</span>' for n in notes[-4:]))
        self.notes.setVisible(bool(notes))
