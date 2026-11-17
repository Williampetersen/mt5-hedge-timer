"""`Aitrade.exe --uninstall`: what Windows runs when Aitrade is removed in "Installed apps"."""

from __future__ import annotations

import os

from PySide6.QtWidgets import QMessageBox

from .. import installer, paths
from .app import create_app


def run_uninstall(silent: bool = False) -> int:
    target = paths.app_dir()
    if not paths.is_installed_program() or installer.read_manifest(target) is None:
        if not silent:
            create_app()
            QMessageBox.information(None, "Aitrade", "This copy of Aitrade was not installed with the setup "
                                                     "program, so there is nothing to remove.")
        return 1
    if not silent:
        create_app()
        answer = QMessageBox.question(
            None, "Remove Aitrade",
            f"Remove Aitrade from this PC?\n\nThe program in {target} and its shortcuts are removed.\n\n"
            f"Your settings, logs and the MetaTrader 5 copies are kept (in {paths.data_dir()}).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return 1
    if installer.running_processes(target):
        if not silent:
            QMessageBox.warning(None, "Remove Aitrade", "Aitrade is still open. Close it, then try again.")
        return 1
    try:
        installer.uninstall(target, wait_for_pid=os.getpid())
    except installer.InstallError as exc:
        if not silent:
            QMessageBox.warning(None, "Remove Aitrade", str(exc))
        return 1
    if not silent:
        QMessageBox.information(None, "Remove Aitrade", "Aitrade was removed.")
    return 0
