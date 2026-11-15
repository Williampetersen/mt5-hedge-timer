"""CSV trade journal: one row per run in data/journal.csv.

Both apps may append to the same file, so writes go through a small lock
file. If the CSV is locked (e.g. open in Excel) the row goes to a separate
"journal_unsaved_*.csv" file instead of being lost.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

log = logging.getLogger("hedgebot.journal")

COLUMNS = (
    # the columns from the spec, in order
    "date", "app_name", "login", "direction", "symbol", "lots",
    "target_time", "send_time", "deviation_ms", "latency_ms", "spread_points",
    "requested_price", "fill_price", "slippage_points", "tp_price",
    "close_time", "close_reason", "profit", "max_adverse", "max_favourable",
    # extra detail
    "sl_price", "close_price", "swap", "commission", "duration_s",
    "position_ticket", "attempts", "status", "note",
)


@contextmanager
def _file_lock(lock_path: Path, timeout_s: float = 10.0, stale_after_s: float = 60.0) -> Iterator[None]:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_after_s:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"journal lock {lock_path} is held by another process")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")


class Journal:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def append(self, row: Mapping[str, Any]) -> Path:
        """Append one row; returns the file actually written."""
        return self._store(row, upsert=False)

    def upsert(self, row: Mapping[str, Any]) -> Path:
        """Replace this app's row for the same position_ticket, or append a new row.

        Used to write a trade as soon as it is filled and fill in the close
        details later, so a killed process never loses the record.
        """
        return self._store(row, upsert=True)

    def _store(self, row: Mapping[str, Any], *, upsert: bool) -> Path:
        clean = {c: _cell(row.get(c)) for c in COLUMNS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        for _ in range(5):
            try:
                with _file_lock(lock):
                    if upsert and clean["position_ticket"] and self._replace(clean):
                        return self.path
                    self._write(self.path, clean)
                return self.path
            except (PermissionError, TimeoutError) as exc:
                log.warning("Journal %s not writable (%s), retrying", self.path, exc)
                time.sleep(1.0)
        fallback = self.path.with_name(
            f"{self.path.stem}_unsaved_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.csv"
        )
        self._write(fallback, clean)
        log.error("Could not write %s (open in Excel?). Row saved to %s instead.", self.path, fallback)
        return fallback

    def _replace(self, row: dict[str, str]) -> bool:
        rows = self.rows()
        for i, r in enumerate(rows):
            if r.get("position_ticket") == row["position_ticket"] and r.get("app_name") == row["app_name"]:
                rows[i] = row
                tmp = self.path.with_name(self.path.name + ".tmp")
                with tmp.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)
                os.replace(tmp, self.path)
                return True
        return False

    @staticmethod
    def _write(path: Path, row: Mapping[str, str]) -> None:
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                writer.writeheader()
            writer.writerow(row)

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def opened_on(self, app_name: str, login: int, day: date) -> Optional[str]:
        """Reason string if this app already logged an opened trade on `day`."""
        try:
            rows = self.rows()
        except (OSError, csv.Error) as exc:
            log.warning("Could not read journal %s: %s", self.path, exc)
            return None
        for r in rows:
            if (r.get("app_name") == app_name and r.get("login") == str(login)
                    and r.get("date") == day.isoformat() and r.get("status") == "opened"):
                return f"the journal already has an opened trade for {app_name} on {day.isoformat()}"
        return None
