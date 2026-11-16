"""Making Aitrade's own MetaTrader 5 copies: what is copied, from where, and the installer steps."""

import io
from pathlib import Path

import pytest

from aitrade import mt5_setup as ms
from aitrade import paths
from aitrade.settings import Account, Settings
from aitrade.terminals import Terminal


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(ms.MT5_DIR_ENV, raising=False)


def make_install(folder: Path, *, used: bool = False) -> Path:
    """A folder that looks like an installed MetaTrader 5 (tiny files)."""
    files = {
        "terminal64.exe": b"T" * 5000,
        "MetaEditor64.exe": b"E" * 3000,
        "metatester64.exe": b"M" * 1000,
        "Terminal.ico": b"I" * 10,
        "uninstall.exe": b"U" * 2000,
        "Config/servers.dat": b"S" * 300,
        "Config/terminal.lic": b"L" * 100,
        "Profiles/Charts/Default/chart01.chr": b"C" * 50,
        "Sounds/alert.wav": b"W" * 40,
        "Bases/Default/History/EURUSD/2024.hcc": b"H" * 9000,
        "Bases/Default/Mail/1.welcome.english.welcome": b"m" * 60,
        "Bases/Default/Symbols/symbols.dat": b"y" * 70,
    }
    if used:
        files.update({
            "Config/accounts.dat": b"SECRET-ACCOUNTS",
            "Config/common.ini": b"ini",
            "config/certificates/x.cer": b"cert",
            "logs/20261116.log": b"log",
            "MQL5/Experts/MyRobot.ex5": b"robot",
            "Tester/cache/x.tst": b"t",
            "llm-agent/state.json": b"{}",
            "Bases/MyBroker-Live/trades/123/deals.dat": b"deals",
            "Bases/alerts.dat": b"a",
            "aitrade-start.ini": b"x",
            "notes.log": b"x",
        })
    for rel, data in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder


def listing(folder: Path) -> set[str]:
    return {str(p.relative_to(folder)).replace("\\", "/").lower() for p in folder.rglob("*") if p.is_file()}


def managed_settings(base: Path) -> Settings:
    s = Settings(mt5_dir=str(base))
    for side in ("buy", "sell"):
        acc = s.account(side)
        acc.managed, acc.portable = True, True
        acc.terminal_path = str(ms.copy_exe(base, side))
    return s


# ---------------------------------------------------------------- what goes into a copy


@pytest.mark.parametrize("path, keep", [
    ("terminal64.exe", True), ("MetaEditor64.exe", True), ("Terminal.ico", True),
    ("Config\\servers.dat", True), ("config/terminal.lic", True),
    ("Profiles\\Charts\\Default\\chart01.chr", True), ("Sounds\\alert.wav", True),
    ("Bases\\Default\\Mail\\1.welcome", True), ("Bases\\Default\\Symbols\\symbols.dat", True),
    ("uninstall.exe", False), ("Config\\accounts.dat", False), ("Config\\common.ini", False),
    ("config\\certificates\\x.cer", False), ("logs\\20261116.log", False), ("MQL5\\Experts\\MyRobot.ex5", False),
    ("Tester\\cache\\x.tst", False), ("llm-agent\\state.json", False),
    ("Bases\\Default\\History\\EURUSD\\2024.hcc", False), ("Bases\\MyBroker-Live\\trades\\1\\deals.dat", False),
    ("Bases\\alerts.dat", False), ("aitrade-start.ini", False), ("mt5-start-abc.ini", False),
    ("notes.log", False), ("", False),
])
def test_only_program_files_are_wanted(path, keep):
    assert ms.wanted(path) is keep


def test_the_plan_puts_the_terminal_last_and_adds_up_the_bytes(tmp_path):
    source = make_install(tmp_path / "src")
    files, total = ms.plan_copy(source)
    assert files[-1] == "terminal64.exe" and "uninstall.exe" not in files
    assert total == 5000 + 3000 + 1000 + 10 + 300 + 100 + 50 + 40 + 60 + 70
    (source / "terminal64.exe").unlink()
    with pytest.raises(ms.SetupError, match="terminal64.exe"):
        ms.plan_copy(source)


