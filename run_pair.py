"""Validate a BUY + SELL config pair, then run both apps as two subprocesses.

  python run_pair.py                                   # config/buy.yaml + config/sell.yaml, scheduled
  python run_pair.py --buy config/buy.yaml --sell config/sell.yaml --test-in 60
  python run_pair.py --dry-run | --check | --loop

With --test-in N one shared target timestamp is computed here and passed to
both children, so they fire on exactly the same instant. Each child's output
is prefixed with [BUY] / [SELL]; at the end the difference between the two
send times is printed in milliseconds.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hedgebot.__main__ import CHILD_ENV, RESULT_PREFIX  # noqa: E402
from hedgebot.config import ConfigError, load_config  # noqa: E402
from hedgebot.pair import validate_pair  # noqa: E402

EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the BUY and SELL apps together.")
    p.add_argument("--buy", default="config/buy.yaml", help="BUY config (default: config/buy.yaml)")
    p.add_argument("--sell", default="config/sell.yaml", help="SELL config (default: config/sell.yaml)")
    p.add_argument("--env", help="path to the .env file (default: .env in the project folder)")
    p.add_argument("--dry-run", action="store_true", help="order_check() instead of order_send() on both sides")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="connect + preflight both sides, then exit")
    mode.add_argument("--loop", action="store_true", help="keep running and trade every scheduled day")
    mode.add_argument("--test-in", type=float, metavar="SECONDS",
                      help="one shared target = now + SECONDS for both sides (DEMO testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def child_args(args: argparse.Namespace, target: Optional[datetime]) -> list[str]:
    extra: list[str] = []
    if args.env:
        extra += ["--env", args.env]
    if args.dry_run:
        extra.append("--dry-run")
    if args.check:
        extra.append("--check")
    if args.loop:
        extra.append("--loop")
    if target is not None:
        extra += ["--target-at", target.astimezone(timezone.utc).isoformat()]
    if args.verbose:
        extra.append("-v")
    return extra


def parse_result_line(line: str) -> Optional[dict]:
    if not line.startswith(RESULT_PREFIX):
        return None
    try:
        return json.loads(line[len(RESULT_PREFIX):])
    except json.JSONDecodeError:
        return None


def describe_pair(buy: dict, sell: dict) -> str:
    """Summary of one shared target: send-time difference in ms."""
    parts = [f"BUY {buy.get('status')}, SELL {sell.get('status')}"]
    if buy.get("send_pc_ts") is not None and sell.get("send_pc_ts") is not None:
        diff = (sell["send_pc_ts"] - buy["send_pc_ts"]) * 1000.0
        parts.append(f"send-time difference {abs(diff):.2f} ms "
                     f"({'SELL' if diff > 0 else 'BUY'} sent later)")
        if buy.get("clock_offset_ms") is not None and sell.get("clock_offset_ms") is not None:
            # Each app applies its own NTP measurement; their small disagreement shows up here.
            parts.append(f"of which {abs(sell['clock_offset_ms'] - buy['clock_offset_ms']):.2f} ms is the "
                         "difference between the two apps' NTP corrections")
        parts.append(f"deviation from target: BUY {buy['deviation_ms']:+.2f} ms, "
                     f"SELL {sell['deviation_ms']:+.2f} ms")
        if buy.get("latency_ms") is not None and sell.get("latency_ms") is not None:
            parts.append(f"round-trip: BUY {buy['latency_ms']:.1f} ms, SELL {sell['latency_ms']:.1f} ms")
    else:
        parts.append("no send-time difference (at least one side did not send)")
    return " | ".join(parts)


def combine_exit_codes(codes: list[int]) -> int:
    if all(c == 0 for c in codes):
        return 0
    if any(c == 1 for c in codes):
        return 1
    return max(codes)


class PairRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.results: dict[float, dict[str, dict]] = {}

    def out(self, text: str) -> None:
        with self.lock:
            print(text, flush=True)

    def pump(self, label: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            result = parse_result_line(line)
            if result is None:
                self.out(f"[{label}] {line}")
                continue
            key = round(float(result.get("target_ts") or 0.0), 3)
            with self.lock:
                bucket = self.results.setdefault(key, {})
                bucket[label] = result
                ready = "BUY" in bucket and "SELL" in bucket
            if ready:
                self.out(f"[PAIR] {describe_pair(bucket['BUY'], bucket['SELL'])}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test_in is not None and args.test_in <= 0:
        print("--test-in must be a positive number of seconds", file=sys.stderr)
        return EXIT_CONFIG
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if args.env:
        args.env = str(Path(args.env).resolve())  # children run in the project folder
    buy_path, sell_path = Path(args.buy), Path(args.sell)
    if not buy_path.is_absolute():
        buy_path = ROOT / buy_path
    if not sell_path.is_absolute():
        sell_path = ROOT / sell_path
    loaded, failed = {}, False
    for label, path in (("buy", buy_path), ("sell", sell_path)):
        try:
            loaded[label] = load_config(path, env_file=args.env)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            failed = True
    if failed:
        return EXIT_CONFIG
    buy, sell = loaded["buy"], loaded["sell"]
    errors, warnings = validate_pair(buy, sell)
    if errors:
        print("Refusing to start: the two configs are not a valid pair:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return EXIT_CONFIG
    for w in warnings:
        print(f"[PAIR] warning: {w}")

    target = None
    if args.test_in is not None:
        target = datetime.now(timezone.utc) + timedelta(seconds=args.test_in)
        print(f"[PAIR] TEST: shared target {target.astimezone(buy.schedule.tz):%Y-%m-%d %H:%M:%S.%f} "
              f"({buy.schedule.timezone}) = now + {args.test_in:g} s")
    print(f"[PAIR] BUY  {buy.name}: login {buy.account.login}, {buy.account.terminal_path}")
    print(f"[PAIR] SELL {sell.name}: login {sell.account.login}, {sell.account.terminal_path}")
    print(f"[PAIR] {buy.trade.lots:g} {buy.trade.symbol}, TP {buy.trade.describe_tp()}, "
          f"SL {buy.trade.describe_sl()}, {buy.schedule.time.isoformat()} {buy.schedule.timezone} "
          f"on {buy.schedule.describe_days()}" + (" [DRY-RUN]" if args.dry_run else ""), flush=True)

    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env[CHILD_ENV] = "1"
    extra = child_args(args, target)
    runner = PairRunner()
    procs: dict[str, subprocess.Popen] = {}
    for label, path in (("BUY", buy_path), ("SELL", sell_path)):
        cmd = [sys.executable, "-u", "-m", "hedgebot", "--config", str(path), *extra]
        procs[label] = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    threads = [threading.Thread(target=runner.pump, args=(label, p), daemon=True) for label, p in procs.items()]
    for t in threads:
        t.start()

    try:
        codes = {label: p.wait() for label, p in procs.items()}
    except KeyboardInterrupt:
        # Ctrl+C reaches both children too; give them time to log and shut down cleanly.
        print("[PAIR] Ctrl+C: waiting for both apps to stop...", flush=True)
        codes = {}
        for label, p in procs.items():
            try:
                codes[label] = p.wait(timeout=20)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                p.terminate()
                codes[label] = 130
    for t in threads:
        t.join(timeout=5)

    print(f"[PAIR] exit codes: BUY {codes.get('BUY')}, SELL {codes.get('SELL')}")
    if not runner.results and not args.check:
        print("[PAIR] no send results were reported")
    for key, bucket in runner.results.items():
        if not ("BUY" in bucket and "SELL" in bucket):
            missing = "SELL" if "BUY" in bucket else "BUY"
            print(f"[PAIR] WARNING: only one side reported for target {key}; {missing} did not report")
    return combine_exit_codes(list(codes.values()))


if __name__ == "__main__":
    sys.exit(main())
