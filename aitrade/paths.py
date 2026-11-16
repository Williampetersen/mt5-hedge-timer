"""Where the app keeps its files on this PC.

Everything lives in one folder per Windows user (%LOCALAPPDATA%\\Aitrade), so
the program folder itself never changes. Set AITRADE_HOME to use another folder
(the tests do this).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "AITRADE_HOME"


def data_dir() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Aitrade"
    base.mkdir(parents=True, exist_ok=True)
    return base


def settings_file() -> Path:
    return data_dir() / "settings.json"


def config_dir() -> Path:
    """The two engine files (buy.yaml, sell.yaml) written before every run. No passwords inside."""
    path = data_dir() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def journal_file() -> Path:
    return data_dir() / "journal.csv"


def project_root() -> Path:
    """Folder that holds the `aitrade` and `hedgebot` packages (the working folder for workers)."""
    return Path(__file__).resolve().parent.parent
