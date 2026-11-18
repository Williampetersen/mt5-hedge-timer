from types import SimpleNamespace as NS

import pytest

from conftest import make_cfg
from fake_mt5 import FakeClock, FakeMT5
from hedgebot import trade_math as tm
from hedgebot.mt5_client import MARKET_SETTLE_S, Mt5Client, Mt5Error, PreflightError

T0 = 1_794_835_785.0  # 2026-11-16 13:29:45 UTC = 14:29:45 CET (a Monday)


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
    client, mt5, clock = make_client(buy_cfg, mt5)
    client.connect()
    client.check_account()
    before = clock.t
    with pytest.raises(PreflightError, match="30 min old"):
        client.preflight()
    assert clock.t - before == pytest.approx(MARKET_SETTLE_S, abs=0.5)  # it waited for fresh prices first


def test_old_prices_right_after_the_terminal_started_are_given_a_moment(buy_cfg):
    """A terminal that has only just started still shows the prices of its last session."""
    mt5 = FakeMT5()
    mt5.tick_age_s = 16 * 3600  # yesterday's last price
    client, mt5, clock = make_client(buy_cfg, mt5)
    real_tick = mt5.symbol_info_tick
    calls = {"n": 0}

    def symbol_info_tick(name):
        calls["n"] += 1
        if calls["n"] == 4:
            mt5.tick_age_s = 0.0  # the first fresh price arrives about a second later
        return real_tick(name)

    mt5.symbol_info_tick = symbol_info_tick
    client.connect()
    client.check_account()
    pf = client.preflight()  # does not raise "market closed"
    assert pf.tp_distance == pytest.approx(25.0)


@pytest.mark.parametrize("broker_hours", [-11, 0, 3, 13])
def test_the_brokers_clock_does_not_matter(buy_cfg, monkeypatch, broker_hours):
    """MT5 reports quote times in the broker's clock; whatever that clock says, the trade goes out."""
    monkeypatch.setattr("fake_mt5.SERVER_OFFSET", broker_hours * 3600)
    client, mt5, clock, pf = armed(buy_cfg)
    res = fire(client, pf)
    assert res.status == "opened" and res.deviation_ms == pytest.approx(0.0)


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
    assert "2.5 s after the target" in res.comment and "allowed 2 s" in res.comment


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


# --------------------------------------------------------------------------- no limit per day


