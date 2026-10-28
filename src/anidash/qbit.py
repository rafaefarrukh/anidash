"""The optional qBittorrent backend behind engine.QbitEngine: its Web API via qbittorrent-api (tracks the
v5 stop/start rename and POST-only delete). Works with qBittorrent 4.4+ and 5.x."""
import threading
import time

import qbittorrentapi

from . import db, vault
from .engine import magnet

CATEGORY = "anidash"
_client = None
_lock = threading.Lock()
_login_failed_at = 0.0
LOGIN_COOLDOWN = 600  # qBit bans an IP after 5 failed logins; never hammer it with bad credentials
LOGIN_ERRORS = (qbittorrentapi.LoginFailed, qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error)


def make(url: str | None = None, username: str | None = None, password: str | None = None) -> qbittorrentapi.Client:
    pw = password if password is not None else vault.get("qbit_password")
    if not pw:  # wallet not up yet; a blank login would count toward qBit's IP ban
        raise RuntimeError("qBittorrent password not readable yet (keyring locked, or setup not finished)")
    c = qbittorrentapi.Client(host=url or db.setting("qbit_url"), username=username or db.setting("qbit_username"),
                              password=pw, REQUESTS_ARGS={"timeout": (5, 20)}, DISABLE_LOGGING_DEBUG_OUTPUT=True,
                              VERIFY_WEBUI_CERTIFICATE=True)
    c.auth_log_in()
    return c


def client() -> qbittorrentapi.Client:
    global _client, _login_failed_at
    with _lock:
        if _client is None:
            wait = LOGIN_COOLDOWN - (time.time() - _login_failed_at)
            if wait > 0:
                raise RuntimeError(f"qBittorrent login failed recently; retrying in {int(wait // 60) + 1} min "
                                   "(fix the login in Settings → qBittorrent)")
            try:
                c = make()
            except LOGIN_ERRORS:
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


def add(infohash: str, name: str, save_path: str, anilist_id: int) -> None:
    res = client().torrents_add(
        urls=magnet(infohash, name), save_path=save_path, category=CATEGORY, tags=str(anilist_id),
        content_layout="NoSubfolder", use_auto_torrent_management=False)
    if str(res).strip().lower().startswith("fail"):
        raise RuntimeError(f"qBittorrent refused the torrent: {res}")


def remove(hashes) -> None:
    hashes = [h for h in ([hashes] if isinstance(hashes, str) else hashes) if h]
    if hashes:
        client().torrents_delete(delete_files=False, torrent_hashes=hashes)  # we trash files ourselves


# -- network binding (VPN) ------------------------------------------------------
def interfaces(c: qbittorrentapi.Client | None = None) -> dict:
    c = c or client()
    prefs = c.app_preferences()
    return {"current": prefs.get("current_network_interface", ""),
            "list": [{"name": i.get("name", ""), "value": i.get("value", "")} for i in c.app_network_interface_list()],
            "bypass_local_auth": bool(prefs.get("bypass_local_auth")),
            "version": c.app_version().lstrip("v")}


def bind_interface(value: str, c: qbittorrentapi.Client | None = None) -> None:
    """'' = any interface. Binding to the VPN interface is the kill switch: if the VPN drops,
    qBittorrent stops transferring instead of falling back to your real IP."""
    c = c or client()
    c.app_set_preferences({"current_network_interface": value, "current_interface_address": ""})
