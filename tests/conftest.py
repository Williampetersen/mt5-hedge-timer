import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hedgebot.config import parse_config  # noqa: E402

ENV = {"BUY_MT5_PASSWORD": "pw-buy", "SELL_MT5_PASSWORD": "pw-sell"}

BUY_RAW = {
    "name": "buy-account",
    "direction": "buy",
    "account": {
        "login": 1001,
        "server": "Broker-Demo",
        "password_env": "BUY_MT5_PASSWORD",
        "terminal_path": "C:\\MT5_Buy\\terminal64.exe",
        "portable": False,
        "allow_real_account": False,
    },
    "trade": {
        "symbol": "XAUUSD",
        "lots": 0.06,
        "tp_money": 150.0,
        "tp_points": None,
        "sl_money": None,
        "sl_points": None,
        "deviation_points": 300,
        "magic": 20261115,
        "comment": "HedgeTimer",
        "max_spread_points": None,
        "check_margin": True,
    },
    "schedule": {
        "time": "14:29:45",
        "timezone": "Europe/Copenhagen",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "dates": [],
        "max_late_ms": 2000,
        "prearm_seconds": 30,
        "use_ntp_offset": False,
        "ntp_server": "pool.ntp.org",
        "max_clock_offset_ms": 500,
    },
    "monitor": {"enabled": True, "until": "23:00:00", "poll_seconds": 1},
}


def sell_raw_from(buy_raw):
    raw = copy.deepcopy(buy_raw)
    raw["name"] = "sell-account"
    raw["direction"] = "sell"
    raw["account"]["login"] = 2002
    raw["account"]["password_env"] = "SELL_MT5_PASSWORD"
    raw["account"]["terminal_path"] = "C:\\MT5_Sell\\terminal64.exe"
    return raw


def make_cfg(raw, env=None, **kw):
    kw.setdefault("check_paths", False)
    return parse_config(raw, source="test.yaml", env=ENV if env is None else env, **kw)


@pytest.fixture
def buy_raw():
    return copy.deepcopy(BUY_RAW)


@pytest.fixture
def sell_raw():
    return sell_raw_from(BUY_RAW)


@pytest.fixture
def buy_cfg(buy_raw):
    return make_cfg(buy_raw)


@pytest.fixture
def sell_cfg(sell_raw):
    return make_cfg(sell_raw)
