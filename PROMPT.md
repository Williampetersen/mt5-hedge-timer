# Project: MT5 Hedge Timer (two apps: one BUY, one SELL)

Build a complete, production-quality Python project in this folder. Follow this spec exactly. If something is ambiguous, make the safest reasonable choice and list your choices at the end. Do not place real orders yourself while developing.

## 1. Goal

Two separate apps, each controlling its **own separate MetaTrader 5 terminal and its own account**:

- **BUY app**: opens one BUY market position on MT5 terminal #1 (account #1)
- **SELL app**: opens one SELL market position on MT5 terminal #2 (account #2)

Both open **at exactly the same scheduled time, in the same second** (default 14:29:45 Copenhagen time), with the same symbol, the same lot size and the same take-profit distance. By default there is **no stop loss**. Everything (account, server, terminal path, time, symbol, lot, TP, SL, days, etc.) must be a variable in config files. Nothing may be hard-coded.

Both apps use **one shared codebase**. The direction comes from the config file, so there is one engine and two configs/launchers.

## 2. Tech stack

- Windows 10/11, Python 3.11+, PowerShell
- `MetaTrader5` (official Python package), `pyyaml`, `python-dotenv`, `tzdata` (required on Windows for `zoneinfo`), `ntplib`, `requests`, `pytest`
- `requirements.txt` with pinned versions, plus a virtual environment in `.venv`

Important fact: one Python process can only connect to one MT5 terminal. That is why there are two processes, and each one uses a **different terminal installation folder** (e.g. `C:\MT5_Buy\terminal64.exe` and `C:\MT5_Sell\terminal64.exe`).

## 3. Folder structure

```
mt5-hedge-timer/
  hedgebot/
    __init__.py
    __main__.py        # CLI entry: python -m hedgebot --config config/buy.yaml
    config.py          # load + validate YAML and .env, dataclasses
    clock.py           # timezone, NTP offset, precise wait-until
    mt5_client.py      # connect, preflight checks, send order, modify TP, monitor
    trade_math.py      # TP/SL price from money amount, lot validation, filling mode
    journal.py         # CSV trade journal
    notify.py          # optional Telegram notifications (never blocks trading)
    pair.py            # validates that a buy config and a sell config form a correct pair
  run_pair.py          # validates both configs, then starts both apps as 2 subprocesses
  config/
    buy.example.yaml
    sell.example.yaml
  scripts/
    start_buy.bat
    start_sell.bat
    start_both.bat     # runs run_pair.py
    setup.ps1          # creates .venv, installs requirements, copies example configs/.env if missing
    register_tasks.ps1 # optional: Windows Task Scheduler entry to start the pair automatically
  mql5/                # if TimedEntry.mq5 is in the project folder, move it here (backup EA option)
  tests/
  logs/                # gitignored
  data/                # gitignored (journal.csv)
  .env.example
  .gitignore
  requirements.txt
  README.md
  PROMPT.md            # this file, keep it in the repo as the spec
```

## 4. Config (YAML, one file per app). All fields are variables

