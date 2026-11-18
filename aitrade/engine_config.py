"""Turn the app's settings into the two engine configs (one BUY, one SELL).

The engine (hedgebot) stays the single place where the trading rules live;
this file only fills in its config from what the user typed. Passwords are
passed to each engine process in its own environment variable, never in a file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

import yaml

from hedgebot.config import LOCAL_TIMEZONE, AppConfig, ConfigError, parse_config
from hedgebot.pair import validate_pair

from .settings import SIDES, Settings

PASSWORD_ENV = {"buy": "AITRADE_BUY_PASSWORD", "sell": "AITRADE_SELL_PASSWORD"}
MONEY_CURRENCY = "USD"  # take profit and stop loss are typed in USD
MAX_CLOCK_OFFSET_MS = 500


def build_engine_config(settings: Settings, side: str) -> dict:
    """The engine's config for one side, as plain data (same shape as config/buy.example.yaml)."""
    acc, plan, adv = settings.account(side), settings.plan, settings.advanced
    return {
        "name": side,
        "direction": side,
        "account": {
            "login": acc.login,
            "server": acc.server.strip(),
            "password_env": PASSWORD_ENV[side],
            "terminal_path": acc.terminal_path.strip(),
            "portable": acc.portable,
            "allow_real_account": acc.real,
            "launch": True,  # the engine starts MetaTrader 5 itself, with Algo Trading on
        },
        "trade": {
            "symbol": acc.symbol.strip(),
            "lots": plan.lots,
            "tp_money": plan.tp_usd,
            "tp_points": None,
            "sl_money": plan.sl_usd,
            "sl_points": None,
            "money_currency": MONEY_CURRENCY,
            "deviation_points": adv.deviation_points,
            "magic": adv.magic,
            "comment": adv.comment,
            "max_spread_points": adv.max_spread_points,
            "check_margin": True,
        },
        "schedule": {
            "time": plan.time,
            "timezone": LOCAL_TIMEZONE,  # the PC's own time zone
            "weekdays": [],
            "dates": [plan.date],  # one run on one date
            "max_late_ms": adv.max_late_ms,
            "prearm_seconds": adv.prearm_seconds,
            # The PC's own clock decides when to open and is never shifted. (advanced.use_ntp only
            # makes the window compare the PC clock with internet time and warn when it is wrong.)
            "use_ntp_offset": False,
            "ntp_server": adv.ntp_server,
            "max_clock_offset_ms": MAX_CLOCK_OFFSET_MS,
        },
        "monitor": {"enabled": True, "until": adv.monitor_until, "poll_seconds": 1},
    }


def check_engine_configs(
    settings: Settings, passwords: Optional[Mapping[str, str]] = None, *, check_paths: bool = True
) -> tuple[list[str], list[str], dict[str, AppConfig]]:
    """Let the engine itself validate both configs and the pair.

    Returns (errors, warnings, configs). Any error means: do not start.
    `passwords` maps 'buy'/'sell' to the password; leave it out to skip the password check.
    """
    env = {PASSWORD_ENV[s]: (passwords or {}).get(s, "") for s in SIDES}
    errors: list[str] = []
    warnings: list[str] = []
    configs: dict[str, AppConfig] = {}
    for side in SIDES:
        try:
            cfg = parse_config(
                build_engine_config(settings, side), source=f"{side} account", env=env,
                require_secrets=passwords is not None, check_paths=check_paths, reject_example_logins=False,
            )
        except ConfigError as exc:
            errors += [f"{side.upper()}: {e}" for e in exc.errors]
            continue
        configs[side] = cfg
        warnings += [f"{side.upper()}: {w}" for w in cfg.warnings if "allow_real_account" not in w]
    if len(configs) == 2:
        pair_errors, pair_warnings = validate_pair(configs["buy"], configs["sell"], same_symbol=False)
        errors += pair_errors
        warnings += [w for w in pair_warnings if "allow_real_account" not in w]
    return errors, warnings, configs


def write_engine_configs(settings: Settings, folder: Path) -> dict[str, Path]:
    """Write buy.yaml and sell.yaml into `folder` and return their paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for side in SIDES:
        path = folder / f"{side}.yaml"
        text = "# Written by Aitrade before every run. Do not edit: change the values in the app.\n"
        text += yaml.safe_dump(build_engine_config(settings, side), sort_keys=False, allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        paths[side] = path
    return paths
