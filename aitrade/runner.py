"""Start the two engine processes and understand what they print.

No window code here, so all of it can be tested. The window owns the two
processes; this file builds their command lines, turns each printed line into
a simple state ("ready", "armed", "opened", ...) for one side, and says when
a run has to be stopped as a whole because one side cannot open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from hedgebot.__main__ import CHILD_ENV, EVENT_PREFIX, RESULT_PREFIX

from .engine_config import PASSWORD_ENV
from .settings import SIDE_TITLES

WORKER_PROGRAM = "aitrade-worker.exe"
MODE_CHECK = "check"  # connect and test everything, send nothing
MODE_RUN = "run"  # wait for the time and open the position
MODE_PRACTICE = "practice"  # like run, but the order is only checked by the broker, not placed

# "2026-11-16 14:29:45.003 CET INFO    message"
_LOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}(?: \S+)? +(DEBUG|INFO|WARNING|ERROR|CRITICAL) +(.*)$"
)
MAX_NOTES = 12
# Warnings that are normal inside the app (a check before the date and time are chosen).
IGNORED_NOTES = ("No future trading day matches the schedule",)
# Warnings of the check at the start of a run that the last check before the time has cleared.
EARLY_ONLY_NOTES = ("MARKET CLOSED right now", "order_check: market closed")

# Side states. FINAL ones mean the process has nothing more to do.
# PREARM: connecting and checking. READY: every check passed, waiting for the last check before the time.
IDLE, STARTING, WAITING, PREARM, ARMED = "idle", "starting", "waiting", "prearm", "armed"
READY = "ready"
OPENED, MONITORING, CLOSED, MONITOR_ENDED = "opened", "monitoring", "closed", "monitor_ended"
CHECK_OK, CHECK_FAILED, PRACTICE_OK = "check_ok", "check_failed", "practice_ok"
SKIPPED, FAILED, STOPPED = "skipped", "failed", "stopped"
PROBLEM_STATES = frozenset({CHECK_FAILED, SKIPPED, FAILED})
POSITION_STATES = frozenset({OPENED, MONITORING, CLOSED, MONITOR_ENDED})
BEFORE_OPEN_STATES = frozenset({STARTING, WAITING, PREARM, READY, ARMED})
# Closer to the set time than this a side is never stopped: its order may be going out.
ABORT_MIN_LEFT_S = 1.0
# This close to the set time both sides have to be armed ("Ready to open"), or neither opens.
ARMED_BY_LEFT_S = 2.0


# --------------------------------------------------------------------------- command lines


def worker_command(
    config_path: Path,
    *,
    mode: str,
    log_dir: Path,
    journal: Path,
    target: Optional[datetime] = None,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> list[str]:
    """Command line for one engine process (one side)."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    if not frozen and os.path.basename(exe).lower() == "pythonw.exe":
        exe = os.path.join(os.path.dirname(exe), "python.exe")  # the worker prints; pythonw cannot
    if frozen:
        # The installed program has a second, console-type program for the engine (it can print).
        worker = os.path.join(os.path.dirname(exe), WORKER_PROGRAM)
        cmd = [worker] if os.path.isfile(worker) else [exe, "worker"]
    else:
        cmd = [exe, "-u", "-m", "aitrade", "worker"]
    cmd += ["--config", str(config_path), "--log-dir", str(log_dir), "--journal", str(journal)]
    if mode == MODE_CHECK:
        cmd.append("--check")
        return cmd
    if mode not in (MODE_RUN, MODE_PRACTICE):
        raise ValueError(f"unknown mode {mode!r}")
    if target is None or target.tzinfo is None:
        raise ValueError("a run needs a target time with a time zone")
    # --check-first: connect and test everything the moment the run starts, not seconds before the time.
    cmd += ["--check-first", "--target-at", target.astimezone(timezone.utc).isoformat()]
    if mode == MODE_PRACTICE:
        cmd.append("--dry-run")
    return cmd


