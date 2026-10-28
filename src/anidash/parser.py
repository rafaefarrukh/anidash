"""Release-name parsing for weekly fansub/raw releases, e.g.

    [SubsPlease] Title - 05 (1080p) [A1B2C3D4].mkv
    [Erai-raws] Title - 05v2 [1080p CR WEB-DL AVC AAC][MultiSub][ABCD1234]
    [ToonsHub] Title S01E05 1080p WEB-DL

v1 optionally used anitomy-ng; v2 drops it (its Python API differs between builds and it has
no wheels for every Windows/Python pair). The fixtures in tests/test_parser.py are the contract."""
import re
from dataclasses import dataclass

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
                 r"(?:\s*[-~]\s*(?P<end>\d{1,4})(?:v\d)?)?(?=[\s\[(.]|$)", re.I)
_SXE = re.compile(r"^\[(?P<group>[^\]]+)\]\s*(?P<title>.+?)\s+S\d{1,2}E(?P<ep>\d{1,4})(?:v(?P<ver>\d))?"
                  r"(?:-?E(?P<end>\d{1,4}))?(?=[\s\[(.]|$)", re.I)
_RES = re.compile(r"(?<![\dx])(2160|1080|720|576|480|360)p", re.I)
_RES_WXH = re.compile(r"(?<!\d)\d{3,4}x(2160|1080|720|576|480|360)(?!\d)", re.I)
_EXT = re.compile(r"\.(mkv|mp4|avi|webm|ts|zip|rar|7z|exe|scr|bat|lnk)$", re.I)
_BATCH = re.compile(r"\b(batch|complete)\b", re.I)


def parse(name: str) -> Parsed | None:
    name = (name or "").strip()
    m = _RX.match(name) or _SXE.match(name)
    if not m or _BATCH.search(name):
        return None
    res = _RES.search(name) or _RES_WXH.search(name)
    ext = _EXT.search(name)
    end = int(m["end"]) if m["end"] else None
    if end is not None and end <= int(m["ep"]):
        end = None  # "05 - 05" is not a range
    return Parsed(group=m["group"].strip(), title=m["title"].strip(), episode=int(m["ep"]),
                  episode_end=end, resolution=int(res.group(1)) if res else None,
                  version=int(m["ver"] or 1), ext=f".{ext.group(1).lower()}" if ext else None)
