"""The engine starting MetaTrader 5 itself: start-up file, first login, Algo Trading, restart."""

from pathlib import Path

import pytest

from conftest import make_cfg
from fake_mt5 import FakeClock, FakeMT5
from hedgebot import terminal
from hedgebot.mt5_client import FIRST_START_TIMEOUT_MS, INIT_TIMEOUT_MS, Mt5Client, Mt5Error


def read_ini(path: Path) -> str:
    raw = path.read_bytes()
    assert raw[:2] == b"\xff\xfe"  # UTF-16 with a byte-order mark, like the terminal's own files
    return raw[2:].decode("utf-16-le")


# ---------------------------------------------------------------- the start-up file


def test_startup_file_only_switches_algo_trading_on_by_default():
    text = terminal.startup_config_text()
    assert "[Common]" not in text and "Password" not in text and "Login" not in text
    assert text.splitlines() == ["[Experts]", "AllowLiveTrading=1", "Enabled=1", "Account=0", "Profile=0"]
    assert text.endswith("\r\n")


def test_startup_file_for_a_first_login_carries_the_account():
    text = terminal.startup_config_text(login=1001, server="Alpha-Demo", password="s3cret=;x")
    lines = text.splitlines()
    assert lines[:5] == ["[Common]", "Login=1001", "Password=s3cret=;x", "Server=Alpha-Demo", "KeepPrivate=1"]
    assert "[Experts]" in lines and "Enabled=1" in lines
    assert "Password" not in terminal.startup_config_text(login=1001, server="Alpha-Demo")


def test_startup_file_is_written_as_utf16_and_wiped(tmp_path):
    path = terminal.write_startup_config(terminal.startup_config_text(login=7, server="S", password="pw"), tmp_path)
    assert path.parent == tmp_path and "Password=pw" in read_ini(path)
    terminal._delete(path)
    assert not path.exists()
    terminal._delete(path)  # deleting twice is fine


# ---------------------------------------------------------------- has this terminal logged in before?


def test_portable_copy_keeps_its_account_file_in_its_own_folder(tmp_path):
    exe = tmp_path / "copy" / "terminal64.exe"
    (tmp_path / "copy").mkdir()
    exe.write_bytes(b"")
    assert terminal.data_folder(str(exe), True) == tmp_path / "copy"
    assert not terminal.has_saved_account(str(exe), True)
    (tmp_path / "copy" / "Config").mkdir()
    (tmp_path / "copy" / "Config" / "accounts.dat").write_bytes(b"x")
    assert terminal.has_saved_account(str(exe), True)


def test_installed_terminal_keeps_it_under_appdata(tmp_path, monkeypatch):
    install = tmp_path / "Program Files" / "Broker MT5"
    install.mkdir(parents=True)
    exe = install / "terminal64.exe"
    exe.write_bytes(b"")
    data = tmp_path / "Roaming" / "MetaQuotes" / "Terminal" / "ABC123"
    (data / "config").mkdir(parents=True)
    (data / "origin.txt").write_bytes(str(install).encode("utf-16"))
    other = tmp_path / "Roaming" / "MetaQuotes" / "Terminal" / "ZZZ"
    other.mkdir()
    (other / "origin.txt").write_bytes("C:\\Somewhere else".encode("utf-16"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert terminal.data_folder(str(exe), False) == data
    assert not terminal.has_saved_account(str(exe), False)
    (data / "config" / "accounts.dat").write_bytes(b"x")
    assert terminal.has_saved_account(str(exe), False)
    assert terminal.data_folder(str(tmp_path / "unknown" / "terminal64.exe"), False) is None


# ---------------------------------------------------------------- ensure_started


class Recorder:
    """Stands in for the Windows parts of terminal.py."""

    def __init__(self, monkeypatch, *, running=False, dialogs=()):
        self.started = []  # (portable, text of the start-up file at the moment of starting)
        self.closed_dialogs = 0
        self.running = running
        self.dialogs = list(dialogs)
        self.config_path = None
        monkeypatch.setattr(terminal, "launch_enabled", lambda: True)
        monkeypatch.setattr(terminal, "is_running", lambda exe: self.running)
        monkeypatch.setattr(terminal, "wait_main_window", lambda exe, timeout, sleep=None: True)
        monkeypatch.setattr(terminal, "blocking_dialog", lambda exe: self.dialogs.pop(0) if self.dialogs else None)
        monkeypatch.setattr(terminal, "close_dialogs", self._close_dialogs)
        monkeypatch.setattr(terminal, "start", self._start)
        monkeypatch.setattr(terminal, "FIRST_LOGIN_SETTLE_S", 0.05)
        monkeypatch.setattr(terminal, "NORMAL_SETTLE_S", 0.02)
        monkeypatch.setattr(terminal, "MIN_CONFIG_LIFETIME_S", 0.0)

    def _close_dialogs(self, exe):
        self.closed_dialogs += 1

    def _start(self, exe, *, portable, config=None):
        self.config_path = config
        self.started.append((portable, read_ini(config)))


def fresh_copy(tmp_path, *, logged_in=False):
    folder = tmp_path / "buy"
    folder.mkdir()
    exe = folder / "terminal64.exe"
    exe.write_bytes(b"")
    if logged_in:
        (folder / "config").mkdir()
        (folder / "config" / "accounts.dat").write_bytes(b"x")
    return str(exe)


KW = dict(login=1001, server="Alpha-Demo", password="pw-b", sleep=lambda s: None)


def test_switched_off_means_hands_off(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch)
    monkeypatch.setattr(terminal, "launch_enabled", lambda: False)
    assert terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW).status == terminal.SKIPPED
    assert rec.started == []


def test_a_running_terminal_is_left_alone(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch, running=True)
    assert terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW).status == terminal.RUNNING
    assert rec.started == []


