"""Installing and removing the program: only our own files, never "everything in a folder"."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from aitrade import installer as ins

FILES_V1 = {
    "Aitrade.exe": b"window-program",
    "aitrade-worker.exe": b"engine-program",
    "_internal/python314.dll": b"p" * 4000,
    "_internal/PySide6/QtCore.pyd": b"q" * 3000,
    "_internal/old/only-in-v1.dll": b"old",
}
FILES_V2 = {
    "Aitrade.exe": b"window-program-2",
    "aitrade-worker.exe": b"engine-program-2",
    "_internal/python314.dll": b"p" * 4100,
    "_internal/PySide6/QtCore.pyd": b"q" * 3000,
    "_internal/new/only-in-v2.dll": b"new",
}


def make_payload(path: Path, files=FILES_V1) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def listing(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def plain_install(payload, target, **kw):
    kw.setdefault("shortcuts", False)
    kw.setdefault("register", False)
    kw.setdefault("running", lambda t: [])
    return ins.install(payload, target, version="1.2.3", **kw)


# ---------------------------------------------------------------- install


def test_install_unpacks_the_program_and_lists_what_it_wrote(tmp_path):
    payload = make_payload(tmp_path / "p.zip")
    target = tmp_path / "Programs" / "Aitrade"
    seen = []
    manifest = plain_install(payload, target, progress=lambda text, frac: seen.append((text, frac)))
    assert listing(target) == set(FILES_V1) | {ins.MANIFEST}
    assert (target / "Aitrade.exe").read_bytes() == b"window-program"
    assert manifest["files"] == sorted(FILES_V1) and manifest["version"] == "1.2.3"
    assert ins.read_manifest(target)["files"] == sorted(FILES_V1)
    assert seen[0] == ("Installing Aitrade", 0.0) and seen[-1] == ("Aitrade is installed", 1.0)


def test_install_makes_shortcuts_and_the_installed_apps_entry(tmp_path):
    payload = make_payload(tmp_path / "p.zip")
    target = tmp_path / "Aitrade"
    calls = {}

    def shortcuts(folder, *, desktop):
        calls["shortcuts"] = (folder, desktop)
        return ["C:\\Users\\x\\Desktop\\Aitrade.lnk"]

    def registry(folder, version, size_kb):
        calls["registry"] = (folder, version, size_kb)

    manifest = ins.install(payload, target, version="1.2.3", desktop_shortcut=False, make_shortcuts=shortcuts,
                           write_registry=registry, running=lambda t: [])
    assert calls["shortcuts"] == (target, False)
    assert calls["registry"][:2] == (target, "1.2.3") and calls["registry"][2] >= 1
    assert manifest["shortcuts"] == ["C:\\Users\\x\\Desktop\\Aitrade.lnk"]
    assert json.loads((target / ins.MANIFEST).read_text(encoding="utf-8"))["shortcuts"] == manifest["shortcuts"]


def test_a_shortcut_problem_does_not_undo_the_install(tmp_path):
    payload = make_payload(tmp_path / "p.zip")

    def broken(folder, *, desktop):
        raise ins.InstallError("no desktop here")

    manifest = ins.install(payload, tmp_path / "A", version="1", make_shortcuts=broken,
                           write_registry=lambda *a: None, running=lambda t: [])
    assert manifest["shortcuts"] == [] and "no desktop here" in manifest["shortcut_problem"]
    assert (tmp_path / "A" / "Aitrade.exe").is_file()


def test_an_update_replaces_the_program_and_keeps_what_is_not_ours(tmp_path):
    target = tmp_path / "Aitrade"
    plain_install(make_payload(tmp_path / "v1.zip", FILES_V1), target)
    (target / "my-notes.txt").write_text("mine")  # something the user put there
    plain_install(make_payload(tmp_path / "v2.zip", FILES_V2), target)
    assert listing(target) == set(FILES_V2) | {ins.MANIFEST, "my-notes.txt"}
    assert (target / "Aitrade.exe").read_bytes() == b"window-program-2"
    assert not (target / "_internal" / "old").exists()  # the old version's folder is gone, it was empty


@pytest.mark.parametrize("make_target, words", [
    (lambda tmp: Path("relative\\folder"), "full folder path"),
    (lambda tmp: Path(tmp.anchor), "not a whole drive"),
    (lambda tmp: tmp / "full", "not empty"),
    (lambda tmp: tmp / "a-file.txt", "is a file"),
])
def test_unsafe_folders_are_refused(tmp_path, make_target, words):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "someone-elses.doc").write_text("x")
    (tmp_path / "a-file.txt").write_text("x")
    payload = make_payload(tmp_path / "p.zip")
    with pytest.raises(ins.InstallError, match=words):
        plain_install(payload, make_target(tmp_path))
    assert (tmp_path / "full" / "someone-elses.doc").read_text() == "x"


def test_install_waits_for_a_running_aitrade_to_be_closed(tmp_path):
    payload = make_payload(tmp_path / "p.zip")
    with pytest.raises(ins.InstallError, match="Close it"):
        ins.install(payload, tmp_path / "A", version="1", shortcuts=False, register=False, running=lambda t: [4321])
    assert not (tmp_path / "A" / "Aitrade.exe").exists()


def test_a_damaged_or_hostile_setup_file_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(ins.InstallError, match="damaged"):
        plain_install(bad, tmp_path / "A")
    outside = make_payload(tmp_path / "slip.zip", {"Aitrade.exe": b"x", "../outside.txt": b"evil"})
    with pytest.raises(ins.InstallError, match="damaged"):
        plain_install(outside, tmp_path / "B" / "Aitrade")
    assert not (tmp_path / "B" / "outside.txt").exists() and not (tmp_path / "outside.txt").exists()
    no_program = make_payload(tmp_path / "none.zip", {"readme.txt": b"x"})
    with pytest.raises(ins.InstallError, match="damaged"):
        plain_install(no_program, tmp_path / "C")


def test_an_install_that_was_cut_off_can_be_finished_or_removed(tmp_path):
    # the second entry cannot be written (its folder is a file): the install stops half way
    broken = make_payload(tmp_path / "broken.zip", {"Aitrade.exe": b"x", "_internal/a": b"file",
                                                    "_internal/a/b.dll": b"cannot be written"})
    target = tmp_path / "Aitrade"
    with pytest.raises(ins.InstallError, match="Could not write the program files"):
        plain_install(broken, target)
    half = ins.read_manifest(target)
    assert half is not None and half["complete"] is False and "Aitrade.exe" in half["files"]
    # it is known as ours, so a good setup file can finish the job in the same folder
    manifest = plain_install(make_payload(tmp_path / "good.zip"), target)
    assert manifest["complete"] is True and listing(target) == set(FILES_V1) | {ins.MANIFEST}
    # and a cut-off install can also be removed cleanly
    other = tmp_path / "Other"
    with pytest.raises(ins.InstallError):
        plain_install(broken, other)
    assert ins.uninstall(other, in_process=True, unregister=lambda: None) == []
    assert not other.exists()


def test_install_needs_free_space(tmp_path, monkeypatch):
    payload = make_payload(tmp_path / "p.zip")
    monkeypatch.setattr(ins, "free_bytes", lambda path: 1000)
    with pytest.raises(ins.InstallError, match="Not enough free space"):
        plain_install(payload, tmp_path / "A")


# ---------------------------------------------------------------- where


def test_default_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    usual = tmp_path / "Local" / "Programs" / "Aitrade"
    monkeypatch.setattr(ins, "free_bytes", lambda path: 50 * ins.GB)
    assert ins.default_install_dir(Path("Q:\\Downloads\\Aitrade-Setup.exe")) == usual
    assert ins.default_install_dir() == usual
    # the Windows drive is nearly full and the setup file sits on a roomier drive
    monkeypatch.setattr(ins, "free_bytes", lambda path: 150 * ins.GB if str(path).startswith("Q:") else 4 * ins.GB)
    assert ins.default_install_dir(Path("Q:\\Downloads\\Aitrade-Setup.exe")) == Path("Q:\\AitradeApp")
    assert ins.default_install_dir() == usual  # nowhere else to go
    # an Aitrade that is already installed is updated where it is
    assert ins.default_install_dir(Path("Q:\\x.exe"), existing=Path("E:\\Old\\Aitrade")) == Path("E:\\Old\\Aitrade")


# ---------------------------------------------------------------- shortcuts and registry


def test_shortcut_script_gets_its_values_through_the_environment(tmp_path):
    seen = {}

    def run(script, env):
        seen["script"], seen["env"] = script, env
        return "C:\\Users\\x\\Start Menu\\Programs\\Aitrade.lnk\r\nC:\\Users\\x\\Desktop\\Aitrade.lnk\r\nnoise\r\n"

    made = ins.create_shortcuts(tmp_path / "Aitrade", desktop=True, run=run)
    assert made == ["C:\\Users\\x\\Start Menu\\Programs\\Aitrade.lnk", "C:\\Users\\x\\Desktop\\Aitrade.lnk"]
    assert seen["env"]["AITRADE_TARGET"] == str(tmp_path / "Aitrade" / "Aitrade.exe")
    assert seen["env"]["AITRADE_DESKTOP"] == "1" and seen["env"]["AITRADE_LNK"] == "Aitrade.lnk"
    assert str(tmp_path) not in seen["script"]  # no path is pasted into the script text
    ins.create_shortcuts(tmp_path / "Aitrade", desktop=False, run=run)
    assert seen["env"]["AITRADE_DESKTOP"] == "0"


def test_only_our_own_shortcut_is_ever_removed(tmp_path):
    ours = tmp_path / "Desktop" / "Aitrade.lnk"
    other = tmp_path / "Desktop" / "MetaTrader 5.lnk"
    ours.parent.mkdir()
    ours.write_bytes(b"x")
    other.write_bytes(b"x")
    ins.remove_shortcuts([str(ours), str(other), str(tmp_path / "Desktop" / "missing" / "Aitrade.lnk")])
    assert not ours.exists() and other.exists()


class FakeRegistry:
    HKEY_CURRENT_USER, REG_SZ, REG_DWORD = "HKCU", "SZ", "DWORD"

    def __init__(self):
        self.keys = {}

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def CreateKey(self, root, path):
        return self._Key(self.keys.setdefault((root, path), {}))

    def OpenKey(self, root, path):
        if (root, path) not in self.keys:
            raise OSError("no such key")
        return self._Key(self.keys[(root, path)])

    def SetValueEx(self, key, name, _reserved, kind, value):
        key.values[name] = (kind, value)

    def QueryValueEx(self, key, name):
        kind, value = key.values[name]
        return value, kind

    def DeleteKey(self, root, path):
        if (root, path) not in self.keys:
            raise OSError("no such key")
        del self.keys[(root, path)]


def test_installed_apps_entry(tmp_path):
    reg = FakeRegistry()
    target = tmp_path / "Aitrade"
    assert ins.installed_location(reg) is None
    ins.register_uninstall(target, "1.2.3", 123456, reg)
    values = reg.keys[("HKCU", ins.UNINSTALL_KEY)]
    assert values["DisplayName"] == ("SZ", "Aitrade") and values["DisplayVersion"] == ("SZ", "1.2.3")
    assert values["UninstallString"] == ("SZ", f'"{target / "Aitrade.exe"}" --uninstall')
    assert values["QuietUninstallString"][1].endswith("--uninstall --silent")
    assert values["EstimatedSize"] == ("DWORD", 123456) and values["NoModify"] == ("DWORD", 1)
    assert ins.installed_location(reg) is None  # the folder holds no installation yet
    plain_install(make_payload(tmp_path / "p.zip"), target)
    assert ins.installed_location(reg) == target
    ins.unregister_uninstall(reg)
    ins.unregister_uninstall(reg)  # twice is fine
    assert ins.installed_location(reg) is None


# ---------------------------------------------------------------- removing


def test_remove_files_deletes_only_the_list_and_only_empty_folders(tmp_path):
    target = tmp_path / "Aitrade"
    plain_install(make_payload(tmp_path / "p.zip"), target)
    (target / "_internal" / "PySide6" / "user-added.txt").write_text("keep me")
    (tmp_path / "neighbour.txt").write_text("keep me too")
    failed = ins.remove_files(target, list(FILES_V1) + ["../neighbour.txt", "C:/Windows/notepad.exe", "ghost.dll"])
    assert failed == []
    assert listing(target) == {ins.MANIFEST, "_internal/PySide6/user-added.txt"}
    assert (tmp_path / "neighbour.txt").exists()
    assert not (target / "_internal" / "old").exists()  # empty, so it went


def test_files_that_cannot_be_deleted_are_reported(tmp_path):
    target = tmp_path / "Aitrade"
    plain_install(make_payload(tmp_path / "p.zip"), target)
    with open(target / "Aitrade.exe", "rb"):  # in use, like a running program
        failed = ins.remove_files(target, ["Aitrade.exe", "aitrade-worker.exe"])
    if sys.platform == "win32":
        assert failed == ["Aitrade.exe"]
    assert not (target / "aitrade-worker.exe").exists()


def test_uninstall_from_outside_removes_program_shortcuts_and_entry(tmp_path):
    target = tmp_path / "Aitrade"
    lnk = tmp_path / "Desktop" / "Aitrade.lnk"
    lnk.parent.mkdir()
    lnk.write_bytes(b"x")
    ins.install(make_payload(tmp_path / "p.zip"), target, version="1", make_shortcuts=lambda f, desktop: [str(lnk)],
                write_registry=lambda *a: None, running=lambda t: [])
    done = []
    failed = ins.uninstall(target, in_process=True, unregister=lambda: done.append("unregistered"))
    assert failed == [] and done == ["unregistered"]
    assert not target.exists() and not lnk.exists()


def test_uninstall_keeps_a_folder_that_still_holds_other_things(tmp_path):
    target = tmp_path / "Aitrade"
    plain_install(make_payload(tmp_path / "p.zip"), target)
    (target / "my-notes.txt").write_text("mine")
    ins.uninstall(target, in_process=True, unregister=lambda: None)
    assert listing(target) == {"my-notes.txt"}


def test_uninstall_needs_an_installation(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ins.InstallError, match="No Aitrade installation"):
        ins.uninstall(tmp_path / "empty", in_process=True, unregister=lambda: None)


def test_the_cleanup_script_names_every_file_and_never_deletes_a_whole_folder(tmp_path):
    target = tmp_path / "It's here" / "Aitrade"
    script = ins.cleanup_script(target, list(FILES_V1) + ["../../escape.txt"], wait_for_pid=4321)
    assert "Wait-Process -Id 4321" in script
    for name in FILES_V1:
        quoted = str(target / name).replace("'", "''")
        assert f"'{quoted}'" in script
    assert "It''s here" in script and "escape.txt" not in script
    assert "-Recurse" not in script and "rmdir" not in script.lower() and "rd /s" not in script.lower()
    assert script.rstrip().endswith("Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_cleanup_script_really_removes_the_program_and_nothing_else(tmp_path):
    target = tmp_path / "It's here" / "Aitrade"
    plain_install(make_payload(tmp_path / "p.zip"), target)
    (target.parent / "neighbour.txt").write_text("keep")
    script = tmp_path / "remove.ps1"
    script.write_text(ins.cleanup_script(target, ins.read_manifest(target)["files"]), encoding="utf-8-sig")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                    str(script)], check=True, timeout=120)
    assert not target.exists()
    assert (target.parent / "neighbour.txt").read_text() == "keep"
    assert not script.exists()  # it deletes itself too


def test_uninstall_from_the_running_program_hands_over_to_the_script(tmp_path):
    target = tmp_path / "Aitrade"
    plain_install(make_payload(tmp_path / "p.zip"), target)
    spawned, done = [], []
    assert ins.uninstall(target, wait_for_pid=999, spawn=spawned.append, unregister=lambda: done.append(1)) == []
    assert done == [1] and len(spawned) == 1
    text = spawned[0].read_text(encoding="utf-8-sig")
    assert "Wait-Process -Id 999" in text and str(target / "Aitrade.exe") in text
    assert (target / "Aitrade.exe").exists()  # the files go only after the program has closed
    spawned[0].unlink()
