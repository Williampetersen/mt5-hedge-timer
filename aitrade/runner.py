"""Start the two engine processes and understand what they print.

No window code here, so all of it can be tested. The window owns the two
processes; this file builds their command lines and turns each printed line
into a simple state ("waiting", "armed", "opened", ...) for one side.
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

# Side states. FINAL ones mean the process has nothing more to do.
IDLE, STARTING, WAITING, PREARM, ARMED = "idle", "starting", "waiting", "prearm", "armed"
OPENED, MONITORING, CLOSED, MONITOR_ENDED = "opened", "monitoring", "closed", "monitor_ended"
CHECK_OK, CHECK_FAILED, PRACTICE_OK = "check_ok", "check_failed", "practice_ok"
SKIPPED, FAILED, STOPPED = "skipped", "failed", "stopped"
PROBLEM_STATES = frozenset({CHECK_FAILED, SKIPPED, FAILED})
POSITION_STATES = frozenset({OPENED, MONITORING, CLOSED, MONITOR_ENDED})


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
    cmd = [exe, "worker"] if frozen else [exe, "-u", "-m", "aitrade", "worker"]
    cmd += ["--config", str(config_path), "--log-dir", str(log_dir), "--journal", str(journal)]
    if mode == MODE_CHECK:
        cmd.append("--check")
        return cmd
    if mode not in (MODE_RUN, MODE_PRACTICE):
        raise ValueError(f"unknown mode {mode!r}")
    if target is None or target.tzinfo is None:
        raise ValueError("a run needs a target time with a time zone")
    cmd += ["--target-at", target.astimezone(timezone.utc).isoformat()]
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
        elif stage == "prearm":
            self.state = PREARM
        elif stage == "armed":
            self.state, self.snapshot = ARMED, d
        elif stage == "check":
            if d.get("ok"):
                self.state, self.snapshot = CHECK_OK, d
                if d.get("skip_reason"):
                    self.note(f"A run today would be skipped: {d['skip_reason']}")
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
            if not self.has_position and self.state not in (CHECK_OK, PRACTICE_OK):
                self.state = STOPPED
            return
        if self.state in (STARTING, WAITING, PREARM, ARMED):
            # Ended without telling us why: show the last thing it printed.
            self.state = CHECK_FAILED if code == 2 else FAILED
            detail = " ".join(self._plain[-6:]) or (self.notes[-1] if self.notes else "")
            self.message = detail or f"the engine stopped unexpectedly (code {code}); see the log"


def send_gap_ms(buy: SideState, sell: SideState) -> Optional[float]:
    """Milliseconds between the two sends (both measured on this PC's clock), or None."""
    a, b = buy.result.get("send_pc_ts"), sell.result.get("send_pc_ts")
    if a is None or b is None:
        return None
    return abs(float(b) - float(a)) * 1000.0
