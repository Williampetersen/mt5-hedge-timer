"""The main window: a header with the PC clock, and either the setup wizard or the run screen.

The PC clock in the header is the clock that decides when the positions open.
It is compared with internet time now and then, only to warn when it is wrong.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hedgebot import clock as engine_clock
from hedgebot.config import local_timezone_name

from .. import APP_NAME, __version__, describe, mt5_setup, paths
from ..settings import Settings, load
from ..vault import Vault
from . import theme
from .run_page import RunPage
from .session import delete_startup_files
from .widgets import label
from .wizard import SetupWizard

CLOCK_CHECK_EVERY_MS = 10 * 60 * 1000
CLOCK_FIX_TIP = "To set the PC clock right: Windows Settings > Time & language > Date & time > Sync now."


def zone_label(now: datetime) -> str:
    """'CET, UTC+1' for an aware datetime: the PC's zone the way people say it."""
    offset = now.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    utc = f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")
    name = now.tzname() or ""
    return utc if not name or name.startswith(("+", "-")) or name == "UTC" else f"{name}, {utc}"


class MainWindow(QMainWindow):
    _clock_checked = Signal(object)  # seconds to add to the PC clock, or None if unreachable

    def __init__(self, vault: Optional[Vault] = None, *, check_clock: bool = True):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1120, 760)
        self.setMinimumSize(980, 640)
        self.vault = vault or Vault()
        self.settings: Settings = load(paths.settings_file())
        try:
            self.timezone_name = local_timezone_name()
        except Exception:
            self.timezone_name = "UTC"
        self.tz = ZoneInfo(self.timezone_name)
        self.zone_label = zone_label(datetime.now(self.tz))
        self.run_page: Optional[RunPage] = None
        self.wizard: Optional[SetupWizard] = None

        # ---- header
        header = QFrame()
        header.setObjectName("header")
        self.clock = label("--:--:--", "clock")
        self.date = label("", "muted")
        self.clock_note = label("", "small")
        clock_box = QVBoxLayout()
        clock_box.setSpacing(0)
        clock_box.addWidget(self.clock, alignment=Qt.AlignmentFlag.AlignRight)
        clock_box.addWidget(self.date, alignment=Qt.AlignmentFlag.AlignRight)
        head = QHBoxLayout(header)
        head.setContentsMargins(22, 10, 22, 10)
        head.addWidget(label(APP_NAME, "brand"))
        head.addWidget(label("Two accounts. One moment.", "muted"))
        head.addStretch(1)
        head.addWidget(self.clock_note)
        head.addSpacing(14)
        head.addLayout(clock_box)

        self.stack = QStackedWidget()
        central = QWidget()
        central.setObjectName("page")
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(200)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._tick()

        self._clock_checked.connect(self._on_clock_checked)
        if check_clock:
            self._clock_timer = QTimer(self)
            self._clock_timer.setInterval(CLOCK_CHECK_EVERY_MS)
            self._clock_timer.timeout.connect(self.check_clock)
            self._clock_timer.start()
            self.check_clock()

        if self.settings.setup_done:
            self.show_run()
        else:
            self.show_wizard(can_cancel=False)

    # ------------------------------------------------------------ pages

    def _set_page(self, page: QWidget) -> None:
        while self.stack.count():
            old = self.stack.widget(0)
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)

    def show_wizard(self, can_cancel: bool = True) -> None:
        self.run_page = None
        self.wizard = SetupWizard(self.settings, self.vault, zone_label=self.zone_label,
                                  can_cancel=can_cancel, mt5_dir=mt5_setup.resolve_mt5_dir(self.settings))
        self.wizard.finished.connect(self._wizard_finished)
        self.wizard.cancelled.connect(self.show_run)
        self._set_page(self.wizard)

    def _wizard_finished(self, settings: Settings) -> None:
        self.settings = settings
        self.show_run()

    def show_run(self) -> None:
        self.wizard = None
        self.run_page = RunPage(self.settings, self.vault, tz=self.tz, zone_label=self.zone_label)
        self.run_page.edit_settings.connect(lambda: self.show_wizard(can_cancel=True))
        self._set_page(self.run_page)

    # ------------------------------------------------------------ clock

    def _tick(self) -> None:
        now = datetime.fromtimestamp(time.time(), self.tz)
        self.clock.setText(now.strftime("%H:%M:%S"))
        self.date.setText(f"{now:%a %d %b %Y} · {zone_label(now)}")

    def check_clock(self) -> None:
        """Compare the PC clock with internet time, without blocking the window."""
        server = self.settings.advanced.ntp_server
        if not self.settings.advanced.use_ntp or not server:
            return

        def work() -> None:
            try:
                offset = engine_clock.measure_ntp_offset(server, samples=2)
            except Exception:
                offset = None
            try:
                self._clock_checked.emit(offset)
            except RuntimeError:
                pass  # the window was closed meanwhile

        threading.Thread(target=work, name="clock-check", daemon=True).start()

    def _on_clock_checked(self, offset: Optional[float]) -> None:
        """Show how far the PC clock is from internet time. The PC clock is never shifted."""
        text, warn = describe.clock_note(offset)
        self.clock_note.setText(text)
        self.clock_note.setStyleSheet(f"color: {theme.WARN};" if warn else "")
        self.clock_note.setToolTip(CLOCK_FIX_TIP if warn and offset is not None else "")

    # ------------------------------------------------------------ closing

    def closeEvent(self, event) -> None:
        page = self.run_page
        if page is not None and page.active:
            answer = QMessageBox.question(
                self, "Close Aitrade?",
                "A run is in progress.\n\nIf you close now, nothing more will be opened. Positions that are "
                "already open stay open in MetaTrader 5 with their take profit.\n\nClose anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            page.stop()
            delete_startup_files(0)  # nothing comes back later to do it once the window is gone
        if self.wizard is not None:
            busy = self.wizard.busy
            self.wizard.stop_work()
            if busy:
                delete_startup_files(0)
        event.accept()


def create_app(argv: Optional[list[str]] = None) -> QApplication:
    app = QApplication.instance() or QApplication([APP_NAME, *(argv or [])])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    if paths.icon_file().is_file():
        app.setWindowIcon(QIcon(str(paths.icon_file())))
    theme.apply(app)
    return app


def run(argv: Optional[list[str]] = None) -> int:
    app = create_app(argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
