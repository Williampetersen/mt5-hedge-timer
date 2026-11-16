"""Plain-English text for what each side is doing. No window code, so it can be tested."""

from __future__ import annotations

from typing import Optional

from . import runner as r
from .runner import SideState

# Tones pick the colour in the window.
MUTED, INFO, GOOD, WARN, BAD = "muted", "info", "good", "warn", "bad"

# The engine's short close reasons, in plain words.
CLOSE_REASONS = {
    "TP": "take profit reached",
    "SL": "stop loss hit",
    "stop-out": "stopped out by the broker (not enough margin)",
    "manual": "closed by hand",
    "manual (mobile)": "closed by hand (phone)",
    "manual (web)": "closed by hand (web)",
    "expert/script": "closed by a robot or script",
}


def money(value: Optional[float], currency: str = "", *, signed: bool = False) -> str:
    if value is None:
        return "-"
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return f"{text} {currency}".rstrip()


def price(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{int(digits)}f}"


def countdown(seconds: float) -> str:
    """'00:12:33', or '1d 02:03:04' for more than a day."""
    s = max(0, int(seconds + 0.999))  # round up: 0.2 s left still shows 00:00:01
    days, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    clock = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{days}d {clock}" if days else clock


def headline(st: SideState) -> tuple[str, str]:
    """(short status text, tone) for one side."""
    s = st.state
    if s == r.IDLE:
        return "Not started", MUTED
    if s == r.STARTING:
        return "Starting…", INFO
    if s == r.WAITING:
        return "Waiting for the time", INFO
    if s == r.PREARM:
        return "Connecting to MetaTrader 5…", INFO
    if s == r.ARMED:
        return "Ready to open", GOOD
    if s == r.CHECK_OK:
        return "All checks passed", GOOD
    if s == r.CHECK_FAILED:
        return "Check failed", BAD
    if s == r.PRACTICE_OK:
        return "Practice OK – nothing was placed", GOOD
    if s in (r.OPENED, r.MONITORING):
        return "Position open", GOOD
    if s == r.CLOSED:
        reason = str(st.closed.get("reason") or "closed")
        reason = CLOSE_REASONS.get(reason, reason)
        return f"Closed: {reason}", GOOD if (st.closed.get("profit") or 0) >= 0 else WARN
    if s == r.MONITOR_ENDED:
        return "Position still open", INFO
    if s == r.SKIPPED:
        return "Not opened", WARN
    if s == r.FAILED:
        return "Failed", BAD
    if s == r.STOPPED:
        return "Stopped", MUTED
    return s, MUTED


def snapshot_lines(d: dict) -> list[str]:
    """Account, price, take profit and margin, from the engine's checks."""
    if not d:
        return []
    digits, ccy = int(d.get("digits") or 2), str(d.get("currency") or "")
    kind = str(d.get("account_kind") or "").capitalize() or "Account"
    lines = [
        f"{kind} account {d.get('login')} · balance {money(d.get('balance'), ccy)}"
        + (f" · leverage 1:{d['leverage']}" if d.get("leverage") else ""),
        f"{d.get('symbol')}: bid {price(d.get('bid'), digits)} / ask {price(d.get('ask'), digits)}"
        f" · spread {float(d.get('spread_points') or 0):g} points",
    ]
    if d.get("tp_price") is not None:
        lines.append(f"Take profit {d.get('tp')} → price {price(d.get('tp_price'), digits)}"
                     f" ({price(d.get('tp_distance'), digits)} away)")
    else:
        lines.append("Take profit: none")
    if d.get("sl_price") is not None:
        lines.append(f"Stop loss {d.get('sl')} → price {price(d.get('sl_price'), digits)}")
    if d.get("margin_required") is not None:
        lines.append(f"Margin needed {money(d.get('margin_required'), ccy)}"
                     f" · free {money(d.get('margin_free'), ccy)}")
    return lines


def timing_line(result: dict) -> Optional[str]:
    dev, lat = result.get("deviation_ms"), result.get("latency_ms")
    if dev is None:
        return None
    when = "after" if dev >= 0 else "before"
    text = f"Sent {abs(dev):.1f} ms {when} the set time"
    if lat is not None:
        text += f" · broker answered in {lat:.0f} ms"
    return text


