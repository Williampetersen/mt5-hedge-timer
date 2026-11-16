"""Give each account its own MetaTrader 5: a private copy in Aitrade's folder, run in portable mode.

MetaTrader 5 only runs one account per program folder, so two accounts need two
folders. Aitrade does not install MetaTrader twice. It takes one MetaTrader 5
that is installed on the PC (or installs the official one from MetaQuotes when
there is none) and copies its program files into `<mt5 folder>\\buy` and
`<mt5 folder>\\sell`. The copies run with /portable, so all their data stays
inside those two folders and they never touch the user's own MetaTrader.

No window code here. Long steps report through `progress(text, fraction)` and
stop when `cancelled()` returns True.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

from hedgebot import terminal

from . import paths
from .settings import SIDE_TITLES, SIDES, Settings
from .terminals import TERMINAL_EXE, Terminal, find_terminals

INSTALLER_URL = "https://download.terminal.free/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"
INSTALLER_SIGNER = "MetaQuotes"  # the installer must carry a valid signature from this company
INSTALLER_MIN_BYTES = 1_000_000
INSTALL_TIMEOUT_S = 30 * 60
GB = 1024 ** 3
MB = 1024 ** 2
SYSTEM_DRIVE_MIN_FREE = 10 * GB  # below this, the copies go next to the app instead of on the Windows drive
COPY_HEADROOM = 200 * MB
MT5_FOLDER_NAME = "AitradeMT5"
MT5_DIR_ENV = "AITRADE_MT5_DIR"  # set this to choose the folder for the copies yourself

Progress = Callable[[str, Optional[float]], None]
Cancelled = Callable[[], bool]

# Never copied from a source: its uninstaller, its logs, its accounts and settings, its own data.
_SKIP_TOP = {"logs", "tester", "mql5", "llm-agent", "uninstall.exe"}
_KEEP_CONFIG = {"servers.dat", "terminal.lic"}


class SetupError(Exception):
    """The setup could not finish. The message is written for the user."""


class SetupCancelled(Exception):
    """The user stopped the setup."""


def _noop(_text: str, _fraction: Optional[float] = None) -> None:
    return None


def _never() -> bool:
    return False


# --------------------------------------------------------------------------- where the copies live


def free_bytes(path: Path) -> int:
    """Free space on the drive of `path` (which does not have to exist yet)."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def _has_copy(base: Path) -> bool:
    return any((base / side / TERMINAL_EXE).is_file() for side in SIDES)


def default_mt5_dir() -> Path:
    """Folder for the two copies when the user has not chosen one.

    Normally inside Aitrade's own data folder. When the Windows drive is nearly
    full, the drive the app itself is on is used instead (X:\\AitradeMT5).
    """
    override = os.environ.get(MT5_DIR_ENV, "").strip()
    if override:
        return Path(override)
    home = paths.data_dir() / "mt5"
    app_anchor = Path(paths.project_root().anchor or home.anchor)
    beside_app = app_anchor / MT5_FOLDER_NAME
    for candidate in (home, beside_app):
        if _has_copy(candidate):  # copies made earlier win over any free-space rule
            return candidate
    same_drive = ntpath.normcase(str(app_anchor)) == ntpath.normcase(home.anchor)
    if free_bytes(home) >= SYSTEM_DRIVE_MIN_FREE or same_drive:
        return home
    if free_bytes(beside_app) > free_bytes(home):
        return beside_app
    return home


def resolve_mt5_dir(settings: Settings) -> Path:
    return Path(settings.mt5_dir) if settings.mt5_dir.strip() else default_mt5_dir()


def copy_dir(base: Path, side: str) -> Path:
    return Path(base) / side


def copy_exe(base: Path, side: str) -> Path:
    return copy_dir(base, side) / TERMINAL_EXE


def missing_copies(settings: Settings) -> list[str]:
    """Sides that use Aitrade's own copy, but whose copy does not exist yet."""
    return [side for side in SIDES
            if settings.account(side).managed and not Path(settings.account(side).terminal_path).is_file()]


def missing_copy_problems(settings: Settings) -> list[str]:
    return [f"MetaTrader 5 is not installed yet for the {SIDE_TITLES[side]} account. Press “Change accounts” "
            "and finish the last step (Install and check)." for side in missing_copies(settings)]


# --------------------------------------------------------------------------- copying


def wanted(relative: str) -> bool:
    """Should this file of a MetaTrader 5 folder go into a fresh copy?

    Program files yes. Anything personal or volatile no: saved accounts and
    settings, logs, the user's own MQL5 programs, downloaded price history.
    """
    parts = [p for p in relative.replace("/", "\\").split("\\") if p]
    if not parts:
        return False
    low = [p.lower() for p in parts]
    top = low[0]
    if top in _SKIP_TOP:
        return False
    if len(parts) == 1:
        return not (top.endswith((".ini", ".log", ".tmp", ".json", ".part")) or top.startswith(("aitrade-", "mt5-start-")))
    if top == "config":
        return len(parts) == 2 and low[1] in _KEEP_CONFIG
    if top == "bases":
        return len(parts) > 2 and low[1] == "default" and low[2] != "history"
    return True


