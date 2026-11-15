import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import make_cfg
from hedgebot import clock as clk

CPH = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc


def sched(buy_raw, **overrides):
    buy_raw["schedule"].update(overrides)
    return make_cfg(buy_raw).schedule


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_same_day_target_in_winter(buy_raw):
    s = sched(buy_raw)
    # Mon 2026-11-16 09:00 UTC = 10:00 CET -> today 14:29:45 CET = 13:29:45 UTC
    t = clk.next_target(utc(2026, 11, 16, 9, 0), s)
    assert t.astimezone(UTC) == utc(2026, 11, 16, 13, 29, 45)
    assert t.tzname() == "CET"


def test_same_day_target_in_summer(buy_raw):
    s = sched(buy_raw)
    t = clk.next_target(utc(2026, 6, 15, 9, 0), s)  # Monday, CEST
    assert t.astimezone(UTC) == utc(2026, 6, 15, 12, 29, 45)
    assert t.tzname() == "CEST"


def test_after_target_goes_to_next_day(buy_raw):
    s = sched(buy_raw)
    t = clk.next_target(utc(2026, 11, 16, 14, 0), s)  # 15:00 CET Monday
    assert t.date() == date(2026, 11, 17)


def test_exactly_at_target_goes_to_next_day_and_1ms_before_stays(buy_raw):
    s = sched(buy_raw)
    at = utc(2026, 11, 16, 13, 29, 45)
    assert clk.next_target(at, s).date() == date(2026, 11, 17)
    assert clk.next_target(at - timedelta(milliseconds=1), s).date() == date(2026, 11, 16)


def test_weekend_is_skipped(buy_raw):
    s = sched(buy_raw)
    t = clk.next_target(utc(2026, 11, 20, 14, 0), s)  # Friday after the target
    assert t.date() == date(2026, 11, 23) and t.weekday() == 0


def test_weekday_filter(buy_raw):
    s = sched(buy_raw, weekdays=["Wed"])
    t = clk.next_target(utc(2026, 11, 16, 9, 0), s)
    assert t.date() == date(2026, 11, 18)


def test_dates_only(buy_raw):
    s = sched(buy_raw, dates=["2026-12-05", "2026-12-10"])
    # 2026-12-05 is a Saturday: explicit dates override weekdays
    t = clk.next_target(utc(2026, 11, 16, 9, 0), s)
    assert t.date() == date(2026, 12, 5)
    t2 = clk.next_target(t.astimezone(UTC) + timedelta(seconds=1), s)
    assert t2.date() == date(2026, 12, 10)


def test_all_dates_in_past(buy_raw):
    s = sched(buy_raw, dates=["2026-01-05"])
    with pytest.raises(clk.NoTargetError):
        clk.next_target(utc(2026, 11, 16, 9, 0), s)


# --- DST change days in Europe/Copenhagen (2026-03-29 and 2026-10-25 are Sundays)

ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_spring_forward_day(buy_raw):
    s = sched(buy_raw, weekdays=ALL_DAYS)
    sat = clk.next_target(utc(2026, 3, 28, 6, 0), s)
    sun = clk.next_target(utc(2026, 3, 28, 20, 0), s)
    assert sat.astimezone(UTC) == utc(2026, 3, 28, 13, 29, 45)  # CET +1
    assert sun.astimezone(UTC) == utc(2026, 3, 29, 12, 29, 45)  # CEST +2
    assert (sat.tzname(), sun.tzname()) == ("CET", "CEST")


def test_fall_back_day(buy_raw):
    s = sched(buy_raw, weekdays=ALL_DAYS)
    sat = clk.next_target(utc(2026, 10, 24, 6, 0), s)
    sun = clk.next_target(utc(2026, 10, 24, 20, 0), s)
    assert sat.astimezone(UTC) == utc(2026, 10, 24, 12, 29, 45)  # CEST
    assert sun.astimezone(UTC) == utc(2026, 10, 25, 13, 29, 45)  # CET
    assert sun.astimezone(UTC) - sat.astimezone(UTC) == timedelta(hours=25)


def test_weekdays_around_dst(buy_raw):
    s = sched(buy_raw)  # Mon-Fri
    fri = clk.next_target(utc(2026, 3, 27, 6, 0), s)
    mon = clk.next_target(utc(2026, 3, 27, 20, 0), s)
    assert fri.astimezone(UTC) == utc(2026, 3, 27, 13, 29, 45)
    assert mon.astimezone(UTC) == utc(2026, 3, 30, 12, 29, 45)


