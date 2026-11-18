"""End-to-end runs of HedgeApp with a fake MT5 and a fake clock."""

from datetime import datetime, timezone

import pytest

from conftest import make_cfg
from fake_mt5 import FakeClock, FakeMT5
from hedgebot.__main__ import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, EXIT_SKIPPED, HedgeApp, main, parse_target_at
from hedgebot.journal import Journal
from hedgebot.mt5_client import Mt5Client
from hedgebot.notify import Notifier

T0 = datetime(2026, 11, 16, 13, 29, 45, tzinfo=timezone.utc)  # 14:29:45 CET, Monday


def make_app(cfg, tmp_path, *, start, mt5=None, dry_run=False, check_first=False):
    clock = FakeClock(start)
    mt5 = mt5 or FakeMT5(clock)
    mt5.clock = clock
    journal = Journal(tmp_path / "journal.csv")
    app = HedgeApp(
        cfg,
        dry_run=dry_run,
        notifier=Notifier(),
        journal=journal,
        clock=clock,
        client_factory=lambda c, clock, sleep: Mt5Client(c, clock=clock, mt5=mt5, sleep=sleep),
        ntp=lambda server: 0.0,
        sleep=clock.sleep,
        boost=False,
        emit_results=False,
        check_first=check_first,
    )
    return app, mt5, clock, journal


def record_connects(mt5, clock, takes_s=0.0):
    """Note the (fake) time of every connect; `takes_s` makes MetaTrader 5 slow to start."""
    times = []
    real_initialize = mt5.initialize

    def initialize(*args, **kwargs):
        times.append(clock.t)
        clock.t += takes_s
        return real_initialize(*args, **kwargs)

    mt5.initialize = initialize
    return times


@pytest.fixture
def no_monitor_cfg(buy_raw):
    buy_raw["monitor"]["enabled"] = False
    return make_cfg(buy_raw)


def test_full_scheduled_run_opens_once(no_monitor_cfg, tmp_path):
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 3 * 3600)
    assert app.run_once() == EXIT_OK
    orders = [r for r in mt5.sent if r["action"] == 1]
    assert len(orders) == 1
    rows = journal.rows()
    assert len(rows) == 1 and rows[0]["status"] == "opened"
    assert rows[0]["target_time"] == "2026-11-16 14:29:45.000 CET"
    assert rows[0]["send_time"].startswith("2026-11-16 14:29:45.")
    assert abs(float(rows[0]["deviation_ms"])) < 5
    assert rows[0]["tp_price"] == "2675.30" and rows[0]["fill_price"] == "2650.30"


def test_there_is_no_limit_per_day(no_monitor_cfg, tmp_path):
    """Every run opens one position, also on the same day and while earlier ones are still open."""
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 60)
    assert app.run_once() == EXIT_OK
    for _ in range(3):  # three more runs, each a minute after the one before
        target = datetime.fromtimestamp(clock.t + 60, timezone.utc)
        assert app.run_once(target) == EXIT_OK
    orders = [r for r in mt5.sent if r["action"] == 1]
    assert len(orders) == 4 and len(mt5.positions) == 4
    rows = journal.rows()
    assert [r["status"] for r in rows] == ["opened"] * 4
    assert len({r["position_ticket"] for r in rows}) == 4  # one journal row per position
    assert len({r["date"] for r in rows}) == 1  # all on the same day


def test_woke_up_too_late_does_not_open(no_monitor_cfg, tmp_path):
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 3600)
    real_sleep = clock.sleep

    def suspended(s):  # the PC sleeps through the target
        real_sleep(s)
        if clock.t > T0.timestamp() - 100:
            clock.t = T0.timestamp() + 600

    app.sleep = suspended
    assert app.run_once(T0) == EXIT_SKIPPED
    assert not mt5.sent and not mt5.initialized
    assert journal.rows()[0]["status"] == "skipped_late"


def test_check_first_tests_everything_at_once_and_again_before_the_target(no_monitor_cfg, tmp_path):
    start = T0.timestamp() - 2 * 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)
    connects = record_connects(mt5, clock)
    assert app.run_once(T0) == EXIT_OK
    assert connects[0] == pytest.approx(start, abs=1)  # connected the moment the run started
    assert connects[1] == pytest.approx(T0.timestamp() - 30, abs=1)  # and again at the prearm
    assert len(connects) == 2 and len(mt5.checked) == 2  # every check ran both times
    assert len([r for r in mt5.sent if r["action"] == 1]) == 1  # one order, at the target
    rows = journal.rows()
    assert len(rows) == 1 and rows[0]["status"] == "opened"
    assert rows[0]["send_time"].startswith("2026-11-16 14:29:45.") and abs(float(rows[0]["deviation_ms"])) < 5


