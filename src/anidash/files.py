"""Naming, path confinement, moves and trash. Nothing is ever unlinked: everything goes to the OS trash
(freedesktop Trash on Linux, the Recycle Bin on Windows) through send2trash, or, where the library's volume
has none (network shares, cloud folders), into <library>/.anidash-trash/<date>/. Emptying that folder after
`managed_trash_days` is the only permanent delete anidash does, and it never leaves that folder.

Names are made Windows-safe on every OS, so a library on a partition shared between Linux and
Windows works from both."""
import logging
import os
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from send2trash import send2trash
from send2trash.exceptions import TrashPermissionError

from . import db, storage

log = logging.getLogger("anidash")
INCOMING, TRASH = ".anidash-incoming", ".anidash-trash"


class PathError(Exception):
    pass


class Locked(Exception):
    """The file is in use (a player has it open, or the engine still holds it); retried later."""


def _locked(e: OSError) -> bool:
    return getattr(e, "winerror", None) in (5, 32, 33) or (isinstance(e, PermissionError) and not isinstance(e, TrashPermissionError))


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_SUBST = {":": "", '"': "'", "<": "(", ">": ")", "|": "-", "?": "", "*": ""}


def library_root() -> Path:
    """Not created here: an unplugged drive must not turn into an empty folder on the system disk
    (jobs.check_library creates it once it knows the volume is the right one)."""
    return Path(str(db.setting("library_path"))).expanduser().resolve()


def sanitize(name: str) -> str:
    name = re.sub(r"\s*:\s+", " - ", name or "")  # "Title: Subtitle" -> "Title - Subtitle", "Re:Zero" -> "ReZero"
    for bad, good in _SUBST.items():
        name = name.replace(bad, good)
    name = re.sub(r"[/\\\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\.{2,}", ".", name)
    name = re.sub(r"\s{2,}", " ", name).strip().lstrip(". ").rstrip(". ")
    name = name[:150].rstrip(". ")
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name or "Untitled"


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def confine(path: Path) -> Path:
    root = library_root()
    real = Path(path).expanduser().resolve()
    r, p = _norm(root), _norm(real)
    if p != r and not p.startswith(r.rstrip(os.sep) + os.sep):
        raise PathError(f"refusing path outside library: {real}")
    return real


def show_dir(display_title: str) -> Path:
    return confine(library_root() / sanitize(display_title))


def episode_name(display_title: str, number: int, total: int | None, ext: str) -> str:
    width = 3 if (total or 0) > 99 else 2
    return f"{sanitize(display_title)} - {number:0{width}d}{ext.lower()}"


def trash(path: Path) -> bool:
    """OS trash where the library's volume has one, else the managed trash. Raises Locked if in use."""
    p = confine(path)
    if not p.exists():
        return False
    root = library_root()
    if storage.classify(root)["has_os_trash"]:
        try:
            send2trash(str(p))
            return True
        except TrashPermissionError as e:  # e.g. no .Trash-<uid> allowed on that mount
            log.info("no OS trash for %s (%s); using %s", p, e, TRASH)
        except OSError as e:
            if _locked(e):
                raise Locked(str(e)) from e
            raise
    _managed_trash(p, root)
    return True


def _free_name(dest: Path) -> Path:
    n, stem, suf = 2, dest.stem, dest.suffix
    while dest.exists():
        dest = dest.with_name(f"{stem} ({n}){suf}")
        n += 1
    return dest


def _managed_trash(p: Path, root: Path) -> None:
    dest = _free_name(root / TRASH / date.today().isoformat() / p.relative_to(root))
    dest.parent.mkdir(parents=True, exist_ok=True)
    hide(root / TRASH)
    try:
        os.replace(p, dest)
    except OSError as e:
        if _locked(e):
            raise Locked(str(e)) from e
        raise


def empty_managed_trash(days: int) -> int:
    """Permanently delete .anidash-trash/<date>/ folders older than `days` (0: never). Nothing else."""
    if days <= 0:
        return 0
    tdir = library_root() / TRASH
    cutoff, n = date.today() - timedelta(days=days), 0
    for d in tdir.iterdir() if tdir.is_dir() else []:
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if day < cutoff and d.is_dir() and not d.is_symlink() and d.resolve().parent == tdir.resolve():
            shutil.rmtree(d)
            n += 1
    return n


def move(src: Path, dst: Path) -> Path:
    """Atomic rename within the library (staging is on the same volume). An existing target goes to the
    trash first, never overwritten. Raises Locked if either file is in use."""
    src, dst = confine(src), confine(dst)
    if dst.exists() and dst != src:
        trash(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        if _locked(e):
            raise Locked(str(e)) from e
        raise
    return dst


def incoming(infohash: str) -> Path:
    """Where a torrent downloads: <library>/.anidash-incoming/<hash>/, on the library's volume, so
    finishing is a rename (no copy, no half-written file in the library)."""
    base = library_root() / INCOMING
    base.mkdir(parents=True, exist_ok=True)
    hide(base)
    return confine(base / infohash.lower())


def hide(path: Path) -> None:
    """Dot-folders are hidden on Linux; Windows needs the attribute."""
    if sys.platform == "win32" and path.exists():
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in (".mkv", ".mp4")
