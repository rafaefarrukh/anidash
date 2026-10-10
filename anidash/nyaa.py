"""Nyaa RSS client. RSS only, no HTML scraping. Max 75 items, no paging, so queries stay narrow."""
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from . import __version__, db

NS = "{https://nyaa.si/xmlns/nyaa}"
_http = httpx.Client(timeout=20, headers={"User-Agent": f"anidash/{__version__}"}, follow_redirects=True)
_lock = threading.Lock()
_last = 0.0
_backoff_until = 0.0
UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
         "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


class NyaaBusy(Exception):
    pass


@dataclass
class Item:
    title: str
    infohash: str
    seeders: int
    size: int
    trusted: bool


def parse_size(s: str) -> int:
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", s or "", re.I)
    return int(float(m.group(1)) * UNITS.get(m.group(2).upper(), 1)) if m else 0


def search(query: str) -> list[Item]:
    global _last, _backoff_until
    if time.time() < _backoff_until:
        raise NyaaBusy(f"backing off until {time.strftime('%H:%M', time.localtime(_backoff_until))}")
    with _lock:  # one request at a time, >= 2 s apart
        gap = 2 - (time.time() - _last)
        if gap > 0:
            time.sleep(gap)
        base = str(db.setting("nyaa_base")).rstrip("/")
        try:
            r = _http.get(f"{base}/", params={"page": "rss", "q": query, "c": "1_2", "f": "0"})
        finally:
            _last = time.time()
    if r.status_code in (429, 403, 503):
        _backoff_until = time.time() + 15 * 60
        raise NyaaBusy(f"Nyaa HTTP {r.status_code} (blocked or rate-limited; VPN exit IP?)")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(NS + tag) or "").strip()
        h = g("infoHash").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            continue
        out.append(Item(title=(it.findtext("title") or "").strip(), infohash=h,
                        seeders=int(g("seeders") or 0), size=parse_size(g("size")),
                        trusted=g("trusted").lower() == "yes"))
    return out
