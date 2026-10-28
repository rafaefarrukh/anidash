"""Where the library lives: local disk, removable drive, network share or a cloud-synced folder.
That decides whether the OS trash can be used, and what Setup/Settings warn about. Every OS call sits
behind a small helper so each branch is unit-tested on Linux."""
import json
import ntpath
import os
import re
import shutil
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
NETWORK_FS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "fuse.gvfsd-fuse", "9p", "afs", "ceph", "glusterfs")
CLOUD_FS = {"fuse.rclone": "rclone", "fuse.onedriver": "OneDrive", "fuse.google-drive-ocamlfuse": "Google Drive"}
SYS_BLOCK = Path("/sys/class/block")
DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 2, 3, 4, 5, 6


def existing(path: Path) -> Path:
    """The path itself or its nearest existing ancestor (a library folder may not exist yet)."""
    p = Path(path).expanduser().absolute()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _under(path: Path, root: str | Path | None) -> bool:
    if not root:
        return False
    a, b = os.path.normcase(str(Path(path).absolute())), os.path.normcase(str(Path(root).absolute()))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


# -- cloud folders (both OSes) -------------------------------------------------------
def _dropbox_roots() -> list[str]:
    files = [Path.home() / ".dropbox" / "info.json"]
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            files.append(Path(os.environ[env]) / "Dropbox" / "info.json")
    roots = []
    for f in files:
        try:
            roots += [v["path"] for v in json.loads(f.read_text()).values() if isinstance(v, dict) and v.get("path")]
        except (OSError, ValueError):
            continue
    return roots


def cloud_provider(path: Path) -> str | None:
    """A sync client's folder: everything written there is uploaded."""
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if _under(path, os.environ.get(env)):
            return "OneDrive"
    if any(_under(path, r) for r in _dropbox_roots()):
        return "Dropbox"
    home = Path.home()
    if _under(path, home / "iCloudDrive"):
        return "iCloud Drive"
    if not IS_WIN:  # Linux sync clients' default folders
        return next((d for d in ("OneDrive", "Dropbox", "Google Drive", "pCloudDrive", "MEGA") if _under(path, home / d)), None)
    return None


# -- Windows ---------------------------------------------------------------------------
def _win_volume(root: str) -> tuple[int, str, str]:
    """(GetDriveTypeW, file system name, volume label) for a drive root like 'C:\\'."""
    import ctypes
    k32 = ctypes.windll.kernel32
    dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
    label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
    if not k32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, None, None, None, fs, 261):
        return dtype, "", ""
    return dtype, fs.value, label.value


def _classify_win(p: Path) -> dict:
    s = str(p)
    root = ntpath.splitdrive(s)[0]
    if s.startswith("\\\\"):  # UNC: \\server\share
        return {"kind": "network", "fs_type": "smb", "has_os_trash": False, "mount": root.lower()}
    dtype, fs, label = _win_volume(root + "\\")
    if fs.lower() == "googledrivefs" or label.lower() == "google drive":
        return {"kind": "cloud", "cloud_provider": "Google Drive", "fs_type": fs, "has_os_trash": False, "mount": root.upper()}
    kind = {DRIVE_REMOVABLE: "removable", DRIVE_REMOTE: "network", DRIVE_CDROM: "removable"}.get(dtype, "local")
    return {"kind": kind, "fs_type": fs, "has_os_trash": dtype == DRIVE_FIXED, "mount": root.upper()}


# -- Linux -------------------------------------------------------------------------------
def _mountinfo() -> list[tuple[str, str, str]]:
    """(mount point, fs type, source) from /proc/self/mountinfo."""
    out = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        left, _, right = line.partition(" - ")
        f, r = left.split(), right.split()
        if len(f) >= 5 and len(r) >= 2:
            mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), f[4])  # "\040" is a space
            out.append((mnt, r[0], r[1]))
    return out


def _block_removable(source: str) -> bool:
    """/dev/sdb1 → is sdb removable media, or attached over USB (USB disks often report removable=0)?"""
    if not source.startswith("/dev/"):
        return False
    dev = SYS_BLOCK / Path(source).name
    try:
        real = Path(os.path.realpath(dev))
    except OSError:
        return False
    if "/usb" in str(real):
        return True
    for d in (real, real.parent):
        try:
            return (d / "removable").read_text().strip() == "1"
        except OSError:
            continue
    return False


def _classify_linux(p: Path) -> dict:
    s = str(p)
    best = max((m for m in _mountinfo() if _under(s, m[0])), key=lambda m: len(m[0]), default=("/", "", ""))
    mount, fs, source = best
    if fs in CLOUD_FS:
        return {"kind": "cloud", "cloud_provider": CLOUD_FS[fs], "fs_type": fs, "has_os_trash": False, "mount": mount}
    if fs.startswith(NETWORK_FS):
        return {"kind": "network", "fs_type": fs, "has_os_trash": False, "mount": mount}
    if _block_removable(source):  # send2trash keeps a .Trash-<uid> on the drive itself
        return {"kind": "removable", "fs_type": fs, "has_os_trash": True, "mount": mount}
    return {"kind": "local", "fs_type": fs, "has_os_trash": True, "mount": mount}


# -- public ------------------------------------------------------------------------------
def classify(path) -> dict:
    """{kind: local|removable|network|cloud, cloud_provider, fs_type, mount, free_bytes, has_os_trash, writable, exists}"""
    p = Path(path).expanduser().absolute()
    base = existing(p)
    info = _classify_win(base) if IS_WIN else _classify_linux(base)
    info.setdefault("cloud_provider", None)
    provider = cloud_provider(p)
    if provider and info["kind"] != "cloud":
        info.update(kind="cloud", cloud_provider=provider)
    try:
        info["free_bytes"] = shutil.disk_usage(base).free
    except OSError:
        info["free_bytes"] = None
    info["writable"] = os.access(base, os.W_OK)
    info["exists"] = p.exists()
    return info


LOW_SPACE = 10e9


def warnings(info: dict) -> list[str]:
    """What Setup and Settings show under the folder picker."""
    out = []
    if info["kind"] == "cloud":
        out.append(f"This folder is synced by {info['cloud_provider'] or 'a cloud client'}: every episode would be uploaded.")
    elif info["kind"] == "network":
        out.append("Network folder: no Recycle Bin/Trash (anidash keeps its own trash folder), and downloads pause while it is offline.")
    elif info["kind"] == "removable":
        out.append("Removable drive: downloads pause while it is unplugged.")
    if not info["writable"]:
        out.append("anidash cannot write to this folder.")
    if info["free_bytes"] is not None and info["free_bytes"] < LOW_SPACE:
        out.append(f"Only {info['free_bytes'] / 1e9:.1f} GB free.")
    return out


def fingerprint(info: dict) -> dict:
    """What must stay the same for the library to count as 'the same place' (see jobs.check_library).
    The mount point matters: an unmounted second disk leaves an empty mount-point folder on the system disk."""
    return {"kind": info["kind"], "fs_type": info["fs_type"], "mount": info.get("mount")}
