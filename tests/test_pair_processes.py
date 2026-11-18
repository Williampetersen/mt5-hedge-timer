"""The two real engine processes, started and followed the way the window does it, without a window.

Nothing real is touched: the processes import tests/fake_site/MetaTrader5.py
instead of the real package. These runs use the real clock, so they show what
the unit tests (fake clock) cannot: both sides open at the set second, a second
run on the same day opens again, and a side that fails takes the other one
down before anything is opened.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("tzlocal")  # the app's engine files say "timezone: local"

from aitrade import runner as r  # noqa: E402
from aitrade.engine_config import write_engine_configs  # noqa: E402
from aitrade.settings import SIDES, Account, Plan, Settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FAKE_SITE = str(Path(__file__).resolve().parent / "fake_site")
# Seconds from starting the two engines to the set time. Both sides have to be ready two seconds
# before the time, so this leaves room for a slow PC to start two Python processes.
RUN_LEAD_S = 12


def make_settings(tmp_path: Path) -> Settings:
    exes = {}
    for side in SIDES:
        folder = tmp_path / f"MT5_{side}"
        folder.mkdir(exist_ok=True)
        exes[side] = folder / "terminal64.exe"
        exes[side].write_bytes(b"")
    settings = Settings(
        buy=Account(broker="Alpha", server="Alpha-Demo", login=1001, terminal_path=str(exes["buy"]),
                    symbol="XAUUSD"),
        sell=Account(broker="Beta", server="Beta-Demo", login=2002, terminal_path=str(exes["sell"]),
                     symbol="XAUUSD.m"),
        plan=Plan(lots=0.06, tp_usd=150.0, date="2030-01-02", time="14:29:45"),
        setup_done=True,
    )
    settings.advanced.prearm_seconds = 5
    return settings


class Pair:
    """Both engine processes of one run, with the window's rule: both sides or none."""

    def __init__(self, settings: Settings, tmp_path: Path, *, mode: str, target: datetime, extra_env=None):
        configs = write_engine_configs(settings, tmp_path / "run")
        self.target = target
        self.journal = tmp_path / "journal.csv"
        self.states = {side: r.SideState(side) for side in SIDES}
        self.seen = {side: [] for side in SIDES}  # every state a side went through
        self.stop_reason = ""
        self._lock = threading.Lock()
        self._procs = {}
        self._threads = []
        for side in SIDES:
            cmd = r.worker_command(configs[side], mode=mode, log_dir=tmp_path / "logs", journal=self.journal,
                                   target=target, frozen=False, executable=sys.executable)
            env = r.worker_env(side, f"pw-{side}")
            env["PYTHONPATH"] = FAKE_SITE + os.pathsep + env.get("PYTHONPATH", "")
            env["HEDGEBOT_LAUNCH"] = "0"  # the pretend broker has no program to start
            env["FAKE_MT5_CLOSE_AFTER"] = "1"
            env.pop("FAKE_MT5_FAIL_LOGIN", None)
            env.update(extra_env or {})
            proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    bufsize=1)
            self._procs[side] = proc
            self.states[side].started()
            thread = threading.Thread(target=self._pump, args=(side, proc), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _pump(self, side: str, proc) -> None:
        state = self.states[side]
        for raw in proc.stdout:
            with self._lock:
                state.on_line(r.parse_line(raw))
                self._note(side)
        code = proc.wait()
        with self._lock:
            state.on_exit(code)
            self._note(side)

    def _note(self, side: str) -> None:
        if not self.seen[side] or self.seen[side][-1] != self.states[side].state:
            self.seen[side].append(self.states[side].state)

    @property
    def ended(self) -> bool:
        return all(st.exit_code is not None for st in self.states.values())

    def stop(self) -> None:
        for side, proc in self._procs.items():
            if proc.poll() is None:
                self.states[side].stop_requested = True
                proc.kill()

    def follow(self, until, timeout: float) -> bool:
        """Follow both sides until `until()` is true, stopping the pair like the window would."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                left = (self.target - datetime.now(timezone.utc)).total_seconds()
                reason = r.pair_abort_reason(self.states["buy"], self.states["sell"], left)
                if reason:  # the same steps as RunPage._stop_pair_if_needed
                    self.stop_reason = self.stop_reason or reason
                    self.stop()
                    for state in self.states.values():
                        if state.stop_requested:
                            state.stopped_before_time = True
                if until():
                    return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        self.stop()
        for thread in self._threads:
            thread.join(timeout=10)
        for proc in self._procs.values():
            if proc.stdout is not None:
                proc.stdout.close()


def describe(pair: Pair) -> dict:
    return {side: (st.state, st.message, st.exit_code, st.notes[-3:]) for side, st in pair.states.items()}


def test_two_runs_on_one_day_both_open_at_the_set_second(tmp_path):
    """No limit per day: the second run opens again, although the journal holds the first one.

    Each run talks to a fresh pretend broker, so no earlier position is open here (the unit
    tests cover that); what carries over is the journal, which used to block a second run.
    """
    settings = make_settings(tmp_path)
    for run_number in (1, 2):
        target = (datetime.now(timezone.utc) + timedelta(seconds=RUN_LEAD_S)).replace(microsecond=0)
        pair = Pair(settings, tmp_path, mode=r.MODE_RUN, target=target)
        try:
            opened = pair.follow(lambda: all(st.has_position for st in pair.states.values()), timeout=40)
            assert opened, describe(pair)
            assert pair.follow(lambda: pair.ended, timeout=30), describe(pair)
        finally:
            pair.close()
        assert {st.state for st in pair.states.values()} == {r.CLOSED}, describe(pair)
        assert {st.exit_code for st in pair.states.values()} == {0} and not pair.stop_reason
        for side in SIDES:
            result = pair.states[side].result
            assert abs(result["send_ts"] - target.timestamp()) < 0.25, side  # at the second that was set
            assert result["clock_offset_ms"] == 0.0, side  # the PC clock as it is, never shifted
        gap = r.send_gap_ms(pair.states["buy"], pair.states["sell"])
        assert gap is not None and gap < 50  # the same instant, in two processes
        journal = pair.journal.read_text(encoding="utf-8")
        assert journal.count("opened") == 2 * run_number and "skipped" not in journal


def test_a_slow_metatrader_on_one_side_means_nothing_opens(tmp_path):
    """The time is near and one MetaTrader 5 needs a minute: the side that is ready must not open alone."""
    settings = make_settings(tmp_path)
    target = (datetime.now(timezone.utc) + timedelta(seconds=RUN_LEAD_S)).replace(microsecond=0)
    pair = Pair(settings, tmp_path, mode=r.MODE_RUN, target=target, extra_env={"FAKE_MT5_SLOW_START": "1001:60"})
    try:
        assert pair.follow(lambda: pair.ended, timeout=40), describe(pair)
    finally:
        pair.close()
    assert "not ready in time for the BUY" in pair.stop_reason, describe(pair)
    assert r.ARMED in pair.seen["sell"] and r.ARMED not in pair.seen["buy"], pair.seen
    assert {st.state for st in pair.states.values()} == {r.STOPPED}, describe(pair)
    assert all(st.nothing_sent for st in pair.states.values())  # also the side that was armed
    assert time.time() < target.timestamp()  # it was all over before the set time
    assert not pair.journal.exists()  # nothing was sent, nothing to write down


def test_one_account_failing_takes_the_other_down_before_anything_opens(tmp_path):
    settings = make_settings(tmp_path)
    target = datetime.now(timezone.utc) + timedelta(minutes=10)
    pair = Pair(settings, tmp_path, mode=r.MODE_RUN, target=target, extra_env={"FAKE_MT5_FAIL_LOGIN": "1001"})
    try:
        assert pair.follow(lambda: pair.ended, timeout=60), describe(pair)
    finally:
        pair.close()
    buy, sell = pair.states["buy"], pair.states["sell"]
    assert buy.state == r.FAILED and "Authorization failed" in buy.message, describe(pair)
    # SELL had passed every check ten minutes ahead of the time, and was stopped so it cannot open alone.
    assert r.READY in pair.seen["sell"] and sell.state == r.STOPPED, pair.seen
    assert "BUY account did not get through" in pair.stop_reason and "Nothing was opened" in pair.stop_reason
    assert buy.nothing_sent and sell.nothing_sent
    assert "opened" not in pair.journal.read_text(encoding="utf-8")
