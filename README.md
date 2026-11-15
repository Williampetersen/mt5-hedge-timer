# MT5 Hedge Timer

Two small apps that open opposite positions **in the same second** on two
separate MetaTrader 5 accounts:

- **BUY app**: opens one BUY market position on MT5 terminal #1 (account #1)
- **SELL app**: opens one SELL market position on MT5 terminal #2 (account #2)

Both use the same symbol, lot size and take-profit, at the same scheduled time
(default **14:29:45 Copenhagen time**, Mon–Fri). By default there is **no stop
loss**. Every value lives in a config file: nothing is hard-coded.

There is one engine (`hedgebot`) and two configs (`config/buy.yaml`,
`config/sell.yaml`). One Python process can only drive one MT5 terminal, so
the two apps run as two processes, each with its **own MT5 installation folder**.

```
run_pair.py ── validates the pair ──┬── python -m hedgebot --config config/buy.yaml   → C:\MT5_Buy  → account #1
                                    └── python -m hedgebot --config config/sell.yaml  → C:\MT5_Sell → account #2
```

What each app does on a trading day:

1. Works out the next target time in the config's timezone (summer/winter time
   handled; the Windows timezone setting is never used, so a UTC VPS works too).
2. Measures the PC clock error against NTP and corrects for it.
3. Sleeps until `prearm_seconds` before the target, then connects to its
   terminal and runs the preflight checks: right account, Algo Trading on,
   demo account (unless allowed), symbol, market open, lot size, margin, TP
   distance, filling mode and a broker-side `order_check`.
4. Sleeps until 1 s before the target, spins until the exact moment, reads a
   fresh price and sends the market order. It logs the send time, the deviation
   from the target, the round-trip time, the spread and the slippage.
5. Corrects the TP from the real fill price so the money amount is exact, then
   watches the position until it closes (TP, SL, stop-out or manual) and writes
   a row to `data/journal.csv`.

---

## Contents

