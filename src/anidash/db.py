"""SQLite storage. One connection per thread, WAL, autocommit unless inside tx().
Schema changes are numbered migrations tracked with PRAGMA user_version (v1 databases are 0)."""
import json
import logging
import sqlite3
import threading
import time
import tomllib
from contextlib import contextmanager, suppress

from . import paths

log = logging.getLogger("anidash")
_local = threading.local()

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS anime(
  anilist_id INTEGER PRIMARY KEY, title_en TEXT, title_romaji TEXT, title_native TEXT,
  description TEXT, cover_url TEXT, site_url TEXT, total_episodes INTEGER, season TEXT,
  season_year INTEGER, airing_status TEXT, display_title TEXT NOT NULL, search_alias TEXT NOT NULL,
  episode_offset INTEGER NOT NULL DEFAULT 0, trusted_only INTEGER NOT NULL DEFAULT 1,
  probe_json TEXT, active INTEGER NOT NULL DEFAULT 1, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preference(
  anilist_id INTEGER NOT NULL REFERENCES anime ON DELETE CASCADE, rank INTEGER NOT NULL,
  grp TEXT NOT NULL, resolution INTEGER NOT NULL, PRIMARY KEY(anilist_id, rank));
CREATE TABLE IF NOT EXISTS episode(
  anilist_id INTEGER NOT NULL REFERENCES anime ON DELETE CASCADE, number INTEGER NOT NULL,
  airs_at REAL, state TEXT NOT NULL DEFAULT 'scheduled', chosen_group TEXT, version INTEGER,
  infohash TEXT, torrent_name TEXT, file_path TEXT, size_bytes INTEGER, downloaded_at REAL,
  delete_after REAL, anilist_synced INTEGER NOT NULL DEFAULT 0,
  replacing INTEGER NOT NULL DEFAULT 0, old_hash TEXT, old_path TEXT, queued_at REAL, search_from REAL,
  PRIMARY KEY(anilist_id, number));
CREATE INDEX IF NOT EXISTS episode_state ON episode(state);
CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, anilist_id INTEGER,
  episode INTEGER, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skipped(
  anilist_id INTEGER NOT NULL REFERENCES anime ON DELETE CASCADE, number INTEGER NOT NULL,
  infohash TEXT NOT NULL, PRIMARY KEY(anilist_id, number, infohash));
"""


def _m1(c: sqlite3.Connection) -> None:
    """v2: AniList progress at track time (episodes up to it are never fetched), replacement timing,
    v1's config.toml folded into settings, seeding settings dropped (torrents are removed on completion)."""
    c.execute("ALTER TABLE anime ADD COLUMN skip_through INTEGER NOT NULL DEFAULT 0")
    c.execute("ALTER TABLE episode ADD COLUMN replace_started REAL")
    c.execute("UPDATE episode SET replace_started=strftime('%s','now') WHERE replacing=1")
    c.execute("DELETE FROM settings WHERE key IN ('seed_ratio','seed_hours')")
    cfg_file = paths.CONFIG_DIR / "v1-config.toml"
    if cfg_file.exists():
        try:
            cfg = tomllib.loads(cfg_file.read_text())
            for k in ("anilist_client_id", "qbit_username", "port"):
                if cfg.get(k) not in (None, ""):
                    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, json.dumps(cfg[k])))
            # a v1 user already went through setup
            c.execute("INSERT OR REPLACE INTO settings VALUES('setup_done','true')")
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("could not read v1 config: %s", e)


def _m2(c: sqlite3.Connection) -> None:
    """Built-in torrent engine. Installs that already finished setup did it with qBittorrent: keep that."""
    if c.execute("SELECT 1 FROM settings WHERE key='setup_done' AND value='true'").fetchone():
        c.execute("INSERT OR IGNORE INTO settings VALUES('engine', '\"qbittorrent\"')")


def _m3(c: sqlite3.Connection) -> None:
    """File moves and trashing that hit a file in use (a player has it open) wait here and are retried
    on every poll, in order, instead of failing the episode."""
    c.execute("""CREATE TABLE IF NOT EXISTS pending_op(
      id INTEGER PRIMARY KEY, kind TEXT NOT NULL, src TEXT NOT NULL, dst TEXT, anilist_id INTEGER, number INTEGER,
      created REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_error TEXT)""")


