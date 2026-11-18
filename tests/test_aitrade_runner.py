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


def test_a_run_checks_everything_first_and_a_check_does_not_need_to():
    """Run and practice connect and test at once (--check-first); a plain check is that test already."""
    run = r.worker_command(Path("buy.yaml"), mode=r.MODE_RUN, target=TARGET, frozen=False, executable="py", **KW)
    practice = r.worker_command(Path("buy.yaml"), mode=r.MODE_PRACTICE, target=TARGET, frozen=False,
                                executable="py", **KW)
    check = r.worker_command(Path("buy.yaml"), mode=r.MODE_CHECK, frozen=False, executable="py", **KW)
    assert "--check-first" in run and "--check-first" in practice and "--check-first" not in check


def test_the_engine_understands_every_command_the_window_builds():
    from hedgebot.__main__ import build_parser

    for mode in (r.MODE_CHECK, r.MODE_RUN, r.MODE_PRACTICE):
        cmd = r.worker_command(Path("buy.yaml"), mode=mode, target=TARGET, frozen=False, executable="py", **KW)
        args = build_parser().parse_args(cmd[cmd.index("worker") + 1:])
        assert args.check is (mode == r.MODE_CHECK) and args.dry_run is (mode == r.MODE_PRACTICE)
        assert args.check_first is (mode != r.MODE_CHECK)


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
    st.on_line(event("checking"))  # the check at the start of the run
    assert st.state == r.PREARM
    st.on_line(event("ready", symbol="XAUUSD", bid=0.9))
    assert st.state == r.READY and st.snapshot["bid"] == 0.9 and st.running and not st.has_position
    st.on_line(event("prearm"))  # the last check, just before the time
    assert st.state == r.PREARM
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
    st.on_line(event("check", ok=True, login=1001))
    assert st.state == r.CHECK_OK and st.snapshot["login"] == 1001 and st.notes == []
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
    for stage in ("checking", "ready", "prearm", "armed"):  # stopped anywhere before the open
        st = r.SideState("buy")
        st.started()
        st.on_line(event(stage))
        st.stop_requested = True
        st.on_exit(1)
        assert st.state == r.STOPPED, stage


def test_a_side_that_failed_keeps_its_reason_when_the_run_is_stopped():
    st = r.SideState("buy")
    st.started()
    st.on_line(result("error", note="Algo Trading is OFF"))
    st.stop_requested = True  # stopped while it was still closing down
    st.on_exit(1)
    assert st.state == r.FAILED and st.message == "Algo Trading is OFF"


def test_a_ready_side_that_dies_silently_is_a_failure():
    st = r.SideState("sell")
    st.started()
    st.on_line(event("ready", symbol="XAUUSD"))
    st.on_exit(1)
    assert st.state == r.FAILED and "code 1" in st.message


# ---------------------------------------------------------------- both sides or none


def _side(name, *lines, exit_code=None):
    st = r.SideState(name)
    st.started()
    for line in lines:
        st.on_line(line)
    if exit_code is not None:
        st.on_exit(exit_code)
    return st


@pytest.mark.parametrize("other_lines", [
    [], [event("waiting")], [event("checking")], [event("ready")], [event("prearm")], [event("armed")],
])
def test_one_side_failing_before_the_time_stops_the_other(other_lines):
    failed = _side("buy", result("error", note="Authorization failed"), exit_code=1)
    other = _side("sell", *other_lines)
    reason = r.pair_abort_reason(failed, other, seconds_left=600)
    assert "BUY account did not get through" in reason and "SELL side was stopped too" in reason
    assert "Nothing was opened" in reason
    # the same the other way round
    failed, other = _side("sell", result("skipped_late", note="late")), _side("buy", *other_lines)
    assert "SELL account" in r.pair_abort_reason(other, failed, seconds_left=10)


def test_a_side_that_died_silently_stops_the_other_too():
    died = _side("sell", event("waiting"), exit_code=1)
    assert r.pair_abort_reason(_side("buy", event("ready")), died, seconds_left=60)