def test_a_new_copy_gets_the_account_once_and_the_file_is_gone_afterwards(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch)
    result = terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW)
    assert result.status == terminal.FIRST_LOGIN and result.dialog == ""
    portable, text = rec.started[0]
    assert portable is True
    assert "Login=1001" in text and "Password=pw-b" in text and "Server=Alpha-Demo" in text and "Enabled=1" in text
    assert not rec.config_path.exists()  # the file with the password does not stay on disk


def test_a_copy_that_logged_in_before_never_gets_a_password_file(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch)
    result = terminal.ensure_started(fresh_copy(tmp_path, logged_in=True), portable=True, **KW)
    assert result.status == terminal.STARTED
    text = rec.started[0][1]
    assert "Password" not in text and "Login" not in text and "Enabled=1" in text
    assert not rec.config_path.exists()


def test_the_users_own_metatrader_never_gets_a_password_file(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch)
    monkeypatch.setenv("APPDATA", str(tmp_path / "nothing"))
    result = terminal.ensure_started(fresh_copy(tmp_path), portable=False, **KW)
    assert result.status == terminal.STARTED
    assert rec.started[0][0] is False and "Password" not in rec.started[0][1]


def test_a_blocking_window_is_closed_and_reported(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch, dialogs=["Open an Account: MetaQuotes Ltd."])
    result = terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW)
    assert result.status == terminal.FIRST_LOGIN and result.dialog == "Open an Account: MetaQuotes Ltd."
    assert rec.closed_dialogs == 1


def test_a_program_that_cannot_start_is_not_fatal(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch)

    def boom(exe, *, portable, config=None):
        rec.config_path = config
        raise OSError("not a valid application")

    monkeypatch.setattr(terminal, "start", boom)
    assert terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW).status == terminal.FAILED
    assert not rec.config_path.exists()


def test_launch_can_be_switched_off_with_an_environment_variable(monkeypatch):
    monkeypatch.setattr(terminal.sys, "platform", "win32")
    monkeypatch.delenv(terminal.LAUNCH_ENV, raising=False)
    assert terminal.launch_enabled()
    for value in ("0", "false", "No"):
        monkeypatch.setenv(terminal.LAUNCH_ENV, value)
        assert not terminal.launch_enabled()
    monkeypatch.setenv(terminal.LAUNCH_ENV, "1")
    monkeypatch.setattr(terminal.sys, "platform", "linux")
    assert not terminal.launch_enabled()


# ---------------------------------------------------------------- the engine's connect()


class FakeLauncher:
    def __init__(self, status=terminal.STARTED, dialog="", on_close=None):
        self.status, self.dialog, self.on_close = status, dialog, on_close
        self.calls, self.closed = [], 0

    def ensure_started(self, exe, **kwargs):
        self.calls.append((exe, kwargs))
        return terminal.Launch(self.status, self.dialog)

    def close(self, exe, **kwargs):
        self.closed += 1
        if self.on_close:
            self.on_close()
        return True


def client(buy_raw, launcher, *, launch=True, mt5=None):
    buy_raw["account"]["launch"] = launch
    buy_raw["account"]["portable"] = True
    clock = FakeClock(1_794_000_000.0)
    mt5 = mt5 or FakeMT5(clock)
    return Mt5Client(make_cfg(buy_raw), clock=clock, mt5=mt5, sleep=clock.sleep, launcher=launcher), mt5


