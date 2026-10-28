"""Which player opens an episode: the user's choice in Settings → Player, or Auto = the first anime-capable
player found (VLC → mpv → MPC-HC → MPC-BE), else the system's default player. Windows' built-in player renders
ASS subtitles badly, so falling back to it on Windows comes with a one-time hint to install VLC."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import db

IS_WIN = sys.platform == "win32"
CHOICES = ("auto", "default", "vlc", "mpv", "mpc-hc", "mpc-be", "custom")
LABELS = {"vlc": "VLC", "mpv": "mpv", "mpc-hc": "MPC-HC", "mpc-be": "MPC-BE"}
AUTO_ORDER = ("vlc", "mpv", "mpc-hc", "mpc-be")
ARGS = {"vlc": ["--fullscreen"], "mpv": ["--fs"], "mpc-hc": ["/fullscreen"], "mpc-be": ["/fullscreen"]}
END_OF_OPTIONS = {"vlc", "mpv"}  # accept "--" before the file, so a name starting with "-" is never an option
VLC_INSTALL = ["winget", "install", "-e", "--id", "VideoLAN.VLC", "--accept-source-agreements", "--accept-package-agreements"]


# -- detection (each returns a command prefix or None; Windows parts behind small helpers for tests) --
def _reg_value(hive: str, sub: str, name: str | None) -> str | None:
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, hive), sub) as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


def _first_file(*candidates) -> str | None:
    return next((str(p) for p in candidates if p and Path(p).is_file()), None)


def _env_paths(*rel: str) -> list[Path]:
    """rel under Program Files, Program Files (x86) and %LOCALAPPDATA%\\Programs."""
    out = []
    for env, prefix in (("ProgramFiles", ""), ("ProgramFiles(x86)", ""), ("LOCALAPPDATA", "Programs")):
        if os.environ.get(env):
            out += [Path(os.environ[env]) / prefix / r for r in rel]
    return out


def _flatpak(app: str) -> list[str] | None:
    fp = shutil.which("flatpak")
    if fp and subprocess.run([fp, "info", app], capture_output=True).returncode == 0:
        return [fp, "run", app]
    return None


def _vlc() -> list[str] | None:
    if exe := shutil.which("vlc"):
        return [exe]
    if IS_WIN:
        reg = [_reg_value(h, s, None) for h in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER")
               for s in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC")]
        exe = _first_file(*reg, *_env_paths("VideoLAN/VLC/vlc.exe"))
        return [exe] if exe else None
    return _flatpak("org.videolan.VLC")


def _mpv() -> list[str] | None:
    if exe := shutil.which("mpv"):
        return [exe]
    if IS_WIN:  # portable zip in Programs, scoop and winget shims
        home, local = Path.home(), Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        exe = _first_file(local / "Programs/mpv/mpv.exe", home / "scoop/shims/mpv.exe", local / "Microsoft/WinGet/Links/mpv.exe",
                          *_env_paths("mpv/mpv.exe"))
        return [exe] if exe else None
    return _flatpak("io.mpv.Mpv")


def _mpc(key: str) -> list[str] | None:
    if not IS_WIN:
        return None
    if key == "mpc-hc":
        exe = _first_file(_reg_value("HKEY_CURRENT_USER", r"Software\MPC-HC\MPC-HC", "ExePath"),
                          *_env_paths("MPC-HC/mpc-hc64.exe", "MPC-HC/mpc-hc.exe", "K-Lite Codec Pack/MPC-HC64/mpc-hc64.exe"))
    else:
        exe = _first_file(_reg_value("HKEY_CURRENT_USER", r"Software\MPC-BE", "ExePath"),
                          _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\MPC-BE", "ExePath"),
                          *_env_paths("MPC-BE x64/mpc-be64.exe", "MPC-BE/mpc-be64.exe", "MPC-BE/mpc-be.exe"))
    return [exe] if exe else None


DETECT = {"vlc": _vlc, "mpv": _mpv, "mpc-hc": lambda: _mpc("mpc-hc"), "mpc-be": lambda: _mpc("mpc-be")}


def detect() -> dict[str, list[str] | None]:
    return {k: fn() for k, fn in DETECT.items()}


# -- arguments ------------------------------------------------------------------------
def split_args(s: str, win: bool | None = None) -> list[str]:
    """User-typed arguments. Windows quoting keeps backslashes ("C:\\x y" stays one argument, quotes removed)."""
    if not (IS_WIN if win is None else win):
        return shlex.split(s)
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(s, posix=False)]


def command(key: str, prefix: list[str], path: Path) -> list[str]:
    if key == "custom":
        args = split_args(str(db.setting("player_args") or ""))
        if any("{file}" in a for a in args):  # the user decides where the file goes
            return [*prefix, *(a.replace("{file}", str(path)) for a in args)]
        return [*prefix, *args, str(path)]
    return [*prefix, *ARGS[key], *(["--"] if key in END_OF_OPTIONS else []), str(path)]


# -- choosing and launching -------------------------------------------------------------
def resolve() -> tuple[str, list[str] | None]:
    """(player key, command prefix); ('default', None) means the system's default player."""
    choice = db.setting("player")
    if choice == "custom":
        p = str(db.setting("player_path") or "").strip()
        exe = p if p and Path(p).is_file() else shutil.which(p) if p else None
        if not exe:
            raise FileNotFoundError(f"Custom player not found: {p or '(no path set)'}")
        return "custom", [exe]
    if choice in DETECT:
        prefix = DETECT[choice]()
        if not prefix:
            raise FileNotFoundError(f"{LABELS[choice]} is not installed (choose another player in Settings)")
        return choice, prefix
    if choice == "auto":
        for key in AUTO_ORDER:
            if prefix := DETECT[key]():
                return key, prefix
    return "default", None


def _detach_kwargs() -> dict:
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def play(path: Path) -> str:
    """Opens the episode; returns the player key used ('vlc', …, 'custom', 'default')."""
    key, prefix = resolve()
    if prefix:
        subprocess.Popen(command(key, prefix, path), **_detach_kwargs())
        return key
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606 - the user's default player
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("No video player found (install VLC or mpv)")
        subprocess.Popen([opener, str(path)], **_detach_kwargs())
    return "default"


def wants_hint(used: str) -> bool:
    """Auto fell back to Windows' own player, whose subtitle rendering is poor: suggest VLC (once)."""
    return IS_WIN and used == "default" and db.setting("player") == "auto" and not db.setting("player_hint_dismissed")


def install_vlc() -> dict:
    """winget when it exists (an argument list in its own console window, never a shell); else the download page."""
    winget = shutil.which("winget") if IS_WIN else None
    if not winget:
        return {"started": False, "url": "https://www.videolan.org/vlc/"}
    kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if IS_WIN else {}
    subprocess.Popen([winget, *VLC_INSTALL[1:]], **kw)
    return {"started": True, "url": None}