def test_nothing_is_stopped_while_both_sides_are_fine():
    buy, sell = _side("buy", event("ready")), _side("sell", event("checking"))
    assert r.pair_abort_reason(buy, sell, seconds_left=600) == ""
    assert r.pair_abort_reason(_side("buy"), _side("sell"), seconds_left=600) == ""


def test_nothing_is_stopped_close_to_the_time():
    """An order may already be on its way: stopping the process then would lose track of it."""
    failed, other = _side("buy", result("failed", note="10019 no money")), _side("sell", event("armed"))
    assert r.pair_abort_reason(failed, other, seconds_left=r.ABORT_MIN_LEFT_S - 0.1) == ""
    assert r.pair_abort_reason(failed, other, seconds_left=-0.2) == ""
    assert r.pair_abort_reason(failed, other, seconds_left=None) == ""  # a check has no time
    assert r.pair_abort_reason(failed, other, seconds_left=r.ABORT_MIN_LEFT_S + 0.1) != ""


def test_a_side_that_is_not_armed_just_before_the_time_stops_the_pair():
    """MetaTrader 5 was slow to start on one account: the other must not open alone."""
    armed = _side("sell", event("armed"))
    for lines in ([], [event("waiting")], [event("checking")], [event("ready")], [event("prearm")]):
        slow = _side("buy", *lines)
        reason = r.pair_abort_reason(slow, armed, seconds_left=1.5)
        assert "not ready in time for the BUY account" in reason and "nothing was opened" in reason
        assert r.pair_abort_reason(armed, slow, seconds_left=1.5) == reason  # the order of the sides is no matter
        assert r.pair_abort_reason(slow, armed, seconds_left=r.ARMED_BY_LEFT_S + 0.5) == ""  # still time
        assert r.pair_abort_reason(slow, armed, seconds_left=r.ABORT_MIN_LEFT_S - 0.1) == ""  # hands off now
    both = r.pair_abort_reason(_side("buy", event("prearm")), _side("sell", event("ready")), seconds_left=1.2)
    assert "not ready in time for the BUY and the SELL account" in both


def test_two_armed_sides_are_left_alone():
    buy, sell = _side("buy", event("armed")), _side("sell", event("armed"))
    for left in (600, 2.5, 1.5, 1.0, 0.5, -1):
        assert r.pair_abort_reason(buy, sell, seconds_left=left) == ""


def test_a_side_with_a_position_is_never_stopped():
    failed = _side("buy", result("failed", note="10019 no money"))
    for lines in ([result("opened")], [result("opened"), event("monitoring")],
                  [result("opened"), event("closed", reason="TP", profit=1.0)]):
        assert r.pair_abort_reason(failed, _side("sell", *lines), seconds_left=600) == ""


def test_the_stop_is_asked_for_once():
    failed, other = _side("buy", result("error", note="x"), exit_code=1), _side("sell", event("ready"))
    assert r.pair_abort_reason(failed, other, seconds_left=600)
    other.stop_requested = True  # the window has stopped it; it has not ended yet
    assert r.pair_abort_reason(failed, other, seconds_left=600) == ""
    other.on_exit(1)
    assert other.state == r.STOPPED and r.pair_abort_reason(failed, other, seconds_left=600) == ""
    both_failed = _side("sell", result("error", note="y"), exit_code=1)
    assert r.pair_abort_reason(failed, both_failed, seconds_left=600) == ""  # nothing left to stop


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


def test_clock_note_is_information_and_never_promises_a_correction():
    assert describe.clock_note(0.012) == ("PC clock checked: 12 ms behind internet time", False)
    assert describe.clock_note(-0.3) == ("PC clock checked: 300 ms ahead of internet time", False)
    text, warn = describe.clock_note(1.34)
    assert warn and text == "PC clock is 1.3 s behind internet time – the positions open by the PC clock"
    text, warn = describe.clock_note(-20.0)
    assert warn and text.startswith("PC clock is 20.0 s ahead of internet time")
    text, warn = describe.clock_note(None)
    assert warn and "could not be checked" in text
    for offset in (0.012, 1.34, -20.0, None):
        assert "corrected" not in describe.clock_note(offset)[0]


