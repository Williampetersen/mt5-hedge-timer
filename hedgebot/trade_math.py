"""Pure trading math and the MT5 constants it needs.

No MetaTrader5 import here, so every rule (TP/SL from money, rounding, lot
steps, filling mode, retry rules, closed-market detection) is unit-tested
without MT5 installed. The numeric constants are the documented MQL5 values.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Iterable, Optional

# ---------------------------------------------------------------- MT5 constants
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
FILLING_NAMES = {ORDER_FILLING_FOK: "FOK", ORDER_FILLING_IOC: "IOC", ORDER_FILLING_RETURN: "RETURN"}

ORDER_TIME_GTC = 0

# Bit flags in symbol_info().filling_mode
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6

TRADE_RETCODE_CHECK_OK = 0  # order_check() success
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_INVALID_FILL = 10030

SUCCESS_RETCODES = frozenset({TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL})
RETRYABLE_RETCODES = frozenset({TRADE_RETCODE_REQUOTE, TRADE_RETCODE_PRICE_CHANGED, TRADE_RETCODE_PRICE_OFF})
MAX_RETRIES = 3

_RETCODE_TEXT = {
    0: "OK",
    10004: "requote",
    10006: "request rejected",
    10007: "request canceled",
    10008: "order placed",
    10009: "done",
    10010: "done (partial fill)",
    10011: "request processing error",
    10012: "request timed out",
    10013: "invalid request",
    10014: "invalid volume",
    10015: "invalid price",
    10016: "invalid stops (TP/SL too close?)",
    10017: "trading disabled",
    10018: "market closed",
    10019: "not enough money",
    10020: "prices changed",
    10021: "no quotes (price off)",
    10024: "too frequent requests",
    10027: "autotrading disabled by the client terminal",
    10030: "unsupported filling mode",
    10031: "no connection with the trade server",
    10040: "position limit reached",
}

_CLOSE_REASONS = {
    DEAL_REASON_CLIENT: "manual",
    DEAL_REASON_MOBILE: "manual (mobile)",
    DEAL_REASON_WEB: "manual (web)",
    DEAL_REASON_EXPERT: "expert/script",
    DEAL_REASON_SL: "SL",
    DEAL_REASON_TP: "TP",
    DEAL_REASON_SO: "stop-out",
    7: "rollover",
    8: "variation margin",
    9: "split",
}


def retcode_text(code: Optional[int]) -> str:
    if code is None:
        return "no reply"
    return f"{code} {_RETCODE_TEXT.get(code, 'unknown retcode')}"


def close_reason_text(reason: int) -> str:
    return _CLOSE_REASONS.get(reason, f"other ({reason})")


def order_type_for(direction: str) -> int:
    return ORDER_TYPE_BUY if direction == "buy" else ORDER_TYPE_SELL


def account_kind(trade_mode: int) -> str:
    return {ACCOUNT_TRADE_MODE_DEMO: "demo", ACCOUNT_TRADE_MODE_CONTEST: "contest"}.get(trade_mode, "real")


def trade_mode_problem(trade_mode: int, direction: str) -> Optional[str]:
    """Why the symbol's trade mode forbids opening this direction, or None."""
    if trade_mode == SYMBOL_TRADE_MODE_FULL:
        return None
    if trade_mode == SYMBOL_TRADE_MODE_LONGONLY:
        return None if direction == "buy" else "the broker allows only BUY (long-only) on this symbol"
    if trade_mode == SYMBOL_TRADE_MODE_SHORTONLY:
        return None if direction == "sell" else "the broker allows only SELL (short-only) on this symbol"
    if trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY:
        return "the symbol is close-only: new positions are not allowed"
    return "trading is disabled for this symbol"


# ---------------------------------------------------------------- rounding


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def step_decimals(step: float) -> int:
    """Decimals needed to show a step: 0.01 -> 2, 0.5 -> 1, 1 -> 0, 1e-05 -> 5."""
    exp = _dec(step).normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def round_to_step(value: float, step: float) -> float:
    """Round to the nearest multiple of step (halves round up), without float noise."""
    if step <= 0:
        raise ValueError("step must be > 0")
    s = _dec(step)
    n = (_dec(value) / s).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(n * s)


