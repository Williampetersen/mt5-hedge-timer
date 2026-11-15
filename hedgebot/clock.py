"""Time handling: next target in the schedule timezone, NTP offset, precise waiting.

Nothing here reads the Windows timezone setting. Targets are built from the
IANA zone in the config (zoneinfo + tzdata) and compared as absolute UTC
instants, so a VPS whose clock runs in UTC behaves exactly like a PC in
Copenhagen.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("hedgebot.clock")
UTC = timezone.utc


class NoTargetError(RuntimeError):
    """No future trading day matches the schedule."""


# --------------------------------------------------------------------------- targets


def localize(day: date, at: dtime, tz: ZoneInfo) -> datetime:
    """Wall-clock `day at` in `tz` as an aware datetime.

    A time inside a spring-forward gap (e.g. 02:30 on the DST change day) is
    moved forward by the gap; an ambiguous autumn time uses its first
    occurrence (summer time).
    """
    naive = datetime.combine(day, at)
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC).astimezone(tz)


def is_trading_day(day: date, schedule) -> bool:
    """`schedule.dates`, when not empty, is the complete list of trading days."""
    if schedule.dates:
        return day in schedule.dates
    return day.weekday() in schedule.weekdays


def next_target(now: datetime, schedule, *, horizon_days: int = 800) -> datetime:
    """First scheduled instant strictly after `now`, as an aware datetime in the schedule zone."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    tz = schedule.tz
    now_ts = now.timestamp()
    day = now.astimezone(tz).date()
    for _ in range(horizon_days + 1):
        if is_trading_day(day, schedule):
            target = localize(day, schedule.time, tz)
            # compare instants, not wall clocks (same-zone datetimes compare naively)
            if target.timestamp() > now_ts:
                return target
        day += timedelta(days=1)
    raise NoTargetError(
        "No future trading day matches the schedule (are all schedule.dates in the past?)"
    )


def late_ms(now_ts: float, target_ts: float) -> float:
    return (now_ts - target_ts) * 1000.0


def is_too_late(now_ts: float, target_ts: float, max_late_ms: float) -> bool:
    """True when it is more than max_late_ms after the target: do NOT open."""
    return late_ms(now_ts, target_ts) > max_late_ms


# --------------------------------------------------------------------------- NTP


def measure_ntp_offset(
    server: str, *, samples: int = 3, timeout_s: float = 2.0, client=None
) -> Optional[float]:
    """Seconds to ADD to the PC clock to get true time (positive = PC is behind).

    Takes a few samples and keeps the one with the smallest network delay.
    Returns None when the server cannot be reached.
    """
    if client is None:
        import ntplib

        client = ntplib.NTPClient()
    best = None
    last_error: Optional[Exception] = None
    for _ in range(max(1, samples)):
        try:
            resp = client.request(server, version=3, timeout=timeout_s)
        except Exception as exc:  # socket errors, timeouts, NTPException
            last_error = exc
            continue
        if best is None or resp.delay < best.delay:
            best = resp
    if best is None:
        log.warning("NTP server %s not reachable (%s)", server, last_error)
        return None
    return float(best.offset)


# --------------------------------------------------------------------------- clock


class Clock:
    """High-resolution wall clock with an optional NTP correction.

    time.time() can tick in 15.6 ms steps on Windows with older Pythons, so the
    clock anchors on the moment time.time() changes and then advances with
    perf_counter(). calibrate() refreshes the anchor (cheap; call it now and then).
    """

    def __init__(self, offset_s: float = 0.0, *, wall=time.time, perf=time.perf_counter):
        self.offset_s = float(offset_s)
        self._wall = wall
        self._perf = perf
        self._anchor_wall = 0.0
        self._anchor_perf = 0.0
        self.calibrate()

    def calibrate(self, max_wait_s: float = 0.05) -> None:
        w0 = self._wall()
        deadline = self._perf() + max_wait_s
        while True:
            p = self._perf()
            w = self._wall()
            if w != w0 or p >= deadline:
                break
        self._anchor_wall, self._anchor_perf = w, p

    def pc_time(self) -> float:
        """PC clock (epoch seconds) without the NTP correction, high resolution."""
        return self._anchor_wall + (self._perf() - self._anchor_perf)

    def time(self) -> float:
        """Corrected epoch seconds: PC clock + NTP offset."""
        return self.pc_time() + self.offset_s

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.time(), UTC)


def wait_until(
    clock,
    target_ts: float,
    *,
    fine_window_s: float = 1.0,
    max_chunk_s: float = 30.0,
    on_tick: Optional[Callable[[], None]] = None,
    tick_every_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    recalibrate_every_s: float = 60.0,
) -> float:
    """Block until clock.time() >= target_ts and return how late we are, in ms.

    Coarse phase: plain sleeps (no CPU use) until `fine_window_s` before the
    target, calling `on_tick` every `tick_every_s` if given (used to keep the
    MT5 price feed warm). Fine phase: 1 ms sleeps, then a yielding spin for the
    final 20 ms. If the PC slept through the target the function simply returns
    a large positive lateness; the caller decides what to do.
    """
    next_cal = clock.time() + recalibrate_every_s
    while True:
        remaining = target_ts - clock.time()
        if remaining <= fine_window_s:
            break
        if on_tick is not None:
            try:
                on_tick()
            except Exception as exc:  # warm-up is best effort
                log.debug("warm-up tick failed: %s", exc)
            chunk = min(remaining - fine_window_s, tick_every_s)
        else:
            chunk = min(remaining - fine_window_s, max_chunk_s)
        sleep(max(chunk, 0.0))
        if clock.time() >= next_cal:
            clock.calibrate()
            next_cal = clock.time() + recalibrate_every_s
    if fine_window_s > 0 and target_ts - clock.time() > 0.1:
        clock.calibrate()  # fresh anchor right before the precise phase
    while True:
        remaining = target_ts - clock.time()
        if remaining <= 0:
            break
        sleep(0.001 if remaining > 0.02 else 0)
    return late_ms(clock.time(), target_ts)


# --------------------------------------------------------------------------- formatting


def fmt_ts(ts: float, tz) -> str:
    """'2026-11-16 14:29:45.003 CET'"""
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} {dt.tzname() or ''}".rstrip()


def fmt_utc(ts: float) -> str:
    return fmt_ts(ts, UTC)


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} s"
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


# --------------------------------------------------------------------------- Windows timing helpers


def enable_high_resolution_timer() -> bool:
    """Ask Windows for 1 ms timer resolution (best effort, released at exit)."""
    if sys.platform != "win32":
        return False
    try:
        import atexit
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        if winmm.timeBeginPeriod(1) == 0:
            atexit.register(winmm.timeEndPeriod, 1)
            return True
    except Exception as exc:
        log.debug("timeBeginPeriod failed: %s", exc)
    return False


def boost_priority() -> bool:
    """Raise this process to HIGH priority so the trigger is not delayed (best effort)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080))  # HIGH_PRIORITY_CLASS
    except Exception as exc:
        log.debug("SetPriorityClass failed: %s", exc)
        return False
