import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

import run_pair
from conftest import BUY_RAW, ROOT, make_cfg, sell_raw_from
from hedgebot.pair import PairError, check_pair, validate_pair


def errors_for(buy_raw, sell_raw):
    errors, _ = validate_pair(make_cfg(buy_raw), make_cfg(sell_raw))
    return errors


def test_valid_pair(buy_cfg, sell_cfg):
    assert validate_pair(buy_cfg, sell_cfg) == ([], [])
    assert check_pair(buy_cfg, sell_cfg) == []


def test_both_buy(buy_raw, sell_raw):
    sell_raw["direction"] = "buy"
    errs = errors_for(buy_raw, sell_raw)
    assert any("--sell config" in e and "must be 'sell'" in e for e in errs)


def test_both_sell(buy_raw, sell_raw):
    buy_raw["direction"] = "sell"
    assert any("--buy config" in e for e in errors_for(buy_raw, sell_raw))


def test_files_swapped(buy_cfg, sell_cfg):
    errors, _ = validate_pair(sell_cfg, buy_cfg)
    assert len([e for e in errors if "direction" in e]) == 2


@pytest.mark.parametrize(
    "section,key,value,label",
    [
        ("trade", "symbol", "XAUUSD.m", "trade.symbol"),
        ("trade", "lots", 0.07, "trade.lots"),
        ("trade", "tp_money", 100.0, "trade.tp_money"),
        ("trade", "sl_money", 50.0, "trade.sl_money"),
        ("schedule", "time", "14:29:46", "schedule.time"),
        ("schedule", "timezone", "Europe/London", "schedule.timezone"),
        ("schedule", "weekdays", ["Mon", "Tue"], "schedule.weekdays"),
        ("schedule", "dates", ["2026-12-05"], "schedule.dates"),
    ],
)
def test_mismatch_is_an_error(buy_raw, sell_raw, section, key, value, label):
    sell_raw[section][key] = value
    errs = errors_for(buy_raw, sell_raw)
    assert any(e.startswith(label) for e in errs), errs


def test_tp_points_vs_money_mismatch(buy_raw, sell_raw):
    sell_raw["trade"]["tp_money"] = None
    sell_raw["trade"]["tp_points"] = 2500
    errs = errors_for(buy_raw, sell_raw)
    assert any(e.startswith("trade.tp_money") for e in errs)
    assert any(e.startswith("trade.tp_points") for e in errs)


def test_sl_points_mismatch(buy_raw, sell_raw):
    sell_raw["trade"]["sl_points"] = 1000
    assert any(e.startswith("trade.sl_points") for e in errors_for(buy_raw, sell_raw))


def test_same_login(buy_raw, sell_raw):
    sell_raw["account"]["login"] = buy_raw["account"]["login"]
    assert any("both configs use login" in e for e in errors_for(buy_raw, sell_raw))


@pytest.mark.parametrize("path", ["C:\\MT5_Buy\\terminal64.exe", "c:/mt5_buy/TERMINAL64.EXE",
                                  "C:\\MT5_Sell\\..\\MT5_Buy\\terminal64.exe"])
def test_same_terminal_path(buy_raw, sell_raw, path):
    sell_raw["account"]["terminal_path"] = path
    assert any("terminal_path" in e for e in errors_for(buy_raw, sell_raw))


def test_same_name(buy_raw, sell_raw):
    sell_raw["name"] = buy_raw["name"]
    assert any("name" in e for e in errors_for(buy_raw, sell_raw))


def test_soft_differences_are_warnings(buy_raw, sell_raw):
    sell_raw["schedule"]["max_late_ms"] = 1000
    sell_raw["trade"]["deviation_points"] = 100
    errors, warnings = validate_pair(make_cfg(buy_raw), make_cfg(sell_raw))
    assert errors == [] and len(warnings) == 2


def test_check_pair_raises(buy_cfg):
    with pytest.raises(PairError):
        check_pair(buy_cfg, buy_cfg)


# --------------------------------------------------------------------------- run_pair helpers


def test_child_args_pass_one_shared_target():
    target = datetime(2026, 11, 16, 13, 29, 45, 123000, tzinfo=timezone.utc)
    args = SimpleNamespace(env=None, dry_run=True, check=False, loop=False, verbose=False)
    assert run_pair.child_args(args, target) == ["--dry-run", "--target-at", "2026-11-16T13:29:45.123000+00:00"]


def test_result_line_and_diff():
    buy = run_pair.parse_result_line(
        'HEDGEBOT_RESULT {"status": "opened", "target_ts": 1.0, "send_pc_ts": 100.0010, '
        '"deviation_ms": 1.0, "latency_ms": 40.0}')
    sell = {"status": "opened", "target_ts": 1.0, "send_pc_ts": 100.0035, "deviation_ms": 3.5, "latency_ms": 55.0}
    text = run_pair.describe_pair(buy, sell)
    assert "send-time difference 2.50 ms" in text and "SELL sent later" in text
    assert run_pair.parse_result_line("2026-11-16 14:29:45.000 CET INFO hello") is None


def test_combine_exit_codes():
    assert run_pair.combine_exit_codes([0, 0]) == 0
    assert run_pair.combine_exit_codes([0, 1]) == 1
    assert run_pair.combine_exit_codes([3, 0]) == 3


def test_run_pair_refuses_invalid_pair(tmp_path):
    """End to end: a bad pair exits with code 2 before any app is started."""
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"")
    buy = dict(BUY_RAW, account=dict(BUY_RAW["account"], terminal_path=str(exe)))
    sell = sell_raw_from(buy)
    sell["trade"]["lots"] = 0.07
    sell["account"]["terminal_path"] = str(exe)  # same terminal too
    (tmp_path / "buy.yaml").write_text(yaml.safe_dump(buy), encoding="utf-8")
    (tmp_path / "sell.yaml").write_text(yaml.safe_dump(sell), encoding="utf-8")
    (tmp_path / ".env").write_text("BUY_MT5_PASSWORD=a\nSELL_MT5_PASSWORD=b\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run_pair.py"), "--buy", str(tmp_path / "buy.yaml"),
         "--sell", str(tmp_path / "sell.yaml"), "--env", str(tmp_path / ".env"), "--check"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "trade.lots differs" in proc.stderr and "terminal_path" in proc.stderr
    assert "[BUY]" not in proc.stdout