def test_check_first_reports_a_problem_at_once_and_does_not_wait(no_monitor_cfg, tmp_path):
    mt5 = FakeMT5()
    mt5.terminal.trade_allowed = False  # Algo Trading is off
    start = T0.timestamp() - 2 * 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, mt5=mt5, check_first=True)
    assert app.run_once(T0) == EXIT_ERROR
    assert clock.t - start < 5  # known two hours before the time, not 30 s before it
    assert not mt5.sent and not mt5.initialized
    assert journal.rows()[0]["status"] == "error" and "Algo Trading is OFF" in journal.rows()[0]["note"]


def test_check_first_survives_an_unexpected_error_and_writes_it_down(no_monitor_cfg, tmp_path):
    start = T0.timestamp() - 2 * 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)

    def broken(*args, **kwargs):
        raise OSError("the MetaTrader5 package fell over")

    mt5.symbol_info = broken
    assert app.run_once(T0) == EXIT_ERROR
    assert not mt5.sent and not mt5.initialized  # nothing sent, and the connection was closed again
    rows = journal.rows()
    assert len(rows) == 1 and rows[0]["status"] == "error" and "fell over" in rows[0]["note"]


def test_check_first_close_to_the_target_connects_now(no_monitor_cfg, tmp_path):
    """45 s to go with a 30 s prearm: no idle 15 s, the connect starts right away."""
    start = T0.timestamp() - 45
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)
    connects = record_connects(mt5, clock)
    assert app.run_once(T0) == EXIT_OK
    assert connects == [pytest.approx(start, abs=1)]  # one connect, at once
    assert journal.rows()[0]["status"] == "opened" and abs(float(journal.rows()[0]["deviation_ms"])) < 5


def test_without_check_first_the_connect_waits_for_the_prearm(no_monitor_cfg, tmp_path):
    start = T0.timestamp() - 45
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start)
    connects = record_connects(mt5, clock)
    assert app.run_once(T0) == EXIT_OK
    assert connects == [pytest.approx(T0.timestamp() - 30, abs=1)]


def test_a_closed_market_at_the_early_check_is_only_a_warning(no_monitor_cfg, tmp_path):
    """Armed on Sunday evening for Monday: the early check warns, the run goes on and opens."""
    mt5 = FakeMT5()
    mt5.tick_age_s = mt5.other_quotes_age_s = 47 * 3600
    start = T0.timestamp() - 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, mt5=mt5, check_first=True)
    real_initialize = mt5.initialize

    def initialize(*args, **kwargs):
        if clock.t > start + 60:  # by the prearm the market is open
            mt5.tick_age_s = mt5.other_quotes_age_s = 0.0
        return real_initialize(*args, **kwargs)

    mt5.initialize = initialize
    assert app.run_once(T0) == EXIT_OK
    assert journal.rows()[0]["status"] == "opened"


def test_a_market_still_closed_at_the_prearm_stops_the_run(no_monitor_cfg, tmp_path):
    mt5 = FakeMT5()
    mt5.tick_age_s = mt5.other_quotes_age_s = 47 * 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 3600, mt5=mt5,
                                        check_first=True)
    assert app.run_once(T0) == EXIT_ERROR
    assert not mt5.sent and "market is closed" in journal.rows()[0]["note"]


def test_metatrader_ready_too_late_opens_nothing_and_says_so(no_monitor_cfg, tmp_path):
    """The time was 10 s away but MetaTrader 5 needed 25 s to start."""
    start = T0.timestamp() - 10
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)
    record_connects(mt5, clock, takes_s=25)
    assert app.run_once(T0) == EXIT_SKIPPED
    assert not mt5.sent
    row = journal.rows()[0]
    assert row["status"] == "skipped_late"
    assert "MetaTrader 5 was ready 15.0 s after the target" in row["note"] and "later time" in row["note"]


def test_a_first_start_that_runs_past_the_target_opens_nothing_and_says_why(no_monitor_cfg, tmp_path):
    """Two minutes to go, but a brand-new MetaTrader 5 needs five minutes for its first start."""
    start = T0.timestamp() - 120
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)
    connects = record_connects(mt5, clock, takes_s=300)
    assert app.run_once(T0) == EXIT_SKIPPED
    assert len(connects) == 1 and not mt5.sent  # no second connect, nothing sent
    row = journal.rows()[0]
    assert row["status"] == "skipped_late" and "MetaTrader 5 was ready 180.0 s after the target" in row["note"]
    assert "asleep" not in row["note"]  # the PC was not asleep: say what really happened


