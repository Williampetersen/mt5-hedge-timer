"""A pretend MetaTrader5 package for demos and end-to-end tests. NEVER shipped with the app.

Put this folder first on PYTHONPATH and the engine imports this file instead of
the real package, so the whole app can be exercised without MT5, a broker or
any order. Behaviour can be steered with environment variables:

  FAKE_MT5_FAIL_LOGIN=<login>   initialize() fails for that account number
  FAKE_MT5_SLOW_START=<login>:<sec>  initialize() takes that long for that account (a slow MetaTrader start)
  FAKE_MT5_CLOSE_AFTER=<sec>    an opened position hits its take profit after this long (default 12)
  FAKE_MT5_REAL=1               the account reports itself as a real-money account
"""

import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_mt5 import FakeMT5  # noqa: E402


class _RealClock:
    @staticmethod
    def time() -> float:
        return time.time()


_fake = FakeMT5(_RealClock(), trade_mode=2 if os.environ.get("FAKE_MT5_REAL") else 0)
_fake.symbols_present = {"XAUUSD", "XAUUSD.m", "GOLD"}
_CLOSE_AFTER = float(os.environ.get("FAKE_MT5_CLOSE_AFTER", "12"))
_opened_at = {}


def initialize(*args, **kwargs):
    login = kwargs.get("login")
    if str(login) == os.environ.get("FAKE_MT5_FAIL_LOGIN", ""):
        _fake.last_err = (-6, "Terminal: Authorization failed")
        return False
    _fake.account.login = login
    slow_login, _, slow_seconds = os.environ.get("FAKE_MT5_SLOW_START", "").partition(":")
    time.sleep(float(slow_seconds) if slow_seconds and str(login) == slow_login else 0.4)  # it needs a moment
    return _fake.initialize(*args, **kwargs)


def _wiggle():
    """Move the price a little and close positions that reached their take profit time."""
    step = random.uniform(-0.25, 0.25)
    _fake.bid = round(_fake.bid + step, 2)
    _fake.ask = round(_fake.bid + 0.30, 2)
    now = time.time()
    for pos in list(_fake.positions):
        _opened_at.setdefault(pos.ticket, now)
        buy = pos.type == 0
        current = _fake.bid if buy else _fake.ask
        diff = (current - pos.price_open) if buy else (pos.price_open - current)
        pos.profit = round(diff * pos.volume * _fake.contract, 2)
        if pos.tp and now - _opened_at[pos.ticket] >= _CLOSE_AFTER:
            _fake.close_position(pos.ticket, pos.tp, reason=5, after_s=now - _opened_at[pos.ticket])


def symbol_info(name):
    info = _fake.symbol_info(name)
    if info is not None:
        info.name = name
    return info


def symbol_info_tick(name):
    _wiggle()
    return _fake.symbol_info_tick(name)


def positions_get(*args, **kwargs):
    _wiggle()
    return _fake.positions_get(*args, **kwargs)


def __getattr__(name):  # everything else comes straight from the fake
    return getattr(_fake, name)
