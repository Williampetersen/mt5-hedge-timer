"""Developer tool: draw every screen of the app into PNG files, without MT5 or a broker.

  .venv\\Scripts\\python.exe scripts\\ui_snapshot.py <output folder> [--live]

Uses a throw-away data folder, an in-memory password store and (with --live) the
pretend MetaTrader5 package from tests/fake_site, so nothing real is touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshots").resolve()
    live = "--live" in sys.argv
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="aitrade-snap-"))
    os.environ["AITRADE_HOME"] = str(home / "data")
    fake_site = str(ROOT / "tests" / "fake_site")
    os.environ["PYTHONPATH"] = fake_site + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ.setdefault("FAKE_MT5_CLOSE_AFTER", "6")
    os.environ["HEDGEBOT_LAUNCH"] = "0"  # the pretend broker has no program to start
    os.environ["AITRADE_MT5_DIR"] = str(home / "own-copies")

    def pretend_setup(settings, *, progress, cancelled):
        """Looks like the real setup (progress, then a terminal64.exe per account), without MetaTrader."""
        sides = [s for s in ("buy", "sell") if not Path(settings.account(s).terminal_path).is_file()]
        for index, side in enumerate(sides):
            for step in range(25):
                progress(f"Copying MetaTrader 5 for the {side.upper()} account ({index + 1} of {len(sides)})",
                         step / 25)
                time.sleep(0.08)
            target = Path(settings.account(side).terminal_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
        progress("MetaTrader 5 is ready for both accounts", 1.0)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from aitrade.ui import run_page as run_page_module
    from aitrade.ui.app import MainWindow, create_app
    from aitrade.vault import MemoryBackend, Vault

    app = create_app()
    window = MainWindow(Vault(MemoryBackend()), check_clock=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()

    def pump(seconds: float = 0.15) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def shot(name: str) -> None:
        pump()
        window.grab().save(str(out / f"{name}.png"))
        print("saved", name)

    wizard = window.wizard
    wizard._setup_runner = pretend_setup
    shot("01_wizard_buy_empty")
    wizard.next_btn.click()
    shot("02_wizard_buy_errors")

    details = {
        "buy": ("Alpha Markets", "AlphaMarkets-Demo", "51234567", "XAUUSD"),
        "sell": ("Beta Capital", "BetaCapital-Demo02", "88001234", "XAUUSD.m"),
    }
    for side in ("buy", "sell"):
        form = wizard.forms[side]
        broker, server, login, symbol = details[side]
        form.broker.setText(broker)
        form.server.setText(server)
        form.login.setText(login)
        form.password.edit.setText("demo-password")
        form.symbol.setText(symbol)
        shot(f"03_wizard_{side}_filled")
        wizard.next_btn.click()
    wizard.plan_form.lots.setText("0.06")
    wizard.plan_form.tp.setText("150")
    shot("04_wizard_trade")
    wizard.next_btn.click()
    shot("05_wizard_install_and_check")
    if live:
        wizard.check_btn.click()  # installs (pretend), then checks
        pump(2.2)
        shot("05b_wizard_installing")
        deadline = time.monotonic() + 60
        while (wizard.session is None or wizard.busy) and time.monotonic() < deadline:
            pump(0.2)
        shot("06_wizard_check_done")
    wizard.next_btn.click()  # Finish
    pump(0.3)
    page = window.run_page
    if page is None:
        print("ERROR: the wizard did not finish:", wizard.errors.text())
        return 1
    shot("07_run_idle")

    if live:
        run_page_module.PRACTICE_LEAD_S = 9
        window.settings.advanced.prearm_seconds = 5
        window.settings.advanced.use_ntp = False
        page.practice_btn.click()
        pump(4.0)
        shot("08_practice_countdown")
        deadline = time.monotonic() + 40
        while page.active and time.monotonic() < deadline:
            pump(0.2)
        shot("09_practice_done")

        # A "real" run against the pretend broker, 9 seconds from now.
        from datetime import datetime, timedelta

        from PySide6.QtCore import QDate

        target = datetime.now() + timedelta(seconds=9)
        page.plan_form.date.setDate(QDate(target.year, target.month, target.day))
        page.plan_form.time.setText(target.strftime("%H:%M:%S"))
        page.run_btn.click()
        pump(3.0)
        shot("10_run_countdown")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not all(
                s.has_position for s in page.session.states.values()):
            pump(0.2)
        pump(2.5)
        shot("11_run_open")
        deadline = time.monotonic() + 40
        while page.active and time.monotonic() < deadline:
            pump(0.2)
        shot("12_run_closed")
        print("states:", {k: (v.state, v.message, v.exit_code) for k, v in page.session.states.items()})
        print(page.log.toPlainText()[-3000:])

    window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