def plan_copy(source_dir: Path) -> tuple[list[str], int]:
    """(relative paths to copy, total bytes). terminal64.exe comes last: its presence means "complete"."""
    source_dir = Path(source_dir)
    files: list[str] = []
    total = 0
    for root, _dirs, names in os.walk(source_dir):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), source_dir)
            if wanted(rel):
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                files.append(rel)
    files.sort(key=lambda r: (r.lower() == TERMINAL_EXE, r.lower()))
    if not files or files[-1].lower() != TERMINAL_EXE:
        raise SetupError(f"There is no {TERMINAL_EXE} in {source_dir}, so it cannot be copied.")
    return files, total


def copy_terminal(source_dir: Path, target_dir: Path, *, progress: Progress = _noop,
                  cancelled: Cancelled = _never, label: str = "Copying MetaTrader 5") -> None:
    """Copy the program files of one MetaTrader 5 into `target_dir` (which becomes a portable copy)."""
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    if ntpath.normcase(str(source_dir)) == ntpath.normcase(str(target_dir)):
        raise SetupError("The copy cannot be made onto itself.")
    files, total = plan_copy(source_dir)
    free = free_bytes(target_dir)
    if free < total + COPY_HEADROOM:
        raise SetupError(
            f"Not enough free space for MetaTrader 5 on {target_dir.anchor or target_dir}: "
            f"{total / MB:.0f} MB are needed, {free / MB:.0f} MB are free."
        )
    done = 0
    progress(label, 0.0)
    for rel in files:
        if cancelled():
            raise SetupCancelled()
        src, dst = source_dir / rel, target_dir / rel
        is_terminal = rel.lower() == TERMINAL_EXE
        # The terminal itself is written under another name first: a copy that was cut off
        # (power loss, crash) must never look like a finished one.
        writing = dst.with_name(dst.name + ".part") if is_terminal else dst
        try:
            size = src.stat().st_size
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_file() and dst.stat().st_size == size and not is_terminal:
                done += size  # left from an interrupted copy
            else:
                with open(src, "rb") as fin, open(writing, "wb") as fout:
                    while True:
                        chunk = fin.read(1024 * 1024)
                        if not chunk:
                            break
                        fout.write(chunk)
                        done += len(chunk)
                        if cancelled():
                            raise SetupCancelled()
                        progress(label, done / total if total else None)
                if is_terminal:
                    os.replace(writing, dst)
        except SetupCancelled:
            _remove_quietly(writing)
            raise
        except OSError as exc:
            _remove_quietly(writing)
            _remove_quietly(target_dir / TERMINAL_EXE)
            raise SetupError(f"Could not copy {rel}: {exc}") from exc
        progress(label, done / total if total else None)
    copied = target_dir / TERMINAL_EXE
    if not copied.is_file() or copied.stat().st_size != (source_dir / TERMINAL_EXE).stat().st_size:
        _remove_quietly(copied)
        raise SetupError("The copy of MetaTrader 5 is not complete. Please try again.")
    progress(label, 1.0)


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- finding or installing a source


def standard_install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "MetaTrader 5"


def _is_under(path: str, folder: Path) -> bool:
    a = ntpath.normcase(ntpath.normpath(str(path)))
    b = ntpath.normcase(ntpath.normpath(str(folder)))
    return a == b or a.startswith(b.rstrip("\\") + "\\")


def find_sources(exclude_under: Optional[Path] = None, found: Optional[Iterable[Terminal]] = None) -> list[Terminal]:
    """MetaTrader 5 installs on this PC that a copy can be made from, the standard one first."""
    listed = list(find_terminals() if found is None else found)
    if exclude_under is not None:
        listed = [t for t in listed if not _is_under(t.path, exclude_under)]
    standard = ntpath.normcase(str(standard_install_dir() / TERMINAL_EXE))
    return sorted(listed, key=lambda t: (ntpath.normcase(t.path) != standard, t.name.lower()))


def download(url: str, dest: Path, *, progress: Progress = _noop, cancelled: Cancelled = _never,
             opener: Callable = urllib.request.urlopen, label: str = "Downloading MetaTrader 5") -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    progress(label, 0.0)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Aitrade"})
        with opener(request, timeout=60) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled():
                    raise SetupCancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                progress(label, done / total if total else None)
        if part.stat().st_size < INSTALLER_MIN_BYTES:
            raise SetupError("The download of MetaTrader 5 is not complete. Check the internet connection.")
        os.replace(part, dest)
    except SetupCancelled:
        _remove_quietly(part)
        raise
    except SetupError:
        _remove_quietly(part)
        raise
    except Exception as exc:  # network errors of every kind
        _remove_quietly(part)
        raise SetupError(f"Could not download MetaTrader 5 from MetaQuotes: {exc}") from exc
    return dest