def test_money_and_price():
    assert describe.money(1234.5, "USD") == "1,234.50 USD"
    assert describe.money(-3, "USD", signed=True) == "-3.00 USD" and describe.money(3, signed=True) == "+3.00"
    assert describe.money(None) == "-" and describe.price(2650.3, 2) == "2650.30" and describe.price(None) == "-"


def test_check_card_text():
    st = r.SideState("buy")
    st.on_line(event("check", ok=True, login=1001, account_kind="demo", currency="USD", balance=10000.0,
                     leverage=100, symbol="XAUUSD", digits=2, bid=2650.0, ask=2650.3, spread_points=30.0,
                     tp="150.00 USD", tp_price=2675.3, tp_distance=25.0, sl="none", sl_price=None,
                     margin_required=159.02, margin_free=10000.0, margin_warning=None))
    assert describe.headline(st) == ("All checks passed", describe.GOOD)
    assert describe.detail_lines(st) == [
        "Demo account 1001 · balance 10,000.00 USD · leverage 1:100",
        "XAUUSD: bid 2650.00 / ask 2650.30 · spread 30 points",
        "Take profit 150.00 USD → price 2675.30 (25.00 away)",
        "Margin needed 159.02 USD · free 10,000.00 USD",
    ]


def test_ready_card_text():
    """Right after Run is pressed: the account, the price and the take profit, long before the time."""
    st = r.SideState("sell")
    st.started()
    st.on_line(event("waiting", target_ts=1.0))
    st.on_line(event("checking"))
    assert describe.headline(st) == ("Connecting to MetaTrader 5…", describe.INFO)
    st.on_line(log("INFO", "Starting MetaTrader 5 for account 2002"))
    assert describe.detail_lines(st) == ["Starting MetaTrader 5 for account 2002"]
    st.on_line(event("ready", login=2002, account_kind="demo", currency="USD", balance=5000.0, leverage=500,
                     symbol="GOLD", digits=2, bid=2650.0, ask=2650.3, spread_points=30.0, tp="150.00 USD",
                     tp_price=2625.0, tp_distance=25.0, sl="none", sl_price=None, margin_required=31.8,
                     margin_free=5000.0, margin_warning=None))
    assert describe.headline(st) == ("Ready – waiting for the time", describe.GOOD)
    assert describe.detail_lines(st) == [
        "Demo account 2002 · balance 5,000.00 USD · leverage 1:500",
        "GOLD: bid 2650.00 / ask 2650.30 · spread 30 points",
        "Take profit 150.00 USD → price 2625.00 (25.00 away)",
        "Margin needed 31.80 USD · free 5,000.00 USD",
        "Every check passed. They run once more just before the time.",
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
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=754, active=True) == \
        ("Opens in 00:12:34", "", describe.INFO)
    buy, sell = _pair(waiting + [event("ready")], waiting + [event("checking")])  # one side still connecting
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=754, active=True)[1] == ""
    buy, sell = _pair(waiting + [event("ready")], waiting + [event("ready"), event("prearm"), event("armed")])
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=754, active=True) == \
        ("Opens in 00:12:34", "Both accounts are ready.", describe.INFO)
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