def detail_lines(st: SideState) -> list[str]:
    s = st.state
    if s == r.STARTING:
        return [st.activity] if st.activity else []
    if s == r.WAITING:
        return ["MetaTrader 5 connects shortly before the time."]
    if s == r.PREARM:
        return [st.activity] if st.activity else []
    if s in (r.ARMED, r.CHECK_OK):
        return snapshot_lines(st.snapshot)
    if s in (r.CHECK_FAILED, r.SKIPPED, r.FAILED):
        return [st.message] if st.message else []
    if s == r.PRACTICE_OK:
        line = timing_line(st.result)
        return [line] if line else []
    if s in (r.OPENED, r.MONITORING, r.CLOSED, r.MONITOR_ENDED):
        p, res = st.position, st.result
        digits, ccy = int(p.get("digits") or 2), str(p.get("currency") or "")
        fill = p.get("fill_price", res.get("fill_price"))
        lines = []
        opened = f"Opened at {price(fill, digits)}"
        slip = p.get("slippage_points")
        if slip is not None:
            opened += f" · slippage {slip:g} points"
        lines.append(opened)
        if p:
            tp = p.get("tp_price")
            lines.append(f"Take profit at {price(tp, digits)}" if tp else "Take profit: none")
            if p.get("sl_price"):
                lines.append(f"Stop loss at {price(p.get('sl_price'), digits)}")
        timing = timing_line(res)
        if timing:
            lines.append(timing)
        if s == r.CLOSED:
            c = st.closed
            cd = int(c.get("digits") or digits)
            lines.append(f"Closed at {price(c.get('close_price'), cd)} · profit "
                         f"{money(c.get('profit'), str(c.get('currency') or ccy), signed=True)}")
            costs = (c.get("commission") or 0.0) + (c.get("swap") or 0.0)
            if costs:
                lines.append(f"Commission and swap: {money(costs, str(c.get('currency') or ccy), signed=True)}")
        elif s == r.MONITOR_ENDED:
            lines.append("The app stopped watching. The position stays in MetaTrader 5 with its take profit.")
            if st.pnl is not None:
                lines.append(f"Profit when watching stopped: {money(st.pnl, ccy, signed=True)}")
        elif st.pnl is not None:
            lines.append(f"Profit now: {money(st.pnl, ccy, signed=True)}")
        return lines
    return []


def closed_summary(buy: SideState, sell: SideState) -> str:
    """'BUY +150.00 USD · SELL -162.40 USD · together -12.40 USD' (profit, commission and swap)."""
    parts, total, currencies = [], 0.0, set()
    for name, st in (("BUY", buy), ("SELL", sell)):
        c = st.closed
        if c.get("profit") is None:
            return ""
        net = float(c["profit"]) + float(c.get("commission") or 0.0) + float(c.get("swap") or 0.0)
        ccy = str(c.get("currency") or "")
        currencies.add(ccy)
        total += net
        parts.append(f"{name} {money(net, ccy, signed=True)}")
    if len(currencies) == 1:
        parts.append(f"together {money(total, currencies.pop(), signed=True)}")
    return " · ".join(parts) + " after costs"


def overall(mode: str, buy: SideState, sell: SideState, *, seconds_left: Optional[float] = None,
            active: bool = False) -> tuple[str, str, str]:
    """(big headline, smaller line, tone) for the whole run."""
    states = (buy.state, sell.state)
    both = lambda *names: all(s in names for s in states)  # noqa: E731
    any_of = lambda *names: any(s in names for s in states)  # noqa: E731

    if mode == r.MODE_CHECK:
        if both(r.CHECK_OK):
            return "Both accounts are ready", "Every check passed. Nothing was sent.", GOOD
        if any_of(r.CHECK_FAILED, r.FAILED):
            return "A check failed", "Read the red text below, fix it, and check again.", BAD
        if active:
            return "Checking both accounts…", "MetaTrader 5 opens and logs in. This can take a minute.", INFO
        return "Check stopped", "", MUTED

    position = (r.OPENED, r.MONITORING, r.CLOSED, r.MONITOR_ENDED)
    practice = mode == r.MODE_PRACTICE
    if both(*position):
        if both(r.CLOSED):
            return "Both positions are closed", closed_summary(buy, sell), GOOD
        if any_of(r.CLOSED):
            return "One position is closed, one is open", "", INFO
        gap = r.send_gap_ms(buy, sell)
        sub = f"The two orders were sent {gap:.1f} ms apart." if gap is not None else ""
        return "Both positions are open", sub, GOOD
    if both(r.PRACTICE_OK):
        gap = r.send_gap_ms(buy, sell)
        sub = "The broker accepted both orders. Nothing was placed."
        if gap is not None:
            sub += f" They were {gap:.1f} ms apart."
        return "Practice run OK", sub, GOOD
    if any_of(*position) and any_of(r.SKIPPED, r.FAILED, r.STOPPED):
        opened = "BUY" if buy.state in position else "SELL"
        other = "SELL" if opened == "BUY" else "BUY"
        return (f"Only {opened} opened – {other} did not",
                f"Look at MetaTrader 5 now: the {opened} position is open without the other side.", BAD)
    if any_of(r.FAILED, r.CHECK_FAILED):
        return "It did not work", "Read the red text below.", BAD
    if any_of(r.SKIPPED):
        return "Not opened", "Read the text below to see why.", WARN
    if both(r.STOPPED) or (not active and any_of(r.STOPPED)):
        return "Stopped", "Nothing more will be opened.", MUTED
    if active:
        if seconds_left is not None and seconds_left > 0:
            what = "Practice in" if practice else "Opens in"
            return f"{what} {countdown(seconds_left)}", "", INFO
        return ("Checking the order…" if practice else "Opening…"), "", INFO
    return "Ready", "", MUTED