def test_a_copy_of_a_used_metatrader_leaves_everything_personal_behind(tmp_path):
    source = make_install(tmp_path / "src", used=True)
    target = tmp_path / "copies" / "buy"
    seen = []
    ms.copy_terminal(source, target, progress=lambda text, frac: seen.append((text, frac)), label="Copying")
    assert listing(target) == {
        "terminal64.exe", "metaeditor64.exe", "metatester64.exe", "terminal.ico", "config/servers.dat",
        "config/terminal.lic", "profiles/charts/default/chart01.chr", "sounds/alert.wav",
        "bases/default/mail/1.welcome.english.welcome", "bases/default/symbols/symbols.dat",
    }
    assert (target / "terminal64.exe").read_bytes() == b"T" * 5000
    assert seen[0] == ("Copying", 0.0) and seen[-1] == ("Copying", 1.0)
    fractions = [f for _, f in seen]
    assert fractions == sorted(fractions)  # the bar only moves forward
    assert "SECRET-ACCOUNTS" not in "".join(p.read_text(errors="ignore") for p in target.rglob("*") if p.is_file())


def test_stopping_a_copy_leaves_no_terminal_and_a_second_run_finishes_it(tmp_path):
    source = make_install(tmp_path / "src")
    target = tmp_path / "copies" / "sell"
    calls = {"n": 0}

    def stop_after_three():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(ms.SetupCancelled):
        ms.copy_terminal(source, target, cancelled=stop_after_three)
    assert not (target / "terminal64.exe").exists()  # no terminal = not installed
    assert any(target.rglob("*"))  # some files are already there
    ms.copy_terminal(source, target)
    assert (target / "terminal64.exe").stat().st_size == 5000


def test_a_copy_needs_free_space_and_a_different_folder(tmp_path, monkeypatch):
    source = make_install(tmp_path / "src")
    monkeypatch.setattr(ms, "free_bytes", lambda path: 1000)
    with pytest.raises(ms.SetupError, match="Not enough free space"):
        ms.copy_terminal(source, tmp_path / "copies" / "buy")
    assert not (tmp_path / "copies" / "buy" / "terminal64.exe").exists()
    monkeypatch.undo()
    with pytest.raises(ms.SetupError, match="onto itself"):
        ms.copy_terminal(source, source)


# ---------------------------------------------------------------- where the copies go


def test_the_folder_can_be_chosen_with_an_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ms.MT5_DIR_ENV, str(tmp_path / "mine"))
    assert ms.default_mt5_dir() == tmp_path / "mine"
    assert ms.resolve_mt5_dir(Settings()) == tmp_path / "mine"
    assert ms.resolve_mt5_dir(Settings(mt5_dir="X:\\chosen")) == Path("X:\\chosen")


def test_copies_normally_live_in_the_apps_own_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "project_root", lambda: Path("Q:\\Aitrade"))
    monkeypatch.setattr(ms, "_has_copy", lambda base: False)
    monkeypatch.setattr(ms, "free_bytes", lambda path: 50 * ms.GB)
    assert ms.default_mt5_dir() == tmp_path / "home" / "mt5"


def test_a_nearly_full_windows_drive_sends_the_copies_next_to_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "project_root", lambda: Path("Q:\\Aitrade"))
    monkeypatch.setattr(ms, "_has_copy", lambda base: False)
    monkeypatch.setattr(ms, "free_bytes", lambda path: 150 * ms.GB if str(path).startswith("Q:") else 4 * ms.GB)
    assert ms.default_mt5_dir() == Path("Q:\\AitradeMT5")
    monkeypatch.setattr(ms, "free_bytes", lambda path: 1 * ms.GB)  # the other drive is no better
    assert ms.default_mt5_dir() == tmp_path / "home" / "mt5"


def test_copies_that_already_exist_decide_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "project_root", lambda: Path("Q:\\Aitrade"))
    monkeypatch.setattr(ms, "free_bytes", lambda path: 50 * ms.GB)
    monkeypatch.setattr(ms, "_has_copy", lambda base: str(base) == "Q:\\AitradeMT5")
    assert ms.default_mt5_dir() == Path("Q:\\AitradeMT5")


def test_missing_copies_are_named(tmp_path):
    base = tmp_path / "copies"
    s = managed_settings(base)
    assert ms.missing_copies(s) == ["buy", "sell"]
    make_install(base / "buy")
    assert ms.missing_copies(s) == ["sell"]
    problems = ms.missing_copy_problems(s)
    assert len(problems) == 1 and "SELL account" in problems[0] and "Install and check" in problems[0]
    own = Settings(buy=Account(terminal_path="C:\\x\\terminal64.exe"), sell=Account(terminal_path="C:\\y\\terminal64.exe"))
    assert ms.missing_copies(own) == []  # the user's own MetaTrader is not Aitrade's business


