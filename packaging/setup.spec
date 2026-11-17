# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for Aitrade-Setup.exe: one file that carries the whole program (a zip) inside.
# packaging\build.py sets AITRADE_PAYLOAD to that zip and runs this.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "aitrade", "aitrade.ico")
PAYLOAD = os.environ["AITRADE_PAYLOAD"]

setup = Analysis(
    [os.path.join(SPECPATH, "setup_main.py")],
    pathex=[ROOT],
    datas=[(PAYLOAD, "."), (ICON, ".")],
    # The setup program only needs Python's own parts and its small window toolkit.
    excludes=["PySide6", "shiboken6", "numpy", "MetaTrader5", "keyring", "win32ctypes", "yaml", "requests",
              "tzdata", "tzlocal", "ntplib", "dotenv", "pytest", "_pytest", "PyInstaller", "pip", "setuptools"],
)
EXE(
    PYZ(setup.pure), setup.scripts, setup.binaries, setup.datas, [],
    name="Aitrade-Setup", console=False, icon=ICON, upx=False,
)
