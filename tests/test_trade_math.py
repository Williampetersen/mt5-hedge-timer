import pytest

from hedgebot import trade_math as tm


# --- money -> price distance


def test_xauusd_150_usd_on_006_lot_is_25_price_distance():
    # XAUUSD: contract 100 oz, tick 0.01 = 1.00 USD per lot
    assert tm.distance_from_tick_value(150, 0.06, 0.01, 1.0) == pytest.approx(25.0)


def test_distance_from_contract_size():
    assert tm.distance_from_contract(150, 0.06, 100) == pytest.approx(25.0)


def test_market_closed_reason():
    utc = 1_000_000_000.0
    server = utc + 7200  # quote times are in the broker's own clock
    assert tm.market_closed_reason("X", server, server, utc) is None
    assert "10 min old" in tm.market_closed_reason("X", server - 600, server, utc)
    assert "market is closed" in tm.market_closed_reason("X", server - 47 * 3600, server - 47 * 3600, utc)


@pytest.mark.parametrize("broker_hours", [-12, -5, 0, 2, 3, 9, 14])
def test_the_brokers_time_zone_never_closes_the_market(broker_hours):
    """Prices that are coming in are fine, whatever clock the broker runs on."""
    utc = 1_000_000_000.0
    server = utc + broker_hours * 3600
    assert tm.market_closed_reason("X", server - 2, server, utc) is None


def test_suggest_symbols():
    names = ["EURUSD", "GOLD", "GOLDEURO", "XAUUSD.m", "XAUEUR", "SILVER"]
    assert tm.suggest_symbols("XAUUSD", names) == ["XAUUSD.m", "XAUEUR", "GOLD", "GOLDEURO"]
    assert tm.suggest_symbols("GOLD", ["XAUUSD", "GOLD.r"]) == ["GOLD.r", "XAUUSD"]
    assert tm.suggest_symbols("XAGUSD", names) == ["SILVER"]


def test_solver_with_linear_profit():
    def profit(d):  # order_calc_profit for XAUUSD 0.06 lot
        return round(d * 0.06 * 100, 2)

    assert tm.solve_distance_for_money(profit, 150.0, 10.0) == pytest.approx(25.0, abs=1e-4)


def test_solver_with_nonlinear_quote_currency():
    # USDJPY on a USD account: profit_usd = lots * 100000 * d / close_price
    open_price, lots = 150.0, 0.5

    def profit(d):
        return lots * 100_000 * d / (open_price + d)

    d = tm.solve_distance_for_money(profit, 100.0, 0.3)
    assert profit(d) == pytest.approx(100.0, abs=0.001)


def test_solver_handles_negative_profit_for_sl():
    d = tm.solve_distance_for_money(lambda d: -d * 6.0, 60.0, 1.0)
    assert d == pytest.approx(10.0)


def test_solver_raises_when_mt5_returns_none():
    with pytest.raises(ValueError):
        tm.solve_distance_for_money(lambda d: None, 150.0, 1.0)


# --- TP/SL prices


def test_buy_tp_and_sl_prices():
    tp, sl = tm.tp_sl_prices("buy", 2650.30, 25.0, 10.0, 0.01, 2)
    assert (tp, sl) == (2675.30, 2640.30)


def test_sell_tp_and_sl_prices():
    tp, sl = tm.tp_sl_prices("sell", 2650.00, 25.0, 10.0, 0.01, 2)
    assert (tp, sl) == (2625.00, 2660.00)


def test_no_sl_by_default():
    tp, sl = tm.tp_sl_prices("buy", 2650.30, 25.0, None, 0.01, 2)
    assert tp == 2675.30 and sl is None


def test_rounding_to_tick_size():
    assert tm.round_price(2675.304, 0.01, 2) == 2675.30
    assert tm.round_price(2675.305, 0.01, 2) == 2675.31  # halves round up
    assert tm.round_price(1.237, 0.05, 2) == 1.25
    assert tm.round_price(1.10004, 0.00001, 5) == 1.10004
    assert tm.round_price(38512.7, 0.5, 1) == 38512.5
    tp, _ = tm.tp_sl_prices("buy", 2650.333, 24.999, None, 0.01, 2)
    assert tp == 2675.33