def test_sources_put_the_standard_metatrader_first_and_skip_own_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
    found = [
        Terminal("C:\\Program Files\\Alpha MetaTrader 5\\terminal64.exe", "Alpha MetaTrader 5"),
        Terminal("D:\\AitradeMT5\\buy\\terminal64.exe", "buy"),
        Terminal("C:\\Program Files\\MetaTrader 5\\terminal64.exe", "MetaTrader 5"),
    ]
    names = [t.name for t in ms.find_sources(exclude_under=Path("D:\\AitradeMT5"), found=found)]
    assert names == ["MetaTrader 5", "Alpha MetaTrader 5"]
    assert len(ms.find_sources(found=found)) == 3


# ---------------------------------------------------------------- download and installer


URL = "https://example.invalid/mt5setup.exe"


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_writes_the_file_and_reports_progress(tmp_path):
    data = b"x" * (ms.INSTALLER_MIN_BYTES + 5)
    seen, asked = [], []

    def opener(request, timeout=0):
        asked.append((request.full_url, request.get_header("User-agent")))
        return FakeResponse(data)

    dest = ms.download("https://example.invalid/mt5setup.exe", tmp_path / "dl" / "mt5setup.exe",
                       progress=lambda t, f: seen.append(f), opener=opener)
    assert dest.read_bytes() == data and seen[-1] == 1.0
    assert asked == [("https://example.invalid/mt5setup.exe", "Aitrade")]  # nothing personal is sent
    assert not dest.with_suffix(".exe.part").exists()


def test_download_problems_are_clear_and_leave_nothing_behind(tmp_path):
    dest = tmp_path / "mt5setup.exe"
    with pytest.raises(ms.SetupError, match="not complete"):
        ms.download(URL, dest, opener=lambda request, timeout=0: FakeResponse(b"tiny"))

    def offline(request, timeout=0):
        raise OSError("no route to host")

    with pytest.raises(ms.SetupError, match="no route to host"):
        ms.download(URL, dest, opener=offline)
    with pytest.raises(ms.SetupCancelled):
        ms.download(URL, dest, opener=lambda request, timeout=0: FakeResponse(b"x" * 10), cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_the_installer_must_be_signed_by_metaquotes(tmp_path):
    good = tmp_path / "good.exe"
    good.write_bytes(b"x")
    ms.check_installer_signature(good, lambda p: ("Valid", "CN=MetaQuotes Ltd., O=MetaQuotes Ltd., C=CY"))
    assert good.exists()
    for status, signer in (("Valid", "CN=Somebody Else"), ("NotSigned", ""), ("HashMismatch", "CN=MetaQuotes Ltd.")):
        bad = tmp_path / "bad.exe"
        bad.write_bytes(b"x")
        with pytest.raises(ms.SetupError, match="signature"):
            ms.check_installer_signature(bad, lambda p, s=status, n=signer: (s, n))
        assert not bad.exists()  # an untrusted installer is deleted, never started


def test_the_real_windows_signature_check_recognises_an_unsigned_file(tmp_path):
    plain = tmp_path / "plain.exe"
    plain.write_bytes(b"MZ" + b"\0" * 200)
    status, signer = ms.signature(plain)
    assert status != "Valid" and "MetaQuotes" not in signer


def test_install_downloads_checks_runs_and_closes_the_opened_window(tmp_path, monkeypatch):
    standard = tmp_path / "Program Files" / "MetaTrader 5"
    monkeypatch.setattr(ms, "standard_install_dir", lambda: standard)
    steps, closed = [], []

    def fake_download(url, dest, *, progress, cancelled):
        steps.append(("download", url))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"installer")
        return dest

    def fake_run(installer, *, progress, cancelled):
        steps.append(("run", installer.name))
        make_install(standard)

    monkeypatch.setattr(ms.terminal, "is_running", lambda exe: True)
    monkeypatch.setattr(ms.terminal, "close", lambda exe, timeout_s=0, sleep=None: closed.append(exe) or True)
    texts = []
    result = ms.install_metatrader(tmp_path / "work", progress=lambda t, f: texts.append(t),
                                   download_file=fake_download, verify=lambda p: ("Valid", "CN=MetaQuotes Ltd."),
                                   run=fake_run, sleep=lambda s: None)
    assert result == standard
    assert steps == [("download", ms.INSTALLER_URL), ("run", "mt5setup.exe")]
    assert closed == [str(standard / "terminal64.exe")]
    assert "Checking the MetaQuotes signature" in texts


