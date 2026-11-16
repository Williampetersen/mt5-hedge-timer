"""Find the MetaTrader 5 copies that are already installed on this PC."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

TERMINAL_EXE = "terminal64.exe"


@dataclass(frozen=True)
class Terminal:
    path: str  # full path of terminal64.exe
    name: str  # the folder name, e.g. "IC Markets MetaTrader 5"

    @property
    def label(self) -> str:
        return f"{self.name}   ({ntpath.dirname(self.path)})"


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _read_origin(origin_file: Path) -> Optional[str]:
    """MT5 writes its install folder into <data folder>\\origin.txt (UTF-16)."""
    try:
        raw = origin_file.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-16", "utf-8-sig"):
        try:
            text = raw.decode(encoding).strip().strip("\x00")
        except UnicodeError:
            continue
        if text and ("\\" in text or "/" in text):
            return text
    return None


def _candidate_folders(extra: Iterable[Path] = ()) -> list[Path]:
    out: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata) / "MetaQuotes" / "Terminal"
        if root.is_dir():
            for data_folder in sorted(root.iterdir()):
                origin = _read_origin(data_folder / "origin.txt")
                if origin:
                    out.append(Path(origin))
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base and Path(base).is_dir():
            try:
                out += [p for p in sorted(Path(base).iterdir()) if p.is_dir()]
            except OSError:
                pass
    out += list(extra)
    return out


def find_terminals(extra_folders: Iterable[Path] = ()) -> list[Terminal]:
    """Every folder that holds a terminal64.exe, without duplicates, sorted by name."""
    seen: set[str] = set()
    found: list[Terminal] = []
    for folder in _candidate_folders(extra_folders):
        exe = folder / TERMINAL_EXE
        try:
            if not exe.is_file():
                continue
        except OSError:
            continue
        key = _norm(str(exe))
        if key in seen:
            continue
        seen.add(key)
        found.append(Terminal(path=str(exe), name=folder.name))
    return sorted(found, key=lambda t: (t.name.lower(), t.path.lower()))
