"""Command lines for the engine processes, and reading what they print."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aitrade import describe
from aitrade import runner as r
from aitrade.engine_config import PASSWORD_ENV
from hedgebot.__main__ import CHILD_ENV, EVENT_PREFIX, RESULT_PREFIX

TARGET = datetime(2026, 12, 4, 14, 29, 45, tzinfo=timezone(timedelta(hours=1)))
KW = dict(log_dir=Path("L"), journal=Path("J.csv"))


def event(stage, **data):
    return r.parse_line(EVENT_PREFIX + json.dumps({"stage": stage, **data}))


def result(status, **data):
    return r.parse_line(RESULT_PREFIX + json.dumps({"status": status, **data}))


def log(level, message):
    return r.parse_line(f"2026-12-04 14:29:45.003 CET {level:<7} {message}")


# ---------------------------------------------------------------- command lines


def test_check_command_from_source():
    cmd = r.worker_command(Path("buy.yaml"), mode=r.MODE_CHECK, frozen=False, executable="C:\\py\\python.exe", **KW)
    assert cmd == ["C:\\py\\python.exe", "-u", "-m", "aitrade", "worker", "--config", "buy.yaml",
                   "--log-dir", "L", "--journal", "J.csv", "--check"]


def test_run_and_practice_pass_one_shared_utc_target():
    run = r.worker_command(Path("buy.yaml"), mode=r.MODE_RUN, target=TARGET, frozen=False, executable="py", **KW)
    assert run[-2:] == ["--target-at", "2026-12-04T13:29:45+00:00"] and "--dry-run" not in run
    practice = r.worker_command(Path("buy.yaml"), mode=r.MODE_PRACTICE, target=TARGET, frozen=False,
                                executable="py", **KW)
    assert practice[-3:] == ["--target-at", "2026-12-04T13:29:45+00:00", "--dry-run"]


def test_installed_app_starts_its_engine_program(tmp_path):
    exe = tmp_path / "Aitrade.exe"
    exe.write_bytes(b"")
    # without the engine program next to it, the window program can do the job itself
    cmd = r.worker_command(Path("buy.yaml"), mode=r.MODE_CHECK, frozen=True, executable=str(exe), **KW)
    assert cmd[:2] == [str(exe), "worker"] and "-m" not in cmd
    worker = tmp_path / r.WORKER_PROGRAM
    worker.write_bytes(b"")
    cmd = r.worker_command(Path("buy.yaml"), mode=r.MODE_CHECK, frozen=True, executable=str(exe), **KW)
    assert cmd[0] == str(worker) and cmd[1] == "--config" and "worker" not in cmd[1:]


def test_windowless_python_is_swapped_for_the_console_one():
    cmd = r.worker_command(Path("b.yaml"), mode=r.MODE_CHECK, frozen=False,
                           executable="C:\\v\\Scripts\\pythonw.exe", **KW)
    assert cmd[0] == "C:\\v\\Scripts\\python.exe"


def test_a_run_needs_a_target_with_a_zone():
    with pytest.raises(ValueError):
        r.worker_command(Path("b.yaml"), mode=r.MODE_RUN, **KW)
    with pytest.raises(ValueError):
        r.worker_command(Path("b.yaml"), mode=r.MODE_RUN, target=datetime(2026, 12, 4, 14, 29, 45), **KW)
    with pytest.raises(ValueError):
        r.worker_command(Path("b.yaml"), mode="nonsense", target=TARGET, **KW)


def test_each_worker_only_gets_its_own_password():
    base = {"PATH": "x", PASSWORD_ENV["buy"]: "old-b", PASSWORD_ENV["sell"]: "old-s"}
    env = r.worker_env("buy", "pw-b", base)
    assert env[PASSWORD_ENV["buy"]] == "pw-b" and PASSWORD_ENV["sell"] not in env
    assert env[CHILD_ENV] == "1" and env["PYTHONUNBUFFERED"] == "1" and env["PATH"] == "x"
    assert base[PASSWORD_ENV["sell"]] == "old-s"  # the caller's mapping is untouched


# ---------------------------------------------------------------- lines


def test_lines_are_classified():
    assert event("armed", login=1).kind == "event" and event("armed", login=1).data["login"] == 1
    assert result("opened").kind == "result"
    line = log("WARNING", "NTP unreachable")
    assert (line.kind, line.level) == ("log", "WARNING") and r.log_message(line) == "NTP unreachable"
    assert r.short_log(line.text) == ("14:29:45.003", "NTP unreachable")
    plain = r.parse_line("Invalid config buy.yaml:\r\n")
    assert (plain.kind, plain.text) == ("text", "Invalid config buy.yaml:")
    assert r.short_log("hello") == ("", "hello")
    assert r.parse_line(EVENT_PREFIX + "{broken").kind == "text"
    assert r.parse_line("2026-12-04 14:29:45.003 +03 ERROR   boom").level == "ERROR"


# ---------------------------------------------------------------- one side


def test_a_normal_run_walks_through_the_states():
    st = r.SideState("buy")
    assert st.state == r.IDLE and not st.running
    st.started()
    assert st.state == r.STARTING and st.running
    st.on_line(event("waiting", target_ts=100.0))
    assert st.state == r.WAITING and st.target_ts == 100.0
    st.on_line(event("prearm"))
    st.on_line(event("armed", symbol="XAUUSD", bid=1.0))
    assert st.state == r.ARMED and st.snapshot["symbol"] == "XAUUSD"
    st.on_line(result("opened", send_pc_ts=10.0, deviation_ms=1.5))
    st.on_line(event("opened", fill_price=2650.3, tp_price=2675.3, tp_error=""))
    st.on_line(event("monitoring"))
    assert st.state == r.MONITORING and st.has_position
    st.on_line(event("pnl", profit=12.5))
    assert st.pnl == 12.5
    st.on_line(event("closed", reason="TP", profit=150.0))
    st.on_exit(0)
    assert st.state == r.CLOSED and st.exit_code == 0 and not st.running and not st.is_problem


def test_skip_and_failure_keep_the_reason():
    st = r.SideState("sell")
    st.started()
    st.on_line(result("skipped_spread", note="spread 80 points is wider than 50"))
    assert st.state == r.SKIPPED and "spread 80" in st.message and st.is_problem
    st = r.SideState("sell")
    st.on_line(result("failed", note="10019 no money"))
    assert st.state == r.FAILED and st.message == "10019 no money"
    st = r.SideState("sell")
    st.on_line(result("dry_run"))
    assert st.state == r.PRACTICE_OK


def test_check_results():
    st = r.SideState("buy")
    st.on_line(event("check", ok=True, login=1001, skip_reason="position #5 is already open"))
    assert st.state == r.CHECK_OK and any("skipped" in n for n in st.notes)
    st = r.SideState("buy")
    st.on_line(event("check", ok=False, error="Algo Trading is OFF"))
    assert st.state == r.CHECK_FAILED and st.message == "Algo Trading is OFF"


def test_warnings_become_notes_without_repeats():
    st = r.SideState("buy")
    st.on_line(log("INFO", "all fine"))
    st.on_line(log("WARNING", "NTP unreachable"))
    st.on_line(log("WARNING", "NTP unreachable"))
    st.on_line(log("ERROR", "MARGIN: too little"))
    st.on_line(event("problem", message="could not correct TP"))
    assert st.notes == ["NTP unreachable", "MARGIN: too little", "could not correct TP"]
    for i in range(40):
        st.on_line(log("WARNING", f"w{i}"))
    assert len(st.notes) == r.MAX_NOTES and st.notes[-1] == "w39"


def test_a_process_that_dies_silently_is_a_failure_with_its_last_words():
    st = r.SideState("buy")
    st.started()
    st.on_line(r.parse_line("Invalid config buy.yaml:"))
    st.on_line(r.parse_line("  - 'account.terminal_path' not found"))
    st.on_exit(2)
    assert st.state == r.CHECK_FAILED and "terminal_path" in st.message
    st = r.SideState("buy")
    st.started()
    st.on_line(event("waiting", target_ts=1.0))
    st.on_exit(1)
    assert st.state == r.FAILED and "code 1" in st.message


def test_stopping_by_hand_is_not_a_failure_and_keeps_an_open_position():
    st = r.SideState("buy")
    st.started()
    st.on_line(event("waiting", target_ts=1.0))
    st.stop_requested = True
    st.on_exit(62097)
    assert st.state == r.STOPPED and not st.is_problem
    st = r.SideState("buy")
    st.on_line(result("opened"))
    st.stop_requested = True
    st.on_exit(1)
    assert st.state == r.OPENED


def test_send_gap():
    buy, sell = r.SideState("buy"), r.SideState("sell")
    assert r.send_gap_ms(buy, sell) is None
    buy.on_line(result("opened", send_pc_ts=100.0000))
    sell.on_line(result("opened", send_pc_ts=100.0032))
    assert r.send_gap_ms(buy, sell) == pytest.approx(3.2, abs=0.01)


# ---------------------------------------------------------------- words on screen


@pytest.mark.parametrize("seconds, text", [
    (0, "00:00:00"), (0.2, "00:00:01"), (59.0, "00:00:59"), (3723.4, "01:02:04"), (90061, "1d 01:01:01"),
    (-5, "00:00:00"),
])
def test_countdown(seconds, text):
    assert describe.countdown(seconds) == text


def test_money_and_price():
    assert describe.money(1234.5, "USD") == "1,234.50 USD"
    assert describe.money(-3, "USD", signed=True) == "-3.00 USD" and describe.money(3, signed=True) == "+3.00"
    assert describe.money(None) == "-" and describe.price(2650.3, 2) == "2650.30" and describe.price(None) == "-"


def test_check_card_text():
    st = r.SideState("buy")
    st.on_line(event("check", ok=True, login=1001, account_kind="demo", currency="USD", balance=10000.0,
                     leverage=100, symbol="XAUUSD", digits=2, bid=2650.0, ask=2650.3, spread_points=30.0,
                     tp="150.00 USD", tp_price=2675.3, tp_distance=25.0, sl="none", sl_price=None,
                     margin_required=159.02, margin_free=10000.0, margin_warning=None, skip_reason=""))
    assert describe.headline(st) == ("All checks passed", describe.GOOD)
    assert describe.detail_lines(st) == [
        "Demo account 1001 · balance 10,000.00 USD · leverage 1:100",
        "XAUUSD: bid 2650.00 / ask 2650.30 · spread 30 points",
        "Take profit 150.00 USD → price 2675.30 (25.00 away)",
        "Margin needed 159.02 USD · free 10,000.00 USD",
    ]


def test_position_card_text():
    st = r.SideState("sell")
    st.on_line(result("opened", deviation_ms=1.26, latency_ms=41.7, fill_price=2650.0))
    st.on_line(event("opened", digits=2, currency="USD", fill_price=2650.0, slippage_points=0.0,
                     tp_price=2625.0, sl_price=None))
    st.on_line(event("pnl", profit=-4.2))
    assert describe.headline(st)[0] == "Position open"
    assert describe.detail_lines(st) == [
        "Opened at 2650.00 · slippage 0 points", "Take profit at 2625.00",
        "Sent 1.3 ms after the set time · broker answered in 42 ms", "Profit now: -4.20 USD",
    ]
    st.on_line(event("closed", reason="TP", close_price=2625.0, profit=150.0, commission=-0.6,
                     swap=0.0, currency="USD", digits=2))
    assert describe.headline(st) == ("Closed: take profit reached", describe.GOOD)
    lines = describe.detail_lines(st)
    assert lines[-2:] == ["Closed at 2625.00 · profit +150.00 USD", "Commission and swap: -0.60 USD"]


def test_problem_card_text():
    st = r.SideState("buy")
    st.on_line(event("check", ok=False, error="Symbol 'XAUUSD' does not exist on this broker."))
    assert describe.headline(st) == ("Check failed", describe.BAD)
    assert describe.detail_lines(st) == ["Symbol 'XAUUSD' does not exist on this broker."]


def _pair(buy_lines, sell_lines):
    buy, sell = r.SideState("buy"), r.SideState("sell")
    for st, lines in ((buy, buy_lines), (sell, sell_lines)):
        st.started()
        for line in lines:
            st.on_line(line)
    return buy, sell


def test_overall_headline_for_a_run():
    waiting = [event("waiting", target_ts=1.0)]
    buy, sell = _pair(waiting, waiting)
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=754, active=True)[0] == "Opens in 00:12:34"
    assert describe.overall(r.MODE_PRACTICE, buy, sell, seconds_left=5, active=True)[0] == "Practice in 00:00:05"
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=-0.1, active=True)[0] == "Opening…"

    buy, sell = _pair([result("opened", send_pc_ts=5.0)], [result("opened", send_pc_ts=5.004)])
    head, sub, tone = describe.overall(r.MODE_RUN, buy, sell, seconds_left=-1, active=True)
    assert head == "Both positions are open" and "4.0 ms apart" in sub and tone == describe.GOOD

    buy, sell = _pair([result("opened")], [result("failed", note="no money")])
    head, sub, tone = describe.overall(r.MODE_RUN, buy, sell, active=True)
    assert head == "Only BUY opened – SELL did not" and tone == describe.BAD and "BUY position" in sub

    buy, sell = _pair([result("skipped_late", note="late")], [result("skipped_late", note="late")])
    assert describe.overall(r.MODE_RUN, buy, sell, active=False)[0] == "Not opened"

    buy, sell = _pair([result("dry_run", send_pc_ts=1.0)], [result("dry_run", send_pc_ts=1.001)])
    assert describe.overall(r.MODE_PRACTICE, buy, sell, active=False)[0] == "Practice run OK"

    closed = [result("opened"), event("closed", reason="TP", profit=150.0)]
    buy, sell = _pair(closed, [result("opened")])
    assert describe.overall(r.MODE_RUN, buy, sell, active=True)[0] == "One position is closed, one is open"


def test_overall_headline_for_a_check():
    ok = [event("check", ok=True)]
    bad = [event("check", ok=False, error="x")]
    buy, sell = _pair(ok, ok)
    assert describe.overall(r.MODE_CHECK, buy, sell, active=False)[0] == "Both accounts are ready"
    buy, sell = _pair(ok, bad)
    assert describe.overall(r.MODE_CHECK, buy, sell, active=False)[2] == describe.BAD
    buy, sell = _pair([], [])
    assert describe.overall(r.MODE_CHECK, buy, sell, active=True)[0] == "Checking both accounts…"


def test_stopped_run_headline():
    buy, sell = _pair([event("waiting", target_ts=1.0)], [event("waiting", target_ts=1.0)])
    for st in (buy, sell):
        st.stop_requested = True
        st.on_exit(1)
    assert describe.overall(r.MODE_RUN, buy, sell, active=False)[0] == "Stopped"
