"""Start anidash at login: an XDG autostart entry on Linux, the HKCU Run key on Windows.
Starting at login (instead of a boot-time system service) also means the keyring is unlocked
by the time anidash asks for secrets."""
import os
import shlex
import shutil
import sys
from pathlib import Path

from . import APP

IS_WIN = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(*args: str) -> list[str]:
    """How to start this same installation again (frozen exe, pipx shim, or python -m)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = shutil.which(APP)
    if exe:
        return [exe, *args]
    return [sys.executable, "-m", "anidash", *args]


def _desktop_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / f"{APP}.desktop"


def is_enabled() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    return _desktop_file().exists()


def enable() -> None:
    cmd = launch_command("service")
    if IS_WIN:
        import subprocess
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
        return
    f = _desktop_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("[Desktop Entry]\nType=Application\nName=anidash\nComment=Anime episode automation (tray)\n"
                 f"Exec={shlex.join(cmd)}\nIcon=anidash\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                 "X-GNOME-Autostart-Delay=10\n")


def disable() -> None:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP)
        except OSError:
            pass
        return
    _desktop_file().unlink(missing_ok=True)


def set_enabled(on: bool) -> None:
    enable() if on else disable()


SYSTEMD_UNIT = """[Unit]
Description=anidash (headless) — anime episode automation
After=network-online.target

[Service]
Type=simple
ExecStart={exec}
Restart=on-failure
RestartSec=15
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
"""


def install_systemd() -> Path:
    """For servers/NAS boxes without a desktop session: a user unit running without the tray.
    Run `loginctl enable-linger $USER` so it starts at boot."""
    unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/anidash-headless.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(SYSTEMD_UNIT.format(exec=shlex.join(launch_command("service", "--no-tray"))))
    return unit


def install_desktop_entry() -> None:
    """pip/pipx installs don't ship a menu entry; add one (and the icon) for the current user.
    Skipped when a package (AUR, distro) already installed one system-wide."""
    if IS_WIN or getattr(sys, "frozen", False) or Path("/usr/share/applications/anidash.desktop").exists():
        return
    from . import paths
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    entry = data / "applications" / f"{APP}.desktop"
    icon = data / "icons/hicolor/512x512/apps" / f"{APP}.png"
    try:
        if not icon.exists():
            icon.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(paths.ASSETS_DIR / "icon.png", icon)
        text = ("[Desktop Entry]\nType=Application\nName=anidash\nGenericName=Anime episode automation\n"
                "Comment=Follow your AniList list: new episodes download, get named and synced\n"
                f"Exec={shlex.join(launch_command())}\nIcon={APP}\nTerminal=false\n"
                "Categories=AudioVideo;Video;Network;\nStartupWMClass=anidash\n")
        if not entry.exists() or entry.read_text() != text:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(text)
    except OSError:
        pass
