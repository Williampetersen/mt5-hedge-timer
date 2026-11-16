"""Start, find and close a MetaTrader 5 terminal program (Windows).

The official Python package can start the terminal itself, but then Algo
Trading is off, and a terminal that has never logged in shows its "Open an
Account" window, which blocks the Python connection. So the engine starts the
terminal first, with a small start-up file (the documented /config key):
Algo Trading on and, only for a terminal that has never logged in, the account
to log into. That file is written to the user's private temp folder and
deleted as soon as the terminal has read it.

Set HEDGEBOT_LAUNCH=0 to switch all of this off (the Python package then
starts the terminal on its own, as before).
"""

from __future__ import annotations

import logging
import ntpath
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("hedgebot.terminal")

LAUNCH_ENV = "HEDGEBOT_LAUNCH"
MAIN_WINDOW_CLASS = "MetaQuotes::MetaTrader"  # the terminal's main window class starts with this
DIALOG_CLASS = "#32770"
WM_CLOSE = 0x0010
MIN_CONFIG_LIFETIME_S = 5.0  # keep the start-up file at least this long, so the terminal has read it

# What ensure_started() did.
SKIPPED, RUNNING, STARTED, FIRST_LOGIN, FAILED = "skipped", "running", "started", "first_login", "failed"
FIRST_LOGIN_SETTLE_S = 30.0  # after a first start: how long to watch for the saved account or a blocking window
NORMAL_SETTLE_S = 3.0


@dataclass(frozen=True)
class Launch:
    status: str
    dialog: str = ""  # title of a window that blocked the terminal and was closed (e.g. "Open an Account")


def launch_enabled() -> bool:
    return sys.platform == "win32" and os.environ.get(LAUNCH_ENV, "1").strip().lower() not in ("0", "false", "no")


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


# --------------------------------------------------------------------------- the terminal's files


def data_folder(exe: str, portable: bool) -> Optional[Path]:
    """Where this terminal keeps its settings: its own folder in portable mode,
    otherwise the folder under %APPDATA%\\MetaQuotes\\Terminal that points back to it."""
    exe_dir = Path(exe).parent
    if portable:
        return exe_dir
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) / "MetaQuotes" / "Terminal" if appdata else None
    if root is None or not root.is_dir():
        return None
    for folder in root.iterdir():
        origin = folder / "origin.txt"
        try:
            raw = origin.read_bytes()
        except OSError:
            continue
        for encoding in ("utf-16", "utf-8-sig"):
            try:
                text = raw.decode(encoding).strip().strip("\x00")
            except UnicodeError:
                continue
            if text and _norm(text) == _norm(str(exe_dir)):
                return folder
    return None


def has_saved_account(exe: str, portable: bool) -> bool:
    """True once this terminal has logged into any account (it then starts without asking)."""
    folder = data_folder(exe, portable)
    return folder is not None and (folder / "config" / "accounts.dat").is_file()


def startup_config_text(*, login: Optional[int] = None, server: Optional[str] = None,
                        password: Optional[str] = None) -> str:
    """The start-up file: Algo Trading on, plus the account for a first login.

    Account=0 and Profile=0 keep Algo Trading on when the account or the chart profile changes.
    """
    lines: list[str] = []
    if login and server:
        lines += ["[Common]", f"Login={int(login)}"]
        if password:
            lines.append(f"Password={password}")
        lines += [f"Server={server}", "KeepPrivate=1"]
    lines += ["[Experts]", "AllowLiveTrading=1", "Enabled=1", "Account=0", "Profile=0"]
    return "\r\n".join(lines) + "\r\n"


def write_startup_config(text: str, folder: Optional[Path] = None) -> Path:
    """Write the start-up file (UTF-16, like the terminal's own files) into the user's temp folder."""
    fd, name = tempfile.mkstemp(prefix="mt5-start-", suffix=".ini", dir=str(folder) if folder else None)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\xff\xfe" + text.encode("utf-16-le"))
    return Path(name)


def _delete(path: Path) -> None:
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:  # overwrite before deleting: the file may hold a password
            fh.write(b"\x00" * size)
        path.unlink()
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            log.warning("Could not delete the start-up file %s: %s", path, exc)


# --------------------------------------------------------------------------- windows and processes


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    cls: str
    title: str
    visible: bool
    enabled: bool
    owned: bool  # has an owner window, i.e. it is a dialog of another window

    @property
    def is_main(self) -> bool:
        return self.visible and not self.owned and self.cls.startswith(MAIN_WINDOW_CLASS)

    @property
    def is_dialog(self) -> bool:
        return self.visible and self.owned and self.cls == DIALOG_CLASS


def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, user32, kernel32


