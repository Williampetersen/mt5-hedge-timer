"""Install and remove Aitrade on a Windows PC.

Used by Aitrade-Setup.exe (install) and by `Aitrade.exe --uninstall` (remove).
Only the Python standard library is used, so the setup program stays small.

Safety rule for removing: only files that the installer itself wrote are ever
deleted. They are listed in a file inside the program folder; folders are only
removed when they are empty. Nothing is deleted "with everything inside".
The user's settings, logs and MetaTrader copies live elsewhere and are kept.
"""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

APP_NAME = "Aitrade"
APP_EXE = "Aitrade.exe"
WORKER_EXE = "aitrade-worker.exe"
MANIFEST = "aitrade-install.json"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Aitrade"
SHORTCUT_NAME = "Aitrade.lnk"
OTHER_DRIVE_FOLDER = "AitradeApp"
GB = 1024 ** 3
MB = 1024 ** 2
SYSTEM_DRIVE_MIN_FREE = 10 * GB  # below this, the program goes onto the drive the setup file is on
HEADROOM = 100 * MB

Progress = Callable[[str, Optional[float]], None]


class InstallError(Exception):
    """The install or removal could not be done. The message is written for the user."""


def _noop(_text: str, _fraction: Optional[float] = None) -> None:
    return None


def _norm(path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


def free_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


# --------------------------------------------------------------------------- where to install


def read_manifest(target: Path) -> Optional[dict]:
    try:
        data = json.loads((Path(target) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME and isinstance(data.get("files"), list) else None


def _write_manifest(target: Path, manifest: dict) -> None:
    (Path(target) / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def installed_location(reg=None) -> Optional[Path]:
    """Folder of an Aitrade that is already installed for this Windows user, if any."""
    if sys.platform != "win32" and reg is None:
        return None
    try:
        if reg is None:
            import winreg as reg
        with reg.OpenKey(reg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, _kind = reg.QueryValueEx(key, "InstallLocation")
    except OSError:
        return None
    folder = Path(str(value))
    return folder if read_manifest(folder) is not None else None


def default_install_dir(setup_file: Optional[Path] = None, *, existing: Optional[Path] = None) -> Path:
    """Where the setup proposes to install.

    An Aitrade that is already installed is updated where it is. Otherwise the
    usual per-user place (no administrator rights needed); when the Windows
    drive is nearly full, a folder on the drive the setup file is on.
    """
    if existing is not None:
        return existing
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    usual = Path(base) / "Programs" / APP_NAME
    if setup_file is None or free_bytes(usual) >= SYSTEM_DRIVE_MIN_FREE:
        return usual
    anchor = Path(setup_file).resolve().anchor
    if anchor and _norm(anchor) != _norm(usual.anchor):
        other = Path(anchor) / OTHER_DRIVE_FOLDER
        if free_bytes(other) > free_bytes(usual):
            return other
    return usual


def check_target(target: Path) -> None:
    """Refuse folders that are not safe to install into."""
    target = Path(target)
    if not target.is_absolute():
        raise InstallError("Choose a full folder path, for example C:\\Programs\\Aitrade.")
    if len(target.parts) < 2:
        raise InstallError("Choose a folder, not a whole drive.")
    if target.is_file():
        raise InstallError(f"{target} is a file. Choose a folder.")
    if target.is_dir() and any(target.iterdir()) and read_manifest(target) is None:
        raise InstallError(f"The folder {target} is not empty. Choose an empty folder or a new name.")


def running_processes(target: Path) -> list[int]:
    """Ids of Aitrade programs that are running from this folder right now."""
    try:
        from hedgebot.terminal import process_ids
    except Exception:
        return []
    pids: list[int] = []
    for name in (APP_EXE, WORKER_EXE):
        pids += process_ids(str(Path(target) / name))
    return [p for p in pids if p != os.getpid()]


# --------------------------------------------------------------------------- files


def _inside(target: Path, relative: str) -> Optional[Path]:
    """`target/relative` if that path stays inside `target`, else None."""
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or ":" in str(rel):
        return None
    return Path(target).joinpath(*rel.parts)


def remove_files(target: Path, files: Iterable[str]) -> list[str]:
    """Delete exactly these files under `target`, then the folders that became empty.

    Returns the files that could not be deleted (for example because they are in use).
    """
    target = Path(target)
    failed: list[str] = []
    folders: set[Path] = set()
    for rel in files:
        path = _inside(target, rel)
        if path is None:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            failed.append(str(rel))
        parent = path.parent
        while _norm(parent) != _norm(target) and _norm(parent).startswith(_norm(target)):
            folders.add(parent)
            parent = parent.parent
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()  # only works on an empty folder
        except OSError:
            pass
    return failed


def install(
    payload: Path,
    target: Path,
    *,
    version: str,
    progress: Progress = _noop,
    desktop_shortcut: bool = True,
    shortcuts: bool = True,
    register: bool = True,
    make_shortcuts: Optional[Callable[..., list[str]]] = None,
    write_registry: Optional[Callable[..., None]] = None,
    running: Optional[Callable[[Path], list[int]]] = None,
) -> dict:
    """Unpack the program into `target`, make the shortcuts and the entry in "Installed apps"."""
    payload, target = Path(payload), Path(target)
    check_target(target)
    if (running or running_processes)(target):
        raise InstallError("Aitrade is running. Close it, then install again.")
    old = read_manifest(target)
    try:
        archive = zipfile.ZipFile(payload)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError(f"The setup file is damaged ({exc}). Download it again.") from exc
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename.replace("\\", "/") for info in entries]
        if any(_inside(target, name) is None for name in names) or APP_EXE not in names:
            raise InstallError("The setup file is damaged. Download it again.")
        total = sum(info.file_size for info in entries)
        free = free_bytes(target)
        if free < total + HEADROOM:
            raise InstallError(f"Not enough free space on {target.anchor}: {total / MB:.0f} MB are needed, "
                               f"{free / MB:.0f} MB are free. Choose a folder on another drive.")
        # Note what is about to be written before writing it: an install that is cut off
        # must still be known as ours, so it can be finished or removed later.
        planned = sorted(set(names) | {str(f).replace("\\", "/") for f in (old["files"] if old else [])})
        try:
            target.mkdir(parents=True, exist_ok=True)
            _write_manifest(target, {"app": APP_NAME, "version": version, "complete": False, "files": planned,
                                     "shortcuts": (old or {}).get("shortcuts", [])})
        except OSError as exc:
            raise InstallError(f"Could not write into {target}: {exc}") from exc
        done = 0
        progress("Installing Aitrade", 0.0)
        try:
            for info, name in zip(entries, names):
                dest = _inside(target, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(dest, "wb") as out:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        progress("Installing Aitrade", done / total if total else None)
        except OSError as exc:
            raise InstallError(f"Could not write the program files: {exc}") from exc
    if old:  # an update: files of the old version that the new one does not have any more
        remove_files(target, [f for f in old["files"] if str(f).replace("\\", "/") not in set(names)])
    manifest = {
        "app": APP_NAME,
        "version": version,
        "complete": True,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": sorted(names),
        "shortcuts": [],
    }
    if shortcuts:
        progress("Making the shortcuts", None)
        try:
            manifest["shortcuts"] = (make_shortcuts or create_shortcuts)(target, desktop=desktop_shortcut)
        except Exception as exc:  # the program is installed; a missing shortcut is not fatal
            manifest["shortcut_problem"] = str(exc)
    _write_manifest(target, manifest)
    if register:
        try:
            (write_registry or register_uninstall)(target, version, max(1, total // 1024))
        except Exception as exc:
            manifest["registry_problem"] = str(exc)
    progress("Aitrade is installed", 1.0)
    return manifest


# --------------------------------------------------------------------------- shortcuts and "Installed apps"

_SHORTCUT_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath('Programs'))
if ($env:AITRADE_DESKTOP -eq '1') { $places += [Environment]::GetFolderPath('Desktop') }
foreach ($place in $places) {
    if (-not $place) { continue }
    $lnk = Join-Path $place $env:AITRADE_LNK
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $env:AITRADE_TARGET
    $s.WorkingDirectory = $env:AITRADE_WORKDIR
    $s.IconLocation = $env:AITRADE_TARGET + ',0'
    $s.Description = 'Aitrade'
    $s.Save()
    [Console]::Out.WriteLine($lnk)
}
"""


def _powershell(script: str, env: dict) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=dict(os.environ, **env), capture_output=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise InstallError(f"Windows could not make the shortcut: {result.stderr.decode('utf-8', 'replace')[:300]}")
    return out


def create_shortcuts(target: Path, *, desktop: bool = True, run: Callable[[str, dict], str] = _powershell) -> list[str]:
    """Start-menu shortcut (and a Desktop one). Returns the shortcut files that were made."""
    exe = Path(target) / APP_EXE
    out = run(_SHORTCUT_SCRIPT, {
        "AITRADE_TARGET": str(exe), "AITRADE_WORKDIR": str(target), "AITRADE_LNK": SHORTCUT_NAME,
        "AITRADE_DESKTOP": "1" if desktop else "0",
    })
    return [line.strip() for line in out.splitlines() if line.strip().lower().endswith(".lnk")]


def remove_shortcuts(paths: Iterable[str]) -> None:
    for item in paths:
        path = Path(str(item))
        if path.name.lower() == SHORTCUT_NAME.lower():  # never anything but our own shortcut
            try:
                path.unlink()
            except OSError:
                pass


def uninstall_values(target: Path, version: str, size_kb: int) -> dict:
    exe = Path(target) / APP_EXE
    return {
        "DisplayName": APP_NAME,
        "DisplayVersion": version,
        "Publisher": APP_NAME,
        "InstallLocation": str(target),
        "DisplayIcon": str(exe),
        "UninstallString": f'"{exe}" --uninstall',
        "QuietUninstallString": f'"{exe}" --uninstall --silent',
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": int(size_kb),
    }


def register_uninstall(target: Path, version: str, size_kb: int, reg=None) -> None:
    """Add Aitrade to "Installed apps" for this Windows user (no administrator rights needed)."""
    if reg is None:
        import winreg as reg
    with reg.CreateKey(reg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in uninstall_values(target, version, size_kb).items():
            kind = reg.REG_DWORD if isinstance(value, int) else reg.REG_SZ
            reg.SetValueEx(key, name, 0, kind, value)


def unregister_uninstall(reg=None) -> None:
    if reg is None:
        import winreg as reg
    try:
        reg.DeleteKey(reg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


# --------------------------------------------------------------------------- removing


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def cleanup_script(target: Path, files: Iterable[str], wait_for_pid: Optional[int] = None) -> str:
    """PowerShell text that deletes the listed files once the program has closed.

    A running program cannot delete itself, so the removal is finished by this
    small script. It contains no "delete everything in a folder" command: one
    line per installed file, and folders are removed only when they are empty.
    """
    target = Path(target)
    paths, folders = [], set()
    for rel in files:
        path = _inside(target, rel)
        if path is None:
            continue
        paths.append(path)
        parent = path.parent
        while _norm(parent) != _norm(target) and _norm(parent).startswith(_norm(target)):
            folders.add(parent)
            parent = parent.parent
    paths.append(target / MANIFEST)
    ordered = sorted(folders, key=lambda p: len(p.parts), reverse=True) + [target]
    lines = ["$ErrorActionPreference = 'SilentlyContinue'"]
    if wait_for_pid:
        lines.append(f"try {{ Wait-Process -Id {int(wait_for_pid)} -Timeout 120 }} catch {{}}")
    lines.append("Start-Sleep -Milliseconds 700")
    lines.append("$files = @(")
    lines += [f"    {_ps_quote(p)}" + ("," if i < len(paths) - 1 else "") for i, p in enumerate(paths)]
    lines.append(")")
    lines.append("foreach ($f in $files) { for ($i = 0; $i -lt 20; $i++) { "
                 "Remove-Item -LiteralPath $f -Force; if (-not (Test-Path -LiteralPath $f)) { break }; "
                 "Start-Sleep -Milliseconds 500 } }")
    lines.append("$folders = @(")
    lines += [f"    {_ps_quote(p)}" + ("," if i < len(ordered) - 1 else "") for i, p in enumerate(ordered)]
    lines.append(")")
    lines.append("foreach ($d in $folders) { if ((Test-Path -LiteralPath $d) -and "
                 "-not (Get-ChildItem -LiteralPath $d -Force)) { Remove-Item -LiteralPath $d -Force } }")
    lines.append("Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force")
    return "\r\n".join(lines) + "\r\n"


def _spawn_detached(script_path: Path) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        creationflags=flags, close_fds=True, cwd=tempfile.gettempdir(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def uninstall(target: Path, *, wait_for_pid: Optional[int] = None, in_process: bool = False,
              spawn: Callable[[Path], None] = _spawn_detached, unregister: Callable[[], None] = unregister_uninstall
              ) -> list[str]:
    """Remove the program from `target`. Settings, logs and MetaTrader copies are kept.

    in_process=True deletes the files right here (used when another program does
    the removing). Otherwise a small script finishes the job after this program
    has closed. Returns the files that could not be deleted (in_process only).
    """
    target = Path(target)
    manifest = read_manifest(target)
    if manifest is None:
        raise InstallError(f"No Aitrade installation was found in {target}.")
    remove_shortcuts(manifest.get("shortcuts") or [])
    unregister()
    if in_process:
        failed = remove_files(target, manifest["files"])
        if not failed:
            remove_files(target, [MANIFEST])
            try:
                target.rmdir()
            except OSError:
                pass
        return failed
    fd, name = tempfile.mkstemp(prefix="aitrade-remove-", suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
        fh.write(cleanup_script(target, manifest["files"], wait_for_pid))
    spawn(Path(name))
    return []
