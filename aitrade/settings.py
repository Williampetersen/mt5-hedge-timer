"""What the user typed in the app: two accounts, the trade plan and a few advanced values.

Saved as JSON in the data folder. Passwords are NOT part of this file; they go
to the Windows password vault (see vault.py).
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

SIDES = ("buy", "sell")
SIDE_TITLES = {"buy": "BUY", "sell": "SELL"}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d):([0-5]\d)$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._#+\-!$&]{1,31}$")


@dataclass
class Account:
    broker: str = ""  # only a label, e.g. "IC Markets"
    server: str = ""  # exactly as in the MT5 login window, e.g. "ICMarketsSC-Demo"
    login: int = 0  # the MT5 account number
    real: bool = False  # False = demo account (the engine refuses real money unless this is True)
    terminal_path: str = ""  # ...\terminal64.exe of the MT5 copy used for this account
    portable: bool = False
    managed: bool = False  # True: Aitrade's own copy of MetaTrader 5 (made by the setup, run in portable mode)
    symbol: str = ""  # the market's exact name at this broker, e.g. XAUUSD or GOLD


@dataclass
class Plan:
    """The values the user confirms before every run."""

    lots: float = 0.01
    tp_usd: float = 100.0
    sl_usd: Optional[float] = None  # None = no stop loss
    date: str = ""  # YYYY-MM-DD, in the PC's own time zone
    time: str = "14:29:45"  # HH:MM:SS, in the PC's own time zone


@dataclass
class Advanced:
    deviation_points: int = 300  # largest accepted slippage
    max_spread_points: Optional[float] = None  # None = open at any spread
    prearm_seconds: int = 30  # connect to MT5 this long before the time
    max_late_ms: int = 2000  # if the PC wakes up later than this, do not open
    use_ntp: bool = True  # correct the PC clock with internet time
    ntp_server: str = "pool.ntp.org"
    monitor_until: str = "23:00:00"  # stop watching the open position at this PC time
    magic: int = 20261115
    comment: str = "Aitrade"


@dataclass
class Settings:
    buy: Account = field(default_factory=Account)
    sell: Account = field(default_factory=Account)
    plan: Plan = field(default_factory=Plan)
    advanced: Advanced = field(default_factory=Advanced)
    setup_done: bool = False
    mt5_dir: str = ""  # folder that holds Aitrade's own MetaTrader 5 copies ("" = chosen automatically)

    def account(self, side: str) -> Account:
        if side not in SIDES:
            raise ValueError(f"side must be buy or sell, not {side!r}")
        return self.buy if side == "buy" else self.sell


@dataclass(frozen=True)
class Problem:
    """One thing the user has to fix. `field` names the input, e.g. 'buy.login' or 'plan.time'."""

    field: str
    message: str

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------- load / save


def _from_dict(cls, raw: Any):
    """Build a dataclass from loaded JSON, ignoring unknown keys and wrong types."""
    obj = cls()
    if not isinstance(raw, dict):
        return obj
    for f in fields(cls):
        if f.name not in raw:
            continue
        value, current = raw[f.name], getattr(obj, f.name)
        if isinstance(current, bool):
            if isinstance(value, bool):
                setattr(obj, f.name, value)
        elif isinstance(current, int):
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(obj, f.name, value)
        elif isinstance(current, float):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                setattr(obj, f.name, float(value))
        elif isinstance(current, str):
            if isinstance(value, str):
                setattr(obj, f.name, value)
        else:  # Optional[float]
            if value is None:
                setattr(obj, f.name, None)
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                setattr(obj, f.name, float(value))
    return obj


def from_dict(raw: Any) -> Settings:
    raw = raw if isinstance(raw, dict) else {}
    return Settings(
        buy=_from_dict(Account, raw.get("buy")),
        sell=_from_dict(Account, raw.get("sell")),
        plan=_from_dict(Plan, raw.get("plan")),
        advanced=_from_dict(Advanced, raw.get("advanced")),
        setup_done=raw.get("setup_done") is True,
        mt5_dir=raw.get("mt5_dir") if isinstance(raw.get("mt5_dir"), str) else "",
    )


def to_dict(settings: Settings) -> dict:
    return {"version": 1, **asdict(settings)}


def load(path: Path) -> Settings:
    """Read the settings file. A missing or damaged file gives fresh settings (the damaged one is kept)."""
    path = Path(path)
    if not path.is_file():
        return Settings()
    try:
        return from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        try:
            path.replace(path.with_suffix(".damaged.json"))
        except OSError:
            pass
        return Settings()


def save(settings: Settings, path: Path) -> None:
    """Write the file in one step, so a crash can never leave half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_dict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- checks