def round_price(price: float, tick_size: float, digits: int) -> float:
    step = tick_size if tick_size and tick_size > 0 else 10.0 ** -digits
    return round(round_to_step(price, step), digits)


# ---------------------------------------------------------------- lots / filling


def validate_lots(lots: float, volume_min: float, volume_max: float, volume_step: float) -> list[str]:
    """Problems with the lot size for this symbol (empty list = OK)."""
    problems = []
    if lots < volume_min - 1e-12:
        problems.append(f"lots {lots:g} is below the broker minimum {volume_min:g}")
    if volume_max and volume_max > 0 and lots > volume_max + 1e-12:
        problems.append(f"lots {lots:g} is above the broker maximum {volume_max:g}")
    if volume_step and volume_step > 0:
        steps = _dec(lots) / _dec(volume_step)
        if steps != steps.to_integral_value():
            problems.append(f"lots {lots:g} is not a multiple of the volume step {volume_step:g}")
    return problems


def select_filling_mode(filling_flags: int) -> int:
    """Pick the order filling type the symbol supports: FOK, then IOC, else RETURN."""
    flags = int(filling_flags or 0)
    if flags & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if flags & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN


# ---------------------------------------------------------------- money <-> price distance


def distance_from_contract(money: float, lots: float, contract_size: float) -> float:
    """Price distance for `money` when the symbol's profit currency is the account currency.

    Exact for linear CFDs/FX quoted in the account currency (XAUUSD on a USD
    account: 150 / (0.06 * 100) = 25.00). Some brokers report a wrong
    trade_tick_value, so this is the better fallback when it applies.
    """
    if money <= 0 or lots <= 0 or contract_size <= 0:
        raise ValueError("money, lots and contract_size must all be > 0")
    return money / (lots * contract_size)


def distance_from_tick_value(money: float, lots: float, tick_size: float, tick_value: float) -> float:
    """Price distance that makes `money` on `lots` (fallback when order_calc_profit is unavailable).

    tick_value is the account-currency value of one tick for 1.0 lot.
    XAUUSD: tick 0.01 worth 1.00 USD/lot -> 150 USD on 0.06 lot = 25.00.
    """
    if money <= 0 or lots <= 0 or tick_size <= 0 or tick_value <= 0:
        raise ValueError("money, lots, tick_size and tick_value must all be > 0")
    return money / (lots * tick_value) * tick_size


def solve_distance_for_money(
    profit_for_distance: Callable[[float], Optional[float]],
    money: float,
    initial_guess: float,
    *,
    max_iter: int = 10,
    abs_tol: float = 0.001,
) -> float:
    """Find the price distance d > 0 where |profit_for_distance(d)| == money.

    profit_for_distance wraps MT5 order_calc_profit (it handles the account
    currency). Profit is (nearly) proportional to the distance, so scaling the
    guess converges in one or two steps, also for non-linear quote currencies.
    """
    if money <= 0:
        raise ValueError("money must be > 0")
    if not initial_guess or initial_guess <= 0:
        raise ValueError("initial guess must be > 0")
    d = float(initial_guess)
    for _ in range(max_iter):
        p = profit_for_distance(d)
        if p is None:
            raise ValueError("order_calc_profit returned nothing")
        p = abs(float(p))
        if not math.isfinite(p) or p == 0:
            raise ValueError(f"profit for distance {d} is {p}")
        if abs(p - money) <= abs_tol:
            return d
        d = d * money / p
    return d


def tp_sl_prices(
    direction: str,
    entry: float,
    tp_distance: Optional[float],
    sl_distance: Optional[float],
    tick_size: float,
    digits: int,
) -> tuple[Optional[float], Optional[float]]:
    """TP and SL prices from an entry price, rounded to the tick size."""
    sign = 1.0 if direction == "buy" else -1.0
    tp = round_price(entry + sign * tp_distance, tick_size, digits) if tp_distance else None
    sl = round_price(entry - sign * sl_distance, tick_size, digits) if sl_distance else None
    for label, value in (("TP", tp), ("SL", sl)):
        if value is not None and value <= 0:
            raise ValueError(f"{label} price would be {value}: the distance is larger than the price")
    return tp, sl


