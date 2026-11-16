"""One run of the pair: starts the BUY and the SELL engine process and follows them."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .. import paths
from ..runner import FAILED, SideState, parse_line, worker_command, worker_env
from ..settings import SIDES


class PairSession(QObject):
    changed = Signal()  # a side's state changed
    line = Signal(str, str, str)  # side, level, text (for the log box)
    finished = Signal()  # both processes have ended

    def __init__(
        self,
        mode: str,
        config_paths: Mapping[str, Path],
        passwords: Mapping[str, str],
        *,
        target: Optional[datetime] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.mode = mode
        self.target = target
        self._config_paths = dict(config_paths)
        self._passwords = dict(passwords)
        self.states = {side: SideState(side) for side in SIDES}
        self._procs: dict[str, QProcess] = {}
        self._buffers = {side: b"" for side in SIDES}
        self._ended: set[str] = set()
        self._finished_sent = False

    # ------------------------------------------------------------ control

    def start(self) -> None:
        for side in SIDES:
            cmd = worker_command(self._config_paths[side], mode=self.mode, log_dir=paths.log_dir(),
                                 journal=paths.journal_file(), target=self.target)
            proc = QProcess(self)
            proc.setProgram(cmd[0])
            proc.setArguments(cmd[1:])
            env = QProcessEnvironment()
            for key, value in worker_env(side, self._passwords.get(side, "")).items():
                env.insert(key, value)
            proc.setProcessEnvironment(env)
            proc.setWorkingDirectory(str(paths.project_root()))
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.readyReadStandardOutput.connect(partial(self._read, side))
            proc.finished.connect(partial(self._process_finished, side))
            proc.errorOccurred.connect(partial(self._process_error, side))
            self._procs[side] = proc
            self.states[side].started()
        self._passwords.clear()  # not needed in this process any longer
        for proc in self._procs.values():
            proc.start()
        self.changed.emit()

    def stop(self) -> None:
        """End both processes now. Positions that are already open stay open in MT5."""
        for side, proc in self._procs.items():
            if proc.state() != QProcess.ProcessState.NotRunning:
                self.states[side].stop_requested = True
                proc.kill()

    @property
    def active(self) -> bool:
        return bool(self._procs) and len(self._ended) < len(self._procs)

    # ------------------------------------------------------------ process callbacks

    def _feed(self, side: str, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        parsed = parse_line(text)
        self.states[side].on_line(parsed)
        if parsed.text.strip():
            self.line.emit(side, parsed.level, parsed.text)

    def _read(self, side: str) -> None:
        proc = self._procs[side]
        data = self._buffers[side] + bytes(proc.readAllStandardOutput().data())
        *lines, rest = data.split(b"\n")
        self._buffers[side] = rest
        for raw in lines:
            self._feed(side, raw)
        if lines:
            self.changed.emit()

    def _end(self, side: str, code: int) -> None:
        if side in self._ended:
            return
        self._read(side)
        if self._buffers[side].strip():
            self._feed(side, self._buffers[side])
        self._buffers[side] = b""
        self._ended.add(side)
        self.states[side].on_exit(code)
        self.changed.emit()
        if not self.active and not self._finished_sent:
            self._finished_sent = True
            self.finished.emit()

    def _process_finished(self, side: str, code: int, _status=None) -> None:
        self._end(side, int(code))

    def _process_error(self, side: str, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            st = self.states[side]
            st.state = FAILED
            st.message = "The trading engine could not be started. Install the app again."
            self._ended.add(side)
            st.exit_code = -1
            self.changed.emit()
            if not self.active and not self._finished_sent:
                self._finished_sent = True
                self.finished.emit()
