"""The window itself, driven like a user would, against the pretend MetaTrader5 package.

Nothing is shown on screen and nothing real is touched: the data folder is a
temporary one, passwords go to an in-memory store, and the engine processes
import tests/fake_site/MetaTrader5.py instead of the real package.
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QDate, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aitrade import paths, runner  # noqa: E402
from aitrade.settings import Account, Plan, Settings, load, save  # noqa: E402
from aitrade.ui import run_page as run_page_module  # noqa: E402
from aitrade.ui.app import MainWindow, create_app, zone_label  # noqa: E402
from aitrade.vault import MemoryBackend, Vault  # noqa: E402

FAKE_SITE = str(Path(__file__).resolve().parent / "fake_site")


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("PYTHONPATH", FAKE_SITE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("FAKE_MT5_CLOSE_AFTER", "2")
    monkeypatch.setenv("HEDGEBOT_LAUNCH", "0")  # the pretend broker has no program to start
    monkeypatch.setenv("AITRADE_MT5_DIR", str(tmp_path / "own-copies"))
    exes = {}
    for side in ("buy", "sell"):
        folder = tmp_path / f"MT5_{side}"
        folder.mkdir()
        exes[side] = folder / "terminal64.exe"
        exes[side].write_bytes(b"")
    return exes


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def wait_until(condition, timeout=40.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.05)
        if condition():
            return True
    return False


def open_window(vault=None):
    window = MainWindow(vault or Vault(MemoryBackend()), check_clock=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    pump()
    return window


def saved_setup(exes, *, prearm=5) -> Vault:
    """Settings as if the wizard had been completed, with both passwords in the vault."""
    settings = Settings(
        buy=Account(broker="Alpha", server="Alpha-Demo", login=1001, terminal_path=str(exes["buy"]),
                    symbol="XAUUSD"),
        sell=Account(broker="Beta", server="Beta-Demo", login=2002, terminal_path=str(exes["sell"]),
                     symbol="XAUUSD.m"),
        plan=Plan(lots=0.06, tp_usd=150.0, date="2030-01-02", time="14:29:45"),
        setup_done=True,
    )
    settings.advanced.prearm_seconds = prearm
    settings.advanced.use_ntp = False
    save(settings, paths.settings_file())
    vault = Vault(MemoryBackend())
    vault.set("buy", 1001, "pw-b")
    vault.set("sell", 2002, "pw-s")
    return vault


# ---------------------------------------------------------------- first-time setup


def test_wizard_from_empty_to_the_run_screen(qapp, home):
    backend = MemoryBackend()
    window = open_window(Vault(backend))
    wizard = window.wizard
    assert wizard is not None and window.run_page is None
    assert not wizard.cancel_btn.isVisible()  # first run: nothing to go back to

    wizard.next_btn.click()  # everything is empty
    assert wizard.pages.currentIndex() == 0 and wizard.errors.isVisible()
    assert "type the password" in wizard.errors.text()

    typed = {"buy": ("Alpha", "Alpha-Demo", "1001", "XAUUSD"), "sell": ("Beta", "Beta-Demo", "2002", "GOLD")}
    for step, side in enumerate(("buy", "sell")):
        form = wizard.forms[side]
        broker, server, login, symbol = typed[side]
        form.broker.setText(broker)
        form.server.setText(server)
        form.login.setText(login)
        form.password.edit.setText(f"pw-{side}")
        form.symbol.setText(symbol)
        form._select_path(str(home[side]))
        wizard.next_btn.click()
        assert wizard.pages.currentIndex() == step + 1, wizard.errors.text()
    wizard.plan_form.lots.setText("0,06")  # a comma works too
    wizard.plan_form.tp.setText("150")
    wizard.plan_form.time.setText("09:15:30")
    wizard.next_btn.click()
    assert wizard.pages.currentIndex() == 3 and wizard.next_btn.text() == "Finish"
    wizard.next_btn.click()
    pump()

    assert window.run_page is not None and window.wizard is None
    saved = load(paths.settings_file())
    assert saved.setup_done and saved.buy.login == 1001 and saved.sell.symbol == "GOLD"
    assert saved.plan.lots == 0.06 and saved.plan.tp_usd == 150.0 and saved.plan.time == "09:15:30"
    assert Vault(backend).get("buy", 1001) == "pw-buy" and Vault(backend).get("sell", 2002) == "pw-sell"
    assert "pw-" not in paths.settings_file().read_text(encoding="utf-8")
    window.close()


def test_wizard_refuses_the_same_mt5_folder_twice(qapp, home):
    window = open_window()
    wizard = window.wizard
    for side, login in (("buy", "1001"), ("sell", "2002")):
        form = wizard.forms[side]
        form.server.setText("Alpha-Demo")
        form.login.setText(login)
        form.password.edit.setText("pw")
        form.symbol.setText("XAUUSD")
        form._select_path(str(home["buy"]))  # the same folder for both
        wizard.next_btn.click()
    assert wizard.pages.currentIndex() == 1
    assert "own copy of MetaTrader 5" in wizard.errors.text()
    window.close()


def test_changing_an_account_moves_the_saved_password(qapp, home):
    vault = saved_setup(home)
    window = open_window(vault)
    window.run_page.settings_btn.click()
    pump()
    wizard = window.wizard
    assert wizard is not None and wizard.cancel_btn.isVisible()
    assert wizard.forms["buy"].login.text() == "1001"
    assert "Saved" in wizard.forms["buy"].password.edit.placeholderText()

    wizard.forms["buy"].login.setText("1005")  # another account: its password is not saved yet
    wizard.next_btn.click()
    assert wizard.pages.currentIndex() == 0 and "type the password" in wizard.errors.text()
    wizard.forms["buy"].password.edit.setText("pw-new")
    for _ in range(4):
        wizard.next_btn.click()
    pump()
    assert window.run_page is not None
    assert vault.get("buy", 1005) == "pw-new" and vault.get("buy", 1001) == ""  # the old one is removed
    assert vault.get("sell", 2002) == "pw-s"
    window.close()


def test_cancel_keeps_the_old_settings(qapp, home):
    vault = saved_setup(home)
    window = open_window(vault)
    window.run_page.settings_btn.click()
    pump()
    window.wizard.forms["buy"].login.setText("999")
    window.wizard.cancel_btn.click()
    pump()
    assert window.run_page is not None and load(paths.settings_file()).buy.login == 1001
    window.close()


# ---------------------------------------------------------------- the run screen


def test_run_screen_shows_the_plan_and_blocks_bad_input(qapp, home):
    vault = saved_setup(home)
    window = open_window(vault)
    page = window.run_page
    assert page.plan_form.lots.text() == "0.06" and page.plan_form.tp.text() == "150.00"
    assert page.plan_form.time.text() == "14:29:45" and page.run_btn.text() == "Run"
    assert "Run opens both positions at 2030-01-02 14:29:45" in page.sub.text()

    page.plan_form.date.setDate(QDate(2026, 1, 1))  # a day in the past
    pump()
    assert "already passed" in page.sub.text()
    page.run_btn.click()
    assert not page.active and "already passed" in page.errors.text()

    page.plan_form.date.setDate(QDate(2030, 1, 2))
    page.plan_form.lots.setText("")
    page.run_btn.click()
    assert not page.active and "Lot size" in page.errors.text()

    page.plan_form.lots.setText("0.06")
    vault.delete("sell", 2002)
    page.run_btn.click()
    assert not page.active and "password of the SELL account is not saved" in page.errors.text()
    window.close()


def test_check_accounts_passes_against_the_pretend_broker(qapp, home):
    window = open_window(saved_setup(home))
    page = window.run_page
    page.check_btn.click()
    assert page.active and page.run_btn.text() == "Stop" and not page.check_btn.isEnabled()
    assert wait_until(lambda: not page.active)
    states = page.session.states
    assert {s.state for s in states.values()} == {runner.CHECK_OK}, {k: v.message for k, v in states.items()}
    assert states["buy"].snapshot["login"] == 1001 and states["sell"].snapshot["symbol"] == "XAUUSD.m"
    assert page.headline.text() == "Both accounts are ready"
    assert "Take profit 150.00 USD" in page.cards["buy"].details.text()
    assert page.run_btn.text() == "Run" and page.check_btn.isEnabled()
    window.close()


def test_a_wrong_password_is_reported_for_that_side_only(qapp, home, monkeypatch):
    monkeypatch.setenv("FAKE_MT5_FAIL_LOGIN", "2002")
    window = open_window(saved_setup(home))
    page = window.run_page
    page.check_btn.click()
    assert wait_until(lambda: not page.active, timeout=60)
    states = page.session.states
    assert states["buy"].state == runner.CHECK_OK
    assert states["sell"].state == runner.CHECK_FAILED and "Authorization failed" in states["sell"].message
    assert page.headline.text() == "A check failed"
    assert "Could not connect to MT5" in page.cards["sell"].details.text()
    window.close()


def test_practice_run_sends_nothing(qapp, home, monkeypatch):
    monkeypatch.setattr(run_page_module, "PRACTICE_LEAD_S", 7)
    window = open_window(saved_setup(home))
    page = window.run_page
    page.practice_btn.click()
    assert page.active
    assert wait_until(lambda: page.headline.text().startswith("Practice in"), timeout=10)
    assert wait_until(lambda: not page.active)
    states = page.session.states
    assert {s.state for s in states.values()} == {runner.PRACTICE_OK}, {k: v.message for k, v in states.items()}
    assert page.headline.text() == "Practice run OK"
    gap = runner.send_gap_ms(states["buy"], states["sell"])
    assert gap is not None and gap < 50  # the same instant, on two processes
    assert abs(states["buy"].result["deviation_ms"]) < 100
    journal = paths.journal_file().read_text(encoding="utf-8")
    assert journal.count("dry_run") == 2 and "opened" not in journal
    window.close()


def test_a_run_opens_both_sides_at_the_set_second_and_follows_them(qapp, home):
    window = open_window(saved_setup(home))
    page = window.run_page
    target = datetime.now().replace(microsecond=0) + timedelta(seconds=8)
    page.plan_form.date.setDate(QDate(target.year, target.month, target.day))
    page.plan_form.time.setText(target.strftime("%H:%M:%S"))
    page.run_btn.click()
    assert page.active, page.errors.text()
    assert page.phase == run_page_module.RUN  # too close to the time for the pre-test
    assert not page.plan_form.lots.isEnabled()  # the plan is locked while it runs
    assert wait_until(lambda: page.headline.text().startswith("Opens in"), timeout=10)
    assert wait_until(lambda: all(s.has_position for s in page.session.states.values()), timeout=30)
    states = page.session.states
    for side in ("buy", "sell"):
        sent = datetime.fromtimestamp(states[side].result["send_ts"])
        assert abs((sent - target).total_seconds()) < 0.25, side  # opened at the second that was set
    assert wait_until(lambda: not page.active, timeout=30)
    assert {s.state for s in states.values()} == {runner.CLOSED}
    assert page.headline.text() == "Both positions are closed" and "together" in page.sub.text()
    assert "Closed: take profit reached" == page.cards["buy"].state.text()
    assert page.run_btn.text() == "Run" and page.plan_form.lots.isEnabled()
    assert paths.journal_file().read_text(encoding="utf-8").count("opened") == 2
    window.close()


def test_run_with_time_to_spare_tests_the_accounts_first_and_stop_ends_it(qapp, home):
    window = open_window(saved_setup(home))
    page = window.run_page
    target = datetime.now() + timedelta(minutes=30)
    page.plan_form.date.setDate(QDate(target.year, target.month, target.day))
    page.plan_form.time.setText(target.strftime("%H:%M:%S"))
    page.run_btn.click()
    assert page.active and page.phase == run_page_module.PRECHECK
    assert wait_until(lambda: page.phase == run_page_module.RUN, timeout=40), page.errors.text()
    assert wait_until(lambda: all(s.state == runner.WAITING for s in page.session.states.values()), timeout=20)
    assert page.headline.text().startswith("Opens in 00:2")
    page.stop()  # the button asks "Stop now?" first; this is what Yes does
    assert wait_until(lambda: not page.active, timeout=15)
    assert {s.state for s in page.session.states.values()} == {runner.STOPPED}
    assert page.headline.text() == "Stopped" and page.run_btn.text() == "Run"
    assert not paths.journal_file().exists()  # nothing was sent
    window.close()


def test_a_failed_pre_test_does_not_start_the_run(qapp, home, monkeypatch):
    monkeypatch.setenv("FAKE_MT5_FAIL_LOGIN", "1001")
    window = open_window(saved_setup(home))
    page = window.run_page
    page.run_btn.click()  # 2030: lots of time, so the accounts are tested first
    assert page.phase == run_page_module.PRECHECK
    assert wait_until(lambda: not page.active, timeout=60)
    pump(0.3)
    assert page.phase == run_page_module.IDLE and not page.active
    assert "was not started because the test failed" in page.errors.text()
    assert page.session.states["buy"].state == runner.CHECK_FAILED
    window.close()


# ---------------------------------------------------------------- Aitrade's own MetaTrader 5 copies


def pretend_setup(settings, *, progress, cancelled):
    """What the real setup leaves behind, without any MetaTrader: a terminal64.exe per own copy."""
    done = []
    for index, side in enumerate(("buy", "sell")):
        account = settings.account(side)
        if account.managed and not Path(account.terminal_path).is_file():
            progress(f"Copying MetaTrader 5 for the {side.upper()} account", index / 2)
            Path(account.terminal_path).parent.mkdir(parents=True, exist_ok=True)
            Path(account.terminal_path).write_bytes(b"")
            done.append(side)
    progress("MetaTrader 5 is ready for both accounts", 1.0)
    return done


def to_the_last_step(wizard):
    """Type two accounts and a trade, leaving "MetaTrader 5 for this account" on Aitrade's own copy."""
    for step, (side, login) in enumerate((("buy", "1001"), ("sell", "2002"))):
        form = wizard.forms[side]
        assert form.is_managed()  # the default choice
        form.server.setText(f"{side.capitalize()}-Demo")
        form.login.setText(login)
        form.password.edit.setText(f"pw-{side}")
        form.symbol.setText("XAUUSD")
        wizard.next_btn.click()
        assert wizard.pages.currentIndex() == step + 1, wizard.errors.text()
    wizard.plan_form.lots.setText("0.06")
    wizard.plan_form.tp.setText("150")
    wizard.next_btn.click()
    assert wizard.pages.currentIndex() == 3, wizard.errors.text()


