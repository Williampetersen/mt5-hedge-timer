"""Load and validate one app config (YAML) plus its secrets (.env).

Validation fails fast with every problem listed at once, so a user can fix the
whole file in one go. Secrets (MT5 password, Telegram token) only ever come
from environment variables / .env and are kept out of repr() and logs.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

# Logins used in the example files; refusing them catches "forgot to edit".
EXAMPLE_LOGINS = frozenset({12345678, 87654321})
MAX_COMMENT_LEN = 31  # MT5 order comment limit

WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_KEYS = {
    "": {"name", "direction", "account", "trade", "schedule", "monitor"},
    "account": {"login", "server", "password_env", "terminal_path", "portable", "allow_real_account"},
    "trade": {
        "symbol", "lots", "tp_money", "tp_points", "sl_money", "sl_points", "deviation_points",
        "magic", "comment", "max_spread_points", "check_margin",
    },
    "schedule": {
        "time", "timezone", "weekdays", "dates", "max_late_ms", "prearm_seconds",
        "use_ntp_offset", "ntp_server", "max_clock_offset_ms",
    },
    "monitor": {"enabled", "until", "poll_seconds"},
}

_MISSING = object()


class ConfigError(Exception):
    """Invalid or missing configuration. str() lists every problem found."""

    def __init__(self, source: str, errors: list[str]):
        self.source = source
        self.errors = list(errors)
        lines = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"Invalid config {source}:\n{lines}")


@dataclass(frozen=True)
class AccountConfig:
    login: int
    server: str
    password_env: str
    terminal_path: str
    portable: bool = False
    allow_real_account: bool = False


@dataclass(frozen=True)
class TradeConfig:
    symbol: str
    lots: float
    tp_money: Optional[float]
    tp_points: Optional[float]
    sl_money: Optional[float]
    sl_points: Optional[float]
    deviation_points: int
    magic: int
    comment: str
    max_spread_points: Optional[float]
    check_margin: bool

    @property
    def has_tp(self) -> bool:
        return self.tp_money is not None or self.tp_points is not None

    @property
    def has_sl(self) -> bool:
        return self.sl_money is not None or self.sl_points is not None

    def describe_tp(self, currency: str = "") -> str:
        return _describe_level(self.tp_money, self.tp_points, currency)

    def describe_sl(self, currency: str = "") -> str:
        return _describe_level(self.sl_money, self.sl_points, currency)


def _describe_level(money: Optional[float], points: Optional[float], currency: str) -> str:
    if money is not None:
        return f"{money:.2f} {currency}".rstrip()
    if points is not None:
        return f"{points:g} points"
    return "none"


@dataclass(frozen=True)
class ScheduleConfig:
    time: dtime
    timezone: str
    weekdays: tuple[int, ...]
    dates: tuple[date, ...]
    max_late_ms: int
    prearm_seconds: float
    use_ntp_offset: bool
    ntp_server: str
    max_clock_offset_ms: float

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def describe_days(self) -> str:
        if self.dates:
            return "only on " + ", ".join(d.isoformat() for d in self.dates)
        return ", ".join(WEEKDAY_ABBR[d] for d in self.weekdays)


@dataclass(frozen=True)
class MonitorConfig:
    enabled: bool
    until: dtime
    poll_seconds: float


@dataclass(frozen=True)
class AppConfig:
    name: str
    direction: str
    account: AccountConfig
    trade: TradeConfig
    schedule: ScheduleConfig
    monitor: MonitorConfig
    source: str = ""
    # Secrets: never shown in repr() and never compared.
    password: str = field(default="", repr=False, compare=False)
    telegram_token: str = field(default="", repr=False, compare=False)
    telegram_chat_id: str = field(default="", repr=False, compare=False)
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def is_buy(self) -> bool:
        return self.direction == "buy"


# --------------------------------------------------------------------------- loading


def load_env(env_file: Optional[str | os.PathLike] = None) -> Optional[Path]:
    """Load .env into os.environ (existing variables win). Returns the file used."""
    from dotenv import load_dotenv

    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if path.is_file():
        load_dotenv(path, override=False, encoding="utf-8")
        return path
    if env_file:
        raise ConfigError(str(path), [f".env file not found: {path}"])
    return None


def load_config(
    path: str | os.PathLike,
    *,
    env_file: Optional[str | os.PathLike] = None,
    require_secrets: bool = True,
    check_paths: bool = True,
) -> AppConfig:
    """Read a YAML config + .env and return a validated AppConfig (or raise ConfigError)."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(str(path), [
            f"config file not found: {path}. Copy config/buy.example.yaml (or sell) to this name "
            "- scripts/setup.ps1 does it - and edit it."
        ])
    env_path = load_env(env_file)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        hint = ""
        if "escape" in str(exc):
            hint = " Tip: write Windows paths in single quotes, e.g. 'C:\\MT5_Buy\\terminal64.exe'."
        raise ConfigError(str(path), [f"YAML syntax error: {exc}.{hint}"]) from None
    return parse_config(
        raw,
        source=str(path),
        env=os.environ,
        env_file=env_path,
        require_secrets=require_secrets,
        check_paths=check_paths,
    )


