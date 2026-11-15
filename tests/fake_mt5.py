"""A small in-memory stand-in for the MetaTrader5 module (XAUUSD-like symbol)."""

from datetime import datetime, timezone
from types import SimpleNamespace as NS

SERVER_OFFSET = 7200  # broker server time = UTC+2


class FakeClock:
    def __init__(self, t: float):
        self.t = float(t)
        self.offset_s = 0.0

    def time(self):
        return self.t

    def pc_time(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t, timezone.utc)

    def calibrate(self):
        pass

    def sleep(self, s):
        self.t += max(float(s), 0.0001)


class FakeMT5:
    def __init__(self, clock=None, *, login=1001, trade_mode=0, contract=100.0):
        self.clock = clock
        self.contract = contract
        self.account = NS(login=login, trade_mode=trade_mode, trade_allowed=True, trade_expert=True,
                          margin_free=10_000.0, balance=10_000.0, equity=10_000.0, currency="USD",
                          leverage=100, margin_mode=2, server="Broker-Demo")
        self.terminal = NS(connected=True, trade_allowed=True, tradeapi_disabled=False, ping_last=25_000,
                           build=5000, path="C:\\MT5_Buy")
        self.symbol = NS(name="XAUUSD", point=0.01, digits=2, trade_tick_size=0.01, trade_tick_value=1.0,
                         trade_tick_value_profit=1.0, trade_tick_value_loss=1.0, volume_min=0.01,
                         volume_max=100.0, volume_step=0.01, filling_mode=1, trade_mode=4,
                         trade_stops_level=0, trade_contract_size=contract, currency_profit="USD")
        self.bid, self.ask = 2650.00, 2650.30
        self.positions = []
        self.deals = []
        self.send_codes = []  # queue of retcodes for order_send (default: done)
        self.check_code = 0
        self.fill_slippage = 0.0
        self.sent = []
        self.checked = []
        self.init_args = None
        self.initialized = False
        self.next_ticket = 500_000
        self.symbols_present = {"XAUUSD"}
        self.last_err = (1, "Success")
        self.tick_age_s = 0.0  # age of this symbol's last quote
        self.other_quotes_age_s = 0.0  # age of the other Market Watch quotes

    # --- session
    def initialize(self, *args, **kwargs):
        self.init_args = (args, kwargs)
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return self.last_err

    def terminal_info(self):
        return self.terminal

    def account_info(self):
        return self.account

    # --- symbols / prices
    def symbol_select(self, name, enable=True):
        return name in self.symbols_present

    def symbol_info(self, name):
        return self.symbol if name in self.symbols_present else None

    def symbols_get(self, group=None):
        server_now = int(self._now() + SERVER_OFFSET)
        listed = [NS(name=n, visible=True, time=server_now - self.other_quotes_age_s)
                  for n in ("EURUSD", "GBPUSD")]
        listed += [NS(name=n, visible=n in self.symbols_present, time=self._tick_server_s())
                   for n in sorted(self.symbols_present | {"GOLD", "XAUEUR.m"})]
        return tuple(listed)

    def _now(self):
        return self.clock.time() if self.clock else 1_794_000_000.0

    def _tick_server_s(self):
        return self._now() + SERVER_OFFSET - self.tick_age_s

    def symbol_info_tick(self, name):
        server_ms = int(self._tick_server_s() * 1000)
        return NS(bid=self.bid, ask=self.ask, time=server_ms // 1000, time_msc=server_ms)

    def order_calc_profit(self, order_type, symbol, volume, price_open, price_close):
        diff = (price_close - price_open) if order_type == 0 else (price_open - price_close)
        return round(diff * volume * self.contract, 2)

    def order_calc_margin(self, order_type, symbol, volume, price):
        return round(volume * self.contract * price / self.account.leverage, 2)

    # --- trading
    def order_check(self, request):
        self.checked.append(dict(request))
        return NS(retcode=self.check_code, comment="Done" if self.check_code == 0 else "rejected",
                  margin=0.0, margin_free=self.account.margin_free)

    def order_send(self, request):
        self.sent.append(dict(request))
        if request["action"] == 6:  # SLTP
            for p in self.positions:
                if p.ticket == request["position"]:
                    p.tp, p.sl = request["tp"], request["sl"]
                    return NS(retcode=10009, comment="Request executed", order=0, deal=0, price=0.0, volume=0.0)
            return NS(retcode=10013, comment="Invalid request", order=0, deal=0, price=0.0, volume=0.0)
        code = self.send_codes.pop(0) if self.send_codes else 10009
        if code is None:
            self.last_err = (-10005, "IPC timeout")
            return None
        if code not in (10009, 10010):
            return NS(retcode=code, comment="rejected", order=0, deal=0, price=0.0, volume=0.0)
        self.next_ticket += 1
        ticket = self.next_ticket
        buy = request["type"] == 0
        fill = round(request["price"] + (self.fill_slippage if buy else -self.fill_slippage), 2)
        server_ms = int((self._now() + SERVER_OFFSET) * 1000)
        self.positions.append(NS(ticket=ticket, symbol=request["symbol"], magic=request["magic"],
                                 type=request["type"], volume=request["volume"], price_open=fill,
                                 tp=request.get("tp", 0.0), sl=request.get("sl", 0.0), profit=0.0,
                                 time_msc=server_ms))
        self.deals.append(NS(ticket=ticket + 1, order=ticket, position_id=ticket, symbol=request["symbol"],
                             magic=request["magic"], entry=0, reason=3, volume=request["volume"], price=fill,
                             profit=0.0, swap=0.0, commission=-0.3, fee=0.0, time=server_ms // 1000,
                             time_msc=server_ms))
        return NS(retcode=code, comment="Request executed", order=ticket, deal=ticket + 1, price=fill,
                  volume=request["volume"])

    def positions_get(self, symbol=None, ticket=None, group=None):
        out = [p for p in self.positions
               if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]
        return tuple(out)

    def history_deals_get(self, *args, position=None, ticket=None):
        out = [d for d in self.deals if position is None or d.position_id == position]
        return tuple(out)

    # --- test helpers
    def close_position(self, ticket, price, reason=5, after_s=60.0):
        pos = next(p for p in self.positions if p.ticket == ticket)
        self.positions.remove(pos)
        opened = next(d for d in self.deals if d.position_id == ticket and d.entry == 0)
        diff = (price - pos.price_open) if pos.type == 0 else (pos.price_open - price)
        self.deals.append(NS(ticket=ticket + 2, order=ticket + 2, position_id=ticket, symbol=pos.symbol,
                             magic=pos.magic, entry=1, reason=reason, volume=pos.volume, price=price,
                             profit=round(diff * pos.volume * self.contract, 2), swap=-1.5, commission=-0.3,
                             fee=0.0, time=opened.time + int(after_s),
                             time_msc=opened.time_msc + int(after_s * 1000)))