def worker_env(side: str, password: str, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Environment for one engine process: only its own password, never the other side's."""
    env = dict(os.environ if base is None else base)
    for name in PASSWORD_ENV.values():
        env.pop(name, None)
    env[PASSWORD_ENV[side]] = password
    env[CHILD_ENV] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# --------------------------------------------------------------------------- output lines


@dataclass(frozen=True)
class Line:
    kind: str  # "event" | "result" | "log" | "text"
    text: str  # what to show in the log box ("" for event/result lines)
    level: str = "INFO"
    data: Optional[dict] = None


def parse_line(raw: str) -> Line:
    line = raw.rstrip("\r\n")
    for prefix, kind in ((EVENT_PREFIX, "event"), (RESULT_PREFIX, "result")):
        if line.startswith(prefix):
            try:
                data = json.loads(line[len(prefix):])
            except json.JSONDecodeError:
                return Line("text", line, "WARNING")
            if isinstance(data, dict):
                return Line(kind, "", "INFO", data)
            return Line("text", line, "WARNING")
    m = _LOG_RE.match(line)
    if m:
        return Line("log", line, m.group(1), None)
    return Line("text", line, "INFO")


def log_message(line: Line) -> str:
    """The message part of a log line (without the timestamp and level)."""
    m = _LOG_RE.match(line.text)
    return m.group(2) if m else line.text


def short_log(text: str) -> tuple[str, str]:
    """('14:29:45.003', 'message') for a log line; ('', text) for anything else."""
    m = _LOG_RE.match(text)
    return (text[11:23], m.group(2)) if m else ("", text)


# --------------------------------------------------------------------------- one side's state


@dataclass
class SideState:
    side: str
    state: str = IDLE
    message: str = ""  # why it was skipped / failed
    target_ts: Optional[float] = None
    snapshot: dict = field(default_factory=dict)  # account, price, TP, margin (after the checks)
    result: dict = field(default_factory=dict)  # the send result
    position: dict = field(default_factory=dict)  # fill price, final TP, ticket
    closed: dict = field(default_factory=dict)  # close reason and profit
    pnl: Optional[float] = None  # floating profit while the position is open
    notes: list[str] = field(default_factory=list)  # warnings and errors from the log
    activity: str = ""  # what it is busy with before the first result (e.g. starting MetaTrader 5)
    exit_code: Optional[int] = None
    stop_requested: bool = False
    reached_armed: bool = False  # it got as far as "Ready to open" (from then on an order can go out)
    stopped_before_time: bool = False  # the window stopped it well before the set time (nothing was sent)
    _plain: list[str] = field(default_factory=list, repr=False)  # last lines that were not log lines

    @property
    def running(self) -> bool:
        return self.state not in (IDLE,) and self.exit_code is None and self.state != STOPPED

    @property
    def has_position(self) -> bool:
        return self.state in POSITION_STATES

    @property
    def is_problem(self) -> bool:
        return self.state in PROBLEM_STATES

    @property
    def nothing_sent(self) -> bool:
        """True only when it is certain that this side sent no order to the broker."""
        if self.has_position or self.result.get("send_ts") is not None:
            return False
        # No send was reported. That settles it when the engine said so itself (a result),
        # when it never got as far as armed, or when the window stopped it before the time.
        return bool(self.result) or not self.reached_armed or self.stopped_before_time

    def started(self) -> None:
        self.state = STARTING

    def note(self, text: str) -> None:
        text = text.strip()
        if text.startswith(IGNORED_NOTES):
            return
        if text and text not in self.notes:
            self.notes.append(text)
            del self.notes[:-MAX_NOTES]

    def on_line(self, line: Line) -> None:
        if line.kind == "event":
            self._on_event(line.data or {})
        elif line.kind == "result":
            self._on_result(line.data or {})
        elif line.kind == "log":
            message = log_message(line)
            if line.level in ("WARNING", "ERROR", "CRITICAL"):
                self.note(message)
            elif message.startswith("Starting MetaTrader 5"):
                self.activity = message
        elif line.text.strip():
            self._plain.append(line.text.strip())
            del self._plain[:-8]

    def _on_event(self, d: dict) -> None:
        stage = d.get("stage")
        if stage == "waiting":
            self.state, self.target_ts = WAITING, d.get("target_ts")
        elif stage in ("checking", "prearm"):  # the check at the start, and the last one before the time
            self.state = PREARM
        elif stage == "ready":
            self.state, self.snapshot, self.activity = READY, d, ""
        elif stage == "armed":
            self.state, self.snapshot, self.activity, self.reached_armed = ARMED, d, "", True
            # the last check passed with the market open: what the first check warned about is over
            self.notes = [n for n in self.notes if not n.startswith(EARLY_ONLY_NOTES)]
        elif stage == "check":
            if d.get("ok"):
                self.state, self.snapshot = CHECK_OK, d
            else:
                self.state, self.message = CHECK_FAILED, str(d.get("error") or "the check failed")
        elif stage == "opened":
            self.state, self.position = OPENED, d
            if d.get("tp_error"):
                self.note(str(d["tp_error"]))
        elif stage == "monitoring":
            if self.state == OPENED:
                self.state = MONITORING
        elif stage == "pnl":
            self.pnl = d.get("profit")
        elif stage == "closed":
            self.state, self.closed = CLOSED, d
        elif stage == "monitor_ended":
            self.state, self.pnl = MONITOR_ENDED, d.get("profit", self.pnl)
        elif stage == "problem":
            self.note(str(d.get("message") or ""))

    def _on_result(self, d: dict) -> None:
        self.result = d
        status = str(d.get("status") or "")
        note = str(d.get("note") or "")
        if status == "opened":
            self.state = OPENED
        elif status == "dry_run":
            self.state = PRACTICE_OK
        elif status.startswith("skipped"):
            self.state, self.message = SKIPPED, note or status
        else:
            self.state, self.message = FAILED, note or status or "the order failed"

    def on_exit(self, code: int) -> None:
        self.exit_code = code
        if self.stop_requested:
            # A side that had already failed keeps its reason, also when it was stopped while closing down.
            if not self.has_position and not self.is_problem and self.state not in (CHECK_OK, PRACTICE_OK):
                self.state = STOPPED
            return
        if self.state in BEFORE_OPEN_STATES:
            # Ended without telling us why: show the last thing it printed.
            self.state = CHECK_FAILED if code == 2 else FAILED
            detail = " ".join(self._plain[-6:]) or (self.notes[-1] if self.notes else "")
            self.message = detail or f"the engine stopped unexpectedly (code {code}); see the log"


def pair_abort_reason(buy: SideState, sell: SideState, seconds_left: Optional[float]) -> str:
    """Why the whole run has to stop now, or "" to carry on.

    The two positions only make sense together, so it is both sides or none:
    - one side has failed and the other has not opened yet: the other must not open alone;
    - the set time is almost there and a side is still not armed (MetaTrader 5 was slow to
      start): the side that is ready must not open alone either.
    In the last second before the set time nothing is stopped any more: an order may be
    going out, and stopping the process then would lose track of it.
    """
    if seconds_left is None or seconds_left < ABORT_MIN_LEFT_S:
        return ""

    def waiting(st: SideState) -> bool:
        return st.state in BEFORE_OPEN_STATES and st.exit_code is None and not st.stop_requested

    for failed, other in ((buy, sell), (sell, buy)):
        if failed.is_problem and waiting(other):
            return (f"The {SIDE_TITLES[failed.side]} account did not get through its checks, so the "
                    f"{SIDE_TITLES[other.side]} side was stopped too. Nothing was opened.")
    if seconds_left < ARMED_BY_LEFT_S:
        late = [SIDE_TITLES[st.side] for st in (buy, sell) if waiting(st) and st.state != ARMED]
        if late:
            return (f"MetaTrader 5 was not ready in time for the {' and the '.join(late)} account, so nothing "
                    "was opened. Choose a time a little later and start again: MetaTrader 5 is open now, "
                    "so it will be ready quickly.")
    return ""


def send_gap_ms(buy: SideState, sell: SideState) -> Optional[float]:
    """Milliseconds between the two sends (both measured on this PC's clock), or None."""
    a, b = buy.result.get("send_pc_ts"), sell.result.get("send_pc_ts")
    if a is None or b is None:
        return None
    return abs(float(b) - float(a)) * 1000.0