def stops_level_problem(
    direction: str,
    bid: float,
    ask: float,
    tp: Optional[float],
    sl: Optional[float],
    stops_level_points: int,
    point: float,
) -> Optional[str]:
    """The broker rejects TP/SL closer than stops_level points to the closing price."""
    if not stops_level_points or stops_level_points <= 0:
        return None
    min_d = stops_level_points * point
    if direction == "buy":  # a long position closes at the bid
        if tp is not None and tp - bid < min_d:
            return f"TP {tp} is closer than the broker minimum of {stops_level_points} points to the bid {bid}"
        if sl is not None and bid - sl < min_d:
            return f"SL {sl} is closer than the broker minimum of {stops_level_points} points to the bid {bid}"
    else:  # a short position closes at the ask
        if tp is not None and ask - tp < min_d:
            return f"TP {tp} is closer than the broker minimum of {stops_level_points} points to the ask {ask}"
        if sl is not None and sl - ask < min_d:
            return f"SL {sl} is closer than the broker minimum of {stops_level_points} points to the ask {ask}"
    return None


def spread_points(bid: float, ask: float, point: float) -> float:
    return round((ask - bid) / point, 1)


def slippage_points(direction: str, requested: float, filled: float, point: float) -> float:
    """Slippage in points; positive = filled at a worse price than requested."""
    diff = (filled - requested) if direction == "buy" else (requested - filled)
    return round(diff / point, 1)


# ---------------------------------------------------------------- send rules


def should_retry(retcode: Optional[int], sends_done: int, max_retries: int = MAX_RETRIES) -> bool:
    """Retry only on requote / price changed / price off, at most `max_retries` times.

    sends_done is the number of order_send calls already made (1 after the first).
    """
    return retcode in RETRYABLE_RETCODES and sends_done <= max_retries


MAX_TICK_AGE_S = 300.0  # older last price (vs the freshest quote on the terminal) = market closed
MAX_QUOTE_GAP_S = 14.5 * 3600  # no broker's clock runs further than this from UTC


def market_closed_reason(
    symbol: str,
    symbol_tick_ts: float,
    freshest_tick_ts: float,
    utc_now_ts: float,
    *,
    max_age_s: float = MAX_TICK_AGE_S,
) -> Optional[str]:
    """Why the market looks closed right now, or None when prices are coming in.

    Only the AGE of the prices is judged, never what time it is at the broker:
    the trigger time comes from the PC clock alone. Quote times are in the
    broker's own clock, so the symbol's last price is compared with the
    freshest quote in Market Watch (same clock, any time zone). order_check()
    does NOT detect a closed market, hence this.
    """
    if abs(freshest_tick_ts - utc_now_ts) > MAX_QUOTE_GAP_S:
        # Further from now than any broker time zone can explain: nothing is quoting at all.
        return ("no fresh prices on this terminal (weekend, holiday or no data feed): "
                "the market is closed")
    age = freshest_tick_ts - symbol_tick_ts
    if age > max_age_s:
        return (f"the last {symbol} price is {age / 60:.0f} min old while other symbols are "
                "quoting: the market for it looks closed")
    return None


_SYMBOL_ALIASES = {
    "XAU": ("GOLD",), "GOLD": ("XAU",),
    "XAG": ("SILVER",), "SILVER": ("XAG",),
}


def suggest_symbols(wanted: str, names: Iterable[str], limit: int = 10) -> list[str]:
    """Broker symbol names that probably mean `wanted` (suffixes, GOLD for XAUUSD, ...)."""
    base = "".join(ch for ch in wanted.upper() if ch.isalnum())
    keys = [base] + ([base[:3]] if len(base) > 3 else [])
    for k in list(keys):
        keys.extend(_SYMBOL_ALIASES.get(k, ()))
    scored = []
    for name in names:
        upper = name.upper()
        for rank, key in enumerate(keys):
            if key and key in upper:
                scored.append((rank, len(name), name))
                break
    return [n for _, _, n in sorted(scored)][:limit]
