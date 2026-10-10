"""qBittorrent (>= 5.0) via qbittorrent-api, which tracks the stop/start rename and POST-only delete."""
import threading
import time
from urllib.parse import quote

import qbittorrentapi

from . import config, db, vault

CATEGORY = "anidash"
TRACKERS = ["http://nyaa.tracker.wf:7777/announce", "udp://open.stealth.si:80/announce",
            "udp://tracker.opentrackr.org:1337/announce", "udp://exodus.desync.com:6969/announce"]
_client = None
_lock = threading.Lock()
_login_failed_at = 0.0
LOGIN_COOLDOWN = 600  # qBit bans an IP after 5 failures; never hammer it with bad credentials


def make(url: str | None = None) -> qbittorrentapi.Client:
    pw = vault.get("qbit_password")
    if not pw:  # wallet not up yet; a blank login would count toward qBit's IP ban
        raise RuntimeError("qBittorrent password not readable from the keyring yet (wallet locked or not running)")
    c = qbittorrentapi.Client(host=url or db.setting("qbit_url"), username=config.load()["qbit_username"],
                              password=pw, REQUESTS_ARGS={"timeout": (5, 20)},
                              DISABLE_LOGGING_DEBUG_OUTPUT=True)
    c.auth_log_in()
    return c


def client() -> qbittorrentapi.Client:
    global _client, _login_failed_at
    with _lock:
        if _client is None:
            wait = LOGIN_COOLDOWN - (time.time() - _login_failed_at)
            if wait > 0:
                raise RuntimeError(f"qBittorrent login failed recently; retrying in {int(wait // 60) + 1} min (fix the password in setup)")
            try:
                c = make()
            except (qbittorrentapi.LoginFailed, qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error):
                _login_failed_at = time.time()
                raise
            if CATEGORY not in c.torrents_categories():
                c.torrents_create_category(name=CATEGORY)
            _client = c
        return _client


def reset() -> None:
    global _client, _login_failed_at
    with _lock:
        _client = None
        _login_failed_at = 0.0


def magnet(infohash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{infohash}&dn={quote(name)}" + "".join(f"&tr={quote(t)}" for t in TRACKERS)


def add(infohash: str, name: str, save_path: str, anilist_id: int) -> None:
    res = client().torrents_add(
        urls=magnet(infohash, name), save_path=save_path, category=CATEGORY, tags=str(anilist_id),
        content_layout="NoSubfolder", use_auto_torrent_management=False,
        ratio_limit=float(db.setting("seed_ratio")), seeding_time_limit=int(float(db.setting("seed_hours")) * 60))
    if str(res).strip().lower().startswith("fail"):
        raise RuntimeError(f"qBittorrent refused the torrent: {res}")


def remove(hashes) -> None:
    hashes = [h for h in ([hashes] if isinstance(hashes, str) else hashes) if h]
    if hashes:
        client().torrents_delete(delete_files=False, torrent_hashes=hashes)  # we trash files ourselves