def signature(path: Path) -> tuple[str, str]:
    """(status, signer) of a program file's digital signature, as Windows judges it. ('Valid', 'CN=...')."""
    if sys.platform != "win32":
        return "Unknown", ""
    env = dict(os.environ, AITRADE_SIG_FILE=str(path))
    command = ("$s = Get-AuthenticodeSignature -LiteralPath $env:AITRADE_SIG_FILE; "
               "[Console]::Out.WriteLine($s.Status); [Console]::Out.WriteLine($s.SignerCertificate.Subject)")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env=env, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "Unknown", str(exc)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return (lines[0] if lines else "Unknown"), (lines[1] if len(lines) > 1 else "")


def check_installer_signature(path: Path, verify: Callable[[Path], tuple[str, str]] = signature) -> None:
    status, signer = verify(path)
    if status != "Valid" or INSTALLER_SIGNER.lower() not in signer.lower():
        _remove_quietly(path)
        raise SetupError(
            "The downloaded MetaTrader 5 installer does not carry a valid MetaQuotes signature "
            f"(status: {status}), so it was not started."
        )


def run_installer(installer: Path, *, progress: Progress = _noop, cancelled: Cancelled = _never,
                  timeout_s: float = INSTALL_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep) -> None:
    """Run the official installer in its automatic mode and wait until it is done."""
    label = "Installing MetaTrader 5 – if Windows asks for permission, click Yes"
    progress(label, None)
    try:
        proc = subprocess.Popen([str(installer), "/auto"], cwd=str(Path(installer).parent))
    except OSError as exc:
        raise SetupError(f"The MetaTrader 5 installer could not be started: {exc}") from exc
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None or terminal.is_running(str(installer)):
        if cancelled():
            raise SetupCancelled()
        if time.monotonic() >= deadline:
            raise SetupError("The MetaTrader 5 installer did not finish. Close it and try again.")
        sleep(1.0)
        progress(label, None)


def install_metatrader(work_dir: Path, *, progress: Progress = _noop, cancelled: Cancelled = _never,
                       download_file: Callable = download, verify: Callable = signature,
                       run: Callable = run_installer, sleep: Callable[[float], None] = time.sleep) -> Path:
    """Download the official installer, check its signature, run it. Returns the installed folder."""
    installer = download_file(INSTALLER_URL, Path(work_dir) / "mt5setup.exe", progress=progress, cancelled=cancelled)
    progress("Checking the MetaQuotes signature", None)
    check_installer_signature(installer, verify)
    run(installer, progress=progress, cancelled=cancelled)
    installed = standard_install_dir()
    exe = installed / TERMINAL_EXE
    for _ in range(30):  # the last files can land a moment after the installer window closes
        if exe.is_file():
            break
        sleep(1.0)
    if not exe.is_file():
        raise SetupError("MetaTrader 5 was not installed. If Windows asked for permission, click Yes, "
                         "then try again.")
    # The installer opens MetaTrader 5 when it is done. Aitrade uses its own copies, so close that one.
    progress("Closing the MetaTrader 5 window the installer opened", None)
    for _ in range(20):
        if terminal.is_running(str(exe)):
            break
        sleep(1.0)
    try:
        terminal.close(str(exe), timeout_s=30, sleep=sleep)
    except Exception:
        pass  # it may run with higher rights; the user can close it by hand
    return installed


# --------------------------------------------------------------------------- the whole job


def run_setup(settings: Settings, *, progress: Progress = _noop, cancelled: Cancelled = _never,
              sources: Optional[Callable[[], list[Terminal]]] = None,
              install: Optional[Callable[..., Path]] = None) -> list[str]:
    """Make every missing own copy. Returns the sides that were set up (empty when nothing was missing)."""
    need = missing_copies(settings)
    if not need:
        return []
    base = resolve_mt5_dir(settings)
    found = sources() if sources is not None else find_sources(exclude_under=base)
    source_dir: Optional[Path] = Path(found[0].path).parent if found else None
    if source_dir is None:
        ready = [s for s in SIDES if s not in need and settings.account(s).managed
                 and Path(settings.account(s).terminal_path).is_file()]
        if ready:  # the other account's copy can be the source (its accounts and settings are not copied)
            source_dir = Path(settings.account(ready[0]).terminal_path).parent
    if source_dir is None:
        installer = install or install_metatrader
        source_dir = installer(base / "installer", progress=progress, cancelled=cancelled)
    for index, side in enumerate(need):
        target = Path(settings.account(side).terminal_path).parent
        label = f"Copying MetaTrader 5 for the {SIDE_TITLES[side]} account ({index + 1} of {len(need)})"
        copy_terminal(source_dir, target, progress=progress, cancelled=cancelled, label=label)
    progress("MetaTrader 5 is ready for both accounts", 1.0)
    return need