def _image_path(kernel32, ctypes, wintypes, pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def process_ids(exe: str) -> list[int]:
    """Ids of the running processes of exactly this terminal64.exe."""
    if sys.platform != "win32":
        return []
    ctypes, wintypes, _user32, kernel32 = _api()
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    count = 4096
    while True:
        arr = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(arr):
            break
        count *= 2
    want = _norm(exe)
    found = []
    for pid in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if pid and _norm(_image_path(kernel32, ctypes, wintypes, pid)) == want:
            found.append(int(pid))
    return found


def is_running(exe: str) -> bool:
    return bool(process_ids(exe))


def windows(exe: str) -> list[Window]:
    """The top-level windows of this terminal (its main window and any dialogs)."""
    if sys.platform != "win32":
        return []
    ctypes, wintypes, user32, kernel32 = _api()
    pids = set(process_ids(exe))
    out: list[Window] = []
    if not pids:
        return out
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(200)
            user32.GetClassNameW(hwnd, cls, 200)
            title = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(hwnd, title, 300)
            out.append(Window(int(hwnd), int(pid.value), cls.value, title.value, bool(user32.IsWindowVisible(hwnd)),
                              bool(user32.IsWindowEnabled(hwnd)), bool(user32.GetWindow(hwnd, 4))))
        return True

    callback = proto(visit)
    user32.EnumWindows(callback, 0)
    return out


def blocking_dialog(exe: str) -> Optional[str]:
    """Title of a dialog that blocks the terminal's main window (e.g. "Open an Account"), else None."""
    wins = windows(exe)
    if any(w.is_main and not w.enabled for w in wins):
        for w in wins:
            if w.is_dialog:
                return w.title or "a dialog"
    return None


def wait_main_window(exe: str, timeout_s: float, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if any(w.is_main for w in windows(exe)):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)


def close_dialogs(exe: str) -> None:
    """Close this terminal's dialogs the way their X / Cancel does (nothing is accepted)."""
    if sys.platform != "win32":
        return
    _ctypes, _wintypes, user32, _kernel32 = _api()
    for w in windows(exe):
        if w.is_dialog:
            user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)


def close(exe: str, timeout_s: float = 90.0, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Ask this terminal to close, the same way a click on its X does. True when it is gone."""
    if sys.platform != "win32":
        return True
    _ctypes, _wintypes, user32, _kernel32 = _api()
    for w in windows(exe):
        if w.is_dialog:
            user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    sleep(0.5)
    for w in windows(exe):
        if w.is_main:
            user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    deadline = time.monotonic() + timeout_s
    while is_running(exe):
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)
    return True


def start(exe: str, *, portable: bool, config: Optional[Path] = None) -> subprocess.Popen:
    args = [str(exe)]
    if portable:
        args.append("/portable")
    if config is not None:
        args.append(f"/config:{config}")
    return subprocess.Popen(args, cwd=str(Path(exe).parent), close_fds=True)


# --------------------------------------------------------------------------- the one call the engine makes


def ensure_started(
    exe: str,
    *,
    portable: bool,
    login: int,
    server: str,
    password: str,
    window_timeout_s: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Launch:
    """Make sure the terminal runs with Algo Trading on. Returns what was done.

    A terminal that is already running is left alone. A portable copy that has
    never logged in gets the account in its start-up file (the only time a
    password is written to disk; the file is deleted right after). A window
    that blocks the terminal at start (a new terminal's "Open an Account") is
    closed, so the Python connection can get through.
    """
    if not launch_enabled():
        return Launch(SKIPPED)
    if is_running(exe):
        title = blocking_dialog(exe)
        if title:  # opened by hand and stuck behind a window: clear the way for the connection
            log.warning("MetaTrader 5 shows the window '%s'; closing that window", title)
            close_dialogs(exe)
            sleep(1.0)
        return Launch(RUNNING, title or "")
    first = bool(portable and password and not has_saved_account(exe, portable))
    text = startup_config_text(login=login, server=server, password=password) if first else startup_config_text()
    try:
        config = write_startup_config(text)
    except OSError as exc:
        log.warning("Could not write the MetaTrader 5 start-up file (%s); starting it without", exc)
        return Launch(FAILED)
    started_at = time.monotonic()
    dialog = ""
    try:
        start(exe, portable=portable, config=config)
        if first:
            log.info("Starting MetaTrader 5 for its first login as %s on %s (this can take a few minutes)",
                     login, server)
        else:
            log.info("Starting MetaTrader 5 with Algo Trading on")
        if not wait_main_window(exe, window_timeout_s, sleep=sleep):
            log.warning("MetaTrader 5 did not show its window within %.0f s", window_timeout_s)
        # Give it a moment: the account gets saved (first login worked) or a window blocks it.
        settle_until = time.monotonic() + (FIRST_LOGIN_SETTLE_S if first else NORMAL_SETTLE_S)
        while time.monotonic() < settle_until:
            title = blocking_dialog(exe)
            if title:
                dialog = title
                log.warning("MetaTrader 5 shows the window '%s'; closing that window", title)
                close_dialogs(exe)
            elif first and has_saved_account(exe, portable):
                break
            sleep(1.0)
        remaining = MIN_CONFIG_LIFETIME_S - (time.monotonic() - started_at)
        if remaining > 0:
            sleep(remaining)
    except OSError as exc:
        log.warning("Could not start MetaTrader 5 at %s (%s); letting the Python package start it", exe, exc)
        return Launch(FAILED)
    finally:
        _delete(config)
    return Launch(FIRST_LOGIN if first else STARTED, dialog)
