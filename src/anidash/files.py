"""Naming, path confinement and trash. Nothing is ever unlinked: everything goes to the OS trash
(freedesktop Trash on Linux, the Recycle Bin on Windows) through send2trash.

Names are made Windows-safe on every OS, so a library on a partition shared between Linux and
Windows works from both."""
import os
import re
from pathlib import Path

from send2trash import send2trash

from . import db


class PathError(Exception):
    pass


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_SUBST = {":": "", '"': "'", "<": "(", ">": ")", "|": "-", "?": "", "*": ""}


def library_root() -> Path:
    root = Path(str(db.setting("library_path"))).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    p = confine(path)
    if not p.exists():
        return False
    send2trash(str(p))
    return True


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in (".mkv", ".mp4")