def parse_time(text: str) -> Optional[dtime]:
    """'14:29:45' -> time(14, 29, 45). Only the full HH:MM:SS form is accepted."""
    m = _TIME_RE.match((text or "").strip())
    return dtime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_date(text: str) -> Optional[date]:
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def validate_account(account: Account, side: str, *, check_path: bool = True) -> list[Problem]:
    title = SIDE_TITLES[side]
    out: list[Problem] = []
    if not account.server.strip():
        out.append(Problem(f"{side}.server", f"{title} account: type the server name exactly as MT5 shows it "
                                              "in the login window (for example ICMarketsSC-Demo)."))
    if account.login <= 0:
        out.append(Problem(f"{side}.login", f"{title} account: type the account number (digits only)."))
    symbol = account.symbol.strip()
    if not symbol:
        out.append(Problem(f"{side}.symbol", f"{title} account: type the symbol, for example XAUUSD."))
    elif not _SYMBOL_RE.match(symbol):
        out.append(Problem(f"{side}.symbol", f"{title} account: '{symbol}' does not look like a symbol name. "
                                              "Copy it from Market Watch in MT5."))
    path = account.terminal_path.strip()
    if not path:
        out.append(Problem(f"{side}.terminal_path", f"{title} account: choose the MetaTrader 5 program "
                                                     "(terminal64.exe) for this account."))
    elif check_path and not account.managed:  # Aitrade's own copy is made in the last setup step
        p = Path(path)
        if p.name.lower() != "terminal64.exe":
            out.append(Problem(f"{side}.terminal_path", f"{title} account: the MetaTrader 5 program must be the "
                                                         "file terminal64.exe."))
        elif not p.is_file():
            out.append(Problem(f"{side}.terminal_path", f"{title} account: this file does not exist: {path}"))
    return out


def validate_accounts(settings: Settings, *, check_path: bool = True) -> list[Problem]:
    """Both accounts, plus the rules between them."""
    out = validate_account(settings.buy, "buy", check_path=check_path)
    out += validate_account(settings.sell, "sell", check_path=check_path)
    b, s = settings.buy, settings.sell
    if b.login > 0 and b.login == s.login and b.server.strip().lower() == s.server.strip().lower():
        out.append(Problem("sell.login", "The BUY and SELL accounts are the same account. "
                                         "Use two different accounts."))
    if b.terminal_path.strip() and _same_path(b.terminal_path, s.terminal_path):
        out.append(Problem("sell.terminal_path", "Both accounts use the same MetaTrader 5 folder. Each account "
                                                 "needs its own copy of MetaTrader 5."))
    return out


def _same_path(a: str, b: str) -> bool:
    import ntpath

    return ntpath.normcase(ntpath.normpath(a.strip())) == ntpath.normcase(ntpath.normpath(b.strip()))


def validate_plan(plan: Plan) -> list[Problem]:
    out: list[Problem] = []
    if not (isinstance(plan.lots, (int, float)) and math.isfinite(plan.lots) and plan.lots > 0):
        out.append(Problem("plan.lots", "Lot size must be more than 0."))
    if not (isinstance(plan.tp_usd, (int, float)) and math.isfinite(plan.tp_usd) and plan.tp_usd > 0):
        out.append(Problem("plan.tp_usd", "Take profit must be more than 0 USD."))
    if plan.sl_usd is not None and not (math.isfinite(plan.sl_usd) and plan.sl_usd > 0):
        out.append(Problem("plan.sl_usd", "Stop loss must be more than 0 USD, or empty for no stop loss."))
    if parse_date(plan.date) is None:
        out.append(Problem("plan.date", "Choose the date (year-month-day)."))
    if parse_time(plan.time) is None:
        out.append(Problem("plan.time", "Write the time as HH:MM:SS, for example 14:29:45."))
    return out


def target_datetime(plan: Plan, tz) -> datetime:
    """The moment to open, as an aware datetime in `tz` (the PC's time zone).

    A time that does not exist on that day (the hour skipped when summer time
    starts) moves forward by the gap, the same way the engine does it.
    """
    from hedgebot.clock import localize

    day, at = parse_date(plan.date), parse_time(plan.time)
    if day is None or at is None:
        raise ValueError("the plan has no valid date and time")
    return localize(day, at, tz)
