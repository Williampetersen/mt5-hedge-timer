from datetime import date
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from conftest import make_cfg
from fake_mt5 import SERVER_OFFSET, FakeClock, FakeMT5
from hedgebot import trade_math as tm
from hedgebot.mt5_client import Mt5Client, Mt5Error, PreflightError

T0 = 1_794_835_785.0  # 2026-11-16 13:29:45 UTC = 14:29:45 CET (a Monday)
CPH = ZoneInfo("Europe/Copenhagen")


def make_client(cfg, mt5=None, t=T0 - 30):
    clock = FakeClock(t)
    mt5 = mt5 or FakeMT5(clock)
    mt5.clock = clock
    return Mt5Client(cfg, clock=clock, mt5=mt5, sleep=clock.sleep), mt5, clock


def armed(cfg, mt5=None):
    client, mt5, clock = make_client(cfg, mt5)
    client.connect()
    client.check_account()
    pf = client.preflight()
    clock.t = T0
    return client, mt5, clock, pf


# --------------------------------------------------------------------------- connect / account


def test_connect_passes_config_values(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    client.connect()
    args, kwargs = mt5.init_args
    assert args == ("C:\\MT5_Buy\\terminal64.exe",)
    assert kwargs["login"] == 1001 and kwargs["password"] == "pw-buy"
    assert kwargs["server"] == "Broker-Demo" and kwargs["portable"] is False


def test_connect_failure_is_reported(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    mt5.initialize = lambda *a, **k: False
    mt5.last_err = (-6, "Terminal: Authorization failed")
    with pytest.raises(Mt5Error, match="Authorization failed"):
        client.connect()


def test_wrong_login_aborts(buy_cfg):
    client, mt5, _ = make_client(buy_cfg, FakeMT5(login=9999))
    client.connect()
    with pytest.raises(Mt5Error, match="logged into account 9999"):
        client.check_account()


def test_algo_trading_off_aborts(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    mt5.terminal.trade_allowed = False
    client.connect()
    with pytest.raises(Mt5Error, match="Algo Trading is OFF"):
        client.check_account()


def test_python_api_disabled_aborts(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    mt5.terminal.tradeapi_disabled = True
    client.connect()
    with pytest.raises(Mt5Error, match="external Python API"):
        client.check_account()


def test_real_account_refused_unless_allowed(buy_raw):
    client, _, _ = make_client(make_cfg(buy_raw), FakeMT5(trade_mode=tm.ACCOUNT_TRADE_MODE_REAL))
    client.connect()
    with pytest.raises(Mt5Error, match="REAL account"):
        client.check_account()
    buy_raw["account"]["allow_real_account"] = True
    client, _, _ = make_client(make_cfg(buy_raw), FakeMT5(trade_mode=tm.ACCOUNT_TRADE_MODE_REAL))
    client.connect()
    client.check_account()  # allowed explicitly


# --------------------------------------------------------------------------- preflight


def test_preflight_computes_tp_distance_and_prebuilds_request(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    assert pf.tp_distance == pytest.approx(25.0)
    assert pf.sl_distance is None
    assert pf.filling == tm.ORDER_FILLING_FOK
    assert pf.tp_preview == 2675.30  # ask 2650.30 + 25
    assert "price" not in pf.request and pf.request["type"] == tm.ORDER_TYPE_BUY
    assert pf.request["volume"] == 0.06 and pf.request["magic"] == 20261115
    assert pf.server_offset_s == SERVER_OFFSET
    assert pf.margin_required == pytest.approx(0.06 * 100 * 2650.30 / 100, abs=0.01)
    assert mt5.checked and not mt5.sent  # order_check only, nothing sent


def test_preflight_sell_side(sell_cfg):
    client, mt5, clock, pf = armed(sell_cfg, FakeMT5(login=2002))
    assert pf.request["type"] == tm.ORDER_TYPE_SELL
    assert pf.tp_preview == 2625.00  # bid 2650.00 - 25


def test_unknown_symbol_suggests_broker_names(buy_cfg):
    mt5 = FakeMT5()
    mt5.symbols_present = {"XAUUSD.m"}  # broker uses a suffix
    client, mt5, _ = make_client(buy_cfg, mt5)
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match=r"does not exist.*XAUUSD\.m, XAUEUR\.m, GOLD"):
        client.preflight()


def test_market_closed_on_weekend_stale_quotes(buy_cfg):
    mt5 = FakeMT5()
    mt5.tick_age_s = mt5.other_quotes_age_s = 47 * 3600  # Friday's last quotes, seen on Sunday
    client, mt5, _ = make_client(buy_cfg, mt5)
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match="market is closed"):
        client.preflight()
    pf = client.preflight(strict_market=False)  # --check only warns
    assert pf.tp_distance == pytest.approx(25.0)


def test_market_closed_for_this_symbol_only(buy_cfg):
    mt5 = FakeMT5()
    mt5.tick_age_s = 1800  # gold paused, FX still quoting
    client, mt5, _ = make_client(buy_cfg, mt5)
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match="30 min old"):
        client.preflight()


def test_wrong_broker_tick_value_does_not_matter(buy_cfg):
    """Seen on a real broker: GOLD tick 0.001 reported with tick_value 1.0 (should be 0.1)."""
    mt5 = FakeMT5()
    mt5.symbol.trade_tick_size = mt5.symbol.point = 0.001
    mt5.symbol.digits = 3
    mt5.symbol.trade_tick_value = mt5.symbol.trade_tick_value_profit = mt5.symbol.trade_tick_value_loss = 1.0
    client, mt5, clock, pf = armed(buy_cfg, mt5)
    assert pf.tp_distance == pytest.approx(25.0)
    mt5.order_calc_profit = lambda *a: None  # even without order_calc_profit
    assert client.distance(150.0, None, 2650.3, mt5.symbol, favourable=True) == pytest.approx(25.0)


def test_market_closed_is_caught_by_order_check(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    mt5.check_code = tm.TRADE_RETCODE_MARKET_CLOSED
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match="market .* is closed"):
        client.preflight()


def test_invalid_lot_step(buy_raw):
    buy_raw["trade"]["lots"] = 0.065
    client, mt5, _ = make_client(make_cfg(buy_raw))
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match="volume step"):
        client.preflight()


def test_long_only_symbol_refuses_sell(sell_cfg):
    mt5 = FakeMT5(login=2002)
    mt5.symbol.trade_mode = tm.SYMBOL_TRADE_MODE_LONGONLY
    client, _, _ = make_client(sell_cfg, mt5)
    client.connect()
    client.check_account()
    with pytest.raises(PreflightError, match="only BUY"):
        client.preflight()


def test_margin_warning(buy_cfg):
    mt5 = FakeMT5()
    mt5.account.margin_free = 10.0
    client, mt5, clock, pf = armed(buy_cfg, mt5)
    assert pf.margin_warning and "free margin" in pf.margin_warning


# --------------------------------------------------------------------------- fire + retries


def fire(client, pf, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("max_late_ms", 2000)
    kw.setdefault("max_spread_points", None)
    return client.fire(pf, T0, **kw)


def test_fire_opens_and_records_timing(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.fill_slippage = 0.05
    res = fire(client, pf)
    assert res.status == "opened" and res.attempts == 1
    assert res.requested_price == 2650.30 and res.fill_price == 2650.35
    assert res.slippage_points == 5.0
    assert res.deviation_ms == pytest.approx(0.0)
    assert res.spread_points == 30
    assert mt5.sent[0]["price"] == 2650.30 and mt5.sent[0]["tp"] == 2675.30
    assert "sl" not in mt5.sent[0]  # no stop loss by default


def test_requotes_are_retried(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.send_codes = [10004, 10020, 10009]
    res = fire(client, pf)
    assert res.status == "opened" and res.attempts == 3


def test_at_most_three_retries(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.send_codes = [10004, 10004, 10021, 10020, 10009]
    res = fire(client, pf)
    assert res.status == "failed" and res.attempts == 4  # 1 send + 3 retries
    assert not mt5.positions


@pytest.mark.parametrize("code", [10006, 10014, 10016, 10018, 10019, 10030])
def test_other_errors_are_not_retried(buy_cfg, code):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.send_codes = [code, 10009]
    res = fire(client, pf)
    assert res.status == "failed" and res.attempts == 1 and res.retcode == code


def test_no_reply_is_not_retried_but_reconciled(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.send_codes = [None]
    res = fire(client, pf)
    assert res.status == "failed" and res.attempts == 1 and len(mt5.sent) == 1


def test_too_late_does_not_send(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    clock.t = T0 + 2.5
    res = fire(client, pf)
    assert res.status == "skipped_late" and not mt5.sent


def test_retries_stop_once_past_max_late(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)

    def slow_requote(request):
        clock.t += 1.5
        return NS(retcode=10004, comment="Requote", order=0, deal=0, price=0.0, volume=0.0)

    mt5.order_send = slow_requote
    res = fire(client, pf)
    assert res.status == "failed" and res.attempts == 2


def test_spread_filter_skips(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf, max_spread_points=20)
    assert res.status == "skipped_spread" and not mt5.sent


def test_dry_run_uses_order_check_only(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    checks_before = len(mt5.checked)
    res = fire(client, pf, dry_run=True)
    assert res.status == "dry_run" and not mt5.sent
    assert len(mt5.checked) == checks_before + 1


# --------------------------------------------------------------------------- after the fill


def test_tp_is_corrected_from_the_fill_price(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    mt5.fill_slippage = 0.07
    res = fire(client, pf)
    assert res.tp_price == 2675.30  # from the requested price
    assert client.correct_tp_sl(pf, res) is None
    modify = mt5.sent[-1]
    assert modify["action"] == tm.TRADE_ACTION_SLTP and modify["position"] == res.position_ticket
    assert modify["tp"] == 2675.37 and modify["sl"] == 0.0
    assert res.tp_price == 2675.37
    assert mt5.positions[0].tp == 2675.37


def test_no_modify_when_already_exact(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf)
    sent = len(mt5.sent)
    client.correct_tp_sl(pf, res)
    assert len(mt5.sent) == sent


def test_sell_tp_corrected_downwards(sell_cfg):
    client, mt5, clock, pf = armed(sell_cfg, FakeMT5(login=2002))
    mt5.fill_slippage = 0.03  # sold 0.03 lower
    res = fire(client, pf)
    assert res.fill_price == 2649.97 and res.slippage_points == 3.0
    client.correct_tp_sl(pf, res)
    assert res.tp_price == 2624.97


def test_sl_money_is_placed_on_the_other_side(buy_raw):
    buy_raw["trade"]["sl_money"] = 60.0
    client, mt5, clock, pf = armed(make_cfg(buy_raw))
    assert pf.sl_distance == pytest.approx(10.0)
    fire(client, pf)
    assert mt5.sent[0]["sl"] == 2640.30


# --------------------------------------------------------------------------- guard + monitor


def test_guard_sees_open_position(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    fire(client, pf)
    reason = client.existing_trade(date(2026, 11, 17), CPH, pf.server_offset_s)
    assert reason and "already open" in reason


def test_guard_sees_position_opened_and_closed_today(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf)
    mt5.close_position(res.position_ticket, 2675.35)
    reason = client.existing_trade(date(2026, 11, 16), CPH, pf.server_offset_s)
    assert reason and "today" in reason
    assert client.existing_trade(date(2026, 11, 17), CPH, pf.server_offset_s) is None


def test_monitor_reports_tp_close(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf)
    client.correct_tp_sl(pf, res)
    polls = {"n": 0}
    real_get = mt5.positions_get

    def positions_get(**kw):
        polls["n"] += 1
        if polls["n"] == 2:
            mt5.positions[0].profit = -42.0
        if polls["n"] == 3:
            mt5.positions[0].profit = 120.0
        if polls["n"] == 4:
            mt5.close_position(res.position_ticket, 2675.30, reason=tm.DEAL_REASON_TP, after_s=3600)
        return real_get(**kw)

    mt5.positions_get = positions_get
    info = client.monitor(res, T0 + 10 * 3600, 1.0)
    assert info.closed and info.reason == "TP"
    assert info.profit == 150.0 and info.close_price == 2675.30
    assert info.max_adverse == -42.0 and info.max_favourable == 120.0
    assert info.duration_s == pytest.approx(3600)
    assert info.swap == -1.5 and info.commission == -0.6


def test_monitor_stops_at_until(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf)
    info = client.monitor(res, T0 + 5, 1.0)
    assert not info.closed and "still open" in info.reason