def test_wizard_installs_its_own_copies_and_then_checks_them(qapp, home, tmp_path):
    window = open_window()
    wizard = window.wizard
    wizard._setup_runner = pretend_setup
    own = tmp_path / "own-copies"
    assert wizard.mt5_dir == own
    to_the_last_step(wizard)
    assert wizard.check_btn.text() == "Install MetaTrader 5 and check both accounts"
    assert not wizard.open_btns["buy"].isVisible()  # nothing to open yet

    wizard.check_btn.click()
    assert wizard.busy
    assert wait_until(lambda: wizard.session is not None and not wizard.busy, timeout=60), wizard.errors.text()
    assert (own / "buy" / "terminal64.exe").is_file() and (own / "sell" / "terminal64.exe").is_file()
    states = wizard.session.states
    assert {s.state for s in states.values()} == {runner.CHECK_OK}, {k: v.message for k, v in states.items()}
    assert wizard.check_headline.text() == "Both accounts are ready"
    assert wizard.check_btn.text() == "Check both accounts again"
    assert wizard.open_btns["buy"].isVisible() and not wizard.setup_bar.isVisible()

    wizard.next_btn.click()  # Finish
    pump()
    saved = load(paths.settings_file())
    assert saved.setup_done and saved.mt5_dir == str(own)
    for side in ("buy", "sell"):
        account = saved.account(side)
        assert account.managed and account.portable
        assert account.terminal_path == str(own / side / "terminal64.exe")
    assert window.run_page is not None
    window.close()


