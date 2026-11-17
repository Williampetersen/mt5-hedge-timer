"""Build the installed program and Aitrade-Setup.exe.

  .venv\\Scripts\\python.exe packaging\\build.py [--work FOLDER] [--out FOLDER] [--app-only]

Steps: draw the icon, build the program folder (Aitrade.exe + aitrade-worker.exe)
with PyInstaller, zip it, and build the one-file setup program around that zip.
The result is dist\\Aitrade-Setup-<version>.exe. Everything else is temporary
and goes into the work folder (by default in the Windows temp folder, which is
faster than a USB drive).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT))

from aitrade import __version__, installer  # noqa: E402


def run(cmd: list[str], env: dict | None = None) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    started = time.monotonic()
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env)
    print(f"  done in {time.monotonic() - started:.0f} s", flush=True)


def pyinstaller(spec: Path, work: Path, env: dict | None = None) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(work / "dist"), "--workpath", str(work / "build"), str(spec)], env)


def zip_folder(folder: Path, dest: Path) -> int:
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, path.relative_to(folder).as_posix())
    return len(files)


def folder_mb(folder: Path) -> float:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) / 1024 ** 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "aitrade-build"))
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--app-only", action="store_true", help="build the program folder, not the setup file")
    args = parser.parse_args()
    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    if not (ROOT / "aitrade" / "aitrade.ico").is_file():
        run([sys.executable, str(PACKAGING / "make_icon.py")])

    pyinstaller(PACKAGING / "aitrade.spec", work)
    app = work / "dist" / "Aitrade"
    for name in (installer.APP_EXE, installer.WORKER_EXE):
        if not (app / name).is_file():
            print(f"ERROR: {name} was not built")
            return 1
    print(f"program folder: {app} ({folder_mb(app):.0f} MB)")
    if args.app_only:
        return 0

    payload = work / "Aitrade-payload.zip"
    count = zip_folder(app, payload)
    print(f"zipped {count} files into {payload.name} ({payload.stat().st_size / 1024 ** 2:.0f} MB)")
    pyinstaller(PACKAGING / "setup.spec", work, dict(os.environ, AITRADE_PAYLOAD=str(payload)))
    built = work / "dist" / "Aitrade-Setup.exe"
    if not built.is_file():
        print("ERROR: Aitrade-Setup.exe was not built")
        return 1
    final = out / f"Aitrade-Setup-{__version__}.exe"
    shutil.copyfile(built, final)
    print(f"setup file: {final} ({final.stat().st_size / 1024 ** 2:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
