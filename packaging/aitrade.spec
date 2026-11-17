# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the installed program: one folder with two programs.
#   Aitrade.exe          the window (no console)
#   aitrade-worker.exe   the engine, one process per account (console type, started hidden)
# Build it with:  .venv\Scripts\python.exe packaging\build.py
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "aitrade", "aitrade.ico")

# Things that are imported in a way PyInstaller cannot see by reading the code.
# numpy: the MetaTrader5 package loads it from inside its compiled part.
ENGINE_HIDDEN = ["tzlocal", "ntplib", "yaml", "dotenv", "requests", "MetaTrader5", "numpy"]
WINDOW_HIDDEN = ENGINE_HIDDEN + ["keyring.backends.Windows"] + collect_submodules("win32ctypes")
TIME_ZONES = collect_data_files("tzdata")
NOT_NEEDED = ["tkinter", "_tkinter", "pytest", "_pytest", "PyInstaller", "pip", "setuptools", "wheel"]

window = Analysis(
    [os.path.join(SPECPATH, "aitrade_gui.py")],
    pathex=[ROOT],
    hiddenimports=WINDOW_HIDDEN,
    datas=TIME_ZONES + [(ICON, "aitrade")],
    excludes=NOT_NEEDED,
)
engine = Analysis(
    [os.path.join(SPECPATH, "aitrade_worker.py")],
    pathex=[ROOT],
    hiddenimports=ENGINE_HIDDEN,
    datas=TIME_ZONES,
    excludes=NOT_NEEDED + ["PySide6", "shiboken6", "keyring", "win32ctypes"],
)

window_exe = EXE(
    PYZ(window.pure), window.scripts, [], exclude_binaries=True,
    name="Aitrade", console=False, icon=ICON, upx=False,
)
engine_exe = EXE(
    PYZ(engine.pure), engine.scripts, [], exclude_binaries=True,
    name="aitrade-worker", console=True, icon=ICON, upx=False,
)
COLLECT(
    window_exe, window.binaries, window.datas,
    engine_exe, engine.binaries, engine.datas,
    name="Aitrade", upx=False,
)