def test_positions_of_earlier_runs_do_not_block_a_new_one(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    first = fire(client, pf)
    assert first.status == "opened"
    pf = client.preflight()  # the next run, while the first position is still open
    second = fire(client, pf)
    third = fire(client, client.preflight())
    assert second.status == third.status == "opened"
    tickets = {first.position_ticket, second.position_ticket, third.position_ticket}
    assert len(tickets) == 3 and len(mt5.positions) == 3
    assert [r["action"] for r in mt5.sent].count(tm.TRADE_ACTION_DEAL) == 3


def test_no_reply_is_not_mistaken_for_an_earlier_position(buy_cfg):
    """With no answer from the broker, only a NEW position counts as opened."""
    client, mt5, clock, pf = armed(buy_cfg)
    earlier = fire(client, pf)
    pf = client.preflight()
    mt5.send_codes = [None]
    res = fire(client, pf)
    assert res.status == "failed" and res.position_ticket is None
    assert [p.ticket for p in mt5.positions] == [earlier.position_ticket]


def test_no_reply_but_the_order_went_through_is_found(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    earlier = fire(client, pf)
    pf = client.preflight()
    real_send = mt5.order_send

    def lost_answer(request):
        real_send(request)  # the broker filled it ...
        mt5.last_err = (-10005, "IPC timeout")
        return None  # ... but the answer never arrived

    mt5.order_send = lost_answer
    res = fire(client, pf)
    assert res.status == "opened" and res.position_ticket not in (None, earlier.position_ticket)
    assert res.fill_price == 2650.30 and len(mt5.positions) == 2


def test_the_list_of_earlier_positions_is_kept_fresh_while_armed(buy_cfg):
    client, mt5, clock, pf = armed(buy_cfg)
    assert client._known_tickets == frozenset()
    # a position of ours appears after the preflight (e.g. a second window) ...
    mt5.positions.append(NS(ticket=777, symbol="XAUUSD", magic=20261115, type=0, volume=0.06,
                            price_open=2650.0, tp=0.0, sl=0.0, profit=0.0, time_msc=0))
    client.warm_tick()  # ... and the armed wait notices it
    assert client._known_tickets == frozenset({777})
    mt5.send_codes = [None]
    assert fire(client, pf).status == "failed"  # not taken for the new one
    mt5.positions_get = lambda **kw: None  # unreadable for a moment: keep the last list
    mt5.last_err = (-1, "pipe broken")
    client.warm_tick()
    assert client._known_tickets == frozenset({777})


def test_a_read_that_misses_a_position_does_not_forget_it(buy_cfg):
    """The list of earlier positions only grows, so one incomplete read cannot make an old position "new"."""
    client, mt5, clock, pf = armed(buy_cfg)
    earlier = fire(client, pf)
    client.preflight()
    assert client._known_tickets == frozenset({earlier.position_ticket})
    real_get = mt5.positions_get
    mt5.positions_get = lambda **kw: ()  # the terminal lists nothing for a moment
    client.warm_tick()
    assert client._known_tickets == frozenset({earlier.position_ticket})
    mt5.positions_get = real_get
    mt5.send_codes = [None]  # no answer, and nothing was filled
    res = fire(client, client.preflight())
    assert res.status == "failed" and res.position_ticket is None


def test_a_late_position_list_never_makes_an_earlier_position_the_new_one(buy_cfg):
    """The broker says done, but the new position shows up 0.3 s later: wait for it, do not guess."""
    client, mt5, clock, pf = armed(buy_cfg)
    earlier = fire(client, pf)
    pf = client.preflight()
    client._known_tickets = frozenset()  # worst case: every read before the send missed the earlier one
    real_get, real_send = mt5.positions_get, mt5.order_send
    listed_from = {"t": None}

    def positions_get(**kw):
        found = real_get(**kw)
        if listed_from["t"] is not None and clock.t < listed_from["t"]:
            found = tuple(p for p in found if p.ticket == earlier.position_ticket)
        return found

    def order_send(request):
        result = real_send(request)
        if request["action"] == tm.TRADE_ACTION_DEAL:
            listed_from["t"] = clock.t + 0.3
        return result

    mt5.positions_get, mt5.order_send = positions_get, order_send
    real_deals = mt5.history_deals_get

    def history_deals_get(*args, **kw):  # the history lags just like the position list
        hidden = listed_from["t"] is not None and clock.t < listed_from["t"]
        return () if hidden else real_deals(*args, **kw)

    mt5.history_deals_get = history_deals_get
    res = fire(client, pf)
    assert res.status == "opened" and res.position_ticket not in (None, earlier.position_ticket)
    assert res.position_ticket == res.order_ticket and len(mt5.positions) == 2
    assert client.correct_tp_sl(pf, res) is None
    assert mt5.positions[0].ticket == earlier.position_ticket and mt5.positions[0].tp == 2675.30  # untouched


def test_a_position_with_another_ticket_than_its_order_is_found_through_the_deal(buy_cfg):
    """Some servers give the position its own number: the order's deal says which position it is."""
    client, mt5, clock, pf = armed(buy_cfg)
    earlier = fire(client, pf)
    pf = client.preflight()
    client._known_tickets = frozenset()  # even without the list of earlier positions
    real_send = mt5.order_send

    def order_send(request):
        result = real_send(request)
        if request["action"] == tm.TRADE_ACTION_DEAL:
            mt5.positions[-1].ticket = 9_000_001  # not the order's ticket
            mt5.deals[-1].position_id = 9_000_001
        return result

    mt5.order_send = order_send
    res = fire(client, pf)
    assert res.status == "opened" and res.position_ticket == 9_000_001 != res.order_ticket
    assert res.position_ticket != earlier.position_ticket


def test_unreadable_positions_stop_the_preflight(buy_cfg):
    client, mt5, _ = make_client(buy_cfg)
    client.connect()
    client.check_account()
    mt5.positions_get = lambda **kw: None
    mt5.last_err = (-1, "pipe broken")
    with pytest.raises(Mt5Error, match="Could not read open positions"):
        client.preflight()


def test_netting_account_still_refuses_a_second_position(buy_cfg):
    """One netting account holds one position per symbol: a new order would be merged into it."""
    mt5 = FakeMT5()
    mt5.account.margin_mode = tm.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    client, mt5, clock, pf = armed(buy_cfg, mt5)
    assert fire(client, pf).status == "opened"
    with pytest.raises(PreflightError, match="NETTING account"):
        client.preflight()


# --------------------------------------------------------------------------- monitor


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
