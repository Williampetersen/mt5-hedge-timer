"""The setup program (packaging/setup_main.py) in its silent mode, run from source with a tiny pretend program."""

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "packaging" / "setup_main.py"


def payload(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Aitrade.exe", b"window")
        archive.writestr("aitrade-worker.exe", b"engine")
        archive.writestr("_internal/base_library.zip", b"lib")
    return path


def run_setup(*args):
    return subprocess.run([sys.executable, str(SETUP), "--silent", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def test_silent_install_writes_the_program_and_a_log(tmp_path):
    target = tmp_path / "Programs" / "Aitrade"
    log = tmp_path / "setup.log"
    result = run_setup("--dir", str(target), "--payload", str(payload(tmp_path / "p.zip")), "--no-shortcuts",
                       "--no-register", "--log", str(log))
    assert result.returncode == 0, result.stderr + log.read_text(encoding="utf-8")
    assert (target / "Aitrade.exe").read_bytes() == b"window"
    assert (target / "_internal" / "base_library.zip").is_file() and (target / "aitrade-install.json").is_file()
    text = log.read_text(encoding="utf-8")
    assert "installed 3 files" in text and text.rstrip().endswith("done")


def test_silent_install_reports_why_it_did_not_install(tmp_path):
    full = tmp_path / "full"
    full.mkdir()
    (full / "something.txt").write_text("not ours")
    log = tmp_path / "setup.log"
    result = run_setup("--dir", str(full), "--payload", str(payload(tmp_path / "p.zip")), "--no-shortcuts",
                       "--no-register", "--log", str(log))
    assert result.returncode == 1
    assert "NOT INSTALLED" in log.read_text(encoding="utf-8") and "not empty" in log.read_text(encoding="utf-8")
    assert sorted(p.name for p in full.iterdir()) == ["something.txt"]