def _m4(c: sqlite3.Connection) -> None:
    """Player choice. A VLC path set by hand becomes a custom player with its arguments; otherwise Auto,
    which still prefers VLC when it is installed."""
    def get(k):
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None
    path, args = str(get("vlc_path") or "").strip(), get("vlc_args")
    new = {"player": "custom", "player_path": path, "player_args": "--fullscreen" if args is None else str(args)} if path else {"player": "auto"}
    for k, v in new.items():
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, json.dumps(v)))
    c.execute("DELETE FROM settings WHERE key IN ('vlc_path','vlc_args')")


MIGRATIONS = [_m1, _m2, _m3, _m4]

SETTING_DEFAULTS = {
    "library_path": "",              # filled with paths.default_library() on first read
    "library_fp": None,              # {kind, fs_type} of the library's volume when it was chosen (jobs.check_library)
    "library_ack": "",               # the cloud-synced library path the user agreed to upload from
    "managed_trash_days": 14,        # .anidash-trash/<date>/ older than this is emptied; 0 = never
    "retention_days": 30,
    "fallback_hours": 24,
    "poll_minutes": 20,
    "give_up_days": 7,
    "backlog": True,                 # fetch already-aired episodes past your AniList progress when tracking
    "nyaa_base": "https://nyaa.si",
    "engine": "builtin",             # builtin (libtorrent) | qbittorrent
    "bt_port": 0,                    # built-in engine's listen port; 0: pick a random high port once
    "bt_interface": "",              # built-in engine: bind to this interface (VPN kill switch); empty: any
    "qbit_url": "http://127.0.0.1:8080",
    "qbit_username": "anidash",
    "update_check": "monthly",       # monthly | off (updates.check); "Check now" works either way
    "update_checked_at": 0,
    "update_latest": None,           # the newest release found: {version, url, published, setup, sums}
    "update_dismissed": "",          # a version whose banner was dismissed
    "player": "auto",                # auto | default | vlc | mpv | mpc-hc | mpc-be | custom (player.CHOICES)
    "player_path": "",               # custom player's executable
    "player_args": "",               # custom player's arguments; "{file}" marks where the file goes, else it's last
    "anilist_client_id": "",         # empty: the built-in public client (anilist.PUBLIC_CLIENT_ID)
    "token_issued": 0,
    "token_expires": 0,
    "port": 8787,
    "setup_done": False,
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is not None and getattr(_local, "path", None) != paths.DB_PATH:  # tests swap the data directory
        c.close()
        c = None
    if c is None:
        _local.path = paths.DB_PATH
        paths.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(paths.DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def close() -> None:
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None


def init() -> None:
    c = conn()
    c.executescript(BASE_SCHEMA)
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    for i, m in enumerate(MIGRATIONS[ver:], ver + 1):
        c.execute("BEGIN IMMEDIATE")
        try:
            m(c)
            c.execute(f"PRAGMA user_version={i}")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        log.info("database migrated to schema %d", i)
    with suppress(OSError):
        paths.DB_PATH.chmod(0o600)


def q(sql, *a):
    return conn().execute(sql, a).fetchall()


def one(sql, *a):
    return conn().execute(sql, a).fetchone()


def x(sql, *a):
    return conn().execute(sql, a)


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


# settings ------------------------------------------------------------------
def _default(key):
    if key == "library_path":
        return str(paths.default_library())
    return SETTING_DEFAULTS.get(key)


def setting(key):
    r = one("SELECT value FROM settings WHERE key=?", key)
    v = json.loads(r["value"]) if r else None
    return _default(key) if v in (None, "") and key in SETTING_DEFAULTS else v


def settings_all() -> dict:
    return {k: setting(k) for k in SETTING_DEFAULTS}


def set_settings(values: dict) -> None:
    with tx() as c:
        for k, v in values.items():
            c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))


# events --------------------------------------------------------------------
def event(level: str, message: str, anilist_id=None, episode=None) -> None:
    getattr(log, {"warn": "warning"}.get(level, level))(message)
    x("INSERT INTO event(ts, level, anilist_id, episode, message) VALUES(?,?,?,?,?)",
      time.time(), level, anilist_id, episode, message)
    x("DELETE FROM event WHERE ts < ?", time.time() - 90 * 86400)
