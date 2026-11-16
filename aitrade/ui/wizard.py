"""First-time setup (and later: changing the accounts). Four steps, one thing per step."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from hedgebot import terminal

from .. import describe, mt5_setup, paths
from ..runner import MODE_CHECK
from ..settings import (
    SIDE_TITLES,
    SIDES,
    Problem,
    Settings,
    save,
    validate_account,
    validate_accounts,
    validate_plan,
)
from ..vault import Vault, VaultError
from . import theme
from .forms import AccountForm, PlanForm
from .launch import LaunchError, collect_passwords, prepare
from .session import PairSession
from .setup_job import SetupJob
from .side_card import SideCard
from .widgets import Card, MessageBox, button, label, row, set_role

STEPS = ("1  BUY account", "2  SELL account", "3  Trade", "4  Install and check")
STEP_BUY, STEP_SELL, STEP_PLAN, STEP_CHECK = range(4)
WIDTH = 820


class SetupWizard(QWidget):
    finished = Signal(object)  # the saved Settings
    cancelled = Signal()

    def __init__(self, settings: Settings, vault: Vault, *, zone_label: str = "", can_cancel: bool = False,
                 mt5_dir: Optional[Path] = None, setup_runner: Optional[Callable] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._original = settings
        self.settings = copy.deepcopy(settings)
        self.vault = vault
        self.mt5_dir = Path(mt5_dir) if mt5_dir is not None else mt5_setup.resolve_mt5_dir(settings)
        self._setup_runner = setup_runner  # tests put a pretend setup here
        self._typed = {side: "" for side in SIDES}
        self.session: Optional[PairSession] = None
        self.job: Optional[SetupJob] = None

        # ---- step bar
        self._step_labels = [label(text, "step") for text in STEPS]
        steps = QHBoxLayout()
        steps.setSpacing(8)
        steps.addStretch(1)
        for lab in self._step_labels:
            steps.addWidget(lab)
        steps.addStretch(1)

        # ---- pages
        self.forms = {side: AccountForm(side, str(mt5_setup.copy_exe(self.mt5_dir, side)), self.mt5_dir)
                      for side in SIDES}
        self.plan_form = PlanForm(zone_label)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._account_page(
            "buy", "The account that will BUY",
            "Type the same details you use to log into MetaTrader 5 with this account."))
        self.pages.addWidget(self._account_page(
            "sell", "The account that will SELL",
            "This must be a different account. It opens the opposite position at the same moment."))
        self.pages.addWidget(self._plan_page())
        self.pages.addWidget(self._check_page())

        self.errors = MessageBox("error")
        self.cancel_btn = button("Cancel")
        self.cancel_btn.setVisible(can_cancel)
        self.back_btn = button("Back")
        self.next_btn = button("Next", "primary")
        self.next_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self._cancel)
        self.back_btn.clicked.connect(self._back)
        self.next_btn.clicked.connect(self._next)
        footer = QHBoxLayout()
        footer.addWidget(self.cancel_btn)
        footer.addStretch(1)
        footer.addWidget(self.back_btn)
        footer.addWidget(self.next_btn)

        column = QWidget()
        column.setMaximumWidth(WIDTH)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        col.addLayout(steps)
        col.addWidget(self.pages)
        col.addWidget(self.errors)
        col.addStretch(1)

        inner = QWidget()
        inner.setObjectName("page")
        center = QHBoxLayout(inner)
        center.setContentsMargins(24, 20, 24, 8)
        center.addStretch(1)
        center.addWidget(column, 100)
        center.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        # Back / Next stay on screen, whatever the page length.
        bar = QWidget()
        bar.setMaximumWidth(WIDTH)
        bar.setLayout(footer)
        footer.setContentsMargins(0, 0, 0, 0)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(24, 8, 24, 16)
        bottom.addStretch(1)
        bottom.addWidget(bar, 100)
        bottom.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll, 1)
        outer.addLayout(bottom)

        for side in SIDES:
            self.forms[side].load(self.settings.account(side), password_saved=self._saved(side))
        self.plan_form.load(self.settings.plan)
        self._show_step(STEP_BUY)

    # ------------------------------------------------------------ pages

    def _account_page(self, side: str, title: str, text: str) -> QWidget:
        card = Card()
        card.body.setSpacing(14)
        card.body.addWidget(label(title, "title"))
        card.body.addWidget(label(text, "muted", wrap=True))
        card.body.addWidget(self.forms[side])
        card.body.addStretch(1)
        return card

    def _plan_page(self) -> QWidget:
        card = Card()
        card.body.setSpacing(14)
        card.body.addWidget(label("The trade", "title"))
        card.body.addWidget(label(
            "Both accounts use the same lot size, take profit and time. "
            "You confirm or change these values every time before you press Run.", "muted", wrap=True))
        card.body.addWidget(self.plan_form)
        card.body.addWidget(label(
            "Take profit is in US dollars: the position closes when it has earned that amount.",
            "small", wrap=True))
        card.body.addStretch(1)
        return card

    def _check_page(self) -> QWidget:
        card = Card()
        card.body.setSpacing(14)
        card.body.addWidget(label("Install and check", "title"))
        card.body.addWidget(label(
            "Aitrade gives each account its own MetaTrader 5, opens both, logs in, and tests the symbol, "
            "lot size, take profit and margin. It does not open any position.", "muted", wrap=True))
        self.check_btn = button("Check both accounts now", "primary")
        self.check_btn.clicked.connect(self._toggle_check)
        self.check_headline = label("", "h2", wrap=True)
        self.check_sub = label("", "muted", wrap=True)
        card.body.addWidget(row(self.check_btn, self.check_headline, stretch=[0, 1]))
        self.setup_bar = QProgressBar()
        self.setup_bar.setTextVisible(False)
        self.setup_bar.setFixedHeight(8)
        self.setup_bar.hide()
        card.body.addWidget(self.setup_bar)
        card.body.addWidget(self.check_sub)
        self.cards = {side: SideCard(side) for side in SIDES}
        card.body.addWidget(row(self.cards["buy"], self.cards["sell"]))
        self.open_btns = {side: button(f"Open the {SIDE_TITLES[side]} MetaTrader 5", "link",
                                       tip="Opens this account's MetaTrader 5 so you can look at it")
                          for side in SIDES}
        for side in SIDES:
            self.open_btns[side].clicked.connect(lambda _checked=False, s=side: self._open_terminal(s))
        card.body.addWidget(row(self.open_btns["buy"], self.open_btns["sell"]))
        card.body.addWidget(label(
            "The first start of a new MetaTrader 5 can take a few minutes. "
            "You can also finish now and check later from the main screen.", "small", wrap=True))
        return card

    # ------------------------------------------------------------ navigation

    def _show_step(self, step: int) -> None:
        self.pages.setCurrentIndex(step)
        for i, lab in enumerate(self._step_labels):
            set_role(lab, "step-current" if i == step else "step-done" if i < step else "step")
        self.back_btn.setEnabled(step > 0)
        self.next_btn.setText("Finish" if step == STEP_CHECK else "Next")
        self.errors.clear_lines()
        if step == STEP_CHECK:
            for side in SIDES:
                self.cards[side].set_account(self.settings.account(side))
            self._update_check_controls()

    def _saved(self, side: str) -> bool:
        try:
            return self.vault.has(side, self.settings.account(side).login)
        except VaultError:
            return False

    def _read_account(self, side: str) -> list[Problem]:
        account = self.settings.account(side)
        self.forms[side].apply(account)
        typed = self.forms[side].typed_password()
        if typed:
            self._typed[side] = typed
        problems = validate_account(account, side)
        if not self._typed[side] and not self._saved(side):
            problems.append(Problem(f"{side}.password", f"{SIDE_TITLES[side]} account: type the password."))
        return problems

    def _next(self) -> None:
        step = self.pages.currentIndex()
        problems: list[Problem] = []
        if step == STEP_BUY:
            problems = self._read_account("buy")
        elif step == STEP_SELL:
            problems = self._read_account("sell")
            if not problems:
                problems = validate_accounts(self.settings)
        elif step == STEP_PLAN:
            self.settings.plan = self.plan_form.read()
            problems = validate_plan(self.settings.plan)
        else:
            self._finish()
            return
        if problems:
            self.errors.show_lines(p.message for p in problems)
            return
        self.errors.clear_lines()
        self._show_step(step + 1)

    def _back(self) -> None:
        self.stop_work()
        self._show_step(max(0, self.pages.currentIndex() - 1))

    def _cancel(self) -> None:
        self.stop_work()
        self.cancelled.emit()

    def stop_work(self) -> None:
        """Stop a running setup or check (leaving the page, closing the window)."""
        if self.job is not None and self.job.running:
            self.job.cancel()
        self._stop_check()

    @property
    def busy(self) -> bool:
        return (self.job is not None and self.job.running) or (self.session is not None and self.session.active)

    # ------------------------------------------------------------ check

    def _toggle_check(self) -> None:
        if self.busy:
            self.stop_work()
            return
        self.errors.clear_lines()
        if mt5_setup.missing_copies(self.settings):
            self._start_setup()
        else:
            self._start_check()

    def _start_setup(self) -> None:
        self.settings.mt5_dir = str(self.mt5_dir)
        self.job = SetupJob(self.settings, self._setup_runner, parent=self)
        self.job.progress.connect(self._setup_progress)
        self.job.done.connect(self._setup_done)
        self.setup_bar.setRange(0, 0)
        self.setup_bar.show()
        self.check_headline.setText("Setting up MetaTrader 5…")
        self.check_headline.setStyleSheet(f"color: {theme.TONE_COLORS[describe.INFO]};")
        self.check_sub.setText("This is done once. It can take a few minutes.")
        self.job.start()
        self._update_check_controls()

    def _setup_progress(self, text: str, fraction: float) -> None:
        if fraction < 0:
            self.setup_bar.setRange(0, 0)  # moving stripe: how long is not known
        else:
            self.setup_bar.setRange(0, 1000)
            self.setup_bar.setValue(int(fraction * 1000))
        self.check_sub.setText(text + (f" – {fraction * 100:.0f} %" if 0 <= fraction < 1 else ""))

    def _setup_done(self, ok: bool, message: str) -> None:
        self.setup_bar.hide()
        self._update_check_controls()
        if not ok:
            self.check_headline.setText("MetaTrader 5 is not set up yet")
            self.check_headline.setStyleSheet(f"color: {theme.TONE_COLORS[describe.BAD]};")
            self.check_sub.setText("")
            self.errors.show_lines([message])
            return
        self.check_sub.setText("MetaTrader 5 is ready for both accounts.")
        self._start_check()

    def _open_terminal(self, side: str) -> None:
        account = self.settings.account(side)
        try:
            if not terminal.is_running(account.terminal_path):
                terminal.start(account.terminal_path, portable=account.portable)
        except OSError as exc:
            self.errors.show_lines([f"Could not open MetaTrader 5: {exc}"])

    def _update_check_controls(self) -> None:
        setting_up = self.job is not None and self.job.running
        checking = self.session is not None and self.session.active
        if setting_up:
            text = "Stop"
        elif checking:
            text = "Stop the check"
        elif mt5_setup.missing_copies(self.settings):
            text = "Install MetaTrader 5 and check both accounts"
        elif self.session is not None:
            text = "Check both accounts again"
        else:
            text = "Check both accounts now"
        self.check_btn.setText(text)
        for side in SIDES:
            path = self.settings.account(side).terminal_path
            self.open_btns[side].setVisible(bool(path) and Path(path).is_file())

    def _start_check(self) -> None:
        try:
            passwords = collect_passwords(self.settings, self.vault, self._typed)
            config_paths = prepare(self.settings, passwords)
        except LaunchError as exc:
            self.errors.show_lines(exc.lines)
            return
        self.session = PairSession(MODE_CHECK, config_paths, passwords, parent=self)
        self.session.changed.connect(self._render_check)
        self.session.finished.connect(self._render_check)
        self.session.start()
        self._render_check()
        self._update_check_controls()

    def _stop_check(self) -> None:
        if self.session is not None and self.session.active:
            self.session.stop()

    def _render_check(self) -> None:
        if self.session is None:
            return
        states = self.session.states
        for side in SIDES:
            self.cards[side].render(states[side])
        head, sub, tone = describe.overall(MODE_CHECK, states["buy"], states["sell"], active=self.session.active)
        self.check_headline.setText(head)
        self.check_headline.setStyleSheet(f"color: {theme.TONE_COLORS[tone]};")
        self.check_sub.setText(sub)
        self._update_check_controls()

    # ------------------------------------------------------------ finish

    def _finish(self) -> None:
        self.stop_work()
        self.settings.mt5_dir = str(self.mt5_dir)
        problems = validate_accounts(self.settings) + validate_plan(self.settings.plan)
        if problems:
            self.errors.show_lines(p.message for p in problems)
            return
        try:
            for side in SIDES:
                new_login = self.settings.account(side).login
                old_login = self._original.account(side).login
                if self._typed[side]:
                    self.vault.set(side, new_login, self._typed[side])
                if old_login > 0 and old_login != new_login:
                    self.vault.delete(side, old_login)
        except VaultError as exc:
            self.errors.show_lines([str(exc)])
            return
        self.settings.setup_done = True
        try:
            save(self.settings, paths.settings_file())
        except OSError as exc:
            self.errors.show_lines([f"Could not save the settings: {exc}"])
            return
        self._typed = {side: "" for side in SIDES}
        self.finished.emit(self.settings)

    def keyPressEvent(self, event) -> None:  # Enter = Next
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._next()
        else:
            super().keyPressEvent(event)