def test_metatrader_ready_a_moment_after_the_target_still_opens(no_monitor_cfg, tmp_path):
    """Inside max_late_ms (2 s): the order goes out the moment the checks are done."""
    start = T0.timestamp() - 10
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, check_first=True)
    record_connects(mt5, clock, takes_s=11)
    assert app.run_once(T0) == EXIT_OK
    row = journal.rows()[0]
    assert row["status"] == "opened" and 900 < float(row["deviation_ms"]) < 2000


def test_the_loop_never_checks_a_day_ahead(no_monitor_cfg, tmp_path):
    """A check that fails long before the time must not give the day up (and the next, and the next)."""
    mt5 = FakeMT5()
    mt5.terminal.trade_allowed = False  # Algo Trading is off right now ...
    start = T0.timestamp() - 3 * 3600
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=start, mt5=mt5, check_first=True)
    real_initialize = mt5.initialize

    def initialize(*args, **kwargs):
        mt5.terminal.trade_allowed = True  # ... and switched on by the time of the prearm
        return real_initialize(*args, **kwargs)

    mt5.initialize = initialize
    real_run_once = app.run_once
    runs = []

    def run_once(target=None, **kwargs):
        if runs:
            raise KeyboardInterrupt  # one day is enough for this test
        runs.append(kwargs)
        return real_run_once(target, **kwargs)

    app.run_once = run_once
    with pytest.raises(KeyboardInterrupt):
        app.run_loop()
    assert runs == [{"check_first": False}]
    rows = journal.rows()
    assert len(rows) == 1 and rows[0]["status"] == "opened"  # it waited for the prearm and traded


def test_check_first_cannot_be_combined_with_the_loop(capsys):
    assert main(["--config", "does-not-matter.yaml", "--loop", "--check-first"]) == EXIT_CONFIG
    assert "cannot be used with --loop" in capsys.readouterr().err


def test_dry_run_sends_nothing(no_monitor_cfg, tmp_path):
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 40, dry_run=True)
    assert app.run_once(T0) == EXIT_OK
    assert not mt5.sent
    assert journal.rows()[0]["status"] == "dry_run"


def test_preflight_failure_returns_error(no_monitor_cfg, tmp_path):
    mt5 = FakeMT5()
    mt5.terminal.trade_allowed = False
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 40, mt5=mt5)
    assert app.run_once(T0) == EXIT_ERROR
    assert not mt5.sent
    assert journal.rows()[0]["status"] == "error"


def test_run_with_monitor_records_close(buy_cfg, tmp_path):
    app, mt5, clock, journal = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 40)
    real_get = mt5.positions_get
    calls = {"n": 0}

    def positions_get(**kw):
        calls["n"] += 1
        if kw.get("ticket") and mt5.positions and calls["n"] > 10:
            mt5.close_position(mt5.positions[0].ticket, 2675.30, reason=5, after_s=1800)
        return real_get(**kw)

    mt5.positions_get = positions_get
    assert app.run_once(T0) == EXIT_OK
    rows = journal.rows()
    assert len(rows) == 1  # written at the fill, then updated with the close
    row = rows[0]
    assert row["status"] == "opened" and row["close_reason"] == "TP"
    assert row["profit"] == "150.00" and row["close_time"].startswith("2026-11-16 14:59:45")


def test_trade_is_journaled_before_monitoring(buy_cfg, tmp_path):
    app, mt5, clock, journal = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 40)
    seen = {}
    real_monitor = Mt5Client.monitor

    def monitor(self, res, until, poll, **kwargs):
        seen["rows"] = journal.rows()  # what a killed process would leave behind
        return real_monitor(self, res, clock.t + 3, poll, **kwargs)

    Mt5Client.monitor = monitor
    try:
        assert app.run_once(T0) == EXIT_OK
    finally:
        Mt5Client.monitor = real_monitor
    assert len(seen["rows"]) == 1 and seen["rows"][0]["status"] == "opened"
    assert seen["rows"][0]["fill_price"] == "2650.30" and seen["rows"][0]["note"] == "monitoring"
    final = journal.rows()
    assert len(final) == 1 and "still open" in final[0]["close_reason"]


def test_check_mode(no_monitor_cfg, tmp_path):
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 3600)
    assert app.run_check() == EXIT_OK
    assert not mt5.sent and not journal.rows()


def test_monitor_until_rolls_to_next_day(buy_raw):
    buy_raw["monitor"]["until"] = "02:00:00"
    app = HedgeApp(make_cfg(buy_raw), notifier=Notifier(), boost=False, emit_results=False)
    until = app.monitor_until(T0.timestamp())
    assert datetime.fromtimestamp(until, timezone.utc) == datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)


def test_parse_target_at():
    assert parse_target_at("2026-11-16T13:29:45+00:00") == T0
    assert parse_target_at("2026-11-16T13:29:45Z") == T0
    with pytest.raises(ValueError):
        parse_target_at("2026-11-16T13:29:45")
