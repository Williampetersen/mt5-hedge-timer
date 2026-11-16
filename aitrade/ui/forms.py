"""The two input forms: one MT5 account, and the trade plan (lot, take profit, date, time)."""

from __future__ import annotations

from datetime import date as ddate
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDate, QRegularExpression, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..mt5_setup import find_sources
from ..settings import SIDE_TITLES, Account, Plan, parse_date, parse_time
from ..terminals import TERMINAL_EXE, Terminal
from .widgets import PasswordEdit, button, field, label, row

MANAGED = "<aitrade-copy>"  # the list entry that means "Aitrade's own copy of MetaTrader 5"


class AccountForm(QWidget):
    """Everything MT5 needs to log into one account."""

    def __init__(self, side: str, managed_exe: str = "", own_folder: Optional[Path] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.side = side
        self.managed_exe = managed_exe  # where Aitrade's own copy for this account lives (or will live)
        self.own_folder = own_folder
        title = SIDE_TITLES[side]

        self.broker = QLineEdit()
        self.broker.setPlaceholderText("For example IC Markets")
        self.server = QLineEdit()
        self.server.setPlaceholderText("For example ICMarketsSC-Demo")
        self.login = QLineEdit()
        self.login.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,19}"), self))
        self.login.setPlaceholderText("Digits only")
        self.password = PasswordEdit()
        self.demo = button("Demo", "segment", tip="A practice account with test money")
        self.real = button("Real money", "segment-danger", tip="A live account with real money")
        self._kind = QButtonGroup(self)
        self._kind.setExclusive(True)
        for choice in (self.demo, self.real):
            choice.setCheckable(True)
            self._kind.addButton(choice)
        self.demo.setChecked(True)
        self.symbol = QLineEdit()
        self.symbol.setPlaceholderText("For example XAUUSD")
        self.terminal = QComboBox()
        self.terminal.setMinimumContentsLength(20)
        self.terminal.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.browse = button("Browse…", tip="Pick terminal64.exe yourself")
        self.refresh = button("Search again", tip="Look for MetaTrader 5 on this PC again")
        self.terminal_hint = label("", "small", wrap=True)

        self.browse.clicked.connect(self._browse)
        self.terminal.currentIndexChanged.connect(lambda _i: self._update_hint())
        self.refresh.clicked.connect(lambda: self.reload_terminals(str(self.terminal.currentData() or "")))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(row(field("Broker name", self.broker), field("Server", self.server,
                                                                   "Exactly as in the MT5 login window.")))
        lay.addWidget(row(field("Account number", self.login),
                          field("Password", self.password,
                                "Kept in the Windows password vault.")))
        lay.addWidget(row(field("Account type", row(self.demo, self.real, spacing=8)),
                          field(f"Symbol on the {title} account", self.symbol,
                                "The exact name in Market Watch (some brokers add letters, like XAUUSD.m).")))
        lay.addWidget(field("MetaTrader 5 for this account",
                            row(self.terminal, self.browse, self.refresh, stretch=[1, 0, 0])))
        lay.addWidget(self.terminal_hint)
        self.reload_terminals("")

    # ------------------------------------------------------------ MT5 program list

    def reload_terminals(self, selected: str = "") -> None:
        found = find_sources(exclude_under=self.own_folder)
        self.terminal.blockSignals(True)
        self.terminal.clear()
        if self.managed_exe:
            self.terminal.addItem("Aitrade's own copy of MetaTrader 5 (recommended)", MANAGED)
        else:
            self.terminal.addItem("Choose…", "")
        for t in found:
            self.terminal.addItem(t.label, t.path)
        self.terminal.blockSignals(False)
        if selected and selected != MANAGED:
            self._select_path(selected)
        else:
            self.terminal.setCurrentIndex(0)
        self._update_hint()

    def is_managed(self) -> bool:
        return self.terminal.currentData() == MANAGED

    def _update_hint(self) -> None:
        if self.is_managed():
            self.terminal_hint.setText(
                "Aitrade makes its own copy of MetaTrader 5 for this account in the last step. "
                "Any MetaTrader you already use stays as it is.")
        elif self.terminal_path():
            self.terminal_hint.setText(
                "Aitrade uses this MetaTrader 5 as it is. Log into the account in it once by hand, "
                "and use a different MetaTrader 5 folder for the other account.")
        else:
            self.terminal_hint.setText("Choose the MetaTrader 5 for this account, or press “Browse…” "
                                       "and pick terminal64.exe.")

    def _select_path(self, path: str) -> None:
        idx = self.terminal.findData(path)
        if idx < 0:  # not in the list (custom folder): add it
            import ntpath

            folder = ntpath.dirname(path)
            self.terminal.addItem(Terminal(path, ntpath.basename(folder) or folder).label, path)
            idx = self.terminal.count() - 1
        self.terminal.setCurrentIndex(idx)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose terminal64.exe", self.terminal_path() or "",
                                              f"MetaTrader 5 ({TERMINAL_EXE})")
        if path:
            self._select_path(path.replace("/", "\\"))

    def terminal_path(self) -> str:
        """The terminal64.exe this account uses (for Aitrade's own copy: where that copy lives)."""
        data = str(self.terminal.currentData() or "")
        return self.managed_exe if data == MANAGED else data

    # ------------------------------------------------------------ load / read

    def load(self, account: Account, *, password_saved: bool) -> None:
        self.broker.setText(account.broker)
        self.server.setText(account.server)
        self.login.setText(str(account.login) if account.login > 0 else "")
        self.password.clear()
        self.password.set_saved(password_saved)
        (self.real if account.real else self.demo).setChecked(True)
        self.symbol.setText(account.symbol)
        own = bool(self.managed_exe) and (account.managed or not account.terminal_path)
        self.reload_terminals(MANAGED if own else account.terminal_path)

    def apply(self, account: Account) -> None:
        """Copy what was typed into `account` (the password is read separately)."""
        account.broker = self.broker.text().strip()
        account.server = self.server.text().strip()
        text = self.login.text().strip()
        account.login = int(text) if text.isdigit() else 0
        account.real = self.real.isChecked()
        account.symbol = self.symbol.text().strip()
        account.terminal_path = self.terminal_path()
        account.managed = self.is_managed()
        account.portable = account.managed  # own copies keep all their data in their own folder

    def typed_password(self) -> str:
        return self.password.text()


