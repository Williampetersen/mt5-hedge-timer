"""CLI entry point.

  python -m hedgebot --config config/buy.yaml              # normal scheduled run (one day)
  python -m hedgebot --config config/buy.yaml --dry-run    # order_check() instead of order_send()
  python -m hedgebot --config config/buy.yaml --test-in 60 # target = now + 60 s (demo testing)
  python -m hedgebot --config config/buy.yaml --check      # connect + preflight, then exit
  python -m hedgebot --config config/buy.yaml --loop       # trade every scheduled day

Exit codes: 0 ok, 1 error, 2 config/usage error, 3 skipped (late, spread,
already traded today), 130 interrupted (Ctrl+C).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # pragma: no cover
    sys.exit("hedgebot needs Python 3.11 or newer")

import argparse
import json
import logging
import os
import platform
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from . import clock as clk
from . import trade_math as tm
from .config import PROJECT_ROOT, AppConfig, ConfigError, load_config
from .journal import Journal
from .mt5_client import CloseInfo, Mt5Client, Mt5Error, Preflight, SendResult
from .notify import Notifier

log = logging.getLogger("hedgebot")
UTC = timezone.utc

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SKIPPED = 3
EXIT_INTERRUPTED = 130

RESULT_PREFIX = "HEDGEBOT_RESULT "  # machine-readable line for run_pair.py
CHILD_ENV = "HEDGEBOT_CHILD"


# --------------------------------------------------------------------------- logging


class TzFormatter(logging.Formatter):
    """Timestamps with milliseconds in the schedule timezone (not the PC's zone)."""

    def __init__(self, fmt: str, tz):
        super().__init__(fmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API)
        dt = datetime.fromtimestamp(record.created, self.tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d} {dt.tzname()}"


class DailyFileHandler(logging.Handler):
    """Writes to logs/<name>_<YYYY-MM-DD>.log and switches file when the date changes."""

    def __init__(self, log_dir: Path, app_name: str, tz):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.app_name = app_name
        self.tz = tz
        self._day = None
        self._stream = None

    def path_for(self, day) -> Path:
        return self.log_dir / f"{self.app_name}_{day.isoformat()}.log"

    def emit(self, record):
        try:
            day = datetime.fromtimestamp(record.created, self.tz).date()
            if day != self._day or self._stream is None:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def _open(self, day):
        if self._stream is not None:
            self._stream.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path_for(day), "a", encoding="utf-8")
        self._day = day

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def setup_logging(cfg: AppConfig, log_dir: Path, verbose: bool = False) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # never crash on a console that can't show a character
    except Exception:
        pass
    tz = cfg.schedule.tz
    fmt = TzFormatter("%(asctime)s %(levelname)-7s %(message)s", tz)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = DailyFileHandler(log_dir, cfg.name, tz)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # urllib3 debug lines would contain the Telegram URL (with the bot token)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- app


