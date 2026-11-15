"""End-to-end runs of HedgeApp with a fake MT5 and a fake clock."""

from datetime import datetime, timezone

import pytest

from conftest import make_cfg
from fake_mt5 import FakeClock, FakeMT5
from hedgebot.__main__ import EXIT_ERROR, EXIT_OK, EXIT_SKIPPED, HedgeApp, parse_target_at
from hedgebot.journal import Journal
from hedgebot.mt5_client import Mt5Client
from hedgebot.notify import Notifier

T0 = datetime(2026, 11, 16, 13, 29, 45, tzinfo=timezone.utc)  # 14:29:45 CET, Monday


def make_app(cfg, tmp_path, *, start, mt5=None, dry_run=False):
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
    )
    return app, mt5, clock, journal


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


def test_second_run_same_day_is_blocked(no_monitor_cfg, tmp_path):
    app, mt5, clock, journal = make_app(no_monitor_cfg, tmp_path, start=T0.timestamp() - 60)
    assert app.run_once() == EXIT_OK
    # A second start (e.g. --test-in) later the same day must not open again
    target = datetime.fromtimestamp(clock.t + 60, timezone.utc)
    assert app.run_once(target) == EXIT_SKIPPED
    assert len([r for r in mt5.sent if r["action"] == 1]) == 1
    assert journal.rows()[-1]["status"] == "skipped_exists"


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

    def monitor(self, res, until, poll):
        seen["rows"] = journal.rows()  # what a killed process would leave behind
        return real_monitor(self, res, clock.t + 3, poll)

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
