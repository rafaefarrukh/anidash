"""Naming, path confinement and trash. Nothing is ever unlinked: everything goes through `gio trash`."""
import re
import subprocess
from pathlib import Path

from . import db


class PathError(Exception):
    pass


def library_root() -> Path:
    root = Path(str(db.setting("library_path"))).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize(name: str) -> str:
    name = re.sub(r"[/\\\x00-\x1f\x7f]", "", name or "")
    name = re.sub(r"\.{2,}", ".", name).lstrip(". ").strip()
    return name[:150] or "Untitled"


def confine(path: Path) -> Path:
    root = library_root()
    real = Path(path).expanduser().resolve()
    if real != root and root not in real.parents:
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
    subprocess.run(["gio", "trash", "--", str(p)], check=True, capture_output=True, timeout=60)
    return True


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in (".mkv", ".mp4")