def _f(value, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


class HedgeApp:
    """Runs one app (one direction, one terminal) through a scheduled day."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        dry_run: bool = False,
        notifier: Optional[Notifier] = None,
        journal: Optional[Journal] = None,
        clock=None,
        client_factory: Optional[Callable] = None,
        ntp: Optional[Callable] = None,
        sleep: Optional[Callable[[float], None]] = None,
        boost: bool = True,
        emit_results: Optional[bool] = None,
    ):
        self.cfg = cfg
        self.dry_run = dry_run
        self.notifier = notifier or Notifier()
        self.journal = journal or Journal(PROJECT_ROOT / "data" / "journal.csv")
        self.clock = clock or clk.Clock()
        self.sleep = sleep or time.sleep
        self.client_factory = client_factory or (
            lambda c, clock, sleep: Mt5Client(c, clock=clock, sleep=sleep)
        )
        self.ntp = ntp or clk.measure_ntp_offset
        self.boost = boost
        self.emit_results = bool(os.environ.get(CHILD_ENV)) if emit_results is None else emit_results
        self.tz = cfg.schedule.tz
        self._digits = 5

    # ---------------------------------------------------------------- helpers

    def fmt(self, ts: float) -> str:
        return clk.fmt_ts(ts, self.tz)

    def notify(self, text: str) -> None:
        self.notifier.send(text)

    def sync_clock(self, stage: str) -> None:
        s = self.cfg.schedule
        self.clock.calibrate()
        if not s.use_ntp_offset:
            if stage == "startup":
                log.info("NTP correction is off; using the PC clock as it is")
            return
        offset = self.ntp(s.ntp_server)
        if offset is None:
            log.warning("NTP (%s) unreachable at %s; keeping the current offset %+.1f ms",
                        s.ntp_server, stage, self.clock.offset_s * 1000)
            return
        self.clock.offset_s = offset
        ms = offset * 1000.0
        side = "behind" if ms > 0 else "ahead of"
        if abs(ms) > s.max_clock_offset_ms:
            msg = (f"PC clock is {abs(ms):.0f} ms {side} NTP time (> {s.max_clock_offset_ms:g} ms). "
                   "Scheduling is corrected, but fix Windows time sync (see README).")
            log.warning(msg)
            self.notify("WARNING: " + msg)
        else:
            log.info("Clock check (%s): PC is %.1f ms %s %s - correction applied", stage, abs(ms), side,
                     s.ntp_server)

    def log_header(self, mode: str) -> None:
        c, t, s = self.cfg, self.cfg.trade, self.cfg.schedule
        log.info("=" * 72)
        log.info("hedgebot %s | Python %s | %s | config %s", __version__, platform.python_version(), mode,
                 c.source)
        log.info("App '%s': %s %g %s, TP %s, SL %s, login %s, terminal %s", c.name, c.direction.upper(),
                 t.lots, t.symbol, t.describe_tp("(account ccy)"), t.describe_sl("(account ccy)"),
                 c.account.login, c.account.terminal_path)
        log.info("Schedule: %s %s on %s; prearm %gs, max late %d ms; times shown in %s", s.time.isoformat(),
                 s.timezone, s.describe_days(), s.prearm_seconds, s.max_late_ms, s.timezone)
        for w in c.warnings:
            log.warning("Config: %s", w)

    def _emit(self, target_ts: float, status: str, res: Optional[SendResult] = None) -> None:
        if not self.emit_results:
            return
        payload = {
            "name": self.cfg.name,
            "direction": self.cfg.direction,
            "status": status,
            "target_ts": target_ts,
            "send_ts": res.send_ts if res else None,
            "send_pc_ts": res.send_pc_ts if res else None,
            "deviation_ms": res.deviation_ms if res else None,
            "latency_ms": res.latency_ms if res else None,
            "requested_price": res.requested_price if res else None,
            "fill_price": res.fill_price if res else None,
            "clock_offset_ms": self.clock.offset_s * 1000.0,
        }
        print(RESULT_PREFIX + json.dumps(payload), flush=True)

    def _journal(self, target_ts: float, status: str, note: str = "",
                 res: Optional[SendResult] = None, close: Optional[CloseInfo] = None) -> None:
        c, d = self.cfg, self._digits
        row = {
            "date": datetime.fromtimestamp(target_ts, self.tz).date().isoformat(),
            "app_name": c.name,
            "login": c.account.login,
            "direction": c.direction,
            "symbol": c.trade.symbol,
            "lots": f"{c.trade.lots:g}",
            "target_time": self.fmt(target_ts),
            "status": status,
            "note": note,
        }
        if res is not None:
            row.update({
                "send_time": self.fmt(res.send_ts) if res.send_ts else "",
                "deviation_ms": _f(res.deviation_ms, 1),
                "latency_ms": _f(res.latency_ms, 1),
                "spread_points": _f(res.spread_points, 0),
                "requested_price": _f(res.requested_price, d),
                "fill_price": _f(res.fill_price, d),
                "slippage_points": _f(res.slippage_points, 1),
                "tp_price": _f(res.tp_price, d),
                "sl_price": _f(res.sl_price, d),
                "position_ticket": res.position_ticket or "",
                "attempts": res.attempts,
            })
        if close is not None:
            row.update({
                "close_time": self.fmt(close.close_ts) if close.close_ts else "",
                "close_reason": close.reason,
                "close_price": _f(close.close_price, d),
                "profit": _f(close.profit, 2),
                "swap": _f(close.swap, 2),
                "commission": _f(close.commission, 2),
                "duration_s": _f(close.duration_s, 0),
                "max_adverse": _f(close.max_adverse, 2),
                "max_favourable": _f(close.max_favourable, 2),
            })
        try:
            path = self.journal.upsert(row)
            log.info("Journal row written to %s (%s%s)", path, status, f", {close.reason}" if close else "")
        except Exception as exc:  # the journal must never break trading
            log.error("Could not write the journal: %s", exc)

    def monitor_until(self, target_ts: float) -> float:
        target_local = datetime.fromtimestamp(target_ts, self.tz)
        until = clk.localize(target_local.date(), self.cfg.monitor.until, self.tz)
        if until.timestamp() <= target_ts:
            until = clk.localize(target_local.date() + timedelta(days=1), self.cfg.monitor.until, self.tz)
        return until.timestamp()

    def _log_preflight(self, pf: Preflight) -> None:
        t, d = self.cfg.trade, pf.digits
        log.info("Symbol %s: digits %d, tick %s = %.2f %s/lot, filling %s, stops level %d points",
                 pf.symbol, d, pf.tick_size, pf.tick_value, pf.currency, pf.filling_name, pf.stops_level)
        log.info("Price now: bid %.*f ask %.*f, spread %g points", d, pf.bid, d, pf.ask, pf.spread_points)
        for label, money, points, dist, preview in (
            ("TP", t.tp_money, t.tp_points, pf.tp_distance, pf.tp_preview),
            ("SL", t.sl_money, t.sl_points, pf.sl_distance, pf.sl_preview),
        ):
            if dist is None:
                log.info("%s: none", label)
            else:
                amount = f"{money:.2f} {pf.currency}" if money is not None else f"{points:g} points"
                log.info("%s %s = %.*f price distance (%.0f points); at the current price %s would be %.*f",
                         label, amount, d, dist, dist / pf.point, label, d, preview)
        if pf.margin_required is not None:
            log.info("Margin required %.2f %s, free margin %.2f %s", pf.margin_required, pf.currency,
                     pf.margin_free, pf.currency)
        if pf.margin_warning:
            log.error("MARGIN: %s", pf.margin_warning)
        log.info("order_check OK; order pre-built (%s %g %s, deviation %d, magic %d)",
                 self.cfg.direction.upper(), t.lots, pf.symbol, t.deviation_points, t.magic)

    # ---------------------------------------------------------------- modes

    def run_check(self) -> int:
        self.log_header("CHECK")
        self.sync_clock("startup")
        try:
            target = clk.next_target(self.clock.now(), self.cfg.schedule)
            log.info("Next scheduled target: %s (%s), in %s", self.fmt(target.timestamp()),
                     clk.fmt_utc(target.timestamp()), clk.fmt_duration(target.timestamp() - self.clock.time()))
        except clk.NoTargetError as exc:
            target = None
            log.warning("%s", exc)
        client = None
        try:
            client = self.client_factory(self.cfg, self.clock, self.sleep)
            client.connect()
            client.check_account()
            pf = client.preflight(strict_market=False)
            self._digits = pf.digits
            self._log_preflight(pf)
            day = (target or self.clock.now()).astimezone(self.tz).date()
            reason = client.existing_trade(day, self.tz, pf.server_offset_s)
            if reason:
                log.warning("On %s the app would SKIP: %s", day.isoformat(), reason)
            log.info("CHECK PASSED for %s (%s account %s). Nothing was sent.", self.cfg.name, pf.account_kind,
                     self.cfg.account.login)
            return EXIT_OK
        except Mt5Error as exc:
            log.error("CHECK FAILED: %s", exc)
            return EXIT_ERROR
        finally:
            if client is not None:
                client.shutdown()

    def run_once(self, target: Optional[datetime] = None) -> int:
        """One scheduled day: wait, prearm, fire, correct TP, monitor, journal."""
        s = self.cfg.schedule
        self.sync_clock("startup")
        if target is None:
            try:
                target = clk.next_target(self.clock.now(), s)
            except clk.NoTargetError as exc:
                log.error("%s", exc)
                return EXIT_CONFIG
        target_ts = target.timestamp()
        log.info("Target %s (%s) in %s [%s]", self.fmt(target_ts), clk.fmt_utc(target_ts),
                 clk.fmt_duration(target_ts - self.clock.time()), "DRY-RUN" if self.dry_run else "LIVE")

        prearm_ts = target_ts - s.prearm_seconds
        if self.clock.time() < prearm_ts:
            log.info("Waiting until prearm at %s", self.fmt(prearm_ts))
            while self.clock.time() < prearm_ts:
                step_to = min(prearm_ts, self.clock.time() + 3600)
                clk.wait_until(self.clock, step_to, fine_window_s=0.0, sleep=self.sleep)
                if self.clock.time() < prearm_ts:
                    log.info("Still waiting: target in %s", clk.fmt_duration(target_ts - self.clock.time()))

        late = clk.late_ms(self.clock.time(), target_ts)
        if late > s.max_late_ms:
            msg = (f"woke up {late / 1000:.1f} s after the target {self.fmt(target_ts)} "
                   "(PC asleep or busy?) - not opening")
            log.error("SKIPPED: %s", msg)
            self.notify("SKIPPED: " + msg)
            self._journal(target_ts, "skipped_late", msg)
            self._emit(target_ts, "skipped_late")
            return EXIT_SKIPPED
        return self._armed_run(target_ts)

    def _armed_run(self, target_ts: float) -> int:
        c, s = self.cfg, self.cfg.schedule
        log.info("PREARM: %.1f s before the target", target_ts - self.clock.time())
        self.sync_clock("prearm")
        if self.boost:
            clk.boost_priority()
        client = None
        res: Optional[SendResult] = None
        close: Optional[CloseInfo] = None
        journaled = False
        try:
            client = self.client_factory(c, self.clock, self.sleep)
            client.connect()
            client.check_account()
            pf = client.preflight()
            self._digits = pf.digits
            self._log_preflight(pf)
            if pf.margin_warning:
                self.notify("WARNING: " + pf.margin_warning)

            target_day = datetime.fromtimestamp(target_ts, self.tz).date()
            reason = (client.existing_trade(target_day, self.tz, pf.server_offset_s)
                      or self.journal.opened_on(c.name, c.account.login, target_day))
            if reason:
                log.warning("SKIPPED (one position per day): %s", reason)
                self.notify(f"SKIPPED: {reason}")
                self._journal(target_ts, "skipped_exists", reason)
                journaled = True
                self._emit(target_ts, "skipped_exists")
                return EXIT_SKIPPED

            lead = target_ts - self.clock.time()
            log.info("ARMED: %s %g %s at %s (in %.1f s)%s", c.direction.upper(), c.trade.lots, pf.symbol,
                     self.fmt(target_ts), lead, " [DRY-RUN: order_check only]" if self.dry_run else "")
            if lead < 2:
                log.warning("Prearm finished only %.1f s before the target; consider a larger prearm_seconds",
                            lead)
            self.notify(f"ARMED: {c.direction.upper()} {c.trade.lots:g} {pf.symbol} at {self.fmt(target_ts)}, "
                        f"TP {c.trade.describe_tp(pf.currency)}, SL {c.trade.describe_sl(pf.currency)}"
                        + (" (dry-run)" if self.dry_run else ""))

            with self.notifier.quiet():
                clk.wait_until(self.clock, target_ts, fine_window_s=1.0, on_tick=client.warm_tick,
                               tick_every_s=0.5, sleep=self.sleep)
                res = client.fire(pf, target_ts, dry_run=self.dry_run, max_late_ms=s.max_late_ms,
                                  max_spread_points=c.trade.max_spread_points)

            self._log_send(res, pf)
            self._emit(target_ts, res.status, res)

            if res.status == "dry_run":
                self.notify(f"DRY-RUN OK: order_check passed at {self.fmt(res.send_ts)} "
                            f"(deviation {res.deviation_ms:+.1f} ms)")
                self._journal(target_ts, "dry_run", "order_check only, nothing sent", res)
                journaled = True
                return EXIT_OK
            if res.status.startswith("skipped"):
                log.error("SKIPPED: %s", res.comment)
                self.notify(f"SKIPPED: {res.comment}")
                self._journal(target_ts, res.status, res.comment, res)
                journaled = True
                return EXIT_SKIPPED
            if not res.opened:
                log.error("ORDER FAILED: %s %s", tm.retcode_text(res.retcode), res.comment)
                self.notify(f"ERROR: order failed - {tm.retcode_text(res.retcode)} {res.comment}")
                self._journal(target_ts, "failed", f"{tm.retcode_text(res.retcode)} {res.comment}", res)
                journaled = True
                return EXIT_ERROR

            tp_error = client.correct_tp_sl(pf, res)
            d = pf.digits
            self.notify(
                f"OPENED {c.direction.upper()} {res.volume or c.trade.lots:g} {pf.symbol} at "
                f"{_f(res.fill_price, d)} (requested {_f(res.requested_price, d)}, slippage "
                f"{_f(res.slippage_points, 1) or '-'} pts), TP {_f(res.tp_price, d) or 'none'}, "
                f"SL {_f(res.sl_price, d) or 'none'}, sent {_f(res.deviation_ms, 1)} ms vs target, "
                f"round-trip {_f(res.latency_ms, 0)} ms"
            )
            if tp_error:
                self.notify("ERROR: " + tp_error)
            # Record the trade now; the close details are filled into the same row later.
            self._journal(target_ts, "opened", tp_error or ("monitoring" if c.monitor.enabled else ""), res)
            if not c.monitor.enabled:
                journaled = True
                return EXIT_OK

            if c.monitor.enabled and not self.dry_run:
                until = self.monitor_until(target_ts)
                log.info("Monitoring until %s", self.fmt(until))
                try:
                    close = client.monitor(res, until, c.monitor.poll_seconds)
                except KeyboardInterrupt:
                    close = CloseInfo(reason="still open (monitor interrupted)")
                    raise
                if close.closed:
                    log.info("CLOSED by %s at %s: profit %s, swap %s, commission %s, duration %s, "
                             "max favourable %+.2f, max adverse %+.2f", close.reason,
                             _f(close.close_price, d), _f(close.profit, 2), _f(close.swap, 2),
                             _f(close.commission, 2), clk.fmt_duration(close.duration_s or 0),
                             close.max_favourable, close.max_adverse)
                    self.notify(f"CLOSED by {close.reason} at {_f(close.close_price, d)}: profit "
                                f"{_f(close.profit, 2)} {pf.currency}, swap {_f(close.swap, 2)}, commission "
                                f"{_f(close.commission, 2)}, duration {clk.fmt_duration(close.duration_s or 0)}")
                else:
                    self.notify(f"Monitor ended, position #{res.position_ticket} still open "
                                f"(P/L {_f(close.last_profit, 2)} {pf.currency})")
            self._journal(target_ts, "opened", tp_error or "", res, close)
            journaled = True
            return EXIT_OK
        except Mt5Error as exc:
            log.error("ERROR: %s", exc)
            self.notify(f"ERROR: {exc}")
            if not journaled and not (res and res.opened):
                self._journal(target_ts, "error", str(exc), res)
                journaled = True
            if res is None:
                self._emit(target_ts, "error")
            return EXIT_ERROR
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # a bug must not lose the record of an open position
            log.exception("Unexpected error: %s", exc)
            self.notify(f"ERROR (unexpected): {exc}")
            if res is None:
                self._emit(target_ts, "error")
            return EXIT_ERROR
        finally:
            if res is not None and res.opened and not journaled:
                self._journal(target_ts, "opened", "run ended early; check the position in MT5", res, close)
            if client is not None:
                client.shutdown()

    def _log_send(self, res: SendResult, pf: Preflight) -> None:
        d = pf.digits
        if res.send_ts is None:
            return
        log.info("TIMING: target %s | sent %s | deviation %+.1f ms | round-trip %.1f ms | attempts %d",
                 self.fmt(res.target_ts), self.fmt(res.send_ts), res.deviation_ms, res.latency_ms, res.attempts)
        log.info("PRICES: spread %g points | requested %.*f | filled %s | slippage %s points | TP %s | SL %s",
                 res.spread_points, d, res.requested_price, _f(res.fill_price, d) or "-",
                 _f(res.slippage_points, 1) or "-", _f(res.tp_price, d) or "none", _f(res.sl_price, d) or "none")
        if res.opened:
            log.info("OPENED position #%s (%s)", res.position_ticket, tm.retcode_text(res.retcode))

    def run_loop(self) -> int:
        log.info("Loop mode: trading on every scheduled day. Press Ctrl+C to stop.")
        last_target_ts: Optional[float] = None
        while True:
            now = self.clock.now()
            if last_target_ts is not None:
                now = max(now, datetime.fromtimestamp(last_target_ts + 0.001, UTC))
            try:
                target = clk.next_target(now, self.cfg.schedule)
            except clk.NoTargetError as exc:
                log.error("%s", exc)
                return EXIT_CONFIG
            last_target_ts = target.timestamp()
            try:
                code = self.run_once(target)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception("Run for %s crashed: %s", self.fmt(last_target_ts), exc)
                self.notify(f"ERROR (unexpected): {exc}")
                code = EXIT_ERROR
            log.info("Day %s finished with exit code %d; moving to the next scheduled day",
                     datetime.fromtimestamp(last_target_ts, self.tz).date().isoformat(), code)


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hedgebot",
        description="Open one BUY or SELL market position at an exact scheduled time on one MT5 terminal.",
    )
    p.add_argument("--config", required=True, help="YAML config, e.g. config/buy.yaml")
    p.add_argument("--env", help="path to the .env file (default: .env in the project folder)")
    p.add_argument("--dry-run", action="store_true",
                   help="do everything, but call order_check() instead of order_send() (nothing is placed)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="connect, run the preflight checks, print TP/margin, then exit")
    mode.add_argument("--loop", action="store_true", help="keep running and trade on every scheduled day")
    mode.add_argument("--test-in", type=float, metavar="SECONDS",
                      help="ignore the schedule: target = now + SECONDS (for DEMO testing)")
    mode.add_argument("--target-at", metavar="ISO_TIME", help=argparse.SUPPRESS)  # set by run_pair.py
    p.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"), help="log folder (default: logs/)")
    p.add_argument("--journal", default=str(PROJECT_ROOT / "data" / "journal.csv"),
                   help="journal CSV (default: data/journal.csv)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def parse_target_at(text: str) -> datetime:
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("--target-at needs a timezone, e.g. 2026-11-16T13:29:45+00:00")
    return dt


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test_in is not None and args.test_in <= 0:
        print("--test-in must be a positive number of seconds", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_config(args.config, env_file=args.env)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg, Path(args.log_dir), args.verbose)
    clk.enable_high_resolution_timer()
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id, prefix=cfg.name)
    app = HedgeApp(cfg, dry_run=args.dry_run, notifier=notifier, journal=Journal(args.journal))
    try:
        if args.check:
            return app.run_check()
        mode = "LOOP" if args.loop else ("TEST-IN" if args.test_in or args.target_at else "SCHEDULED")
        app.log_header(mode + (" DRY-RUN" if args.dry_run else ""))
        if args.loop:
            return app.run_loop()
        target = None
        if args.target_at:
            try:
                target = parse_target_at(args.target_at).astimezone(cfg.schedule.tz)
            except ValueError as exc:
                log.error("Bad --target-at: %s", exc)
                return EXIT_CONFIG
        elif args.test_in is not None:
            target = (app.clock.now() + timedelta(seconds=args.test_in)).astimezone(cfg.schedule.tz)
            log.info("TEST MODE: schedule ignored, target = now + %g s", args.test_in)
        return app.run_once(target)
    except KeyboardInterrupt:
        log.warning("Interrupted by user (Ctrl+C). Any open position stays in MT5 with its TP/SL.")
        return EXIT_INTERRUPTED
    finally:
        notifier.flush(10.0)
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
