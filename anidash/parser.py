"""Release-name parsing. Uses anitomy-ng when installed, with a regex fallback tuned to
SubsPlease / Erai-raws / ToonsHub style names. Tests: tests/test_parser.py."""
import re
from dataclasses import dataclass

try:  # optional native parser
    import anitomy_ng as _anitomy  # type: ignore
except ImportError:  # pragma: no cover
    _anitomy = None

VIDEO_EXT = (".mkv", ".mp4")


@dataclass
class Parsed:
    group: str
    title: str
    episode: int
    episode_end: int | None
    resolution: int | None
    version: int
    ext: str | None


_RX = re.compile(r"^\[(?P<group>[^\]]+)\]\s*(?P<title>.+?)\s+-\s+(?P<ep>\d{1,4})(?:v(?P<ver>\d))?"
                 r"(?:\s*[-~]\s*(?P<end>\d{1,4}))?(?=[\s\[(.]|$)", re.I)
_SXE = re.compile(r"^\[(?P<group>[^\]]+)\]\s*(?P<title>.+?)\s+S\d{1,2}E(?P<ep>\d{1,4})(?:v(?P<ver>\d))?(?:-E?(?P<end>\d{1,4}))?\b", re.I)
_RES = re.compile(r"(?<!\d)(2160|1080|720|576|480|360)p", re.I)
_EXT = re.compile(r"\.(mkv|mp4|avi|webm|ts|zip|rar|7z|exe|scr)$", re.I)


def _from_anitomy(name: str) -> Parsed | None:
    try:
        d = _anitomy.parse(name)
        d = d if isinstance(d, dict) else {k: getattr(d, k) for k in dir(d) if not k.startswith("_")}
        ep = d.get("episode_number") or d.get("episode")
        if isinstance(ep, (list, tuple)):
            ep, end = ep[0], ep[-1] if len(ep) > 1 else None
        else:
            end = None
        res = re.sub(r"\D", "", str(d.get("video_resolution") or d.get("resolution") or "")) or None
        ext = d.get("file_extension") or d.get("extension")
        return Parsed(group=str(d.get("release_group") or ""), title=str(d.get("anime_title") or d.get("title") or ""),
                      episode=int(ep), episode_end=int(end) if end else None, resolution=int(res) if res else None,
                      version=int(d.get("release_version") or 1), ext=f".{ext}".lower() if ext else None)
    except Exception:
        return None


def parse(name: str) -> Parsed | None:
    if _anitomy is not None:
        p = _from_anitomy(name)
        if p and p.group:
            return p
    m = _RX.match(name.strip()) or _SXE.match(name.strip())
    if not m:
        return None
    res = _RES.search(name)
    ext = _EXT.search(name.strip())
    return Parsed(group=m["group"].strip(), title=m["title"].strip(), episode=int(m["ep"]),
                  episode_end=int(m["end"]) if m["end"] else None,
                  resolution=int(res.group(1)) if res else None, version=int(m["ver"] or 1),
                  ext=f".{ext.group(1).lower()}" if ext else None)
