"""The app's own logic (no window): settings, password vault, engine files, terminals."""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

import hedgebot.config as engine_config_module
from aitrade import paths, terminals
from aitrade.engine_config import PASSWORD_ENV, build_engine_config, check_engine_configs, write_engine_configs
from aitrade.settings import (
    Account,
    Plan,
    Settings,
    from_dict,
    load,
    parse_time,
    save,
    target_datetime,
    to_dict,
    validate_account,
    validate_accounts,
    validate_plan,
)
from aitrade.vault import SERVICE, MemoryBackend, Vault, VaultError

CPH = ZoneInfo("Europe/Copenhagen")


@pytest.fixture(autouse=True)
def pc_in_copenhagen(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_config_module, "local_timezone_name", lambda: "Europe/Copenhagen")
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "home"))


def make_settings(tmp_path: Path) -> Settings:
    exes = {}
    for side in ("buy", "sell"):
        folder = tmp_path / f"MT5_{side}"
        folder.mkdir(exist_ok=True)
        exes[side] = folder / "terminal64.exe"
        exes[side].write_bytes(b"")
    return Settings(
        buy=Account(broker="Alpha", server="Alpha-Demo", login=1001, terminal_path=str(exes["buy"]),
                    symbol="XAUUSD"),
        sell=Account(broker="Beta", server="Beta-Demo", login=2002, terminal_path=str(exes["sell"]),
                     symbol="GOLD"),
        plan=Plan(lots=0.06, tp_usd=150.0, sl_usd=None, date="2026-12-04", time="14:29:45"),
        setup_done=True,
    )


# ---------------------------------------------------------------- settings file


def test_settings_round_trip(tmp_path):
    s = make_settings(tmp_path)
    s.sell.real = True
    s.plan.sl_usd = 75.5
    file = tmp_path / "x" / "settings.json"
    save(s, file)
    assert load(file) == s
    assert not file.with_suffix(".tmp").exists()


def test_settings_file_never_holds_a_password(tmp_path):
    s = make_settings(tmp_path)
    file = tmp_path / "settings.json"
    save(s, file)
    assert "password" not in file.read_text(encoding="utf-8").lower()


def test_missing_file_gives_fresh_settings(tmp_path):
    s = load(tmp_path / "nope.json")
    assert s == Settings() and s.setup_done is False


def test_damaged_file_is_kept_aside(tmp_path):
    file = tmp_path / "settings.json"
    file.write_text("{ not json", encoding="utf-8")
    assert load(file) == Settings()
    assert (tmp_path / "settings.damaged.json").exists()


def test_unknown_keys_and_wrong_types_are_ignored():
    s = from_dict({
        "buy": {"login": "abc", "server": 5, "real": "yes", "extra": 1, "symbol": "XAUUSD"},
        "plan": {"lots": "big", "tp_usd": 200, "sl_usd": None, "time": "09:00:00"},
        "advanced": {"prearm_seconds": True, "magic": 77},
        "setup_done": "true",
    })
    assert s.buy.login == 0 and s.buy.server == "" and s.buy.real is False and s.buy.symbol == "XAUUSD"
    assert s.plan.lots == 0.01 and s.plan.tp_usd == 200.0 and s.plan.time == "09:00:00"
    assert s.advanced.prearm_seconds == 30 and s.advanced.magic == 77
    assert s.setup_done is False
    assert to_dict(s)["version"] == 1


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("text, ok", [
    ("14:29:45", True), ("00:00:00", True), ("23:59:59", True),
    ("14:29", False), ("24:00:00", False), ("9:05:00", False), ("14:60:00", False), ("", False),
])
def test_time_must_be_hh_mm_ss(text, ok):
    assert (parse_time(text) is not None) is ok


def test_a_good_setup_has_no_problems(tmp_path):
    s = make_settings(tmp_path)
    assert validate_accounts(s) == [] and validate_plan(s.plan) == []


def test_account_problems_name_the_field(tmp_path):
    problems = validate_account(Account(), "buy")
    assert {p.field for p in problems} == {"buy.server", "buy.login", "buy.symbol", "buy.terminal_path"}
    assert all(p.message.startswith("BUY account") for p in problems)

    s = make_settings(tmp_path)
    s.buy.terminal_path = str(tmp_path / "MT5_buy" / "metaeditor64.exe")
    assert [p.field for p in validate_account(s.buy, "buy")] == ["buy.terminal_path"]
    s.buy.terminal_path = str(tmp_path / "gone" / "terminal64.exe")
    assert "does not exist" in validate_account(s.buy, "buy")[0].message
    s.buy.symbol = "XAU USD"
    assert any(p.field == "buy.symbol" for p in validate_account(s.buy, "buy"))


