"""Find and launch VLC. If VLC is missing, fall back to the system's default video player."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import db

IS_WIN = sys.platform == "win32"


def _windows_vlc() -> str | None:
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        exe = winreg.QueryValueEx(k, None)[0]  # default value is the exe path
                        if exe and Path(exe).exists():
                            return exe
                except OSError:
                    continue
    except ImportError:
        pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            for p in (Path(base) / "VideoLAN/VLC/vlc.exe", Path(base) / "Programs/VideoLAN/VLC/vlc.exe"):
                if p.exists():
                    return str(p)
    return None


def find_vlc() -> list[str] | None:
    """Command prefix for VLC, or None."""
    custom = str(db.setting("vlc_path") or "").strip()
    if custom:
        return [custom] if Path(custom).exists() or shutil.which(custom) else None
    exe = shutil.which("vlc")
    if exe:
        return [exe]
    if IS_WIN:
        exe = _windows_vlc()
        return [exe] if exe else None
    flatpak = shutil.which("flatpak")
    if flatpak and subprocess.run([flatpak, "info", "org.videolan.VLC"], capture_output=True).returncode == 0:
        return [flatpak, "run", "org.videolan.VLC"]
    return None


def _detach_kwargs() -> dict:
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def play(path: Path) -> str:
    """Returns 'vlc' or 'default' (the program that was used)."""
    vlc = find_vlc()
    if vlc:
        args = shlex.split(str(db.setting("vlc_args") or ""), posix=not IS_WIN)
        subprocess.Popen([*vlc, *args, "--", str(path)], **_detach_kwargs())
        return "vlc"
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606 - opens with the user's default player
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("Neither VLC nor xdg-open was found")
        subprocess.Popen([opener, str(path)], **_detach_kwargs())
    return "default"