def test_launch_is_off_unless_the_config_asks_for_it(buy_raw):
    launcher = FakeLauncher()
    c, mt5 = client(buy_raw, launcher, launch=False)
    c.connect()
    assert launcher.calls == [] and mt5.init_args[1]["timeout"] == INIT_TIMEOUT_MS


def test_connect_starts_the_terminal_with_the_account(buy_raw):
    launcher = FakeLauncher()
    c, mt5 = client(buy_raw, launcher)
    c.connect()
    exe, kwargs = launcher.calls[0]
    assert exe == "C:\\MT5_Buy\\terminal64.exe"
    assert kwargs["portable"] is True and kwargs["login"] == 1001 and kwargs["server"] == "Broker-Demo"
    assert kwargs["password"] == "pw-buy"
    assert c.connected and mt5.init_args[1]["timeout"] == INIT_TIMEOUT_MS and launcher.closed == 0


def test_a_first_login_gets_more_time(buy_raw):
    c, mt5 = client(buy_raw, FakeLauncher(terminal.FIRST_LOGIN))
    c.connect()
    assert mt5.init_args[1]["timeout"] == FIRST_START_TIMEOUT_MS


def test_a_terminal_opened_by_hand_is_restarted_with_algo_trading_on(buy_raw):
    clock = FakeClock(1_794_000_000.0)
    mt5 = FakeMT5(clock)
    mt5.terminal.trade_allowed = False  # opened by hand: Algo Trading is off

    def restarted():
        mt5.terminal.trade_allowed = True

    launcher = FakeLauncher(terminal.RUNNING, on_close=restarted)
    c, _ = client(buy_raw, launcher, mt5=mt5)
    c.connect()
    assert launcher.closed == 1 and len(launcher.calls) == 2 and c.connected
    c.check_account()  # no "Algo Trading is OFF" any more


def test_the_restart_happens_only_once(buy_raw):
    clock = FakeClock(1_794_000_000.0)
    mt5 = FakeMT5(clock)
    mt5.terminal.trade_allowed = False
    launcher = FakeLauncher(terminal.STARTED)
    c, _ = client(buy_raw, launcher, mt5=mt5)
    c.connect()
    assert launcher.closed == 1 and len(launcher.calls) == 2
    with pytest.raises(Mt5Error, match="Algo Trading is OFF"):
        c.check_account()


def test_a_failed_first_login_says_so(buy_raw):
    clock = FakeClock(1_794_000_000.0)
    mt5 = FakeMT5(clock)
    mt5.initialize = lambda *a, **k: False
    mt5.last_err = (-10001, "IPC send failed")
    launcher = FakeLauncher(terminal.FIRST_LOGIN, dialog="Open an Account")
    c, _ = client(buy_raw, launcher, mt5=mt5)
    with pytest.raises(Mt5Error) as exc:
        c.connect()
    text = str(exc.value)
    assert "IPC send failed" in text and "did not log in with this account number, password and server" in text
    assert "'Open an Account'" in text and len(launcher.calls) == 1  # refused: no second try


def test_a_failed_connection_is_tried_twice_when_nothing_was_refused(buy_raw):
    clock = FakeClock(1_794_000_000.0)
    mt5 = FakeMT5(clock)
    mt5.initialize = lambda *a, **k: False
    mt5.last_err = (-10005, "IPC timeout")
    launcher = FakeLauncher(terminal.STARTED)
    c, _ = client(buy_raw, launcher, mt5=mt5)
    with pytest.raises(Mt5Error, match="IPC timeout"):
        c.connect()
    assert len(launcher.calls) == 2  # started again (our way) for the second try


def test_a_terminal_stuck_behind_a_window_gets_that_window_closed(tmp_path, monkeypatch):
    rec = Recorder(monkeypatch, running=True, dialogs=["Open an Account"])
    result = terminal.ensure_started(fresh_copy(tmp_path), portable=True, **KW)
    assert result.status == terminal.RUNNING and result.dialog == "Open an Account"
    assert rec.closed_dialogs == 1 and rec.started == []


def test_a_broken_launcher_does_not_stop_the_engine(buy_raw):
    class Broken:
        def ensure_started(self, exe, **kwargs):
            raise RuntimeError("no windows here")

    c, mt5 = client(buy_raw, Broken())
    c.connect()
    assert c.connected
