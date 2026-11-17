"""Aitrade-Setup.exe: installs (or updates) Aitrade on this PC.

Double-click for a small window (add --auto to start installing right away). For scripts:
  Aitrade-Setup.exe --silent [--dir FOLDER] [--no-desktop] [--no-shortcuts] [--no-register] [--log FILE]
Exit code 0 = installed, 1 = not installed (the reason is in the log file).
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

if not getattr(sys, "frozen", False):  # run from the source code: the packages are one folder up
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aitrade import __version__, installer  # noqa: E402

PAYLOAD_NAME = "Aitrade-payload.zip"
LOG_NAME = "Aitrade-Setup.log"


def bundle_dir() -> Path:
    """Where PyInstaller unpacked this setup program (or this folder when run from source)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def setup_file() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()


class Log:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.path.write_text(f"Aitrade setup {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                                 encoding="utf-8")
        except OSError:
            pass

    def __call__(self, text: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {text}\n")
        except OSError:
            pass


def do_install(target: Path, args, log: Log, progress=None) -> dict:
    payload = Path(args.payload) if args.payload else bundle_dir() / PAYLOAD_NAME
    log(f"installing into {target} from {payload.name}")
    last = {"text": ""}

    def report(text, fraction):
        if text != last["text"]:
            last["text"] = text
            log(text)
        if progress:
            progress(text, fraction)

    manifest = installer.install(
        payload, target, version=__version__, progress=report, desktop_shortcut=not args.no_desktop,
        shortcuts=not args.no_shortcuts, register=not args.no_register,
    )
    log(f"installed {len(manifest['files'])} files, shortcuts: {manifest['shortcuts']}")
    for key in ("shortcut_problem", "registry_problem"):
        if manifest.get(key):
            log(f"{key}: {manifest[key]}")
    return manifest


def run_silent(args, log: Log) -> int:
    target = Path(args.dir) if args.dir else installer.default_install_dir(
        setup_file(), existing=installer.installed_location())
    try:
        do_install(target, args, log)
    except installer.InstallError as exc:
        log(f"NOT INSTALLED: {exc}")
        return 1
    except Exception:
        log("NOT INSTALLED (unexpected):\n" + traceback.format_exc())
        return 1
    log("done")
    return 0


def run_window(args, log: Log) -> int:
    import ctypes
    import tkinter as tk
    from tkinter import filedialog, ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-resolution screens
    except Exception:
        pass
    existing = installer.installed_location()
    root = tk.Tk()
    root.title(f"Aitrade {__version__} setup")
    root.resizable(False, False)
    icon = bundle_dir() / "aitrade.ico"
    if icon.is_file():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass
    pad = ttk.Frame(root, padding=22)
    pad.grid(sticky="nsew")
    ttk.Label(pad, text="Update Aitrade" if existing else "Install Aitrade",
              font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(pad, wraplength=520, justify="left", text=(
        "Aitrade opens a BUY on one MetaTrader 5 account and a SELL on another at the same moment.\n"
        "The program is installed for this Windows user only. No administrator rights are needed."
    )).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 16))
    ttk.Label(pad, text="Install in this folder:").grid(row=2, column=0, columnspan=3, sticky="w")
    folder = tk.StringVar(value=str(Path(args.dir) if args.dir else installer.default_install_dir(
        setup_file(), existing=existing)))
    entry = ttk.Entry(pad, textvariable=folder, width=62)
    entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 0))

    def browse():
        chosen = filedialog.askdirectory(title="Choose a folder for Aitrade", mustexist=False)
        if chosen:
            path = Path(chosen)
            folder.set(str(path if path.name.lower().startswith("aitrade") else path / "Aitrade"))

    change = ttk.Button(pad, text="Change…", command=browse)
    change.grid(row=3, column=2, sticky="e", padx=(8, 0), pady=(4, 0))
    desktop = tk.BooleanVar(value=not args.no_desktop)
    tick = ttk.Checkbutton(pad, text="Put a shortcut on the Desktop", variable=desktop)
    tick.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
    bar = ttk.Progressbar(pad, maximum=1000, length=520)
    bar.grid(row=5, column=0, columnspan=3, sticky="we", pady=(18, 4))
    status = ttk.Label(pad, text="", wraplength=520, justify="left")
    status.grid(row=6, column=0, columnspan=3, sticky="w")
    buttons = ttk.Frame(pad)
    buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(18, 0))
    install_btn = ttk.Button(buttons, text="Update" if existing else "Install")
    close_btn = ttk.Button(buttons, text="Close", command=root.destroy)
    install_btn.grid(row=0, column=0, padx=(0, 8))
    close_btn.grid(row=0, column=1)
    events: "queue.Queue[tuple]" = queue.Queue()
    state = {"installed": None, "code": 1}

    def worker(target: Path):
        args.no_desktop = not desktop.get()
        try:
            do_install(target, args, log, progress=lambda text, fraction: events.put(("progress", text, fraction)))
            events.put(("done", target))
        except installer.InstallError as exc:
            log(f"NOT INSTALLED: {exc}")
            events.put(("error", str(exc)))
        except Exception as exc:
            log("NOT INSTALLED (unexpected):\n" + traceback.format_exc())
            events.put(("error", f"The setup failed: {exc}"))

    def start():
        target = Path(folder.get().strip())
        for widget in (install_btn, change, entry, tick):
            widget.state(["disabled"])
        status.configure(text="Starting…", foreground="")
        threading.Thread(target=worker, args=(target,), daemon=True).start()

    def launch():
        target = state["installed"]
        try:
            subprocess.Popen([str(target / installer.APP_EXE)], cwd=str(target))
        except OSError as exc:
            status.configure(text=f"Could not start Aitrade: {exc}", foreground="#b00020")
            return
        root.destroy()

    def poll():
        try:
            while True:
                event = events.get_nowait()
                if event[0] == "progress":
                    _kind, text, fraction = event
                    status.configure(text=text, foreground="")
                    if fraction is None:
                        bar.configure(mode="indeterminate")
                        bar.step(20)
                    else:
                        bar.configure(mode="determinate", value=int(fraction * 1000))
                elif event[0] == "done":
                    state["installed"], state["code"] = event[1], 0
                    bar.configure(mode="determinate", value=1000)
                    status.configure(text=f"Aitrade is installed in {event[1]}.", foreground="#1b7f3b")
                    install_btn.configure(text="Start Aitrade", command=launch)
                    install_btn.state(["!disabled"])
                else:
                    bar.configure(mode="determinate", value=0)
                    status.configure(text=event[1], foreground="#b00020")
                    for widget in (install_btn, change, entry, tick):
                        widget.state(["!disabled"])
        except queue.Empty:
            pass
        root.after(100, poll)

    install_btn.configure(command=start)
    install_btn.focus_set()
    root.bind("<Return>", lambda _event: install_btn.invoke())
    root.after(100, poll)
    if args.auto:
        root.after(400, start)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    root.mainloop()
    return state["code"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="Aitrade-Setup", add_help=False)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--dir")
    parser.add_argument("--no-desktop", action="store_true")
    parser.add_argument("--no-shortcuts", action="store_true")
    parser.add_argument("--no-register", action="store_true")
    parser.add_argument("--log")
    parser.add_argument("--auto", action="store_true")  # window mode: start installing right away
    parser.add_argument("--payload")  # only for tests: another program zip than the built-in one
    args, _unknown = parser.parse_known_args(argv)
    log = Log(Path(args.log) if args.log else Path(tempfile.gettempdir()) / LOG_NAME)
    try:
        return run_silent(args, log) if args.silent else run_window(args, log)
    except Exception:
        log("the setup program failed:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
