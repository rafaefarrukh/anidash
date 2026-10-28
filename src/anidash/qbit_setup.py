"""First-run qBittorrent auto-setup.

qBittorrent ships with the Web UI off (desktop) or on with a random temporary password (5.x).
Rather than asking people to click through Options, we:
  1. find the config file (native, Flatpak, Windows %APPDATA%),
  2. wait until qBittorrent is closed (it rewrites the file on exit),
  3. enable the Web UI on 127.0.0.1, set a dedicated user with a random password
     (PBKDF2-SHA512, 100k iterations, 16-byte salt; the format qBittorrent >= 4.2 reads),
     keep localhost authentication ON,
  4. back up the original file, start qBittorrent again, and log in.
"""
import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

IS_WIN = sys.platform == "win32"
PROC_NAMES = {"qbittorrent", "qbittorrent-nox", "qbittorrent.exe", "qbittorrent-nox.exe"}
FLATPAK_ID = "org.qbittorrent.qBittorrent"


@dataclass
class Install:
    kind: str                 # "native" | "nox" | "flatpak" | "windows"
    config: str               # path to qBittorrent.conf / .ini
    command: list[str] = field(default_factory=list)  # how to start it ([] = unknown)
    config_exists: bool = False


def pbkdf2_value(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()})"


def verify_pbkdf2(password: str, value: str) -> bool:
    m = re.fullmatch(r'"?@ByteArray\(([^:]+):([^)]+)\)"?', value.strip())
    if not m:
        return False
    salt, dk = base64.b64decode(m[1]), base64.b64decode(m[2])
    return secrets.compare_digest(hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000), dk)


# -- discovery ----------------------------------------------------------------
def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _windows_exe() -> str | None:
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\qBittorrent", r"SOFTWARE\WOW6432Node\qBittorrent"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        d = winreg.QueryValueEx(k, "InstallLocation")[0]
                        if d and (Path(d) / "qbittorrent.exe").exists():
                            return str(Path(d) / "qbittorrent.exe")
                except OSError:
                    continue
    except ImportError:
        pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            for p in (Path(base) / "qBittorrent/qbittorrent.exe", Path(base) / "Programs/qBittorrent/qbittorrent.exe"):
                if p.exists():
                    return str(p)
    return shutil.which("qbittorrent")


def detect() -> list[Install]:
    """Every qBittorrent installation we can see, most likely first."""
    found: list[Install] = []
    if IS_WIN:
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        cfg = appdata / "qBittorrent" / "qBittorrent.ini"
        exe = _windows_exe()
        if exe or cfg.exists():
            found.append(Install("windows", str(cfg), [exe] if exe else [], cfg.exists()))
        return found
    native_cfg = _xdg_config() / "qBittorrent" / "qBittorrent.conf"
    if shutil.which("qbittorrent"):
        found.append(Install("native", str(native_cfg), [shutil.which("qbittorrent")], native_cfg.exists()))
    if shutil.which("qbittorrent-nox"):
        found.append(Install("nox", str(native_cfg), [shutil.which("qbittorrent-nox"), "--daemon"], native_cfg.exists()))
    flatpak = shutil.which("flatpak")
    if flatpak and subprocess.run([flatpak, "info", FLATPAK_ID], capture_output=True).returncode == 0:
        cfg = Path.home() / ".var/app" / FLATPAK_ID / "config/qBittorrent/qBittorrent.conf"
        found.append(Install("flatpak", str(cfg), [flatpak, "run", FLATPAK_ID], cfg.exists()))
    return found


def running() -> list[dict]:
    out = []
    for p in psutil.process_iter(["pid", "name", "username", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if name in PROC_NAMES or (name.startswith("qbittorrent") and len(name) <= 20) or FLATPAK_ID in cmd:
            out.append({"pid": p.info["pid"], "name": name})
    return out


def systemd_unit_active() -> str | None:
    """qbittorrent-nox started by systemd restarts itself; we must not race it."""
    if IS_WIN or not shutil.which("systemctl"):
        return None
    for scope, unit in ((["--user"], "qbittorrent-nox.service"), ([], f"qbittorrent-nox@{os.environ.get('USER', '')}.service")):
        r = subprocess.run(["systemctl", *scope, "is-active", unit], capture_output=True, text=True)
        if r.stdout.strip() == "active":
            return ("systemctl --user " if scope else "sudo systemctl ") + f"restart {unit}"
    return None


# -- config editing -------------------------------------------------------------
def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def read_section(path: Path, section: str) -> dict[str, str]:
    out, cur = {}, None
    for line in _read_lines(path):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif cur == section and "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
    return out


def set_keys(path: Path, section: str, values: dict[str, str]) -> None:
    """Line-based edit of a QSettings INI file: replace or append keys in one section, leave
    everything else byte-for-byte (configparser would lowercase keys and mangle QSettings values)."""
    lines = _read_lines(path)
    remaining = dict(values)
    out, cur, sec_end = [], None, None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur == section:
                sec_end = len(out)
            cur = s[1:-1]
        elif cur == section and "=" in s:
            k = s.split("=", 1)[0]
            if k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
                continue
        out.append(line)
    if cur == section:
        sec_end = len(out)
    adds = [f"{k}={v}" for k, v in remaining.items()]
    if sec_end is None:
        if out and out[-1].strip():
            out.append("")
        out += [f"[{section}]", *adds]
    else:
        while sec_end > 0 and not out[sec_end - 1].strip():  # keep the blank line between sections
            sec_end -= 1
        out[sec_end:sec_end] = adds
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".anidash-tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def configure(inst: Install, username: str, password: str, port: int = 8080) -> dict:
    """Write the Web UI settings. qBittorrent must be closed. Returns {'backup': path|None, 'url': ...}."""
    if running():
        raise RuntimeError("qBittorrent is still running. Quit it first (File → Exit, or the tray icon → Exit).")
    cfg = Path(inst.config)
    backup = None
    if cfg.exists():
        backup = cfg.with_name(cfg.name + f".anidash-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(cfg, backup)
    set_keys(cfg, "Preferences", {
        "WebUI\\Enabled": "true",
        "WebUI\\Address": "127.0.0.1",
        "WebUI\\Port": str(port),
        "WebUI\\Username": username,
        "WebUI\\Password_PBKDF2": f'"{pbkdf2_value(password)}"',
        "WebUI\\LocalHostAuth": "true",            # never bypass auth for localhost
        "WebUI\\HostHeaderValidation": "true",
        "WebUI\\CSRFProtection": "true",
    })
    set_keys(cfg, "LegalNotice", {"Accepted": "true"})  # qBit 4.x shows a blocking dialog otherwise
    return {"backup": str(backup) if backup else None, "url": f"http://127.0.0.1:{port}"}


def start(inst: Install) -> bool:
    if not inst.command:
        return False
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(inst.command, **kw)
    return True


def report() -> dict:
    """Everything the setup wizard shows."""
    insts = detect()
    return {"installs": [asdict(i) for i in insts], "running": running(), "systemd": systemd_unit_active(),
            "install_hint": install_hint()}


def install_hint() -> str:
    if IS_WIN:
        return "winget install qBittorrent.qBittorrent"
    if shutil.which("pacman"):
        return "sudo pacman -S qbittorrent"
    if shutil.which("apt"):
        return "sudo apt install qbittorrent"
    if shutil.which("dnf"):
        return "sudo dnf install qbittorrent"
    if shutil.which("zypper"):
        return "sudo zypper install qbittorrent"
    return "flatpak install flathub " + FLATPAK_ID
