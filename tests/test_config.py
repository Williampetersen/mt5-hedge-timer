from datetime import date, time as dtime

import pytest
import yaml

from conftest import ENV, ROOT, make_cfg
from hedgebot.config import ConfigError, load_config, parse_config


def errors_of(raw, env=None, **kw):
    with pytest.raises(ConfigError) as exc:
        make_cfg(raw, env, **kw)
    return "\n".join(exc.value.errors)


def test_valid_config_parses(buy_raw):
    cfg = make_cfg(buy_raw)
    assert cfg.direction == "buy" and cfg.is_buy
    assert cfg.account.login == 1001
    assert cfg.trade.lots == 0.06 and cfg.trade.tp_money == 150.0 and cfg.trade.sl_money is None
    assert cfg.schedule.time == dtime(14, 29, 45)
    assert cfg.schedule.weekdays == (0, 1, 2, 3, 4)
    assert cfg.schedule.dates == ()
    assert cfg.monitor.until == dtime(23, 0, 0)
    assert cfg.password == "pw-buy"


def test_password_never_in_repr(buy_raw):
    assert "pw-buy" not in repr(make_cfg(buy_raw))


def test_example_files_are_valid_apart_from_placeholders():
    for name in ("buy.example.yaml", "sell.example.yaml"):
        raw = yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
        cfg = parse_config(raw, env=ENV, check_paths=False, reject_example_logins=False)
        assert cfg.schedule.time == dtime(14, 29, 45)
        assert cfg.trade.tp_money == 150.0
    # ...and the untouched placeholder login is refused
    raw = yaml.safe_load((ROOT / "config" / "buy.example.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ConfigError, match="example value"):
        parse_config(raw, env=ENV, check_paths=False)


@pytest.mark.parametrize("direction", ["long", "", "BUYY"])
def test_bad_direction(buy_raw, direction):
    buy_raw["direction"] = direction
    assert "direction" in errors_of(buy_raw)


def test_direction_is_case_insensitive(buy_raw):
    buy_raw["direction"] = "SELL"
    assert make_cfg(buy_raw).direction == "sell"


@pytest.mark.parametrize("value", ["25:00:00", "14:29", "2pm", "14:60:00", None])
def test_bad_time_format(buy_raw, value):
    buy_raw["schedule"]["time"] = value
    assert "schedule.time" in errors_of(buy_raw)


def test_unquoted_time_gets_a_helpful_message(buy_raw):
    # YAML 1.1 reads an unquoted 14:29:45 as the base-60 number 52185
    buy_raw["schedule"]["time"] = yaml.safe_load("t: 14:29:45")["t"]
    assert "quotes" in errors_of(buy_raw)


def test_bad_timezone(buy_raw):
    buy_raw["schedule"]["timezone"] = "Europe/Copenhagenn"
    assert "IANA" in errors_of(buy_raw)


@pytest.mark.parametrize("lots", [0, -0.01, "abc", None, True])
def test_bad_lots(buy_raw, lots):
    buy_raw["trade"]["lots"] = lots
    assert "trade.lots" in errors_of(buy_raw)


def test_tp_money_and_points_together(buy_raw):
    buy_raw["trade"]["tp_points"] = 2500
    assert "only one of 'trade.tp_money'" in errors_of(buy_raw)


def test_sl_money_and_points_together(buy_raw):
    buy_raw["trade"]["sl_money"] = 100
    buy_raw["trade"]["sl_points"] = 1000
    assert "only one of 'trade.sl_money'" in errors_of(buy_raw)


def test_tp_points_alone_is_fine(buy_raw):
    buy_raw["trade"]["tp_money"] = None
    buy_raw["trade"]["tp_points"] = 2500
    cfg = make_cfg(buy_raw)
    assert cfg.trade.tp_points == 2500.0 and cfg.trade.tp_money is None


def test_missing_password_env(buy_raw):
    msg = errors_of(buy_raw, env={})
    assert "BUY_MT5_PASSWORD is missing or empty" in msg


def test_empty_password_env(buy_raw):
    assert "BUY_MT5_PASSWORD" in errors_of(buy_raw, env={"BUY_MT5_PASSWORD": ""})


def test_password_not_required_when_disabled(buy_raw):
    cfg = make_cfg(buy_raw, env={}, require_secrets=False)
    assert cfg.password == ""


def test_terminal_path_must_exist(buy_raw, tmp_path):
    buy_raw["account"]["terminal_path"] = str(tmp_path / "nope" / "terminal64.exe")
    assert "terminal_path' not found" in errors_of(buy_raw, check_paths=True)
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"")
    buy_raw["account"]["terminal_path"] = str(exe)
    assert make_cfg(buy_raw, check_paths=True).account.terminal_path == str(exe)


def test_unknown_key_is_rejected(buy_raw):
    buy_raw["trade"]["sl_mony"] = 50
    assert "unknown key 'trade.sl_mony'" in errors_of(buy_raw)


def test_missing_required_key(buy_raw):
    del buy_raw["trade"]["magic"]
    assert "missing required key 'trade.magic'" in errors_of(buy_raw)


def test_missing_section(buy_raw):
    del buy_raw["schedule"]
    assert "missing section 'schedule:'" in errors_of(buy_raw)


def test_boolean_as_string_rejected(buy_raw):
    buy_raw["account"]["allow_real_account"] = "false"
    assert "true or false" in errors_of(buy_raw)


def test_comment_too_long(buy_raw):
    buy_raw["trade"]["comment"] = "x" * 32
    assert "at most 31" in errors_of(buy_raw)


def test_weekday_names(buy_raw):
    buy_raw["schedule"]["weekdays"] = ["monday", "FRI", "Wed"]
    assert make_cfg(buy_raw).schedule.weekdays == (0, 2, 4)
    buy_raw["schedule"]["weekdays"] = ["Mon", "Funday"]
    assert "unknown day" in errors_of(buy_raw)


def test_no_trading_days(buy_raw):
    buy_raw["schedule"]["weekdays"] = []
    buy_raw["schedule"]["dates"] = []
    assert "no trading days" in errors_of(buy_raw)


def test_dates_accept_yaml_dates_and_strings(buy_raw):
    buy_raw["schedule"]["dates"] = [date(2026, 12, 10), "2026-12-05"]
    assert make_cfg(buy_raw).schedule.dates == (date(2026, 12, 5), date(2026, 12, 10))
    buy_raw["schedule"]["dates"] = ["05-12-2026"]
    assert "YYYY-MM-DD" in errors_of(buy_raw)


def test_ntp_server_required_when_ntp_on(buy_raw):
    buy_raw["schedule"]["use_ntp_offset"] = True
    buy_raw["schedule"]["ntp_server"] = None
    assert "ntp_server" in errors_of(buy_raw)


def test_real_account_flag_gives_warning(buy_raw):
    buy_raw["account"]["allow_real_account"] = True
    assert any("real-money" in w for w in make_cfg(buy_raw).warnings)


def test_all_errors_reported_at_once(buy_raw):
    buy_raw["direction"] = "up"
    buy_raw["trade"]["lots"] = 0
    buy_raw["schedule"]["timezone"] = "Mars/Base"
    msg = errors_of(buy_raw)
    assert "direction" in msg and "trade.lots" in msg and "IANA" in msg


def test_telegram_needs_both_values(buy_raw):
    cfg = make_cfg(buy_raw, env={**ENV, "TELEGRAM_BOT_TOKEN": "123:abc"})
    assert cfg.telegram_token == "" and any("Telegram" in w for w in cfg.warnings)
    cfg = make_cfg(buy_raw, env={**ENV, "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
    assert cfg.telegram_token == "123:abc" and cfg.telegram_chat_id == "42"


def test_load_config_from_files(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "environ", dict(os.environ))  # load_dotenv must not leak into other tests
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"")
    cfg_file = tmp_path / "buy.yaml"
    text = (ROOT / "config" / "buy.example.yaml").read_text(encoding="utf-8")
    text = text.replace("12345678", "555001").replace("C:\\MT5_Buy\\terminal64.exe", str(exe))
    cfg_file.write_text(text, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("BUY_MT5_PASSWORD=secret-from-dotenv\n", encoding="utf-8")
    os.environ.pop("BUY_MT5_PASSWORD", None)
    cfg = load_config(cfg_file, env_file=env_file)
    assert cfg.account.login == 555001
    assert cfg.password == "secret-from-dotenv"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_double_quoted_windows_path_hint(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "environ", dict(os.environ))
    f = tmp_path / "bad.yaml"
    f.write_text('account:\n  terminal_path: "C:\\MT5_Buy\\terminal64.exe"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="single quotes"):
        load_config(f, env_file=None)