def test_the_two_accounts_must_differ(tmp_path):
    s = make_settings(tmp_path)
    s.sell.login, s.sell.server = s.buy.login, "alpha-demo"
    assert any("same account" in p.message for p in validate_accounts(s))

    s = make_settings(tmp_path)
    s.sell.terminal_path = s.buy.terminal_path.upper().replace("\\", "/")
    assert any("own copy" in p.message for p in validate_accounts(s))


def test_same_login_at_two_brokers_is_fine(tmp_path):
    s = make_settings(tmp_path)
    s.sell.login = s.buy.login
    assert validate_accounts(s) == []


def test_plan_problems():
    bad = Plan(lots=0, tp_usd=-5, sl_usd=0.0, date="04-12-2026", time="14:29")
    assert {p.field for p in validate_plan(bad)} == {"plan.lots", "plan.tp_usd", "plan.sl_usd", "plan.date",
                                                    "plan.time"}
    assert validate_plan(Plan(lots=0.06, tp_usd=150, sl_usd=float("nan"), date="2026-12-04"))[0].field \
        == "plan.sl_usd"


def test_target_is_the_pc_local_moment():
    winter = target_datetime(Plan(date="2026-12-04", time="14:29:45"), CPH)
    assert winter.astimezone(timezone.utc) == datetime(2026, 12, 4, 13, 29, 45, tzinfo=timezone.utc)
    summer = target_datetime(Plan(date="2026-07-01", time="14:29:45"), CPH)
    assert summer.astimezone(timezone.utc) == datetime(2026, 7, 1, 12, 29, 45, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        target_datetime(Plan(date="", time="14:29:45"), CPH)


# ---------------------------------------------------------------- vault


def test_vault_saves_per_side_and_account():
    backend = MemoryBackend()
    vault = Vault(backend)
    assert vault.get("buy", 1001) == "" and not vault.has("buy", 1001)
    vault.set("buy", 1001, "pw-b")
    vault.set("sell", 2002, "pw-s")
    assert vault.get("buy", 1001) == "pw-b" and vault.get("sell", 2002) == "pw-s"
    assert vault.get("sell", 1001) == ""  # another side is another entry
    assert set(backend.items) == {(SERVICE, "buy:1001"), (SERVICE, "sell:2002")}
    vault.delete("buy", 1001)
    vault.delete("buy", 1001)  # deleting twice is fine
    assert not vault.has("buy", 1001)


def test_vault_refuses_empty_values_and_wraps_errors():
    vault = Vault(MemoryBackend())
    with pytest.raises(VaultError):
        vault.set("buy", 1001, "")
    with pytest.raises(VaultError):
        vault.set("buy", 0, "pw")
    assert vault.get("buy", 0) == ""

    class Broken:
        def get_password(self, *a):
            raise OSError("vault locked")

    with pytest.raises(VaultError) as exc:
        Vault(Broken()).get("buy", 1001)
    assert "vault locked" in str(exc.value)


# ---------------------------------------------------------------- engine files


def test_engine_config_has_the_app_values(tmp_path):
    s = make_settings(tmp_path)
    s.plan.sl_usd = 80.0
    s.sell.real = True
    buy, sell = build_engine_config(s, "buy"), build_engine_config(s, "sell")
    assert (buy["direction"], sell["direction"]) == ("buy", "sell")
    assert buy["account"]["login"] == 1001 and buy["account"]["allow_real_account"] is False
    assert sell["account"]["allow_real_account"] is True
    assert buy["account"]["password_env"] != sell["account"]["password_env"]
    assert buy["trade"]["symbol"] == "XAUUSD" and sell["trade"]["symbol"] == "GOLD"
    for cfg in (buy, sell):
        assert cfg["trade"]["tp_money"] == 150.0 and cfg["trade"]["sl_money"] == 80.0
        assert cfg["trade"]["money_currency"] == "USD" and cfg["trade"]["lots"] == 0.06
        assert cfg["schedule"]["time"] == "14:29:45" and cfg["schedule"]["dates"] == ["2026-12-04"]
        assert cfg["schedule"]["timezone"] == "local" and cfg["schedule"]["weekdays"] == []


def test_only_the_pc_clock_decides_the_time(tmp_path):
    """The engine never shifts the PC clock by internet time, whatever the saved settings say."""
    s = make_settings(tmp_path)
    for use_ntp in (True, False):
        s.advanced.use_ntp = use_ntp
        for side in ("buy", "sell"):
            assert build_engine_config(s, side)["schedule"]["use_ntp_offset"] is False
    _, _, configs = check_engine_configs(s, {"buy": "pw-b", "sell": "pw-s"})
    assert configs["buy"].schedule.use_ntp_offset is False and configs["sell"].schedule.use_ntp_offset is False
    assert configs["buy"].schedule.timezone == "Europe/Copenhagen"  # the PC's own zone


def test_the_engine_accepts_what_the_app_writes(tmp_path):
    s = make_settings(tmp_path)
    errors, warnings, configs = check_engine_configs(s, {"buy": "pw-b", "sell": "pw-s"})
    assert errors == []
    assert any("symbol name differs" in w for w in warnings)  # XAUUSD vs GOLD is allowed, but mentioned
    assert configs["buy"].password == "pw-b" and configs["sell"].password == "pw-s"
    assert configs["buy"].schedule.timezone == "Europe/Copenhagen"
    assert configs["sell"].trade.money_currency == "USD"


def test_the_engine_reports_missing_passwords_and_files(tmp_path):
    s = make_settings(tmp_path)
    errors, _, _ = check_engine_configs(s, {"buy": "pw-b", "sell": ""})
    assert len(errors) == 1 and errors[0].startswith("SELL:") and "password" in errors[0]
    Path(s.buy.terminal_path).unlink()
    errors, _, _ = check_engine_configs(s, {"buy": "pw-b", "sell": "pw-s"})
    assert any(e.startswith("BUY:") and "terminal_path" in e for e in errors)
    errors, _, _ = check_engine_configs(s, None, check_paths=False)  # no password check, no file check
    assert errors == []


def test_written_engine_files_load_and_hold_no_password(tmp_path):
    s = make_settings(tmp_path)
    files = write_engine_configs(s, tmp_path / "run")
    assert set(files) == {"buy", "sell"}
    for side, file in files.items():
        text = file.read_text(encoding="utf-8")
        assert "pw-" not in text
        raw = yaml.safe_load(text)
        assert raw == build_engine_config(s, side)
        cfg = engine_config_module.load_config(file, env_file=None, require_secrets=False)
        assert cfg.direction == side and cfg.account.password_env == PASSWORD_ENV[side]


# ---------------------------------------------------------------- finding MetaTrader 5


def test_terminals_are_found_from_mt5_data_folders(tmp_path, monkeypatch):
    install_a = tmp_path / "Brokers" / "Alpha MetaTrader 5"
    install_b = tmp_path / "Brokers" / "Beta MT5"
    for folder in (install_a, install_b):
        folder.mkdir(parents=True)
        (folder / "terminal64.exe").write_bytes(b"")
    gone = tmp_path / "Brokers" / "Removed"
    appdata = tmp_path / "AppData" / "Roaming"
    for i, (install, encoding) in enumerate(((install_a, "utf-16"), (install_b, "utf-8"), (gone, "utf-16"),
                                              (install_a, "utf-16"))):
        data = appdata / "MetaQuotes" / "Terminal" / f"HASH{i}"
        data.mkdir(parents=True)
        (data / "origin.txt").write_bytes(str(install).encode(encoding))
    (appdata / "MetaQuotes" / "Terminal" / "Community").mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(env, raising=False)

    found = terminals.find_terminals()
    assert [t.name for t in found] == ["Alpha MetaTrader 5", "Beta MT5"]  # no duplicate, no removed one
    assert found[0].path == str(install_a / "terminal64.exe")
    assert "Alpha MetaTrader 5" in found[0].label and str(install_a) in found[0].label


def test_app_files_live_in_the_data_folder(tmp_path):
    assert paths.data_dir() == tmp_path / "home"
    assert paths.settings_file().parent == paths.data_dir()
    assert paths.config_dir().is_dir() and paths.log_dir().is_dir()
    assert json.loads(json.dumps(str(paths.journal_file()))).endswith("journal.csv")