_NUMBER = QRegularExpression(r"\d{0,9}([.,]\d{0,2})?")


def _number(text: str) -> float:
    """What the user typed as a number; NaN when it is not one (the plan check reports it)."""
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return float("nan")


class PlanForm(QWidget):
    """Lot size, take profit, stop loss, date and time: confirmed before every run."""

    changed = Signal()

    def __init__(self, zone_label: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.lots = QLineEdit()
        self.lots.setValidator(QRegularExpressionValidator(_NUMBER, self))
        self.lots.setPlaceholderText("For example 0.06")
        self.tp = QLineEdit()
        self.tp.setValidator(QRegularExpressionValidator(_NUMBER, self))
        self.tp.setPlaceholderText("For example 150")
        self.sl = QLineEdit()
        self.sl.setValidator(QRegularExpressionValidator(_NUMBER, self))
        self.sl.setPlaceholderText("None")
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.time = QLineEdit()
        self.time.setInputMask("00:00:00")  # always HH:MM:SS
        self.time.setPlaceholderText("HH:MM:SS")

        zone = f" ({zone_label})" if zone_label else ""
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(14)
        grid.addWidget(field("Lot size", self.lots), 0, 0)
        grid.addWidget(field("Take profit (USD)", self.tp), 0, 1)
        grid.addWidget(field("Date", self.date), 1, 0)
        grid.addWidget(field("Time (HH:MM:SS)", self.time), 1, 1)
        grid.addWidget(field("Stop loss (USD, optional)", self.sl), 2, 0)
        grid.addWidget(label(f"Date and time are this PC's own time{zone}.", "small", wrap=True), 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        for edit in (self.lots, self.tp, self.sl, self.time):
            edit.textChanged.connect(self.changed)
        self.date.dateChanged.connect(self.changed)

    def load(self, plan: Plan, *, today: Optional[ddate] = None) -> None:
        today = today or ddate.today()
        self.lots.setText(f"{plan.lots:.2f}" if plan.lots > 0 else "")
        self.tp.setText(f"{plan.tp_usd:.2f}" if plan.tp_usd > 0 else "")
        self.sl.setText("" if plan.sl_usd is None else f"{plan.sl_usd:.2f}")
        day = parse_date(plan.date)
        if day is None or day < today:
            day = today  # an old date is never what the user wants
        self.date.setDate(QDate(day.year, day.month, day.day))
        self.time.setText(plan.time if parse_time(plan.time) else "14:29:45")

    def read(self) -> Plan:
        sl_text = self.sl.text().strip()
        sl = _number(sl_text) if sl_text else None
        if sl is not None and sl == 0:
            sl = None
        return Plan(
            lots=_number(self.lots.text()),
            tp_usd=_number(self.tp.text()),
            sl_usd=sl,
            date=self.date.date().toString("yyyy-MM-dd"),
            time=self.time.text().strip(),
        )

    def set_locked(self, locked: bool) -> None:
        for w in (self.lots, self.tp, self.sl, self.date, self.time):
            w.setEnabled(not locked)