def test_nothing_was_opened_is_only_said_when_it_is_certain():
    """After the set time a failure can mean an order went out. Then the text must not say "nothing"."""
    def text(buy_lines, sell_lines, **exits):
        buy, sell = _pair(buy_lines, sell_lines)
        for st in (buy, sell):
            if st.side in exits:
                st.on_exit(exits[st.side])
        return describe.overall(r.MODE_RUN, buy, sell, seconds_left=-5, active=False)[1]

    armed = [event("waiting"), event("prearm"), event("armed")]
    before_any_send = [result("error", note="Algo Trading is OFF", send_ts=None)]
    # both failed before anything could be sent: certain
    assert text(before_any_send, before_any_send).startswith("Nothing was opened.")
    assert text(before_any_send, [result("skipped_late", note="late", send_ts=None)]).startswith("Nothing was")
    assert text([result("failed", note="no price at the target", send_ts=None)], before_any_send) \
        .startswith("Nothing was opened.")
    # an order went out and the outcome is not known: look at MetaTrader 5
    unknown = "a position may be open"
    assert unknown in text(armed + [result("failed", note="10008 order placed, check MT5", send_ts=7.0)],
                           before_any_send)
    assert unknown in text(armed + [result("failed", note="no reply", send_ts=7.0)], before_any_send)
    # a side that died while armed, without a word
    assert unknown in text(armed, before_any_send, buy=1)
    # ... but one that died before it was armed cannot have sent anything
    assert text([event("waiting"), event("checking")], before_any_send, buy=1).startswith("Nothing was opened.")


def test_a_side_the_window_stopped_before_the_time_sent_nothing():
    armed = _side("sell", event("waiting"), event("prearm"), event("armed"))
    assert armed.reached_armed and not armed.nothing_sent  # armed, no result yet: not known
    armed.stop_requested = True
    armed.stopped_before_time = True  # what the run page notes when it stops the pair
    armed.on_exit(1)
    assert armed.state == r.STOPPED and armed.nothing_sent
    ready = _side("buy", event("ready"))
    assert ready.nothing_sent and not ready.reached_armed
    opened = _side("buy", result("opened", send_ts=5.0))
    assert not opened.nothing_sent


def test_a_failed_practice_run_never_talks_about_open_positions():
    buy, sell = _pair([result("dry_run", send_ts=5.0)], [result("failed", note="10019 no money", send_ts=5.0)])
    head, sub, tone = describe.overall(r.MODE_PRACTICE, buy, sell, seconds_left=-1, active=False)
    assert head == "It did not work" and "places nothing" in sub and "may be open" not in sub


def test_warnings_of_the_first_check_go_when_the_last_check_has_passed():
    st = r.SideState("buy")
    st.started()
    st.on_line(event("checking"))
    st.on_line(log("INFO", "Starting MetaTrader 5 with Algo Trading on"))
    st.on_line(log("WARNING", "MARKET CLOSED right now: no fresh prices on this terminal. This is only a check"))
    st.on_line(log("WARNING", "order_check: market closed (Market closed) - fine for a check"))
    st.on_line(log("WARNING", "free margin is low"))
    st.on_line(event("ready", symbol="XAUUSD"))
    assert st.activity == "" and len(st.notes) == 3  # still true while it waits
    st.on_line(event("prearm"))
    assert describe.detail_lines(st) == []  # not "Starting MetaTrader 5" again: it is running
    st.on_line(event("armed", symbol="XAUUSD"))
    assert st.notes == ["free margin is low"]  # the market was open at the last check


def test_headline_when_one_side_failed_and_the_other_was_stopped():
    buy, sell = _pair([result("error", note="Authorization failed")], [event("ready")])
    buy.on_exit(1)
    assert describe.overall(r.MODE_RUN, buy, sell, seconds_left=600, active=True)[0] == "It did not work"
    sell.stop_requested = True
    sell.on_exit(1)
    head, sub, tone = describe.overall(r.MODE_RUN, buy, sell, seconds_left=590, active=False)
    assert (head, tone) == ("It did not work", describe.BAD) and sub.startswith("Nothing was opened.")
    assert describe.headline(buy) == ("Failed", describe.BAD) and describe.headline(sell)[0] == "Stopped"
    assert describe.detail_lines(buy) == ["Authorization failed"]
