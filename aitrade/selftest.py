"""`Aitrade.exe --selftest report.json`: check that the installed program is complete.

The installed program is built from many parts (Python, Qt, the MetaTrader
package, the Windows vault, time zone data). This runs each part once, without
touching any account, and writes what worked to a small file. Nothing is shown
on screen and nothing real is changed: the window is drawn off screen, the
vault test uses a throw-away entry, and the engine is started against a
MetaTrader program that does not exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

SELFTEST_LOGIN = 990000001


def _window() -> str:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from aitrade.ui.app import MainWindow, create_app
    from aitrade.vault import MemoryBackend, Vault

    app = create_app()
    window = MainWindow(Vault(MemoryBackend()), check_clock=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.01)
    image = window.grab()
    page = type(window.stack.currentWidget()).__name__
    icon = not app.windowIcon().isNull()
    window.close()
    if image.width() < 900 or image.height() < 600:
        raise RuntimeError(f"the window is too small: {image.width()}x{image.height()}")
    return f"{page} {image.width()}x{image.height()}, icon {'yes' if icon else 'NO'}, zone {window.zone_label}"


def _timezone() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from hedgebot.config import local_timezone_name

    name = local_timezone_name()
    return f"{name} {datetime.now(ZoneInfo(name)):%H:%M:%S %Z}"


def _vault() -> str:
    from aitrade.vault import Vault

    vault = Vault()
    vault.set("selftest", SELFTEST_LOGIN, "throwaway-value")
    try:
        if vault.get("selftest", SELFTEST_LOGIN) != "throwaway-value":
            raise RuntimeError("the saved value did not come back")
    finally:
        vault.delete("selftest", SELFTEST_LOGIN)
    if vault.get("selftest", SELFTEST_LOGIN):
        raise RuntimeError("the test entry could not be deleted")
    return type(vault.backend.get_keyring()).__name__


def _metatrader_package() -> str:
    import MetaTrader5 as mt5
    import numpy

    return f"MetaTrader5 {mt5.__version__}, numpy {numpy.__version__}"


def _small_parts() -> str:
    import ntplib  # noqa: F401
    import requests  # noqa: F401
    import yaml

    from aitrade import mt5_setup
    from hedgebot import terminal

    return f"yaml {yaml.__version__}, start-up file {len(terminal.startup_config_text())} chars, " \
           f"copies in {mt5_setup.default_mt5_dir()}"


def _engine() -> str:
    """Start the real engine program against a MetaTrader that does not exist: it must answer, not crash."""
    from aitrade import runner
    from aitrade.engine_config import write_engine_configs
    from aitrade.settings import Account, Plan, Settings

    tmp = Path(tempfile.mkdtemp(prefix="aitrade-selftest-"))
    exes = {}
    for side in ("buy", "sell"):
        folder = tmp / f"mt5-{side}"
        folder.mkdir()
        exes[side] = folder / "terminal64.exe"
        exes[side].write_bytes(b"")
    settings = Settings(
        buy=Account(server="Selftest-Demo", login=1001, terminal_path=str(exes["buy"]), symbol="XAUUSD"),
        sell=Account(server="Selftest-Demo", login=2002, terminal_path=str(exes["sell"]), symbol="XAUUSD"),
        plan=Plan(lots=0.01, tp_usd=10.0, date="2030-01-02", time="14:29:45"),
    )
    settings.advanced.use_ntp = False
    configs = write_engine_configs(settings, tmp / "run")
    cmd = runner.worker_command(configs["buy"], mode=runner.MODE_CHECK, log_dir=tmp / "logs",
                                journal=tmp / "journal.csv")
    env = runner.worker_env("buy", "selftest-not-a-password")
    env["HEDGEBOT_LAUNCH"] = "0"
    from aitrade import paths

    result = subprocess.run(cmd, env=env, capture_output=True, timeout=180, cwd=str(paths.project_root()),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
    events = [runner.parse_line(line) for line in out.splitlines()]
    checks = [e.data for e in events if e.kind == "event" and e.data.get("stage") == "check"]
    if not checks:
        raise RuntimeError(f"the engine gave no answer (exit {result.returncode}): {out[-400:]}")
    if checks[-1].get("ok"):
        raise RuntimeError("the engine reported success against a MetaTrader that does not exist")
    error = str(checks[-1].get("error"))
    if "Could not connect to MT5" not in error:
        # Anything else means the engine never got as far as talking to MetaTrader (a missing part).
        raise RuntimeError(f"the engine could not do its job: {error[:300]}")
    return f"{Path(cmd[0]).name} answered (exit {result.returncode}): {str(checks[-1].get('error'))[:120]}"


def run_selftest(report_path: str = "") -> int:
    steps: list[tuple[str, Callable[[], str]]] = [
        ("window", _window), ("time zone", _timezone), ("password vault", _vault),
        ("MetaTrader package", _metatrader_package), ("small parts", _small_parts), ("engine program", _engine),
    ]
    from aitrade import __version__, paths

    report = {"version": __version__, "installed_program": paths.is_installed_program(),
              "folder": str(paths.app_dir()), "python": sys.version.split()[0], "steps": []}
    ok = True
    for name, step in steps:
        started = time.monotonic()
        try:
            info, passed = step(), True
        except Exception as exc:  # every problem must end up in the report
            info, passed = f"{type(exc).__name__}: {exc}", False
            ok = False
        report["steps"].append({"name": name, "ok": passed, "info": info,
                                "seconds": round(time.monotonic() - started, 1)})
    report["ok"] = ok
    text = json.dumps(report, indent=1)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_selftest(sys.argv[1] if len(sys.argv) > 1 else ""))