```yaml
name: buy-account                 # used in logs/journal
direction: buy                    # buy | sell  (must be opposite in the two files)

account:
  login: 12345678
  server: "BrokerName-Server"
  password_env: BUY_MT5_PASSWORD  # name of the variable in .env (password is NEVER in YAML)
  terminal_path: 'C:\MT5_Buy\terminal64.exe'
  portable: false
  allow_real_account: false       # if false, refuse to trade unless the account is DEMO

trade:
  symbol: XAUUSD                  # allow broker suffixes like XAUUSD.m
  lots: 0.06
  tp_money: 150.0                 # TP in account currency from the fill price; null = no TP
  tp_points: null                 # alternative: TP in points (use only one of tp_money/tp_points)
  sl_money: null                  # null = no stop loss (default)
  sl_points: null
  deviation_points: 300           # max slippage
  magic: 20261115
  comment: "HedgeTimer"
  max_spread_points: null         # null = no spread filter; if set and spread is wider, skip and log
  check_margin: true              # log + notify if free margin < required margin before sending

schedule:
  time: "14:29:45"                # HH:MM:SS
  timezone: "Europe/Copenhagen"   # IANA name; handles summer/winter time automatically
  weekdays: [Mon, Tue, Wed, Thu, Fri]
  dates: []                       # e.g. ["2026-12-05", "2026-12-10"]; if not empty, ONLY these dates
  max_late_ms: 2000               # if we are later than this after the target, do NOT open
  prearm_seconds: 30              # connect, select symbol, warm up ticks this many seconds before
  use_ntp_offset: true            # correct PC clock error using NTP
  ntp_server: "pool.ntp.org"
  max_clock_offset_ms: 500        # warn (and notify) if PC clock is off by more than this

monitor:
  enabled: true                   # after opening, watch the position until it closes
  until: "23:00:00"               # stop monitoring at this local time (position stays open in MT5)
  poll_seconds: 1
```

`.env` (gitignored), with `.env.example` committed:

```
BUY_MT5_PASSWORD=
SELL_MT5_PASSWORD=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Validation (fail fast with a clear message): direction is buy/sell, the time format, a valid timezone, lots > 0, only one of tp_money/tp_points, only one of sl_money/sl_points, the password env var exists, and terminal_path exists.

## 5. Exact behaviour of each app

1. Load config and .env, set up logging (console + `logs/<name>_<YYYY-MM-DD>.log`, timestamps with **milliseconds**).
2. Compute the next target time as a timezone-aware datetime in `schedule.timezone`, respecting weekdays/dates. Correct for DST using `zoneinfo`. **Never rely on the Windows timezone setting**: it must also work on a VPS whose clock is in UTC.
3. If `use_ntp_offset`, measure the PC clock offset once at startup and again at prearm. Apply it to all scheduling. Warn if it is above `max_clock_offset_ms`.
4. Wait (sleep coarsely, no busy CPU) until `prearm_seconds` before the target.
5. **Prearm:** `mt5.initialize(path=..., login=..., password=..., server=..., portable=...)`, then preflight checks:
   - connected and logged into the **expected login** (abort if it's a different account)
   - `terminal_info().trade_allowed` is True (the Algo Trading button is ON), otherwise abort with clear instructions
   - account is DEMO unless `allow_real_account: true`
   - symbol exists and is selected, trade mode allows the direction, and the market is open
   - lots respect volume_min/max/step
   - required margin vs free margin (`order_calc_margin`), logged
   - TP/SL distance computed and logged, e.g. "TP 150 USD = 25.00 price distance"
   - pick the filling mode from `symbol_info().filling_mode` (FOK → IOC → RETURN)
   - pre-build the order request so only price/TP need filling in at the target moment
6. **Precise trigger:** sleep until ~1 s before the target, then spin-wait (with tiny sleeps) until the exact target time. At the target: read a fresh tick, fill in price (ask for buy, bid for sell) and TP, and call `order_send`. Log **the target time, the actual send time, the deviation in ms, the round-trip latency in ms, the spread at send, the requested price vs the fill price and the slippage**.
7. If later than `max_late_ms` (e.g. the PC was asleep), do NOT open; log and notify.
8. Retry up to 3 times only on requote / price changed / price off (10004, 10020, 10021). Never retry on other errors. Never open more than one position per day per app: check for an existing position with the same magic and symbol first.
9. **After the fill:** recompute TP (and SL if set) from the **actual fill price** so the money amount is exact, and modify the position (`TRADE_ACTION_SLTP`) if it differs by ≥ 1 tick. Compute money→price distance with `order_calc_profit` (it handles account currency), falling back to tick_value/tick_size.
10. **Monitor** (if enabled): every `poll_seconds`, track the floating P/L, max favourable and max adverse excursion. When the position closes, read the deals history and record the close reason (TP, SL, stop-out, manual), close price, profit, swap, commission and duration.
11. Append a row to `data/journal.csv`: date, app name, login, direction, symbol, lots, target time, send time, deviation ms, latency ms, spread, requested price, fill price, slippage, TP price, close time, close reason, profit, max adverse, max favourable.
12. Telegram (only if token+chat id are set): send messages on armed / opened / closed / error. Send **after** the order, in a background thread with a short timeout, so notifications can never delay or break trading.
13. `mt5.shutdown()` cleanly. Return exit code 0 on success, non-zero on failure.

## 6. CLI options

```
python -m hedgebot --config config/buy.yaml            # normal scheduled run
python -m hedgebot --config config/buy.yaml --dry-run  # everything, but order_check() instead of order_send()
python -m hedgebot --config config/buy.yaml --test-in 60   # target = now + 60 s (for demo testing)
python -m hedgebot --config config/buy.yaml --check    # only connect + preflight + print TP/margin, then exit
python -m hedgebot --config config/buy.yaml --loop     # keep running and trade every scheduled day
```

`run_pair.py --buy config/buy.yaml --sell config/sell.yaml [same flags]`:
- validates the pair first and refuses to start if it's not valid: one is buy and one is sell; same symbol, lots, TP, SL, time, timezone, weekdays and dates; **different** logins and **different** terminal paths
- with `--test-in N`, computes ONE shared target timestamp and passes it to both children, so they fire on exactly the same target
- starts both as subprocesses, prefixes their console output with [BUY]/[SELL], and at the end prints the difference in ms between the two send times

## 7. Tests (pytest, MT5 mocked; must pass without MT5 installed)

- target time calculation incl. DST change days in Europe/Copenhagen, weekday/dates filtering, a UTC-based PC
- TP/SL price from money for buy and sell (e.g. XAUUSD 0.06 lot, 150 USD → 25.00 distance), rounding to tick size
- lot step validation, filling mode selection
- pair validation (all failure cases)
- max_late_ms skip logic, the one-position-per-day guard, the retry rules

## 8. README.md

Clear step-by-step for a Windows user: install Python, Git, MT5 twice into different folders, log into each account once and enable Algo Trading, run `scripts/setup.ps1`, fill in `.env` and the two YAML files, run `--check`, then `--test-in 60` on DEMO accounts, then the scheduled run. Include troubleshooting (Algo Trading off, wrong login, symbol suffix, market closed, clock offset, PC sleep), how to use Task Scheduler, and how to clone the project on another PC. Add a short risk section: with no SL and a small account, the losing side can be stopped out, and both sides can lose on a whipsaw.

## 9. Git + GitHub

- `.gitignore`: `.env`, `config/buy.yaml`, `config/sell.yaml` (only the *.example.yaml files are committed), `.venv/`, `logs/`, `data/`, `__pycache__/`, `*.log`
- Before every commit, check that no password, login or token is staged (e.g. `git diff --cached` + grep). If any is found, stop and tell me.
- `git init`, meaningful commits, branch `main`
- Create a **private** GitHub repo named `mt5-hedge-timer` with GitHub CLI: `gh repo create mt5-hedge-timer --private --source=. --remote=origin --push`. If `gh` isn't installed or logged in, stop and give me the exact PowerShell commands (`winget install GitHub.cli`, `gh auth login`), then continue after I confirm.
- At the end, print the repo URL and the commands to clone and set it up on another PC.

## 10. Order of work

1. Scaffold the structure, requirements and setup script
2. config + clock + trade_math with tests → run pytest until green
3. mt5_client + CLI + journal + notify
4. pair.py + run_pair.py + bat/ps1 scripts
5. README
6. Run all tests, then `--check` if MT5 is installed (never a live order)
7. Commit and push to GitHub
8. Give me a short summary: what was built, how to run the first demo test, and the assumptions you made