def test_tp_price_cannot_go_negative():
    with pytest.raises(ValueError):
        tm.tp_sl_prices("sell", 20.0, 25.0, None, 0.01, 2)


def test_step_decimals():
    assert [tm.step_decimals(s) for s in (0.01, 0.5, 1, 1e-05, 0.001)] == [2, 1, 0, 5, 3]


# --- lots / filling


@pytest.mark.parametrize("lots", [0.01, 0.06, 0.07, 1.0, 100.0])
def test_valid_lots(lots):
    assert tm.validate_lots(lots, 0.01, 100.0, 0.01) == []


@pytest.mark.parametrize(
    "lots,fragment",
    [(0.005, "below"), (150.0, "above"), (0.065, "multiple"), (0.061, "multiple")],
)
def test_invalid_lots(lots, fragment):
    problems = tm.validate_lots(lots, 0.01, 100.0, 0.01)
    assert problems and fragment in problems[0]


def test_lot_step_of_0_1():
    assert tm.validate_lots(0.3, 0.1, 50, 0.1) == []
    assert tm.validate_lots(0.06, 0.1, 50, 0.1)


@pytest.mark.parametrize(
    "flags,expected",
    [
        (1, tm.ORDER_FILLING_FOK),
        (2, tm.ORDER_FILLING_IOC),
        (3, tm.ORDER_FILLING_FOK),
        (0, tm.ORDER_FILLING_RETURN),
        (4, tm.ORDER_FILLING_RETURN),  # BOC only
        (6, tm.ORDER_FILLING_IOC),
    ],
)
def test_filling_mode_selection(flags, expected):
    assert tm.select_filling_mode(flags) == expected


# --- retry rules


def test_retry_only_on_requote_price_changed_price_off():
    for code in (10004, 10020, 10021):
        assert tm.should_retry(code, 1)
    for code in (None, 10006, 10009, 10012, 10014, 10016, 10018, 10019, 10030, 10031):
        assert not tm.should_retry(code, 1)


def test_retry_at_most_three_times():
    assert tm.should_retry(10004, 1)  # retry 1
    assert tm.should_retry(10004, 3)  # retry 3
    assert not tm.should_retry(10004, 4)  # 4 sends done = 3 retries used


# --- misc


def test_spread_and_slippage():
    assert tm.spread_points(2650.00, 2650.30, 0.01) == 30
    assert tm.slippage_points("buy", 2650.30, 2650.35, 0.01) == 5  # worse
    assert tm.slippage_points("buy", 2650.30, 2650.25, 0.01) == -5  # better
    assert tm.slippage_points("sell", 2650.00, 2649.95, 0.01) == 5  # worse
    assert tm.slippage_points("sell", 2650.00, 2650.02, 0.01) == -2  # better


def test_stops_level():
    assert tm.stops_level_problem("buy", 100.0, 100.1, 100.5, None, 40, 0.01) is None
    assert "TP" in tm.stops_level_problem("buy", 100.0, 100.1, 100.5, None, 100, 0.01 * 10)
    assert "SL" in tm.stops_level_problem("sell", 100.0, 100.1, None, 100.2, 50, 0.01)
    assert tm.stops_level_problem("sell", 100.0, 100.1, 99.0, None, 0, 0.01) is None


def test_trade_mode_rules():
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_FULL, "sell") is None
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_LONGONLY, "buy") is None
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_LONGONLY, "sell")
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_SHORTONLY, "buy")
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_CLOSEONLY, "buy")
    assert tm.trade_mode_problem(tm.SYMBOL_TRADE_MODE_DISABLED, "buy")


def test_close_reasons():
    assert tm.close_reason_text(tm.DEAL_REASON_TP) == "TP"
    assert tm.close_reason_text(tm.DEAL_REASON_SL) == "SL"
    assert tm.close_reason_text(tm.DEAL_REASON_SO) == "stop-out"
    assert tm.close_reason_text(tm.DEAL_REASON_CLIENT) == "manual"