def test_time_inside_spring_gap_moves_forward():
    t = clk.localize(date(2026, 3, 29), dtime(2, 30), CPH)
    assert t.astimezone(UTC) == utc(2026, 3, 29, 1, 30)
    assert (t.hour, t.minute, t.tzname()) == (3, 30, "CEST")


def test_ambiguous_autumn_time_uses_first_occurrence():
    t = clk.localize(date(2026, 10, 25), dtime(2, 30), CPH)
    assert t.astimezone(UTC) == utc(2026, 10, 25, 0, 30)
    assert t.tzname() == "CEST"


def test_pc_in_utc_or_any_zone_gives_same_target(buy_raw):
    """A VPS running in UTC (or any zone) must get the same absolute instant."""
    s = sched(buy_raw)
    now_utc = utc(2026, 11, 16, 9, 0)
    expected = clk.next_target(now_utc, s)
    for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        assert clk.next_target(now_utc.astimezone(ZoneInfo(zone)), s) == expected


def test_naive_now_is_rejected(buy_raw):
    with pytest.raises(ValueError):
        clk.next_target(datetime(2026, 11, 16, 9, 0), sched(buy_raw))


# --- lateness / waiting


def test_too_late_logic():
    target = 1_000_000.0
    assert not clk.is_too_late(target + 1.999, target, 2000)
    assert not clk.is_too_late(target + 2.000, target, 2000)
    assert clk.is_too_late(target + 2.001, target, 2000)
    assert not clk.is_too_late(target - 5, target, 0)


class FakeClock:
    def __init__(self, t):
        self.t = t
        self.offset_s = 0.0
        self.sleeps = []

    def time(self):
        return self.t

    def pc_time(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t, UTC)

    def calibrate(self):
        pass

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += max(s, 0.0001)


def test_wait_until_with_fake_clock_never_overshoots_much():
    c = FakeClock(1000.0)
    late = clk.wait_until(c, 1100.0, sleep=c.sleep)
    assert 0 <= late < 1.0
    assert max(c.sleeps) <= 30.0  # coarse sleeps are chunked


def test_wait_until_calls_warmup_ticks():
    c = FakeClock(1000.0)
    ticks = []
    clk.wait_until(c, 1005.0, on_tick=lambda: ticks.append(c.t), tick_every_s=0.5, sleep=c.sleep)
    assert 7 <= len(ticks) <= 9  # every 0.5 s until 1 s before


def test_wait_until_reports_lateness_after_pc_sleep():
    c = FakeClock(1000.0)

    def sleepy(s):
        c.t += 3600  # the PC was suspended for an hour

    late = clk.wait_until(c, 1100.0, sleep=sleepy)
    assert clk.is_too_late(c.time(), 1100.0, 2000)
    assert late > 3_000_000


def test_wait_until_real_clock_precision():
    c = clk.Clock()
    target = c.time() + 0.15
    late = clk.wait_until(c, target)
    assert 0 <= late < 20  # generous for busy CI machines; typically < 1 ms


def test_clock_applies_offset():
    c = clk.Clock(offset_s=2.5)
    assert abs((c.time() - c.pc_time()) - 2.5) < 1e-3
    assert abs(c.pc_time() - time.time()) < 0.05


def test_ntp_offset_picks_lowest_delay_sample():
    replies = iter([
        SimpleNamespace(offset=0.30, delay=0.20),
        SimpleNamespace(offset=0.12, delay=0.02),
        SimpleNamespace(offset=0.50, delay=0.40),
    ])
    client = SimpleNamespace(request=lambda *a, **k: next(replies))
    assert clk.measure_ntp_offset("x", client=client) == pytest.approx(0.12)


def test_ntp_offset_unreachable_returns_none():
    def boom(*a, **k):
        raise OSError("no network")

    assert clk.measure_ntp_offset("x", client=SimpleNamespace(request=boom)) is None


def test_formatting():
    ts = utc(2026, 11, 16, 13, 29, 45).timestamp() + 0.0034
    assert clk.fmt_ts(ts, CPH) == "2026-11-16 14:29:45.003 CET"
    assert clk.fmt_duration(42.25) == "42.2 s" or clk.fmt_duration(42.25) == "42.3 s"
    assert clk.fmt_duration(3725) == "1h 02m 05s"