1. [Install Python](#1-install-python)
2. [Install Git](#2-install-git)
3. [Install MetaTrader 5 twice](#3-install-metatrader-5-twice)
4. [Log in once and enable Algo Trading](#4-log-in-once-and-enable-algo-trading)
5. [Get the project and run setup](#5-get-the-project-and-run-setup)
6. [Fill in .env and the two YAML files](#6-fill-in-env-and-the-two-yaml-files)
7. [Check each side](#7-check-each-side---check)
8. [Demo test with both apps](#8-demo-test-with-both-apps---test-in-60)
9. [The scheduled run](#9-the-scheduled-run)
10. [Start automatically with Task Scheduler](#10-start-automatically-with-task-scheduler)

Also: [command reference](#command-reference) · [journal and logs](#journal-and-logs) ·
[troubleshooting](#troubleshooting) · [another PC](#use-the-project-on-another-pc) ·
[risks](#risks) · [backup EA](#backup-option-the-mql5-ea) · [tests](#running-the-tests)

---

## 1. Install Python

1. Download Python **3.12** (3.11–3.14 also work) from
   <https://www.python.org/downloads/windows/> → "Windows installer (64-bit)".
2. In the installer, tick **"Add python.exe to PATH"**, then *Install Now*.
3. Check it in PowerShell: `py --version`

## 2. Install Git

Download from <https://git-scm.com/download/win> and install with the default
options, or in PowerShell: `winget install Git.Git`. Git is only needed to
download and update the project.

## 3. Install MetaTrader 5 twice

Each app needs its **own** MT5 folder. Two common ways:

- **Run the installer twice.** Download MT5 from your broker. In the installer
  click **Settings** and set the folder to `C:\MT5_Buy`. Run the installer again
  and choose `C:\MT5_Sell`.
- **Copy an existing installation.** Close MT5, then copy its folder (for example
  `C:\Program Files\<Broker> MetaTrader 5`) to `C:\MT5_Buy` and again to
  `C:\MT5_Sell`.

You now have `C:\MT5_Buy\terminal64.exe` and `C:\MT5_Sell\terminal64.exe`.
Any folder names work; they just have to be different and match `terminal_path`
in the configs.

## 4. Log in once and enable Algo Trading

Do this in **both** terminals (start `C:\MT5_Buy\terminal64.exe`, then
`C:\MT5_Sell\terminal64.exe`):

1. **File → Login to Trade Account**: log into the account for that side
   (BUY account in `MT5_Buy`, SELL account in `MT5_Sell`). Tick **Save password**.
   Write down the exact **server name** shown in the dialog.
2. Click the **Algo Trading** button in the toolbar so it turns **green**.
3. **Tools → Options → Expert Advisors**: tick **Allow algorithmic trading**, and
   make sure **"Disable automatic trading via external Python API"** is *not* ticked.
4. In **Market Watch** (Ctrl+M), right-click → *Show All*, find the symbol and note
   its **exact** name. Gold is `XAUUSD` at many brokers, but some use a suffix
   (`XAUUSD.m`, `XAUUSD.r`) or another name altogether (`GOLD`).

Start with **demo accounts**. The apps refuse real accounts unless you set
`allow_real_account: true`.

## 5. Get the project and run setup

In PowerShell:

```powershell
cd $HOME\Desktop
git clone https://github.com/Williampetersen/mt5-hedge-timer.git Trade2026
cd Trade2026
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates the virtual environment `.venv`, installs the pinned
requirements, and creates `config\buy.yaml`, `config\sell.yaml` and `.env` from
the example files. It never overwrites files that already exist, so it is safe
to run again.

## 6. Fill in .env and the two YAML files

**`.env`** (open with Notepad: `notepad .env`). This file stays on your PC
and is never committed.

```
BUY_MT5_PASSWORD=the-password-of-account-1
SELL_MT5_PASSWORD=the-password-of-account-2
TELEGRAM_BOT_TOKEN=          # optional
TELEGRAM_CHAT_ID=            # optional
```

**`config\buy.yaml`** and **`config\sell.yaml`**: change at least these:

| Key | BUY file | SELL file |
|---|---|---|
| `name` | `buy-account` | `sell-account` (must differ) |
| `direction` | `buy` | `sell` |
| `account.login` | account #1 number | account #2 number (must differ) |
| `account.server` | server name from the login dialog | server name |
| `account.password_env` | `BUY_MT5_PASSWORD` | `SELL_MT5_PASSWORD` |
| `account.terminal_path` | `'C:\MT5_Buy\terminal64.exe'` | `'C:\MT5_Sell\terminal64.exe'` (must differ) |
| `trade.symbol` | exact Market Watch name | **same** |
| `trade.lots`, `tp_money`/`tp_points`, `sl_money`/`sl_points` | your values | **same** |
| `schedule.time`, `timezone`, `weekdays`, `dates` | your values | **same** |

Rules worth knowing:

- Keep the quotes around times: `time: "14:29:45"`. Without quotes YAML reads it as
  a number. The app tells you if this happens.
- Write Windows paths in **single** quotes: `'C:\MT5_Buy\terminal64.exe'`.
- `tp_money: 150.0` is 150 in the **account currency**, measured from the fill price.
  For XAUUSD with 0.06 lot that is a 25.00 price distance. Use `tp_points` instead
  to set a fixed distance in points. Use one of them only; `null` means no TP.
- `sl_money` / `sl_points`: `null` (the default) means **no stop loss**.
- `dates: ["2026-12-05", "2026-12-10"]`: when not empty, the apps trade **only** on
  these dates (weekdays are then ignored).
- Unknown keys are rejected, so a typo such as `sl_mony` stops the app instead of
  being silently ignored.

`run_pair.py` refuses to start unless the two files form a correct pair: one
`buy` and one `sell`, identical symbol, lots, TP, SL, time, timezone, weekdays
and dates, and **different** logins and terminal folders.

## 7. Check each side (`--check`)

```powershell
.venv\Scripts\python.exe -m hedgebot --config config\buy.yaml --check
.venv\Scripts\python.exe -m hedgebot --config config\sell.yaml --check
# or both at once:
.venv\Scripts\python.exe run_pair.py --check
```

`--check` connects, runs every preflight check, prints the TP distance, margin
and next target time, and then exits. **Nothing is sent.** Look for
`CHECK PASSED`. You can run it on a weekend: a closed market is reported as a
warning in check mode, whereas a real run would stop.

Example:

```
Account 1234567 (demo, hedging) on Broker-Demo: balance 10000.00 USD, ...
Symbol XAUUSD: digits 2, tick 0.01 = 1.00 USD/lot, filling FOK, stops level 0 points
TP 150.00 USD = 25.00 price distance (2500 points); at the current price TP would be 2675.30
Margin required 318.04 USD, free margin 10000.00 USD
CHECK PASSED for buy-account (demo account 1234567). Nothing was sent.
```

## 8. Demo test with both apps (`--test-in 60`)

Only on **demo** accounts, while the market is open:

```powershell
# 1) rehearsal: everything, but order_check() instead of order_send()
.venv\Scripts\python.exe run_pair.py --test-in 60 --dry-run

# 2) real demo orders, 60 seconds from now
.venv\Scripts\python.exe run_pair.py --test-in 60
```

`--test-in N` ignores the schedule. `run_pair.py` computes **one** shared
target timestamp and passes it to both apps, so they fire on exactly the same
instant. Output from each app is prefixed with `[BUY]` / `[SELL]`, and when
both have sent you get a summary:

```
[BUY]  ... TIMING: target 2026-11-16 11:02:00.000 CET | sent 2026-11-16 11:02:00.000 CET | deviation +0.2 ms | round-trip 41.3 ms
[SELL] ... TIMING: target 2026-11-16 11:02:00.000 CET | sent 2026-11-16 11:02:00.000 CET | deviation +0.3 ms | round-trip 44.8 ms
[PAIR] BUY opened, SELL opened | send-time difference 3.78 ms (BUY sent later) | of which 3.82 ms is the
       difference between the two apps' NTP corrections | deviation from target: BUY +0.06 ms, SELL +0.09 ms | ...
```

Each app measures the clock against NTP on its own, and two measurements
usually disagree by a few milliseconds. The `[PAIR]` line shows how much of the
send-time difference comes from that. In the example, the scheduling itself
differed by only 0.04 ms. Either way both orders go out in the same second.

Each app opens at most one position per day (see [below](#one-position-per-day)),
so close the demo positions in MT5 if you want to test again on the same day.

## 9. The scheduled run

**Double-click `START.bat`** in the project folder. It starts both apps (BUY and
SELL). They wait until the scheduled time in your configs, open at that exact
moment, and then watch the positions. Keep the window open. While it runs,
Windows will not go to sleep. The screen may still turn off, which is fine.

The same from PowerShell:

```powershell
.\START.bat                       # validate the pair, then run today's (or the next) scheduled trade
.\START.bat --loop                # keep running and trade on every scheduled day
```

Or start one side on its own: `scripts\start_buy.bat`, `scripts\start_sell.bat`
(extra arguments such as `--check` are passed on).

Recommendations:

- Keep both MT5 terminals **open and logged in**. The app can start a terminal
  itself, but that takes time and may eat into `prearm_seconds`.
- Start the apps at least a few minutes before the target time.
- The PC must **not sleep**. If it wakes up more than `max_late_ms` after the target,
  the apps deliberately do **not** open.

## 10. Start automatically with Task Scheduler

```powershell
# Recommended: start the pair with --loop every time you log in
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1

# Or: start once per weekday at 14:20 in the PC's own clock (no --loop)
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Mode Weekly -At 14:20

# Rehearsal task (order_check only)
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -DryRun

# Remove the task
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
```

The task runs only while your Windows user is **logged on**, because MT5 needs
the desktop session. Lock the screen (Win+L) rather than signing out. In
Weekly mode, `-At` is in the **PC's own timezone**. On a UTC VPS, 14:20
Copenhagen time is 13:20 UTC in winter and 12:20 UTC in summer, so `--loop`
mode is simpler there. Open `taskschd.msc` to see or edit the task.

---

## Command reference

```
python -m hedgebot --config config/buy.yaml              # normal scheduled run (one day)
python -m hedgebot --config config/buy.yaml --dry-run    # everything, but order_check() instead of order_send()
python -m hedgebot --config config/buy.yaml --test-in 60 # target = now + 60 s (demo testing)
python -m hedgebot --config config/buy.yaml --check      # connect + preflight + print TP/margin, then exit
python -m hedgebot --config config/buy.yaml --loop       # keep running, trade every scheduled day

python run_pair.py --buy config/buy.yaml --sell config/sell.yaml [--dry-run] [--test-in N | --check | --loop]
```

Other options: `--env PATH` (another .env file), `--log-dir`, `--journal`, `-v` (debug logging).

Exit codes: `0` success, `1` error (connection, preflight or order failed),
`2` config error or invalid pair, `3` skipped (too late, spread too wide, or
already traded today), `130` stopped with Ctrl+C.

### One position per day

Before arming, each app refuses to open when:

- a position with the same **magic number and symbol** is still open on its account
  (from any day), or
- its account history shows that it already opened one **today** (schedule timezone), or
- `data/journal.csv` already has an `opened` row for this app today.

A blocked side is logged, sent to Telegram, and written to the journal as
`skipped_exists`. Note that this check runs **per account**. If yesterday's BUY is
still open but yesterday's SELL hit its TP, today only the SELL side opens.

### Timing rules

- Retries happen **only** on requote / price changed / price off (retcodes 10004,
  10020, 10021), at most 3 times, and never once `max_late_ms` has passed.
  Every other error stops immediately. If the terminal gives no reply, the app
  checks whether a position appeared before reporting failure, and never re-sends.
- `max_late_ms`: if the order would go out later than this after the target
  (PC asleep, very slow connect), the app does **not** open.
- `max_spread_points`: if set and the spread at the target is wider, the app skips.
- Telegram messages are sent from a background thread with a 5 s timeout and
  are held back around the trigger moment, so they never delay an order.

## Journal and logs

- `logs/<name>_<YYYY-MM-DD>.log`: everything, with millisecond timestamps in the
  schedule timezone.
- `data/journal.csv`: one row per run with date, app name, login, direction,
  symbol, lots, target time, send time, deviation ms, latency ms, spread, requested
  price, fill price, slippage, TP price, close time, close reason (TP / SL /
  stop-out / manual), profit, max adverse, max favourable, plus SL, close price,
  swap, commission, duration, ticket, attempts, status and note.
  `status` is one of `opened`, `dry_run`, `failed`, `error`, `skipped_late`,
  `skipped_spread`, `skipped_exists`.
- If `journal.csv` is open in Excel (locked), the row is saved to
  `data/journal_unsaved_*.csv` instead of being lost.

Telegram (optional): create a bot with **@BotFather**, put its token in
`TELEGRAM_BOT_TOKEN`. Then send the bot a message and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id for
`TELEGRAM_CHAT_ID`. You get messages on armed / opened / closed / skipped / error.

## Troubleshooting

| Message / symptom | Fix |
|---|---|
| `Algo Trading is OFF in this terminal` | Click **Algo Trading** in that terminal's toolbar so it is green. |
| `Python trading is blocked` | Tools → Options → Expert Advisors → untick *Disable automatic trading via external Python API*. |
| `logged into account X, but the config expects Y` | The terminal is logged into another account. Log into the right one (File → Login), or fix `account.login`. The app never trades on the wrong account. |
| `Could not connect ... Authorization failed` | Wrong password in `.env` (use the master password, not the investor one), or wrong `account.server`: copy it exactly from the login dialog. |
| `Symbol 'XAUUSD' does not exist ... Similar symbols: XAUUSD.m, GOLD` | Use the broker's exact name in **both** configs, e.g. `symbol: XAUUSD.m` or `symbol: GOLD`. |
| `market is closed` / `price is N min old` | Run during market hours (gold: Mon–Fri, with a daily break around midnight server time). `--check` only warns about this. |
| `REAL account and allow_real_account is false` | Intended safety stop. Use a demo account, or set `allow_real_account: true` deliberately. |
| `PC clock is N ms behind/ahead` | Scheduling is corrected via NTP anyway, but fix the clock: Settings → Time & language → Date & time → **Sync now**, or in an admin PowerShell `w32tm /resync`. Make sure "Set time automatically" is on. |
| `woke up N s after the target ... not opening` | The PC slept or was busy. Settings → System → Power → Screen and sleep → *When plugged in, put my device to sleep after*: **Never**. |
| `Prearm finished only N s before the target` | Connecting took long (terminal was closed?). Keep the terminals open, or raise `prearm_seconds`. |
| `not a known IANA timezone` | Use names like `Europe/Copenhagen`. If the name is right, rerun `scripts\setup.ps1` (installs `tzdata`). |
| `put the time in quotes` | Write `time: "14:29:45"` with quotes. |
| `found unknown escape character` | Use single quotes for Windows paths: `'C:\MT5_Buy\terminal64.exe'`. |
| `The two configs are not a valid pair` | The message lists each difference. Make those fields identical (or different, for login / terminal_path / name). |
| `setup.ps1 cannot be loaded because running scripts is disabled` | Run it as shown: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`. |
| One side `skipped_exists` | A position from an earlier day is still open, or the app already traded today. See [one position per day](#one-position-per-day). |

## Use the project on another PC

The repository is private, so log in to GitHub first:

```powershell
winget install GitHub.cli
gh auth login
gh repo clone Williampetersen/mt5-hedge-timer $HOME\Desktop\Trade2026
cd $HOME\Desktop\Trade2026
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Then repeat steps 3, 4, 6 and 7 on that PC: install MT5 twice, log in, fill in
`.env` and both YAML files, run `--check`. Your `.env` and YAML configs are not
in the repository: copy them over by hand (e.g. on a USB stick), or fill them
in again. To get updates later: `git pull`, then run `scripts\setup.ps1` again.

## Risks

- **No stop loss (the default).** If the price runs far in one direction, the
  losing side keeps losing. On a small account it can be **stopped out** by
  the broker before its TP or the other side's TP is reached, and then you are
  left with a single unhedged position.
- **Both sides can lose.** In a whipsaw, the price can move toward one TP,
  reverse, and travel far the other way. Spread, commission and swap are paid
  twice, and slippage on either fill shifts both TPs.
- **The two sides are independent.** If one app fails (connection, margin,
  requote limit, PC asleep) and the other opens, you hold an unhedged position.
  Watch the `[PAIR]` summary and your Telegram messages.
- Broker execution, gaps and outages are outside this program's control. Test on
  demo accounts first, and only trade money you can afford to lose.

## Backup option: the MQL5 EA

`mql5/TimedEntry.mq5` is a stand-alone Expert Advisor that does the same job
inside MT5 (one copy per terminal; Direction = Buy on one, Sell on the other).
To use it: in each terminal, File → Open Data Folder → `MQL5\Experts`, copy the
file there, compile it in MetaEditor (F7), and drag it onto a chart. It uses
the PC clock by default and has no NTP correction, pair validation or journal.

## Running the tests

```powershell
.venv\Scripts\python.exe -m pytest
```

The tests mock MT5 completely (they run without MetaTrader installed). They
cover target times (including the DST change days in Europe/Copenhagen and a
UTC-based PC), TP/SL from money for buy and sell, tick rounding, lot steps,
filling mode, pair validation, the `max_late_ms` rule, the one-position-per-day
guard, the retry rules, and full simulated runs.

## Project layout

```
START.bat            double-click: runs BUY + SELL at the scheduled time
hedgebot/            the engine (one codebase for both apps)
  __main__.py        CLI + the daily run (wait, prearm, fire, correct TP, monitor, journal)
  config.py          YAML + .env loading and validation
  clock.py           timezone targets, NTP offset, precise waiting
  mt5_client.py      connect, preflight, send, TP correction, monitor
  trade_math.py      TP/SL from money, rounding, lots, filling, retry + guard rules
  journal.py         CSV journal
  notify.py          optional Telegram
  pair.py            buy/sell pair validation
run_pair.py          validates both configs, runs both apps, prints the send-time difference
config/              *.example.yaml (your buy.yaml / sell.yaml are gitignored)
scripts/             setup.ps1, start_*.bat, register_tasks.ps1
mql5/                TimedEntry.mq5 backup EA
tests/               pytest suite (MT5 mocked)
```

`PROMPT.md` is the original specification.
