"""Steps shared by the wizard's check and the run screen before any engine process starts."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from .. import paths
from ..engine_config import check_engine_configs, write_engine_configs
from ..settings import SIDE_TITLES, SIDES, Settings
from ..vault import Vault, VaultError


class LaunchError(Exception):
    """Something the user must fix before starting. `lines` are ready to show."""

    def __init__(self, lines: list[str]):
        self.lines = list(lines)
        super().__init__("; ".join(self.lines))


def collect_passwords(settings: Settings, vault: Vault, typed: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The password for each side: the one just typed, else the one saved in Windows."""
    out: dict[str, str] = {}
    missing: list[str] = []
    for side in SIDES:
        password = (typed or {}).get(side, "")
        if not password:
            try:
                password = vault.get(side, settings.account(side).login)
            except VaultError as exc:
                raise LaunchError([str(exc)]) from exc
        if not password:
            missing.append(f"The password of the {SIDE_TITLES[side]} account is not saved. "
                           "Open the account settings and type it.")
        out[side] = password
    if missing:
        raise LaunchError(missing)
    return out


def prepare(settings: Settings, passwords: Mapping[str, str]) -> dict[str, Path]:
    """Validate with the engine's own rules, then write the two engine files."""
    errors, _warnings, _configs = check_engine_configs(settings, passwords)
    if errors:
        raise LaunchError(errors)
    return write_engine_configs(settings, paths.config_dir())
