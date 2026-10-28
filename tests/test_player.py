"""Phase 5: player choice. Detection is faked, so every branch (Windows registry and paths too) runs anywhere."""
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anidash import api, db, player, scheduler

TOKEN = "t" * 43
H = {"X-Anidash-Token": TOKEN}


@pytest.fixture()
def installed(monkeypatch):
    """Which players 'exist': {key: command prefix}."""
    found = {}
    monkeypatch.setattr(player, "DETECT", {k: (lambda k=k: found.get(k)) for k in player.AUTO_ORDER})
    return found


@pytest.fixture()
def launched(monkeypatch):
    calls = []

    def popen(argv, **kw):
        assert isinstance(argv, list) and not kw.get("shell")  # never a shell
        calls.append(argv)
    monkeypatch.setattr(player.subprocess, "Popen", popen)
    return calls


EP = Path("/lib/Show/Show - 05.mkv")


# -- choosing --------------------------------------------------------------------------
@pytest.mark.parametrize("have, expect", [
    ({"vlc": ["vlc"], "mpv": ["mpv"], "mpc-hc": ["mpc"]}, "vlc"),
    ({"mpv": ["mpv"], "mpc-be": ["be"]}, "mpv"),
    ({"mpc-be": ["be"], "mpc-hc": ["hc"]}, "mpc-hc"),
    ({"mpc-be": ["be"]}, "mpc-be"),
    ({}, "default"),
])
def test_auto_order(home, installed, have, expect):
    installed.update(have)
    assert player.resolve()[0] == expect


def test_explicit_choice_must_be_installed(home, installed):
    installed["vlc"] = ["vlc"]
    db.set_settings({"player": "mpv"})
    with pytest.raises(FileNotFoundError, match="mpv is not installed"):
        player.resolve()
    db.set_settings({"player": "default"})
    assert player.resolve() == ("default", None)  # even with VLC installed


def test_custom_player(home, tmp_path, launched):
    exe = tmp_path / "My Player" / "pläyer.exe"  # spaces and non-ASCII
    db.set_settings({"player": "custom", "player_path": str(exe)})
    with pytest.raises(FileNotFoundError, match="Custom player not found"):
        player.resolve()
    exe.parent.mkdir()
    exe.write_text("")
    db.set_settings({"player_args": "--profile=anime -f"})
    assert player.play(EP) == "custom" and launched[-1] == [str(exe), "--profile=anime", "-f", str(EP)]
    db.set_settings({"player_args": "--open {file} --fullscreen"})
    player.play(EP)
    assert launched[-1] == [str(exe), "--open", str(EP), "--fullscreen"]


@pytest.mark.parametrize("key, prefix, expect", [
    ("vlc", ["/usr/bin/vlc"], ["/usr/bin/vlc", "--fullscreen", "--", str(EP)]),
    ("mpv", ["flatpak", "run", "io.mpv.Mpv"], ["flatpak", "run", "io.mpv.Mpv", "--fs", "--", str(EP)]),
    ("mpc-hc", ["C:/MPC-HC/mpc-hc64.exe"], ["C:/MPC-HC/mpc-hc64.exe", "/fullscreen", str(EP)]),  # MPC has no "--"
    ("mpc-be", ["C:/MPC-BE/mpc-be64.exe"], ["C:/MPC-BE/mpc-be64.exe", "/fullscreen", str(EP)]),
])
def test_commands(home, key, prefix, expect):
    assert player.command(key, prefix, EP) == expect


# -- arguments ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, win, expect", [
    ('--sub-file "a b.ass" -f', False, ["--sub-file", "a b.ass", "-f"]),
    ("--x='single quoted' \\-y", False, ["--x=single quoted", "-y"]),
    ('"C:\\Program Files\\subs\\a b.ass" /play', True, ["C:\\Program Files\\subs\\a b.ass", "/play"]),
    ("C:\\subs\\a.ass /fullscreen", True, ["C:\\subs\\a.ass", "/fullscreen"]),  # posix would eat the backslashes
    ("", True, []),
])
def test_split_args(text, win, expect):
    assert player.split_args(text, win=win) == expect


# -- launching and the default player ----------------------------------------------------
def test_default_player_linux(home, installed, launched, monkeypatch):
    monkeypatch.setattr(player, "IS_WIN", False)
    monkeypatch.setattr(player.shutil, "which", lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None)
    assert player.play(EP) == "default" and launched == [["/usr/bin/xdg-open", str(EP)]]
    monkeypatch.setattr(player.shutil, "which", lambda n: None)
    with pytest.raises(FileNotFoundError):
        player.play(EP)


