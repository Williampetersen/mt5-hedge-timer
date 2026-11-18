"""The machine-readable progress lines the engine prints for the desktop app."""

import json
from datetime import datetime, timezone

from conftest import make_cfg
from fake_mt5 import FakeClock, FakeMT5
from hedgebot.__main__ import EVENT_PREFIX, EXIT_ERROR, EXIT_OK, EXIT_SKIPPED, RESULT_PREFIX, HedgeApp
from hedgebot.journal import Journal
from hedgebot.mt5_client import Mt5Client
from hedgebot.notify import Notifier

T0 = datetime(2026, 11, 16, 13, 29, 45, tzinfo=timezone.utc)  # 14:29:45 CET, Monday


def make_app(cfg, tmp_path, *, start, dry_run=False, emit=True, check_first=False):
    clock = FakeClock(start)
    mt5 = FakeMT5(clock)
    app = HedgeApp(
        cfg, dry_run=dry_run, notifier=Notifier(), journal=Journal(tmp_path / "journal.csv"), clock=clock,
        client_factory=lambda c, clock, sleep: Mt5Client(c, clock=clock, mt5=mt5, sleep=sleep),
        ntp=lambda server: 0.0, sleep=clock.sleep, boost=False, emit_results=emit, check_first=check_first,
    )
    return app, mt5, clock


def parse(out: str):
    events, results = [], []
    for line in out.splitlines():
        if line.startswith(EVENT_PREFIX):
            events.append(json.loads(line[len(EVENT_PREFIX):]))
        elif line.startswith(RESULT_PREFIX):
            results.append(json.loads(line[len(RESULT_PREFIX):]))
    return events, results


def test_run_reports_every_stage(buy_raw, tmp_path, capsys):
    buy_raw["monitor"]["enabled"] = False
    app, mt5, clock = make_app(make_cfg(buy_raw), tmp_path, start=T0.timestamp() - 120)
    assert app.run_once() == EXIT_OK
    events, results = parse(capsys.readouterr().out)
    assert [e["stage"] for e in events] == ["waiting", "prearm", "armed", "opened"]
    assert all(e["direction"] == "buy" and e["name"] == "buy-account" for e in events)
    waiting, _, armed, opened = events
    assert waiting["target_ts"] == T0.timestamp() and waiting["dry_run"] is False
    assert armed["login"] == 1001 and armed["account_kind"] == "demo" and armed["currency"] == "USD"
    assert armed["symbol"] == "XAUUSD" and armed["tp"] == "150.00 USD" and armed["tp_price"] == 2675.30
    assert armed["margin_required"] > 0 and armed["margin_warning"] is None
    assert opened["fill_price"] == 2650.30 and opened["tp_price"] == 2675.30 and opened["ticket"]
    assert results[0]["status"] == "opened" and results[0]["note"] == ""


def test_check_first_reports_ready_long_before_the_target(buy_raw, tmp_path, capsys):
    buy_raw["monitor"]["enabled"] = False
    app, mt5, clock = make_app(make_cfg(buy_raw), tmp_path, start=T0.timestamp() - 600, check_first=True)
    assert app.run_once() == EXIT_OK
    events, results = parse(capsys.readouterr().out)
    assert [e["stage"] for e in events] == ["waiting", "checking", "ready", "prearm", "armed", "opened"]
    ready = events[2]
    assert ready["login"] == 1001 and ready["symbol"] == "XAUUSD" and ready["tp_price"] == 2675.30
    assert ready["target_ts"] == T0.timestamp() and ready["margin_required"] > 0
    assert results[0]["status"] == "opened"


def test_check_first_failure_is_a_result_with_the_reason(buy_cfg, tmp_path, capsys):
    app, mt5, clock = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 600, check_first=True)
    mt5.symbols_present = {"GOLD"}
    assert app.run_once() == EXIT_ERROR
    events, results = parse(capsys.readouterr().out)
    assert [e["stage"] for e in events] == ["waiting", "checking"]
    assert results[0]["status"] == "error" and "does not exist" in results[0]["note"]


def test_monitor_reports_profit_and_close(buy_cfg, tmp_path, capsys, monkeypatch):
    app, mt5, clock = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 60)
    monkeypatch.setattr("hedgebot.__main__.PNL_EVENT_EVERY_S", 0.0)
    polls = {"n": 0}
    real_positions_get = mt5.positions_get

    def positions_get(**kwargs):
        if kwargs.get("ticket") and mt5.positions:
            polls["n"] += 1
            if polls["n"] == 4:
                mt5.close_position(kwargs["ticket"], 2675.30, reason=5)
        return real_positions_get(**kwargs)

    mt5.positions_get = positions_get
    assert app.run_once() == EXIT_OK
    events, _ = parse(capsys.readouterr().out)
    stages = [e["stage"] for e in events]
    assert stages[:5] == ["waiting", "prearm", "armed", "opened", "monitoring"]
    assert "pnl" in stages and stages[-1] == "closed"
    closed = events[-1]
    assert closed["reason"] == "TP" and closed["profit"] == 150.0 and closed["currency"] == "USD"


def test_dry_run_reports_a_result_but_no_position(buy_cfg, tmp_path, capsys):
    app, mt5, _ = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 60, dry_run=True)
    assert app.run_once() == EXIT_OK
    events, results = parse(capsys.readouterr().out)
    assert [e["stage"] for e in events] == ["waiting", "prearm", "armed"]
    assert events[0]["dry_run"] is True
    assert results[0]["status"] == "dry_run" and not [r for r in mt5.sent if r["action"] == 1]


def test_skips_and_failures_carry_the_reason(buy_cfg, tmp_path, capsys):
    app, mt5, clock = make_app(buy_cfg, tmp_path, start=T0.timestamp() + 30)  # woke up 30 s late
    target = datetime.fromtimestamp(T0.timestamp(), timezone.utc)
    assert app.run_once(target) == EXIT_SKIPPED
    _, results = parse(capsys.readouterr().out)
    assert results[0]["status"] == "skipped_late" and "after the target" in results[0]["note"]

    app, mt5, clock = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 60)
    mt5.terminal.trade_allowed = False  # Algo Trading is off
    assert app.run_once() == EXIT_ERROR
    _, results = parse(capsys.readouterr().out)
    assert results[0]["status"] == "error" and "Algo Trading is OFF" in results[0]["note"]


def test_check_reports_the_account_or_the_error(buy_cfg, tmp_path, capsys):
    app, mt5, _ = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 3600)
    assert app.run_check() == EXIT_OK
    events, _ = parse(capsys.readouterr().out)
    check = events[-1]
    assert check["stage"] == "check" and check["ok"] is True and "skip_reason" not in check
    assert check["balance"] == 10_000.0 and check["leverage"] == 100 and check["spread_points"] == 30

    app, mt5, _ = make_app(buy_cfg, tmp_path, start=T0.timestamp() - 3600)
    mt5.symbols_present = {"GOLD"}
    assert app.run_check() == EXIT_ERROR
    events, _ = parse(capsys.readouterr().out)
    assert events[-1]["ok"] is False and "does not exist" in events[-1]["error"]


def test_nothing_is_printed_when_run_by_hand(buy_raw, tmp_path, capsys):
    buy_raw["monitor"]["enabled"] = False
    app, _, _ = make_app(make_cfg(buy_raw), tmp_path, start=T0.timestamp() - 60, emit=False)
    assert app.run_once() == EXIT_OK
    out = capsys.readouterr().out
    assert EVENT_PREFIX not in out and RESULT_PREFIX not in out