def test_a_failed_install_is_explained_and_nothing_is_checked(qapp, home):
    from aitrade import mt5_setup

    def no_internet(settings, *, progress, cancelled):
        progress("Downloading MetaTrader 5", 0.1)
        raise mt5_setup.SetupError("Could not download MetaTrader 5 from MetaQuotes: no internet")

    window = open_window()
    wizard = window.wizard
    wizard._setup_runner = no_internet
    to_the_last_step(wizard)
    wizard.check_btn.click()
    assert wait_until(lambda: not wizard.busy and wizard.errors.isVisible(), timeout=20)
    assert "Could not download MetaTrader 5" in wizard.errors.text()
    assert wizard.session is None  # no check without MetaTrader
    assert wizard.check_headline.text() == "MetaTrader 5 is not set up yet"
    assert wizard.check_btn.text() == "Install MetaTrader 5 and check both accounts"
    window.close()


def test_the_install_can_be_stopped(qapp, home):
    def slow(settings, *, progress, cancelled):
        from aitrade import mt5_setup

        for i in range(200):
            if cancelled():
                raise mt5_setup.SetupCancelled()
            progress("Copying MetaTrader 5 for the BUY account (1 of 2)", i / 200)
            time.sleep(0.02)

    window = open_window()
    wizard = window.wizard
    wizard._setup_runner = slow
    to_the_last_step(wizard)
    wizard.check_btn.click()
    assert wait_until(lambda: "Copying MetaTrader 5" in wizard.check_sub.text(), timeout=10)
    assert wizard.check_btn.text() == "Stop" and wizard.setup_bar.isVisible()
    wizard.check_btn.click()  # Stop
    assert wait_until(lambda: not wizard.busy and wizard.errors.isVisible(), timeout=10)
    assert "The setup was stopped." in wizard.errors.text() and wizard.session is None
    window.close()