def parse_config(
    raw: Any,
    *,
    source: str = "<config>",
    env: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
    require_secrets: bool = True,
    check_paths: bool = True,
    reject_example_logins: bool = True,
) -> AppConfig:
    """Validate an already-parsed YAML mapping. Raises ConfigError listing all problems."""
    env = os.environ if env is None else env
    if not isinstance(raw, dict):
        raise ConfigError(source, ["the file must contain YAML 'key: value' lines (it is empty or not a mapping)"])

    v = _Validator()
    v.unknown_keys(raw, "")

    name = v.string(raw, "", "name")
    if name is not None and not _NAME_RE.match(name):
        v.err(f"'name' may only contain letters, digits, '.', '_' and '-' (got {name!r})")
    direction = v.string(raw, "", "direction")
    if direction is not None:
        direction = direction.lower()
        if direction not in ("buy", "sell"):
            v.err(f"'direction' must be buy or sell (got {direction!r})")

    # ---- account
    acc = v.section(raw, "account")
    login = v.integer(acc, "account", "login", minimum=1)
    if login is not None and reject_example_logins and login in EXAMPLE_LOGINS:
        v.err(f"'account.login' is still the example value {login}; put your real MT5 account number")
    server = v.string(acc, "account", "server")
    password_env = v.string(acc, "account", "password_env")
    terminal_path = v.string(acc, "account", "terminal_path")
    portable = v.boolean(acc, "account", "portable", default=False)
    allow_real = v.boolean(acc, "account", "allow_real_account", default=False)

    password = ""
    if password_env is not None:
        if not _ENV_NAME_RE.match(password_env):
            v.err(f"'account.password_env' must be a variable name like BUY_MT5_PASSWORD (got {password_env!r})")
        else:
            password = env.get(password_env, "") or ""
            if require_secrets and not password:
                where = f"in {env_file}" if env_file else f"(no .env file found at {DEFAULT_ENV_FILE})"
                v.err(f"password variable {password_env} is missing or empty {where}; "
                      f"add the line {password_env}=your_password to .env")
    if terminal_path is not None and check_paths and not Path(terminal_path).is_file():
        v.err(f"'account.terminal_path' not found: {terminal_path} - install MetaTrader 5 into its own "
              "folder (see README) and point to its terminal64.exe")

    # ---- trade
    tr = v.section(raw, "trade")
    symbol = v.string(tr, "trade", "symbol")
    lots = v.number(tr, "trade", "lots", gt=0)
    tp_money = v.number(tr, "trade", "tp_money", default=None, nullable=True, gt=0)
    tp_points = v.number(tr, "trade", "tp_points", default=None, nullable=True, gt=0)
    sl_money = v.number(tr, "trade", "sl_money", default=None, nullable=True, gt=0)
    sl_points = v.number(tr, "trade", "sl_points", default=None, nullable=True, gt=0)
    if tp_money is not None and tp_points is not None:
        v.err("use only one of 'trade.tp_money' and 'trade.tp_points' (set the other to null)")
    if sl_money is not None and sl_points is not None:
        v.err("use only one of 'trade.sl_money' and 'trade.sl_points' (set the other to null)")
    deviation = v.integer(tr, "trade", "deviation_points", minimum=0)
    magic = v.integer(tr, "trade", "magic", minimum=0, maximum=2**63 - 1)
    comment = v.string(tr, "trade", "comment", allow_empty=True)
    if comment is not None and len(comment) > MAX_COMMENT_LEN:
        v.err(f"'trade.comment' is {len(comment)} characters; MT5 allows at most {MAX_COMMENT_LEN}")
    max_spread = v.number(tr, "trade", "max_spread_points", default=None, nullable=True, gt=0)
    check_margin = v.boolean(tr, "trade", "check_margin")

    # ---- schedule
    sc = v.section(raw, "schedule")
    at = v.time_of_day(sc, "schedule", "time")
    tz_name = v.string(sc, "schedule", "timezone")
    if tz_name is not None:
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            v.err(f"'schedule.timezone' {tz_name!r} is not a known IANA timezone such as Europe/Copenhagen "
                  "(on Windows the 'tzdata' package must be installed)")
            tz_name = None
    weekdays = v.weekdays(sc, "schedule", "weekdays")
    dates = v.dates(sc, "schedule", "dates")
    if weekdays is not None and dates is not None and not weekdays and not dates:
        v.err("no trading days: set 'schedule.weekdays' (e.g. [Mon, Tue, Wed, Thu, Fri]) or 'schedule.dates'")
    max_late_ms = v.integer(sc, "schedule", "max_late_ms", minimum=0)
    prearm = v.number(sc, "schedule", "prearm_seconds", ge=5, le=3600)
    use_ntp = v.boolean(sc, "schedule", "use_ntp_offset")
    ntp_server = v.string(sc, "schedule", "ntp_server", default=None, allow_none=True)
    if use_ntp and not ntp_server:
        v.err("'schedule.ntp_server' is required when use_ntp_offset is true (e.g. \"pool.ntp.org\")")
    max_offset = v.number(sc, "schedule", "max_clock_offset_ms", ge=0)

    # ---- monitor
    mo = v.section(raw, "monitor")
    mon_enabled = v.boolean(mo, "monitor", "enabled")
    mon_until = v.time_of_day(mo, "monitor", "until")
    mon_poll = v.number(mo, "monitor", "poll_seconds", ge=0.1, le=3600)

    # ---- warnings (non-fatal)
    if allow_real:
        v.warn("allow_real_account is TRUE: this app will trade a real-money account")
    if dates and weekdays:
        off = [d.isoformat() for d in dates if d.weekday() not in weekdays]
        if off:
            v.warn("schedule.dates overrides weekdays; these dates are not in weekdays: " + ", ".join(off))
    tg_token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if bool(tg_token) != bool(tg_chat):
        v.warn("Telegram is off: set BOTH TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    if v.errors:
        raise ConfigError(source, v.errors)

    return AppConfig(
        name=name,
        direction=direction,
        account=AccountConfig(
            login=login, server=server, password_env=password_env, terminal_path=terminal_path,
            portable=portable, allow_real_account=allow_real,
        ),
        trade=TradeConfig(
            symbol=symbol, lots=float(lots),
            tp_money=_f(tp_money), tp_points=_f(tp_points), sl_money=_f(sl_money), sl_points=_f(sl_points),
            deviation_points=deviation, magic=magic, comment=comment,
            max_spread_points=_f(max_spread), check_margin=check_margin,
        ),
        schedule=ScheduleConfig(
            time=at, timezone=tz_name, weekdays=weekdays, dates=dates, max_late_ms=max_late_ms,
            prearm_seconds=float(prearm), use_ntp_offset=use_ntp, ntp_server=ntp_server or "",
            max_clock_offset_ms=float(max_offset),
        ),
        monitor=MonitorConfig(enabled=mon_enabled, until=mon_until, poll_seconds=float(mon_poll)),
        source=source,
        password=password,
        telegram_token=tg_token if tg_chat else "",
        telegram_chat_id=tg_chat if tg_token else "",
        warnings=tuple(v.warnings),
    )


def _f(x: Optional[float]) -> Optional[float]:
    return None if x is None else float(x)


# --------------------------------------------------------------------------- validator


class _Validator:
    """Collects every problem instead of stopping at the first one."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def unknown_keys(self, mapping: dict, section: str) -> None:
        allowed = _KEYS[section]
        for key in mapping:
            if str(key) not in allowed:
                full = f"{section}.{key}" if section else str(key)
                self.err(f"unknown key '{full}' (typo?). Allowed here: {', '.join(sorted(allowed))}")

    def section(self, raw: dict, key: str) -> dict:
        val = raw.get(key)
        if val is None:
            self.err(f"missing section '{key}:'")
            return {}
        if not isinstance(val, dict):
            self.err(f"'{key}' must be a section with indented 'key: value' lines")
            return {}
        self.unknown_keys(val, key)
        return val

    def _get(self, sec: dict, section: str, key: str, default: Any) -> tuple[str, Any]:
        full = f"{section}.{key}" if section else key
        if key in sec:
            return full, sec[key]
        if default is _MISSING:
            if sec or not section:  # don't repeat errors for a section that is missing entirely
                self.err(f"missing required key '{full}'")
            return full, _MISSING
        return full, default

    def string(self, sec, section, key, default=_MISSING, *, allow_empty=False, allow_none=False):
        full, val = self._get(sec, section, key, default)
        if val is _MISSING or (val is None and allow_none):
            return None
        if not isinstance(val, str):
            self.err(f"'{full}' must be text (got {val!r})")
            return None
        val = val.strip()
        if not val and not allow_empty:
            self.err(f"'{full}' must not be empty")
            return None
        return val

    def boolean(self, sec, section, key, default=_MISSING) -> Optional[bool]:
        full, val = self._get(sec, section, key, default)
        if val is _MISSING:
            return None
        if not isinstance(val, bool):
            self.err(f"'{full}' must be true or false without quotes (got {val!r})")
            return None
        return val

    def number(self, sec, section, key, default=_MISSING, *, nullable=False, gt=None, ge=None, le=None):
        full, val = self._get(sec, section, key, default)
        if val is _MISSING:
            return None
        if val is None:
            if not nullable:
                self.err(f"'{full}' must be a number (got null)")
            return None
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
            self.err(f"'{full}' must be a number (got {val!r})")
            return None
        if gt is not None and not val > gt:
            self.err(f"'{full}' must be greater than {gt} (got {val})")
            return None
        if ge is not None and val < ge:
            self.err(f"'{full}' must be at least {ge} (got {val})")
            return None
        if le is not None and val > le:
            self.err(f"'{full}' must be at most {le} (got {val})")
            return None
        return val

    def integer(self, sec, section, key, default=_MISSING, *, minimum=None, maximum=None):
        full, val = self._get(sec, section, key, default)
        if val is _MISSING:
            return None
        if isinstance(val, str) and val.strip().isdigit():
            val = int(val.strip())
        if isinstance(val, bool) or not isinstance(val, int):
            self.err(f"'{full}' must be a whole number (got {val!r})")
            return None
        if minimum is not None and val < minimum:
            self.err(f"'{full}' must be at least {minimum} (got {val})")
            return None
        if maximum is not None and val > maximum:
            self.err(f"'{full}' must be at most {maximum} (got {val})")
            return None
        return val

    def time_of_day(self, sec, section, key, default=_MISSING) -> Optional[dtime]:
        full, val = self._get(sec, section, key, default)
        if val is _MISSING:
            return None
        if isinstance(val, dtime):
            return val
        if isinstance(val, int) and not isinstance(val, bool):
            # Unquoted 14:29:45 is a base-60 integer in YAML 1.1.
            self.err(f"'{full}': put the time in quotes, e.g. \"14:29:45\" "
                     f"(without quotes YAML reads it as the number {val})")
            return None
        m = _TIME_RE.match(val.strip()) if isinstance(val, str) else None
        if not m:
            self.err(f"'{full}' must be \"HH:MM:SS\" (got {val!r})")
            return None
        h, mi, s = (int(g) for g in m.groups())
        if h > 23 or mi > 59 or s > 59:
            self.err(f"'{full}' is not a valid time of day: {val!r}")
            return None
        return dtime(h, mi, s)

    def weekdays(self, sec, section, key) -> Optional[tuple[int, ...]]:
        full, val = self._get(sec, section, key, _MISSING)
        if val is _MISSING:
            return None
        if val is None:
            return ()
        if isinstance(val, str):
            val = [p for p in re.split(r"[,\s]+", val) if p]
        if not isinstance(val, list):
            self.err(f"'{full}' must be a list like [Mon, Tue, Wed, Thu, Fri]")
            return None
        days: set[int] = set()
        ok = True
        for item in val:
            idx = _WEEKDAYS.get(item.strip().lower()) if isinstance(item, str) else None
            if idx is None:
                self.err(f"'{full}' has an unknown day {item!r}; use Mon Tue Wed Thu Fri Sat Sun")
                ok = False
            else:
                days.add(idx)
        return tuple(sorted(days)) if ok else None

    def dates(self, sec, section, key) -> Optional[tuple[date, ...]]:
        full, val = self._get(sec, section, key, [])
        if val is None:
            return ()
        if not isinstance(val, list):
            self.err(f"'{full}' must be a list like [\"2026-12-05\", \"2026-12-10\"] (or [])")
            return None
        out: set[date] = set()
        ok = True
        for item in val:
            if isinstance(item, datetime):
                self.err(f"'{full}' entry {item!r} must be a date only (YYYY-MM-DD)")
                ok = False
            elif isinstance(item, date):
                out.add(item)
            elif isinstance(item, str):
                try:
                    out.add(date.fromisoformat(item.strip()))
                except ValueError:
                    self.err(f"'{full}' entry {item!r} is not a date in YYYY-MM-DD format")
                    ok = False
            else:
                self.err(f"'{full}' entry {item!r} is not a date in YYYY-MM-DD format")
                ok = False
        return tuple(sorted(out)) if ok else None
