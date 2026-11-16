"""Check that a BUY config and a SELL config form a correct hedge pair."""

from __future__ import annotations

import math
import ntpath
from typing import Any, Callable

from .config import WEEKDAY_ABBR, AppConfig


class PairError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("The two configs are not a valid pair:\n" + "\n".join(f"  - {e}" for e in errors))


def _norm_path(path: str) -> str:
    """Windows path comparison: case-insensitive, / and \\ equal, .. resolved."""
    return ntpath.normcase(ntpath.normpath(path.strip()))


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def _show(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, tuple):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


# Must be identical in both files: same trade, same moment.
MUST_MATCH: list[tuple[str, Callable[[AppConfig], Any]]] = [
    ("trade.symbol", lambda c: c.trade.symbol),
    ("trade.lots", lambda c: c.trade.lots),
    ("trade.tp_money", lambda c: c.trade.tp_money),
    ("trade.tp_points", lambda c: c.trade.tp_points),
    ("trade.sl_money", lambda c: c.trade.sl_money),
    ("trade.sl_points", lambda c: c.trade.sl_points),
    ("trade.money_currency", lambda c: c.trade.money_currency),
    ("schedule.time", lambda c: c.schedule.time.isoformat()),
    ("schedule.timezone", lambda c: c.schedule.timezone),
    ("schedule.weekdays", lambda c: tuple(WEEKDAY_ABBR[d] for d in c.schedule.weekdays)),
    ("schedule.dates", lambda c: tuple(d.isoformat() for d in c.schedule.dates)),
]

# Allowed to differ, but usually a mistake.
SHOULD_MATCH: list[tuple[str, Callable[[AppConfig], Any]]] = [
    ("trade.deviation_points", lambda c: c.trade.deviation_points),
    ("trade.max_spread_points", lambda c: c.trade.max_spread_points),
    ("schedule.max_late_ms", lambda c: c.schedule.max_late_ms),
    ("schedule.prearm_seconds", lambda c: c.schedule.prearm_seconds),
    ("schedule.use_ntp_offset", lambda c: c.schedule.use_ntp_offset),
    ("account.allow_real_account", lambda c: c.account.allow_real_account),
]


def validate_pair(buy: AppConfig, sell: AppConfig, *, same_symbol: bool = True) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Any error means: do not start.

    same_symbol=False allows a different symbol name on each account (two brokers
    often name the same market differently, e.g. XAUUSD and GOLD); it becomes a warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if buy.direction != "buy":
        errors.append(f"the --buy config ({buy.source}) has direction '{buy.direction}'; it must be 'buy'")
    if sell.direction != "sell":
        errors.append(f"the --sell config ({sell.source}) has direction '{sell.direction}'; it must be 'sell'")
    if buy.name == sell.name:
        errors.append(f"both configs use name '{buy.name}'; give each app its own name "
                      "(it names the log files and journal rows)")
    for label, get in MUST_MATCH:
        a, b = get(buy), get(sell)
        if not _same(a, b):
            if label == "trade.symbol" and not same_symbol:
                warnings.append(f"the symbol name differs: buy={_show(a)} sell={_show(b)} "
                                "(fine if both names are the same market at two brokers)")
                continue
            errors.append(f"{label} differs: buy={_show(a)} sell={_show(b)} (must be the same in both files)")
    if buy.account.login == sell.account.login:
        errors.append(f"both configs use login {buy.account.login}; each app needs its own account")
    if _norm_path(buy.account.terminal_path) == _norm_path(sell.account.terminal_path):
        errors.append(
            f"both configs use the same terminal_path ({buy.account.terminal_path}); one Python process "
            "drives one terminal, so install MT5 twice (e.g. C:\\MT5_Buy and C:\\MT5_Sell)"
        )
    for label, get in SHOULD_MATCH:
        a, b = get(buy), get(sell)
        if not _same(a, b):
            warnings.append(f"{label} differs: buy={_show(a)} sell={_show(b)}")
    return errors, warnings


def check_pair(buy: AppConfig, sell: AppConfig) -> list[str]:
    """Raise PairError if invalid; otherwise return the warnings."""
    errors, warnings = validate_pair(buy, sell)
    if errors:
        raise PairError(errors)
    return warnings
