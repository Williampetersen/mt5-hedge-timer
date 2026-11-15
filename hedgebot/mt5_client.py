"""All MetaTrader 5 access: connect, preflight checks, send, TP/SL correction, monitoring.

The MetaTrader5 module is injected (or imported lazily), so the tests run
with a fake module and no MT5 installed. Python can drive only ONE terminal
per process, which is why the BUY and SELL apps are separate processes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from . import trade_math as tm
from .config import AppConfig

log = logging.getLogger("hedgebot.mt5")

RES_S_OK = 1  # MT5 last_error() code for "success"
INIT_TIMEOUT_MS = 60_000


class Mt5Error(RuntimeError):
    """MT5 is not usable for trading. The message is written for the user."""


class PreflightError(Mt5Error):
    """A preflight check failed; nothing was sent."""


def load_mt5_module():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:
        raise Mt5Error(
            "The MetaTrader5 Python package is not installed in this environment "
            "(it exists only for Windows). Run scripts\\setup.ps1."
        ) from exc
    return mt5


@dataclass
class Preflight:
    symbol: str
    order_type: int
    digits: int
    point: float
    tick_size: float
    tick_value: float
    currency: str
    filling: int
    stops_level: int
    tp_distance: Optional[float]
    sl_distance: Optional[float]
    request: dict  # pre-built order; only price/tp/sl are added at the trigger
    price: float
    bid: float
    ask: float
    spread_points: float
    tp_preview: Optional[float]
    sl_preview: Optional[float]
    margin_required: Optional[float]
    margin_free: float
    margin_warning: Optional[str]
    server_offset_s: int
    account_kind: str
    hedging: bool

    @property
    def filling_name(self) -> str:
        return tm.FILLING_NAMES.get(self.filling, str(self.filling))


@dataclass
class SendResult:
    status: str = "pending"  # opened | dry_run | failed | skipped_late | skipped_spread
    target_ts: float = 0.0
    send_ts: Optional[float] = None  # NTP-corrected clock
    send_pc_ts: Optional[float] = None  # raw PC clock (to compare the two apps on one PC)
    deviation_ms: Optional[float] = None
    latency_ms: Optional[float] = None
    spread_points: Optional[float] = None
    requested_price: Optional[float] = None
    fill_price: Optional[float] = None
    slippage_points: Optional[float] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    retcode: Optional[int] = None
    comment: str = ""
    attempts: int = 0
    order_ticket: Optional[int] = None
    deal_ticket: Optional[int] = None
    position_ticket: Optional[int] = None
    volume: Optional[float] = None

    @property
    def opened(self) -> bool:
        return self.status == "opened"


@dataclass
class CloseInfo:
    closed: bool = False
    reason: str = ""
    close_price: Optional[float] = None
    close_ts: Optional[float] = None
    profit: Optional[float] = None
    swap: Optional[float] = None
    commission: Optional[float] = None
    duration_s: Optional[float] = None
    max_favourable: float = 0.0
    max_adverse: float = 0.0
    last_profit: Optional[float] = None


class Mt5Client:
    def __init__(self, cfg: AppConfig, *, clock, mt5=None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.clock = clock
        self.mt5 = mt5 if mt5 is not None else load_mt5_module()
        self._sleep = sleep
        self.connected = False
        self.account = None
        self.terminal = None
        self.info = None  # symbol_info from preflight
        self.last_tick = None

    @property
    def direction(self) -> str:
        return self.cfg.direction

    @property
    def is_buy(self) -> bool:
        return self.cfg.direction == "buy"

    # ------------------------------------------------------------------ helpers

    def _last_error(self) -> tuple[int, str]:
        try:
            code, msg = self.mt5.last_error()
            return int(code), str(msg)
        except Exception:
            return -1, "unknown error"

    def _err(self) -> str:
        code, msg = self._last_error()
        return f"{msg} (code {code})"

    def positions(self, **kwargs) -> tuple:
        res = self.mt5.positions_get(**kwargs)
        if res is None:
            code, msg = self._last_error()
            if code == RES_S_OK:
                return ()
            raise Mt5Error(f"Could not read open positions: {msg} (code {code})")
        return tuple(res)

    def deals(self, *args, **kwargs) -> Optional[tuple]:
        """Deal history, () if there is none, or None if MT5 reported an error."""
        res = self.mt5.history_deals_get(*args, **kwargs)
        if res is None:
            code, _ = self._last_error()
            return () if code == RES_S_OK else None
        return tuple(res)

    # ------------------------------------------------------------------ connect

    def connect(self, attempts: int = 2) -> None:
        a = self.cfg.account
        err = ""
        for i in range(1, attempts + 1):
            ok = self.mt5.initialize(
                a.terminal_path,
                login=a.login,
                password=self.cfg.password,
                server=a.server,
                portable=a.portable,
                timeout=INIT_TIMEOUT_MS,
            )
            if ok:
                self.connected = True
                return
            err = self._err()
            try:
                self.mt5.shutdown()
            except Exception:
                pass
            if i < attempts:
                log.warning("MT5 initialize failed (%s); retrying in 3 s", err)
                self._sleep(3.0)
        raise Mt5Error(
            f"Could not connect to MT5 at {a.terminal_path} as login {a.login} on {a.server}: {err}. "
            "Check the password in .env, the exact server name from the MT5 login dialog, "
            "and that you can log in manually in this terminal."
        )

    def shutdown(self) -> None:
        if self.connected:
            try:
                self.mt5.shutdown()
            except Exception as exc:
                log.debug("mt5.shutdown failed: %s", exc)
            self.connected = False

    def check_account(self) -> None:
        """Right login, Algo Trading on, trading allowed, DEMO unless real is allowed."""
        a = self.cfg.account
        term = self.mt5.terminal_info()
        acc = self.mt5.account_info()
        if term is None or acc is None:
            raise Mt5Error(f"Could not read terminal/account info: {self._err()}")
        self.terminal, self.account = term, acc
        if int(acc.login) != a.login:
            raise Mt5Error(
                f"Terminal {a.terminal_path} is logged into account {acc.login}, but the config expects "
                f"{a.login}. Refusing to trade on the wrong account."
            )
        if not getattr(term, "connected", True):
            raise Mt5Error("The terminal is not connected to the broker (check internet and the server name).")
        if not term.trade_allowed:
            raise Mt5Error(
                "Algo Trading is OFF in this terminal. Click the 'Algo Trading' button in the MT5 toolbar "
                "so it turns green, then run again."
            )
        if getattr(term, "tradeapi_disabled", False):
            raise Mt5Error(
                "Python trading is blocked: in MT5 open Tools > Options > Expert Advisors and untick "
                "'Disable automatic trading via external Python API'."
            )
        if not getattr(acc, "trade_allowed", True):
            raise Mt5Error("Trading is not allowed on this account (logged in with the investor password?).")
        if not getattr(acc, "trade_expert", True):
            raise Mt5Error("The broker does not allow automated (expert) trading on this account.")
        kind = tm.account_kind(acc.trade_mode)
        if acc.trade_mode != tm.ACCOUNT_TRADE_MODE_DEMO:
            if not a.allow_real_account:
                raise Mt5Error(
                    f"Account {acc.login} is a {kind.upper()} account and account.allow_real_account is false. "
                    "Refusing to trade. Use a demo account, or set allow_real_account: true if you mean it."
                )
            log.warning("*** %s ACCOUNT %s: real money will be traded ***", kind.upper(), acc.login)
        margin_mode = getattr(acc, "margin_mode", tm.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
        log.info(
            "Account %s (%s, %s) on %s: balance %.2f %s, equity %.2f, free margin %.2f, leverage 1:%s",
            acc.login, kind, "hedging" if margin_mode == tm.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING else "netting",
            getattr(acc, "server", a.server), acc.balance, acc.currency, acc.equity, acc.margin_free,
            getattr(acc, "leverage", "?"),
        )
        log.info(
            "Terminal %s (build %s), ping %.1f ms, Algo Trading ON",
            getattr(term, "path", a.terminal_path), getattr(term, "build", "?"),
            getattr(term, "ping_last", 0) / 1000.0,
        )

    # ------------------------------------------------------------------ preflight

    def _wait_tick(self, symbol: str, timeout_s: float = 3.0):
        deadline = time.monotonic() + timeout_s
        while True:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is not None and tick.bid > 0 and tick.ask > 0:
                return tick
            if time.monotonic() >= deadline:
                return None
            self._sleep(0.1)

    def _suggest_symbols(self, symbol: str) -> str:
        try:
            names = tm.suggest_symbols(symbol, [s.name for s in (self.mt5.symbols_get() or ())])
        except Exception:
            names = []
        return f" Similar symbols on this broker: {', '.join(names)}." if names else ""

    def _freshest_quote_ts(self, fallback: float) -> float:
        """Newest last-quote time (server time) across Market Watch: our stand-in for 'server now'."""
        try:
            times = [float(s.time) for s in (self.mt5.symbols_get() or ())
                     if getattr(s, "visible", True) and getattr(s, "time", 0)]
        except Exception as exc:
            log.debug("symbols_get failed: %s", exc)
            times = []
        return max(times + [fallback])

    def distance(self, money: Optional[float], points: Optional[float], price: float, info, *,
                 favourable: bool) -> Optional[float]:
        """Price distance for a money amount (exact via order_calc_profit) or a point count."""
        if points is not None:
            return points * info.point
        if money is None:
            return None
        lots = self.cfg.trade.lots
        tick_size = info.trade_tick_size or info.point
        tv_key = "trade_tick_value_profit" if favourable else "trade_tick_value_loss"
        tick_value = getattr(info, tv_key, 0.0) or info.trade_tick_value
        contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        same_ccy = bool(self._currency()) and getattr(info, "currency_profit", "") == self._currency()
        guess = None
        if same_ccy and contract > 0:  # brokers sometimes report a wrong tick value; this is exact
            guess = tm.distance_from_contract(money, lots, contract)
        elif tick_value and tick_value > 0:
            guess = tm.distance_from_tick_value(money, lots, tick_size, tick_value)
        sign = (1.0 if self.is_buy else -1.0) * (1.0 if favourable else -1.0)
        order_type = tm.order_type_for(self.direction)

        def profit(d: float) -> Optional[float]:
            close = price + sign * d
            if close <= 0:
                return None
            return self.mt5.order_calc_profit(order_type, info.name, lots, price, close)

        try:
            return tm.solve_distance_for_money(profit, money, guess or 1000 * tick_size)
        except (ValueError, ZeroDivisionError) as exc:
            if guess is None:
                raise PreflightError(
                    f"Cannot turn {money} {self._currency()} into a price distance for {info.name}: {exc}"
                ) from None
            log.warning("order_calc_profit unavailable (%s); using the tick value instead", exc)
            return guess

    def _currency(self) -> str:
        return getattr(self.account, "currency", "") or ""

    def preflight(self, *, strict_market: bool = True) -> Preflight:
        """All checks before arming. strict_market=False (used by --check) only warns
        when the market is closed, so the setup can be verified on a weekend."""
        mt5, t = self.mt5, self.cfg.trade
        sym = t.symbol
        if not mt5.symbol_select(sym, True):
            if mt5.symbol_info(sym) is None:
                raise PreflightError(
                    f"Symbol '{sym}' does not exist on this broker.{self._suggest_symbols(sym)} "
                    "Set trade.symbol to the exact Market Watch name, including any suffix (e.g. XAUUSD.m)."
                )
            raise PreflightError(f"Could not select {sym} in Market Watch: {self._err()}")
        info = mt5.symbol_info(sym)
        if info is None:
            raise PreflightError(f"Could not read symbol info for {sym}: {self._err()}")
        self.info = info
        problem = tm.trade_mode_problem(info.trade_mode, self.direction)
        if problem:
            raise PreflightError(f"{sym}: {problem}.")
        tick = self._wait_tick(sym)
        if tick is None:
            raise PreflightError(f"No prices for {sym}: the market is closed or the symbol is not streaming.")
        self.last_tick = tick
        tick_ts = getattr(tick, "time_msc", 0) / 1000.0 or float(tick.time)
        server_offset, closed = tm.market_state(sym, tick_ts, self._freshest_quote_ts(tick_ts), self.clock.time())
        if closed:
            if strict_market:
                raise PreflightError(f"{closed[0].upper()}{closed[1:]}.")
            log.warning("MARKET CLOSED: %s. Continuing because this is only a check; "
                        "a real run would stop here.", closed)
        if server_offset is None:
            server_offset = 0
            log.warning("Broker server time unknown (no fresh quotes); the same-day guard uses UTC")
        lot_problems = tm.validate_lots(t.lots, info.volume_min, info.volume_max, info.volume_step)
        if lot_problems:
            raise PreflightError(f"{sym}: " + "; ".join(lot_problems) + ".")

        order_type = tm.order_type_for(self.direction)
        tick_size = info.trade_tick_size or info.point
        price = tick.ask if self.is_buy else tick.bid
        tp_d = self.distance(t.tp_money, t.tp_points, price, info, favourable=True)
        sl_d = self.distance(t.sl_money, t.sl_points, price, info, favourable=False)
        try:
            tp, sl = tm.tp_sl_prices(self.direction, price, tp_d, sl_d, tick_size, info.digits)
        except ValueError as exc:
            raise PreflightError(str(exc)) from None
        problem = tm.stops_level_problem(self.direction, tick.bid, tick.ask, tp, sl,
                                         getattr(info, "trade_stops_level", 0), info.point)
        if problem:
            raise PreflightError(problem)

        acc = mt5.account_info() or self.account
        hedging = getattr(acc, "margin_mode", tm.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) == \
            tm.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        if not hedging:
            others = self.positions(symbol=sym)
            if others:
                raise PreflightError(
                    f"This is a NETTING account and it already has a {sym} position (#{others[0].ticket}); "
                    "a new order would be merged into it. Close it first or use a hedging account."
                )

        margin = mt5.order_calc_margin(order_type, sym, t.lots, price)
        free = float(getattr(acc, "margin_free", 0.0))
        margin_warning = None
        if margin is None:
            log.warning("order_calc_margin failed: %s", self._err())
        elif t.check_margin and margin > free:
            margin_warning = (f"free margin {free:.2f} {self._currency()} is below the required "
                              f"{margin:.2f} for {t.lots:g} lot {sym}")

        filling = tm.select_filling_mode(info.filling_mode)
        request = {
            "action": tm.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(t.lots),
            "type": order_type,
            "deviation": int(t.deviation_points),
            "magic": int(t.magic),
            "comment": t.comment,
            "type_time": tm.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        filling = self._order_check(request, price, tp, sl, strict_market=strict_market)
        request["type_filling"] = filling

        return Preflight(
            symbol=sym, order_type=order_type, digits=int(info.digits), point=float(info.point),
            tick_size=float(tick_size), tick_value=float(info.trade_tick_value or 0.0),
            currency=self._currency(), filling=filling, stops_level=int(getattr(info, "trade_stops_level", 0)),
            tp_distance=tp_d, sl_distance=sl_d, request=request, price=price, bid=tick.bid, ask=tick.ask,
            spread_points=tm.spread_points(tick.bid, tick.ask, info.point), tp_preview=tp, sl_preview=sl,
            margin_required=margin, margin_free=free, margin_warning=margin_warning,
            server_offset_s=server_offset, account_kind=tm.account_kind(getattr(acc, "trade_mode", 2)),
            hedging=hedging,
        )

    def _order_check(self, request: dict, price: float, tp: Optional[float], sl: Optional[float], *,
                     strict_market: bool = True) -> int:
        """Run order_check (sends nothing). Returns the filling mode the broker accepts."""
        check = dict(request, price=price)
        if tp is not None:
            check["tp"] = tp
        if sl is not None:
            check["sl"] = sl
        first = request["type_filling"]
        candidates = [first] + [f for f in (tm.ORDER_FILLING_FOK, tm.ORDER_FILLING_IOC,
                                            tm.ORDER_FILLING_RETURN) if f != first]
        result = None
        for filling in candidates:
            result = self.mt5.order_check(dict(check, type_filling=filling))
            if result is None:
                raise PreflightError(f"order_check failed: {self._err()}")
            if result.retcode != tm.TRADE_RETCODE_INVALID_FILL:
                break
            log.warning("Filling mode %s rejected by the broker, trying the next one",
                        tm.FILLING_NAMES.get(filling, filling))
        code = result.retcode
        if code in (tm.TRADE_RETCODE_CHECK_OK, tm.TRADE_RETCODE_DONE):
            return filling
        comment = getattr(result, "comment", "")
        if code == tm.TRADE_RETCODE_MARKET_CLOSED:
            if not strict_market:
                log.warning("order_check: market closed (%s) - fine for a check, a real run would stop", comment)
                return filling
            raise PreflightError(f"The market for {request['symbol']} is closed right now ({comment}).")
        if code == tm.TRADE_RETCODE_NO_MONEY:
            raise PreflightError(f"Not enough free margin for {request['volume']:g} lot {request['symbol']} "
                                 f"({comment}).")
        raise PreflightError(f"order_check rejected the order: {tm.retcode_text(code)} ({comment}).")

    def warm_tick(self) -> None:
        """Called every ~0.5 s while armed to keep the price feed warm."""
        tick = self.mt5.symbol_info_tick(self.cfg.trade.symbol)
        if tick is not None:
            self.last_tick = tick

    # ------------------------------------------------------------------ guard

    def existing_trade(self, target_date: date, tz, server_offset_s: int) -> Optional[str]:
        """Reason not to open (open position or already opened today), else None."""
        t = self.cfg.trade
        positions = self.positions(symbol=t.symbol)  # raises if unreadable: never guess
        now = self.clock.time()
        # Deal times are broker server time, so ask for a wide window and filter exactly.
        deals = self.deals(int(now - 3 * 86400), int(now + 2 * 86400))
        if deals is None:
            log.warning("Could not read the deal history (%s); only open positions were checked", self._err())
            deals = ()
        return tm.existing_trade_reason(
            positions, deals, magic=t.magic, symbol=t.symbol, target_date=target_date, tz=tz,
            server_offset_s=server_offset_s,
        )

    # ------------------------------------------------------------------ send

    def find_position(self, order_ticket: Optional[int] = None, wait_s: float = 0.0):
        t = self.cfg.trade
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if order_ticket:
                    found = self.positions(ticket=order_ticket)
                    if found:
                        return found[0]
                ours = [p for p in self.positions(symbol=t.symbol) if p.magic == t.magic]
                if ours:
                    return max(ours, key=lambda p: getattr(p, "time_msc", 0))
            except Mt5Error as exc:
                log.warning("%s", exc)
            if time.monotonic() >= deadline:
                return None
            self._sleep(0.1)

    def fire(self, pf: Preflight, target_ts: float, *, dry_run: bool, max_late_ms: float,
             max_spread_points: Optional[float]) -> SendResult:
        """Send at the target. Retries only on requote/price changed/price off, at most 3 times."""
        mt5, clock = self.mt5, self.clock
        res = SendResult(target_ts=target_ts)
        result = None
        while True:
            late = (clock.time() - target_ts) * 1000.0
            if late > max_late_ms:
                res.status = "skipped_late" if res.attempts == 0 else "failed"
                res.comment = f"{late:.0f} ms after the target (max_late_ms is {max_late_ms:g})"
                if res.attempts:
                    res.comment += f"; gave up after {res.attempts} attempts ({tm.retcode_text(res.retcode)})"
                return res
            tick = mt5.symbol_info_tick(pf.symbol)
            if tick is None or tick.bid <= 0 or tick.ask <= 0:
                res.status = "failed"
                res.comment = f"no price for {pf.symbol} at the target: {self._err()}"
                return res
            price = tick.ask if self.is_buy else tick.bid
            spread = tm.spread_points(tick.bid, tick.ask, pf.point)
            res.spread_points = spread
            if max_spread_points is not None and spread > max_spread_points:
                res.status = "skipped_spread"
                res.comment = f"spread {spread:g} points is wider than max_spread_points {max_spread_points:g}"
                return res
            tp, sl = tm.tp_sl_prices(self.direction, price, pf.tp_distance, pf.sl_distance,
                                     pf.tick_size, pf.digits)
            request = dict(pf.request)
            request["price"] = price
            if tp is not None:
                request["tp"] = tp
            if sl is not None:
                request["sl"] = sl

            send_ts, send_pc = clock.time(), clock.pc_time()
            t0 = time.perf_counter()
            result = mt5.order_check(request) if dry_run else mt5.order_send(request)
            latency = (time.perf_counter() - t0) * 1000.0

            res.attempts += 1
            res.send_ts, res.send_pc_ts = send_ts, send_pc
            res.deviation_ms = (send_ts - target_ts) * 1000.0
            res.latency_ms = latency
            res.requested_price, res.tp_price, res.sl_price = price, tp, sl
            res.retcode = getattr(result, "retcode", None)
            res.comment = getattr(result, "comment", "") if result is not None else self._err()
            log.info("%s #%d: %s %g %s @ %.*f -> %s (%s), round-trip %.1f ms",
                     "order_check" if dry_run else "order_send", res.attempts, self.direction.upper(),
                     pf.request["volume"], pf.symbol, pf.digits, price, tm.retcode_text(res.retcode),
                     res.comment, latency)

            if dry_run:
                res.status = "dry_run" if res.retcode in (tm.TRADE_RETCODE_CHECK_OK, tm.TRADE_RETCODE_DONE) \
                    else "failed"
                return res
            if res.retcode in tm.SUCCESS_RETCODES or res.retcode == tm.TRADE_RETCODE_PLACED:
                break
            if tm.should_retry(res.retcode, res.attempts):
                log.warning("%s: retrying with a fresh price (retry %d of %d)",
                            tm.retcode_text(res.retcode), res.attempts, tm.MAX_RETRIES)
                continue
            # Not retried. If the outcome is unknown (no reply / timeout) make sure nothing opened.
            if result is None or res.retcode == tm.TRADE_RETCODE_TIMEOUT:
                pos = self.find_position(wait_s=3.0)
                if pos is not None:
                    log.warning("order_send gave %s but position #%s exists: treating it as opened",
                                tm.retcode_text(res.retcode), pos.ticket)
                    self._apply_fill(res, pos, None, pf)
                    return res
            res.status = "failed"
            return res

        res.order_ticket = int(getattr(result, "order", 0) or 0) or None
        res.deal_ticket = int(getattr(result, "deal", 0) or 0) or None
        res.volume = getattr(result, "volume", None)
        pos = self.find_position(res.order_ticket, wait_s=3.0)
        if pos is None and res.retcode == tm.TRADE_RETCODE_PLACED:
            res.status = "failed"
            res.comment = "order placed but no position appeared within 3 s - check MT5"
            return res
        self._apply_fill(res, pos, result, pf)
        if res.retcode == tm.TRADE_RETCODE_DONE_PARTIAL:
            log.warning("Partial fill: %s of %s lots", res.volume, pf.request["volume"])
        return res

    def _apply_fill(self, res: SendResult, pos, result, pf: Preflight) -> None:
        if pos is not None:
            res.position_ticket = int(pos.ticket)
            res.fill_price = float(pos.price_open)
            res.tp_price = float(pos.tp) or None
            res.sl_price = float(pos.sl) or None
            res.volume = float(pos.volume)
        else:
            res.fill_price = float(getattr(result, "price", 0.0) or 0.0) or res.requested_price
            res.position_ticket = res.order_ticket
            log.warning("Filled, but the position is not visible yet; using the order result price")
        if res.requested_price is not None and res.fill_price is not None:
            res.slippage_points = tm.slippage_points(self.direction, res.requested_price, res.fill_price,
                                                     pf.point)
        res.status = "opened"

    # ------------------------------------------------------------------ after the fill

    def correct_tp_sl(self, pf: Preflight, res: SendResult) -> Optional[str]:
        """Recompute TP/SL from the real fill price and modify if off by >= 1 tick.

        Returns an error text if the modification failed (the initial TP stays in place).
        """
        t = self.cfg.trade
        if not (t.has_tp or t.has_sl) or res.fill_price is None or res.position_ticket is None:
            return None
        info = self.info
        try:
            tp_d = self.distance(t.tp_money, t.tp_points, res.fill_price, info, favourable=True)
            sl_d = self.distance(t.sl_money, t.sl_points, res.fill_price, info, favourable=False)
            new_tp, new_sl = tm.tp_sl_prices(self.direction, res.fill_price, tp_d, sl_d, pf.tick_size,
                                             pf.digits)
        except (Mt5Error, ValueError) as exc:
            msg = f"could not recompute TP/SL from the fill price ({exc}); the initial TP/SL stays in place"
            log.error(msg)
            return msg
        cur_tp, cur_sl = res.tp_price or 0.0, res.sl_price or 0.0
        try:
            found = self.positions(ticket=res.position_ticket)
            if found:
                cur_tp, cur_sl = float(found[0].tp or 0.0), float(found[0].sl or 0.0)
        except Mt5Error as exc:
            log.warning("%s", exc)
        tol = pf.tick_size * 0.999
        tp_off = new_tp is not None and abs(new_tp - cur_tp) >= tol
        sl_off = new_sl is not None and abs(new_sl - cur_sl) >= tol
        if not (tp_off or sl_off):
            res.tp_price, res.sl_price = new_tp, new_sl
            log.info("TP/SL already exact for the fill price %s (TP %s, SL %s)", res.fill_price,
                     new_tp, new_sl if new_sl is not None else "none")
            return None
        request = {
            "action": tm.TRADE_ACTION_SLTP,
            "symbol": pf.symbol,
            "position": int(res.position_ticket),
            "tp": float(new_tp) if new_tp is not None else 0.0,
            "sl": float(new_sl) if new_sl is not None else 0.0,
            "magic": int(t.magic),
        }
        result = self.mt5.order_send(request)
        code = getattr(result, "retcode", None)
        if code == tm.TRADE_RETCODE_DONE:
            def p(x):
                return f"{x:.{pf.digits}f}" if x else "none"

            log.info("TP/SL corrected from the fill price %s: TP %s -> %s, SL %s -> %s", p(res.fill_price),
                     p(cur_tp), p(new_tp), p(cur_sl), p(new_sl))
            res.tp_price, res.sl_price = new_tp, new_sl
            return None
        reason = getattr(result, "comment", "") if result is not None else self._err()
        msg = (f"could not correct TP/SL on #{res.position_ticket} ({tm.retcode_text(code)}, {reason}); "
               f"the TP from the requested price ({cur_tp or 'none'}) stays in place")
        log.error(msg)
        return msg

    def monitor(self, res: SendResult, until_ts: float, poll_s: float, *, log_every_s: float = 60.0) -> CloseInfo:
        """Track floating P/L until the position closes or until_ts is reached."""
        info = CloseInfo()
        ticket = res.position_ticket
        if ticket is None:
            info.reason = "unknown position"
            return info
        log.info("Monitoring position #%s (poll every %g s)", ticket, poll_s)
        next_log = 0.0
        error_since = None
        last_reconnect = 0.0
        while True:
            now = self.clock.time()
            if now >= until_ts:
                info.reason = "still open (monitor ended)"
                log.info("Monitor window ended; position #%s stays open in MT5 with its TP/SL", ticket)
                return info
            try:
                found = self.positions(ticket=ticket)
                error_since = None
            except Mt5Error as exc:
                if error_since is None:
                    error_since = now
                    log.warning("Lost contact with MT5 (%s); will keep trying", exc)
                if now - error_since > 30 and now - last_reconnect > 60:
                    last_reconnect = now
                    self._reconnect()
                self._sleep(poll_s)
                continue
            if found:
                profit = float(found[0].profit)
                info.last_profit = profit
                info.max_favourable = max(info.max_favourable, profit)
                info.max_adverse = min(info.max_adverse, profit)
                if now >= next_log:
                    log.info("Position #%s open: P/L %+.2f %s (max favourable %+.2f, max adverse %+.2f)",
                             ticket, profit, self._currency(), info.max_favourable, info.max_adverse)
                    next_log = now + log_every_s
            else:
                details = self._close_details(ticket, res)
                if details is None:
                    # History can lag the position list by a moment; confirm before concluding.
                    for _ in range(20):
                        self._sleep(0.5)
                        details = self._close_details(ticket, res)
                        if details is not None:
                            break
                    if details is None:
                        try:
                            if self.positions(ticket=ticket):
                                continue  # it was a glitch: still open
                        except Mt5Error:
                            pass
                        info.closed = True
                        info.reason = "closed (no closing deal found)"
                        info.close_ts = self.clock.time()
                        return info
                details.max_favourable = info.max_favourable
                details.max_adverse = info.max_adverse
                details.last_profit = info.last_profit
                return details
            self._sleep(poll_s)

    def _reconnect(self) -> None:
        log.warning("Reconnecting to MT5...")
        try:
            self.mt5.shutdown()
        except Exception:
            pass
        self.connected = False
        try:
            self.connect(attempts=1)
            log.info("Reconnected to MT5")
        except Mt5Error as exc:
            log.error("Reconnect failed: %s", exc)

    def _close_details(self, ticket: int, res: SendResult) -> Optional[CloseInfo]:
        deals = self.deals(position=ticket)
        if not deals:
            return None
        ins = [d for d in deals if d.entry == tm.DEAL_ENTRY_IN]
        outs = [d for d in deals if d.entry in (tm.DEAL_ENTRY_OUT, tm.DEAL_ENTRY_OUT_BY, tm.DEAL_ENTRY_INOUT)]
        if not outs:
            return None
        last = max(outs, key=lambda d: d.time_msc)
        volume = sum(d.volume for d in outs)
        close_price = sum(d.price * d.volume for d in outs) / volume if volume else last.price
        duration = (last.time_msc - min(d.time_msc for d in ins)) / 1000.0 if ins else None
        if duration is not None and res.send_ts is not None:
            close_ts = res.send_ts + duration  # avoids converting broker server time
        else:
            close_ts = self.clock.time()
        return CloseInfo(
            closed=True,
            reason=tm.close_reason_text(last.reason),
            close_price=round(close_price, int(getattr(self.info, "digits", 5) or 5)),
            close_ts=close_ts,
            profit=round(sum(d.profit for d in outs), 2),
            swap=round(sum(d.swap for d in deals), 2),
            commission=round(sum(d.commission for d in deals) + sum(getattr(d, "fee", 0.0) for d in deals), 2),
            duration_s=duration,
        )
