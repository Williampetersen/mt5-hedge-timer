"""schedule.timezone: local, trade.money_currency: USD, and a different symbol name per account."""

from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pytest

import hedgebot.config as config
from conftest import make_cfg, sell_raw_from
from fake_mt5 import FakeClock, FakeMT5
from hedgebot import clock as clk
from hedgebot.config import ConfigError
from hedgebot.mt5_client import Mt5Client, PreflightError
from hedgebot.pair import validate_pair

T0 = datetime(2026, 11, 16, 13, 0, 0, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- timezone: local


def test_local_timezone_becomes_the_pc_zone(buy_raw, monkeypatch):
    monkeypatch.setattr(config, "local_timezone_name", lambda: "Europe/Copenhagen")
    buy_raw["schedule"]["timezone"] = "local"
    cfg = make_cfg(buy_raw)
    assert cfg.schedule.timezone == "Europe/Copenhagen"


def test_local_timezone_is_case_insensitive_and_reports_failure(buy_raw, monkeypatch):
    def boom():
        raise LookupError("could not read this PC's time zone: nope")

    monkeypatch.setattr(config, "local_timezone_name", boom)
    buy_raw["schedule"]["timezone"] = "Local"
    with pytest.raises(ConfigError) as exc:
        make_cfg(buy_raw)
    assert "local" in str(exc.value) and "Europe/Copenhagen" in str(exc.value)


def test_real_pc_timezone_can_be_read():
    name = config.local_timezone_name()
    assert "/" in name or name == "UTC"


def test_one_date_and_a_time_is_one_target(buy_raw, monkeypatch):
    """What the app writes: no weekdays, one date, HH:MM:SS, the PC's zone."""
    monkeypatch.setattr(config, "local_timezone_name", lambda: "Europe/Copenhagen")
    buy_raw["schedule"].update(timezone="local", weekdays=[], dates=["2026-12-04"], time="14:29:45")
    cfg = make_cfg(buy_raw)
    target = clk.next_target(datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc), cfg.schedule)
    assert target.date() == date(2026, 12, 4)
    assert target.astimezone(timezone.utc) == datetime(2026, 12, 4, 13, 29, 45, tzinfo=timezone.utc)
    with pytest.raises(clk.NoTargetError):  # the date has passed: nothing to do
        clk.next_target(datetime(2026, 12, 4, 13, 29, 46, tzinfo=timezone.utc), cfg.schedule)


# ---------------------------------------------------------------- money_currency


def test_money_currency_defaults_to_the_account_currency(buy_cfg):
    assert buy_cfg.trade.money_currency == "account"
    assert buy_cfg.trade.describe_tp("EUR") == "150.00 EUR"


@pytest.mark.parametrize("value, expected", [("USD", "USD"), ("usd", "USD"), ("Account", "account")])
def test_money_currency_is_normalised(buy_raw, value, expected):
    buy_raw["trade"]["money_currency"] = value
    assert make_cfg(buy_raw).trade.money_currency == expected


def test_money_currency_usd_is_shown_as_usd(buy_raw):
    buy_raw["trade"]["money_currency"] = "USD"
    assert make_cfg(buy_raw).trade.describe_tp("EUR") == "150.00 USD"


@pytest.mark.parametrize("bad", ["dollars", "US", "", 5])
def test_money_currency_rejects_nonsense(buy_raw, bad):
    buy_raw["trade"]["money_currency"] = bad
    with pytest.raises(ConfigError) as exc:
        make_cfg(buy_raw)
    assert "money_currency" in str(exc.value)


def _client(buy_raw, *, currency, rates, money_currency="USD"):
    buy_raw["trade"]["money_currency"] = money_currency
    cfg = make_cfg(buy_raw)
    clock = FakeClock(T0)
    mt5 = FakeMT5(clock)
    mt5.account.currency = currency
    quotes = {name: NS(bid=bid, ask=ask, time=int(T0), time_msc=int(T0 * 1000))
              for name, (bid, ask) in rates.items()}
    gold_tick = mt5.symbol_info_tick
    mt5.symbol_info_tick = lambda name: quotes.get(name) or (gold_tick(name) if name == "XAUUSD" else None)
    mt5.symbols_get = lambda group=None: tuple(
        NS(name=n, visible=True, time=int(T0), currency_base=n[:3], currency_profit=n[3:6]) for n in rates)
    mt5.symbol_select = lambda name, enable=True: name in rates or name == "XAUUSD"
    client = Mt5Client(cfg, clock=clock, mt5=mt5, sleep=clock.sleep)
    client.account = mt5.account
    return client, mt5


def test_usd_on_a_usd_account_is_unchanged(buy_raw):
    client, _ = _client(buy_raw, currency="USD", rates={})
    assert client.to_account_currency(150.0) == 150.0


def test_usd_to_eur_uses_eurusd(buy_raw):
    client, _ = _client(buy_raw, currency="EUR", rates={"EURUSD": (1.2499, 1.2501)})
    assert client.to_account_currency(150.0) == pytest.approx(120.0, abs=0.001)


def test_usd_to_jpy_uses_usdjpy(buy_raw):
    client, _ = _client(buy_raw, currency="JPY", rates={"USDJPY": (149.99, 150.01)})
    assert client.to_account_currency(100.0) == pytest.approx(15000.0, abs=0.01)


def test_broker_suffix_on_the_rate_symbol_is_accepted(buy_raw):
    client, _ = _client(buy_raw, currency="EUR", rates={"EURUSD.m": (1.2499, 1.2501)})
    assert client.to_account_currency(150.0) == pytest.approx(120.0, abs=0.001)


def test_no_rate_symbol_is_a_clear_error(buy_raw):
    client, _ = _client(buy_raw, currency="DKK", rates={"EURUSD": (1.25, 1.25)})
    with pytest.raises(PreflightError) as exc:
        client.to_account_currency(150.0)
    assert "USD" in str(exc.value) and "DKK" in str(exc.value)


def test_account_money_is_never_converted(buy_raw):
    client, _ = _client(buy_raw, currency="EUR", rates={}, money_currency="account")
    assert client.to_account_currency(150.0) == 150.0


def test_tp_distance_for_usd_on_a_eur_account(buy_raw):
    """150 USD at EURUSD 1.25 = 120 EUR; the fake pays price-move x lots x 100 in account money."""
    client, mt5 = _client(buy_raw, currency="EUR", rates={"EURUSD": (1.2499, 1.2501)})
    info = mt5.symbol_info("XAUUSD")
    d = client.distance(150.0, None, 2650.30, info, favourable=True)
    assert d == pytest.approx(120.0 / (0.06 * 100.0), abs=0.01)  # 20.00 instead of 25.00


# ---------------------------------------------------------------- pair


def test_pair_requires_the_same_money_currency(buy_raw):
    sell_raw = sell_raw_from(buy_raw)
    buy_raw["trade"]["money_currency"] = "USD"
    errors, _ = validate_pair(make_cfg(buy_raw), make_cfg(sell_raw))
    assert any("money_currency" in e for e in errors)


def test_different_symbol_names_can_be_allowed(buy_raw):
    sell_raw = sell_raw_from(buy_raw)
    sell_raw["trade"]["symbol"] = "GOLD"
    buy, sell = make_cfg(buy_raw), make_cfg(sell_raw)
    errors, _ = validate_pair(buy, sell)
    assert any("trade.symbol" in e for e in errors)  # the strict default stays
    errors, warnings = validate_pair(buy, sell, same_symbol=False)
    assert errors == []
    assert any("symbol" in w and "GOLD" in w for w in warnings)