def test_default_player_windows_and_the_hint(home, installed, monkeypatch):
    opened = []
    monkeypatch.setattr(player, "IS_WIN", True)
    monkeypatch.setattr(player.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(player, "_detach_kwargs", lambda: {})
    assert player.play(EP) == "default" and opened == [str(EP)]
    assert player.wants_hint("default")
    assert not player.wants_hint("vlc")
    db.set_settings({"player": "default"})
    assert not player.wants_hint("default")  # chosen on purpose: no nagging
    db.set_settings({"player": "auto", "player_hint_dismissed": True})
    assert not player.wants_hint("default")
    monkeypatch.setattr(player, "IS_WIN", False)
    db.set_settings({"player_hint_dismissed": False})
    assert not player.wants_hint("default")  # Linux default players render ASS fine


def test_install_vlc(monkeypatch, launched):
    monkeypatch.setattr(player, "IS_WIN", True)
    monkeypatch.setattr(player.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(player.shutil, "which", lambda n: "C:/winget.exe" if n == "winget" else None)
    assert player.install_vlc() == {"started": True, "url": None}
    assert launched == [["C:/winget.exe", "install", "-e", "--id", "VideoLAN.VLC", "--accept-source-agreements", "--accept-package-agreements"]]
    monkeypatch.setattr(player.shutil, "which", lambda n: None)
    assert player.install_vlc() == {"started": False, "url": "https://www.videolan.org/vlc/"}


# -- detection ---------------------------------------------------------------------------
@pytest.fixture()
def win(monkeypatch, tmp_path):
    monkeypatch.setattr(player, "IS_WIN", True)
    monkeypatch.setattr(player.shutil, "which", lambda n: None)
    for env, sub in (("ProgramFiles", "pf"), ("ProgramFiles(x86)", "pf86"), ("LOCALAPPDATA", "local")):
        monkeypatch.setenv(env, str(tmp_path / sub))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    reg = {}
    monkeypatch.setattr(player, "_reg_value", lambda hive, sub, name: reg.get((hive, sub, name)))
    return tmp_path, reg


def touch(p: Path) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return str(p)


def test_windows_vlc_from_registry_then_program_files(win):
    tmp, reg = win
    assert player._vlc() is None
    exe = touch(tmp / "pf86" / "VideoLAN/VLC/vlc.exe")
    assert player._vlc() == [exe]
    reg_exe = touch(tmp / "elsewhere" / "vlc.exe")
    reg[("HKEY_LOCAL_MACHINE", r"SOFTWARE\VideoLAN\VLC", None)] = reg_exe
    assert player._vlc() == [reg_exe]


@pytest.mark.parametrize("where", ["local/Programs/mpv/mpv.exe", "home/scoop/shims/mpv.exe", "local/Microsoft/WinGet/Links/mpv.exe", "pf/mpv/mpv.exe"])
def test_windows_mpv_locations(win, where):
    tmp, _ = win
    assert player._mpv() is None
    exe = touch(tmp / where)
    assert player._mpv() == [exe]


def test_windows_mpc_hc_and_be(win):
    tmp, reg = win
    assert player._mpc("mpc-hc") is None and player._mpc("mpc-be") is None
    hc = touch(tmp / "pf" / "MPC-HC/mpc-hc64.exe")
    be = touch(tmp / "pf" / "MPC-BE x64/mpc-be64.exe")
    assert player._mpc("mpc-hc") == [hc] and player._mpc("mpc-be") == [be]
    custom_hc = touch(tmp / "tools" / "mpc-hc64.exe")
    reg[("HKEY_CURRENT_USER", r"Software\MPC-HC\MPC-HC", "ExePath")] = custom_hc
    assert player._mpc("mpc-hc") == [custom_hc]
    reg[("HKEY_CURRENT_USER", r"Software\MPC-BE", "ExePath")] = str(tmp / "gone.exe")  # stale registry entry
    assert player._mpc("mpc-be") == [be]


def test_linux_path_and_flatpak(monkeypatch):
    monkeypatch.setattr(player, "IS_WIN", False)
    monkeypatch.setattr(player.shutil, "which", lambda n: {"mpv": "/usr/bin/mpv", "flatpak": "/usr/bin/flatpak"}.get(n))
    monkeypatch.setattr(player.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0 if argv[-1] == "org.videolan.VLC" else 1))
    assert player._mpv() == ["/usr/bin/mpv"]
    assert player._vlc() == ["/usr/bin/flatpak", "run", "org.videolan.VLC"]
    assert player._mpc("mpc-hc") is None  # Windows-only players


# -- API -------------------------------------------------------------------------------------
@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    api.configure(8787, TOKEN)
    with TestClient(api.app, base_url="http://127.0.0.1:8787") as c:
        yield c


def test_api_lists_players_and_validates(client, installed):
    installed["mpv"] = ["/usr/bin/mpv"]
    r = client.get("/api/player").json()
    assert r["choice"] == "auto" and r["auto"] == "mpv"
    assert [(p["key"], p["path"]) for p in r["players"]] == [("vlc", None), ("mpv", "/usr/bin/mpv"), ("mpc-hc", None), ("mpc-be", None)]
    assert client.patch("/api/settings", json={"player": "winamp"}, headers=H).status_code == 422
    assert client.patch("/api/settings", json={"player": "mpv"}, headers=H).json()["player"] == "mpv"


def test_api_play_reports_player_and_missing_choice(client, home, installed, launched):
    f = home / "Anime" / "S" / "S - 01.mkv"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    db.x("INSERT INTO anime(anilist_id, display_title, search_alias, added_at) VALUES(1,'S','S',0)")
    db.x("INSERT INTO episode(anilist_id, number, state, file_path) VALUES(1,1,'done',?)", str(f))
    installed["vlc"] = ["/usr/bin/vlc"]
    r = client.post("/api/play/1/1", headers=H).json()
    assert r["player"] == "vlc" and r["label"] == "VLC" and r["hint"] is False and launched[-1][-1] == str(f.resolve())
    db.set_settings({"player": "mpc-hc"})
    r = client.post("/api/play/1/1", headers=H)
    assert r.status_code == 409 and "MPC-HC is not installed" in r.json()["detail"]
    assert client.post("/api/player/hint/dismiss", headers=H).status_code == 200 and db.setting("player_hint_dismissed") is True