def test_run_screen_says_when_an_own_copy_is_missing(qapp, home, tmp_path):
    vault = saved_setup(home)
    settings = load(paths.settings_file())
    settings.buy.managed = settings.buy.portable = True
    settings.buy.terminal_path = str(tmp_path / "own-copies" / "buy" / "terminal64.exe")
    save(settings, paths.settings_file())
    window = open_window(vault)
    page = window.run_page
    page.check_btn.click()
    assert not page.active
    assert "MetaTrader 5 is not installed yet for the BUY account" in page.errors.text()
    window.close()


def test_an_own_copy_is_remembered_when_the_accounts_are_changed(qapp, home, tmp_path):
    vault = saved_setup(home)
    settings = load(paths.settings_file())
    own = tmp_path / "own-copies"
    for side in ("buy", "sell"):
        account = settings.account(side)
        account.managed = account.portable = True
        account.terminal_path = str(own / side / "terminal64.exe")
        Path(account.terminal_path).parent.mkdir(parents=True)
        Path(account.terminal_path).write_bytes(b"")
    settings.mt5_dir = str(own)
    save(settings, paths.settings_file())
    window = open_window(vault)
    window.run_page.settings_btn.click()
    pump()
    wizard = window.wizard
    assert wizard.forms["buy"].is_managed() and wizard.forms["sell"].is_managed()
    assert "own copy" in wizard.forms["buy"].terminal.currentText()
    wizard.forms["sell"]._select_path(str(home["sell"]))  # switch SELL to the user's own MetaTrader
    for _ in range(3):
        wizard.next_btn.click()
    assert wizard.check_btn.text() == "Check both accounts now"  # nothing to install
    wizard.next_btn.click()
    pump()
    saved = load(paths.settings_file())
    assert saved.buy.managed and saved.buy.portable
    assert not saved.sell.managed and not saved.sell.portable and saved.sell.terminal_path == str(home["sell"])
    window.close()


def test_zone_label():
    from zoneinfo import ZoneInfo

    assert zone_label(datetime(2026, 11, 16, 12, tzinfo=ZoneInfo("Europe/Copenhagen"))) == "CET, UTC+1"
    assert zone_label(datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Europe/Copenhagen"))) == "CEST, UTC+2"
    assert zone_label(datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Asia/Kolkata"))) == "IST, UTC+5:30"
    assert zone_label(datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("America/New_York"))) == "EDT, UTC-4"