def test_install_refuses_a_bad_signature_and_reports_a_refused_install(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "standard_install_dir", lambda: tmp_path / "nowhere")
    ran = []

    def fake_download(url, dest, *, progress, cancelled):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"installer")
        return dest

    with pytest.raises(ms.SetupError, match="signature"):
        ms.install_metatrader(tmp_path / "w", download_file=fake_download, verify=lambda p: ("NotSigned", ""),
                              run=lambda *a, **k: ran.append(1), sleep=lambda s: None)
    assert ran == []  # never started
    with pytest.raises(ms.SetupError, match="was not installed"):
        ms.install_metatrader(tmp_path / "w", download_file=fake_download,
                              verify=lambda p: ("Valid", "CN=MetaQuotes Ltd."),
                              run=lambda *a, **k: ran.append(1), sleep=lambda s: None)
    assert ran == [1]


def test_the_installer_link_is_the_official_https_one():
    assert ms.INSTALLER_URL.startswith("https://") and ms.INSTALLER_URL.endswith("/mt5/mt5setup.exe")
    assert "?" not in ms.INSTALLER_URL  # no tracking parameters


# ---------------------------------------------------------------- the whole job


def test_nothing_to_do_when_both_copies_exist(tmp_path):
    base = tmp_path / "copies"
    s = managed_settings(base)
    make_install(base / "buy")
    make_install(base / "sell")
    assert ms.run_setup(s, sources=lambda: pytest.fail("must not look for a source")) == []


def test_both_copies_come_from_the_metatrader_on_the_pc(tmp_path):
    source = make_install(tmp_path / "Program Files" / "MetaTrader 5", used=True)
    s = managed_settings(tmp_path / "copies")
    texts = []
    done = ms.run_setup(s, progress=lambda t, f: texts.append(t),
                        sources=lambda: [Terminal(str(source / "terminal64.exe"), "MetaTrader 5")],
                        install=lambda *a, **k: pytest.fail("must not install when a MetaTrader is there"))
    assert done == ["buy", "sell"] and ms.missing_copies(s) == []
    for side in ("buy", "sell"):
        assert "config/accounts.dat" not in listing(tmp_path / "copies" / side)
    assert any("BUY account (1 of 2)" in t for t in texts) and any("SELL account (2 of 2)" in t for t in texts)
    assert texts[-1] == "MetaTrader 5 is ready for both accounts"


def test_the_other_accounts_copy_is_the_source_when_nothing_else_is_there(tmp_path):
    base = tmp_path / "copies"
    s = managed_settings(base)
    make_install(base / "buy", used=True)  # in use: it has saved accounts and logs
    done = ms.run_setup(s, sources=lambda: [], install=lambda *a, **k: pytest.fail("must not install"))
    assert done == ["sell"]
    assert "terminal64.exe" in listing(base / "sell") and "config/accounts.dat" not in listing(base / "sell")


def test_metatrader_is_installed_when_the_pc_has_none(tmp_path):
    base = tmp_path / "copies"
    s = managed_settings(base)
    installed = tmp_path / "Program Files" / "MetaTrader 5"
    asked = []

    def install(work_dir, *, progress, cancelled):
        asked.append(work_dir)
        return make_install(installed)

    assert ms.run_setup(s, sources=lambda: [], install=install) == ["buy", "sell"]
    assert asked == [base / "installer"] and ms.missing_copies(s) == []


def test_a_setup_error_does_not_leave_a_half_copy_that_looks_installed(tmp_path, monkeypatch):
    source = make_install(tmp_path / "src")
    s = managed_settings(tmp_path / "copies")
    monkeypatch.setattr(ms, "free_bytes", lambda path: 10)
    with pytest.raises(ms.SetupError):
        ms.run_setup(s, sources=lambda: [Terminal(str(source / "terminal64.exe"), "MetaTrader 5")])
    assert ms.missing_copies(s) == ["buy", "sell"]
