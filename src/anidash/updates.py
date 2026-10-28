"""Update check against GitHub Releases: monthly (or off) plus "Check now". Nothing installs by itself.
On Windows, *Update* downloads the release's setup.exe, verifies it against the release's SHA256SUMS.txt
and runs it silently (it stops anidash, upgrades and starts it again). Elsewhere it shows the command."""
import hashlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__, db

REPO = "rafaefarrukh/anidash"
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
EVERY = 30 * 86400
_VER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_STAGE = {"a": 0, "b": 1, "rc": 2, None: 3}


def parse(v: str) -> tuple | None:
    """'2.0.0rc1' < '2.0.0' < '2.0.1'. None for anything else."""
    m = _VER.match(v.strip())
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3]), _STAGE[m[4]], int(m[5] or 0)


def is_prerelease(v: str) -> bool:
    p = parse(v)
    return bool(p) and p[3] < 3


def _fetch() -> list[dict]:
    r = httpx.get(RELEASES, headers={"Accept": "application/vnd.github+json", "User-Agent": f"anidash/{__version__}"},
                  timeout=15, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def _pick(releases: list[dict], current: str) -> dict | None:
    """The newest release we'd offer: never drafts; pre-releases only to people already running one."""
    pre_ok, best = is_prerelease(current), None
    for rel in releases:
        v = parse(rel.get("tag_name") or "")
        if not v or rel.get("draft") or (rel.get("prerelease") and not pre_ok):
            continue
        if best is None or v > best[0]:
            best = (v, rel)
    return best[1] if best else None


def _asset(rel: dict, pred) -> str | None:
    return next((a["browser_download_url"] for a in rel.get("assets", []) if pred(a["name"])), None)


def check(force: bool = False) -> dict | None:
    """Ask GitHub (monthly unless forced); returns the cached result otherwise."""
    if not force and (db.setting("update_check") == "off" or time.time() - float(db.setting("update_checked_at") or 0) < EVERY):
        return db.setting("update_latest")
    rel = _pick(_fetch(), __version__)
    info = None
    if rel:
        info = {"version": rel["tag_name"].lstrip("v"), "url": rel.get("html_url"), "published": rel.get("published_at"),
                "setup": _asset(rel, lambda n: n.lower().endswith("-setup.exe")),
                "sums": _asset(rel, lambda n: n == "SHA256SUMS.txt")}
    db.set_settings({"update_checked_at": time.time(), "update_latest": info})
    return info


def available() -> dict | None:
    """The cached newer release, if it is newer than this one and not dismissed."""
    info = db.setting("update_latest")
    if not info or (parse(info["version"]) or ()) <= (parse(__version__) or ()):
        return None
    if db.setting("update_dismissed") == info["version"]:
        return None
    return {**info, "how": how_to_update()}


def how_to_update() -> dict:
    """Windows installs (frozen): the in-app updater. Otherwise the package manager's command."""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return {"kind": "installer", "command": None}
    arch = Path(__file__).resolve().as_posix().startswith("/usr/lib/")  # AUR / system package
    return {"kind": "command", "command": "paru -Syu anidash" if arch else "pipx upgrade anidash"}


def _github(url: str | None) -> str:
    if not url or urlparse(url).scheme != "https" or urlparse(url).hostname != "github.com" or f"/{REPO}/releases/download/" not in url:
        raise ValueError(f"not a {REPO} release asset: {url}")
    return url


def download_verified(info: dict, dest_dir: Path | None = None) -> Path:
    """setup.exe into a fresh temp folder, checked against SHA256SUMS.txt of the same release."""
    setup_url, sums_url = _github(info.get("setup")), _github(info.get("sums"))
    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": f"anidash/{__version__}"}) as c:
        sums = c.get(sums_url)
        sums.raise_for_status()
        name = setup_url.rsplit("/", 1)[1]
        want = next((ln.split()[0].lower() for ln in sums.text.splitlines() if ln.split()[-1:] == [name] or ln.endswith(" *" + name)), None)
        if not want:
            raise ValueError(f"{name} is not listed in SHA256SUMS.txt")
        dest = Path(dest_dir or tempfile.mkdtemp(prefix="anidash-update-")) / name
        h = hashlib.sha256()
        with c.stream("GET", setup_url) as r, open(dest, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 16):
                h.update(chunk)
                f.write(chunk)
    if h.hexdigest() != want:
        dest.unlink(missing_ok=True)
        raise ValueError("the downloaded installer does not match SHA256SUMS.txt; nothing was run")
    return dest


def install(info: dict) -> Path:
    """Run the verified installer silently (argument list, detached). The installer stops anidash itself
    (`anidash quit --wait`) and starts it again after a silent upgrade."""
    setup = download_verified(info)
    kw = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    subprocess.Popen([str(setup), "/SILENT", "/NORESTART"], **kw)
    return setup
