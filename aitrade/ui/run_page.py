"""The everyday screen: confirm lot size, take profit, date and time, then press Run."""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget

from .. import describe, paths
from ..mt5_setup import missing_copy_problems
from ..runner import CHECK_OK, MODE_CHECK, MODE_PRACTICE, MODE_RUN, SideState, short_log
from ..settings import SIDE_TITLES, SIDES, Settings, save, target_datetime, validate_accounts, validate_plan
from ..vault import Vault
from . import theme
from .forms import PlanForm
from .launch import LaunchError, collect_passwords, prepare
from .session import PairSession
from .side_card import SideCard
from .widgets import Card, MessageBox, button, label, row, set_kind

PRECHECK_MIN_LEAD_S = 120  # with more time than this left, Run tests both accounts first
PRACTICE_LEAD_S = 60
MIN_LEAD_S = 5
LOG_COLORS = {"WARNING": theme.WARN, "ERROR": theme.SELL, "CRITICAL": theme.SELL}

IDLE, PRECHECK, CHECK, RUN, PRACTICE = "idle", "precheck", "check", "run", "practice"


class RunPage(QWidget):
    edit_settings = Signal()

    def __init__(self, settings: Settings, vault: Vault, *, tz, zone_label: str = "",
                 clock_offset: Callable[[], float] = lambda: 0.0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.settings = settings
        self.vault = vault
        self.tz = tz
        self._clock_offset = clock_offset
        self.session: Optional[PairSession] = None
        self.phase = IDLE
        self._shown_mode = MODE_RUN
        self._pending: Optional[tuple[datetime, Mapping[str, Path], dict[str, str]]] = None

        # ---- left: the plan and the buttons
        self.plan_form = PlanForm(zone_label)
        self.plan_form.load(settings.plan)
        plan_card = Card("Confirm the trade")
        plan_card.body.addWidget(self.plan_form)

        self.real_notice = MessageBox("notice")
        self.errors = MessageBox("error")
        self.run_btn = button("Run", "run")
        self.run_btn.setMinimumHeight(52)
        self.check_btn = button("Check accounts", tip="Open MetaTrader 5, log in and test everything. "
                                                      "Sends nothing.")
        self.practice_btn = button("Practice run", tip="A full run 60 seconds from now. The broker checks "
                                                       "the order, but no position is opened.")
        self.settings_btn = button("Change accounts", "link")
        self.folder_btn = button("Open the log folder", "link")
        self.run_btn.clicked.connect(self._run_clicked)
        self.check_btn.clicked.connect(lambda: self._start(MODE_CHECK))
        self.practice_btn.clicked.connect(lambda: self._start(MODE_PRACTICE))
        self.settings_btn.clicked.connect(self.edit_settings)
        self.folder_btn.clicked.connect(self._open_folder)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(plan_card)
        left.addWidget(self.real_notice)
        left.addWidget(self.run_btn)
        left.addWidget(row(self.check_btn, self.practice_btn))
        left.addWidget(self.errors)
        left.addStretch(1)
        left.addWidget(row(self.settings_btn, self.folder_btn, stretch=[0, 0]),
                       alignment=Qt.AlignmentFlag.AlignLeft)
        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(400)

        # ---- right: status, the two sides, the log
        self.headline = label("Ready", "big")
        self.sub = label("", "muted", wrap=True)
        status = Card()
        status.body.setSpacing(4)
        status.body.addWidget(self.headline)
        status.body.addWidget(self.sub)
        self.cards = {side: SideCard(side) for side in SIDES}
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setPlaceholderText("What the two accounts are doing shows up here.")
        self.log.setMinimumHeight(120)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(status)
        right.addWidget(row(self.cards["buy"], self.cards["sell"]), 3)
        right.addWidget(self.log, 2)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(16)
        lay.addWidget(left_box)
        lay.addLayout(right, 1)

        self.plan_form.changed.connect(self._refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self.reload_accounts()
        self._refresh()

    # ------------------------------------------------------------ helpers

    def now(self) -> datetime:
        """The true time (PC clock corrected with internet time), in the PC's time zone."""
        return datetime.fromtimestamp(time.time() + self._clock_offset(), self.tz)

    @property
    def active(self) -> bool:
        return self.session is not None and self.session.active

    def reload_accounts(self) -> None:
        for side in SIDES:
            self.cards[side].set_account(self.settings.account(side))
        real = [SIDE_TITLES[s] for s in SIDES if self.settings.account(s).real]
        if real:
            self.real_notice.show_lines([f"Real money: the {' and '.join(real)} account"
                                         f"{'s are' if len(real) > 1 else ' is'} not a demo account."])
        else:
            self.real_notice.clear_lines()

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))

    def _append_log(self, side: str, level: str, text: str) -> None:
        stamp, message = short_log(text)
        if not message.strip("= "):
            return  # the engine's ===== separator lines
        color = LOG_COLORS.get(level, theme.TEXT)
        side_color = theme.SIDE_COLORS.get(side, theme.MUTED)
        tag = SIDE_TITLES.get(side, side)
        self.log.appendHtml(
            f'<span style="color:{theme.MUTED}">{html.escape(stamp) or "&nbsp;" * 12}</span>&nbsp;'
            f'<span style="color:{side_color}">{tag}</span>{"&nbsp;" * (5 - len(tag))}'
            f'<span style="color:{color}">{html.escape(message).replace("  ", "&nbsp; ")}</span>'
        )

    def _note(self, text: str) -> None:
        self.log.appendHtml(f'<span style="color:{theme.ACCENT_HOVER}">— {html.escape(text)} —</span>')

    # ------------------------------------------------------------ starting

    def _run_clicked(self) -> None:
        if self.active:
            self._stop_clicked()
        else:
            self._start(MODE_RUN)

    def _start(self, mode: str) -> None:
        if self.active:
            return
        self.errors.clear_lines()
        plan = self.plan_form.read()
        problems = [p.message for p in validate_plan(plan) + validate_accounts(self.settings)]
        problems += missing_copy_problems(self.settings)
        if problems:
            self.errors.show_lines(problems)
            return
        self.settings.plan = plan
        try:
            save(self.settings, paths.settings_file())
        except OSError as exc:
            self.errors.show_lines([f"Could not save the settings: {exc}"])
            return

        target: Optional[datetime] = None
        lead = 0.0
        if mode == MODE_RUN:
            target = target_datetime(plan, self.tz)
            lead = (target - self.now()).total_seconds()
            if lead < MIN_LEAD_S:
                self.errors.show_lines([f"{plan.date} {plan.time} has already passed. Choose a later time."])
                return
        elif mode == MODE_PRACTICE:
            target = self.now() + timedelta(seconds=PRACTICE_LEAD_S)

        try:
            passwords = collect_passwords(self.settings, self.vault)
            config_paths = prepare(self.settings, passwords)
        except LaunchError as exc:
            self.errors.show_lines(exc.lines)
            return

        if mode == MODE_RUN and not self._confirm_real(plan, target):
            return

        self.log.clear()
        if mode == MODE_RUN and lead > PRECHECK_MIN_LEAD_S:
            self._pending = (target, config_paths, passwords)
            self._note("Testing both accounts before waiting")
            self._begin(MODE_CHECK, PRECHECK, config_paths, passwords, None)
        else:
            self._begin(mode, {MODE_RUN: RUN, MODE_PRACTICE: PRACTICE, MODE_CHECK: CHECK}[mode],
                        config_paths, passwords, target)

    def _confirm_real(self, plan, target: datetime) -> bool:
        real = [s for s in SIDES if self.settings.account(s).real]
        if not real:
            return True
        lines = [f"{SIDE_TITLES[s]} {plan.lots:g} {self.settings.account(s).symbol} on account "
                 f"{self.settings.account(s).login}" + (" (REAL MONEY)" if s in real else " (demo)")
                 for s in SIDES]
        answer = QMessageBox.warning(
            self, "Real money",
            "This will open real-money positions:\n\n" + "\n".join(lines)
            + f"\n\nat {target:%Y-%m-%d %H:%M:%S}, take profit {plan.tp_usd:,.2f} USD.\n\nStart?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _begin(self, mode: str, phase: str, config_paths: Mapping[str, Path], passwords: Mapping[str, str],
               target: Optional[datetime]) -> None:
        self.phase = phase
        self._shown_mode = mode
        session = PairSession(mode, config_paths, passwords, target=target, parent=self)
        session.changed.connect(self._refresh)
        session.line.connect(self._append_log)
        session.finished.connect(self._session_finished)
        self.session = session
        session.start()
        self._refresh()

    # ------------------------------------------------------------ stopping / finishing

    def _stop_clicked(self) -> None:
        if not self.active:
            return
        if self.phase in (RUN, PRACTICE):
            answer = QMessageBox.question(
                self, "Stop?",
                "Stop now?\n\nNothing more will be opened. Positions that are already open stay open "
                "in MetaTrader 5 with their take profit.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.stop()

    def stop(self) -> None:
        self._pending = None
        if self.session is not None:
            self.session.stop()

    def _session_finished(self) -> None:
        session, pending = self.session, self._pending
        self._pending = None
        if self.phase == PRECHECK and session is not None and pending is not None:
            states = session.states
            skip = [f"{SIDE_TITLES[s]}: {states[s].snapshot.get('skip_reason')}" for s in SIDES
                    if states[s].snapshot.get("skip_reason")]
            if all(states[s].state == CHECK_OK for s in SIDES) and not skip:
                target, config_paths, passwords = pending
                if (target - self.now()).total_seconds() < MIN_LEAD_S:
                    self.errors.show_lines(["The test took too long and the time has passed. "
                                            "Choose a later time and press Run again."])
                else:
                    self._note("Both accounts are ready. Waiting for the time")
                    self._begin(MODE_RUN, RUN, config_paths, passwords, target)
                    return
            elif skip:
                self.errors.show_lines(["The run would be skipped, so it was not started:"] + skip
                                       + ["Close that position in MetaTrader 5 first, then press Run again."])
            else:
                self.errors.show_lines(["The run was not started because the test failed. "
                                        "Fix the red text on the right, then press Run again."])
        self.phase = IDLE
        self._refresh()

    # ------------------------------------------------------------ drawing

    def _idle_text(self) -> tuple[str, str, str]:
        plan = self.plan_form.read()
        if validate_plan(plan):
            return "Ready", "Fill in the values on the left.", describe.MUTED
        target = target_datetime(plan, self.tz)
        left = (target - self.now()).total_seconds()
        if left < MIN_LEAD_S:
            return "Ready", f"{plan.date} {plan.time} has already passed. Choose a later time.", describe.WARN
        return ("Ready", f"Run opens both positions at {plan.date} {plan.time} – "
                         f"in {describe.countdown(left)}.", describe.MUTED)

    def _refresh(self) -> None:
        active = self.active
        states = self.session.states if self.session else {s: SideState(s) for s in SIDES}
        if self.session is None:
            head, sub, tone = self._idle_text()
        else:
            left = None
            if self.session.target is not None:
                left = (self.session.target - self.now()).total_seconds()
            head, sub, tone = describe.overall(self._shown_mode, states["buy"], states["sell"],
                                               seconds_left=left, active=active)
            if self.phase == PRECHECK and active:
                sub = "Testing both accounts first. Then the app waits for the time."
            elif self.phase in (RUN, PRACTICE) and active and left is not None and left > 0 and not sub:
                sub = f"at {self.session.target:%Y-%m-%d %H:%M:%S} on this PC's clock. Keep the PC on."
            elif not active and head == "Ready":
                head, sub, tone = self._idle_text()
        self.headline.setText(head)
        plain = tone == describe.MUTED or head == "Ready"
        self.headline.setStyleSheet(f"color: {theme.TEXT if plain else theme.TONE_COLORS[tone]};")
        self.sub.setText(sub)
        self.sub.setStyleSheet(f"color: {theme.WARN};" if tone == describe.WARN and head == "Ready" else "")
        for side in SIDES:
            self.cards[side].render(states[side])
        self.run_btn.setText("Stop" if active else "Run")
        set_kind(self.run_btn, "stop" if active else "run")
        for w in (self.check_btn, self.practice_btn, self.settings_btn):
            w.setEnabled(not active)
        self.plan_form.set_locked(active)
