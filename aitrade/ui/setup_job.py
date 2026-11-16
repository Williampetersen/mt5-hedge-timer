"""Runs the MetaTrader 5 setup (download, install, copy) in the background, so the window stays alive."""

from __future__ import annotations

import copy
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from .. import mt5_setup
from ..settings import Settings


class SetupJob(QObject):
    progress = Signal(str, float)  # what it is doing, and how far (0..1, or -1 when that is unknown)
    done = Signal(bool, str)  # worked?, and the message to show when it did not

    def __init__(self, settings: Settings, run: Optional[Callable] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = copy.deepcopy(settings)  # the thread works on its own copy
        self._run = run or mt5_setup.run_setup
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last = (0.0, "", -2.0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, name="mt5-setup", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _report(self, text: str, fraction: Optional[float] = None) -> None:
        value = -1.0 if fraction is None else max(0.0, min(1.0, float(fraction)))
        now = time.monotonic()
        last_time, last_text, last_value = self._last
        # At most a few updates per second, but never drop a new step or the end of one.
        if text == last_text and now - last_time < 0.2 and value < 1.0 and abs(value - last_value) < 0.2:
            return
        self._last = (now, text, value)
        self._emit(self.progress, text, value)

    def _emit(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            pass  # the window was closed while the job was still running

    def _work(self) -> None:
        try:
            self._run(self._settings, progress=self._report, cancelled=self._cancel.is_set)
        except mt5_setup.SetupCancelled:
            self._emit(self.done, False, "The setup was stopped.")
        except mt5_setup.SetupError as exc:
            self._emit(self.done, False, str(exc))
        except Exception as exc:  # a bug must show up as a message, not as a frozen screen
            self._emit(self.done, False, f"The setup failed: {exc}")
        else:
            self._emit(self.done, True, "")
