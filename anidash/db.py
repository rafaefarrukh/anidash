"""SQLite storage. One connection per thread, WAL, autocommit unless inside tx()."""
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH

log = logging.getLogger("anidash")
_local = threading.local()

SCHEMA = """
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

SETTING_DEFAULTS = {
    "library_path": "/mnt/shared/Anime",
    "retention_days": 30,
    "fallback_hours": 24,
    "poll_minutes": 20,
    "give_up_days": 7,
    "nyaa_base": "https://nyaa.si",
    "qbit_url": "http://127.0.0.1:8080",
    "seed_ratio": 1.0,
    "seed_hours": 24,
    "vlc_args": "--fullscreen",
    "token_issued": 0,
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)
    DB_PATH.chmod(0o600)


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
def setting(key):
    r = one("SELECT value FROM settings WHERE key=?", key)
    return json.loads(r["value"]) if r else SETTING_DEFAULTS.get(key)


def settings_all() -> dict:
    out = dict(SETTING_DEFAULTS)
    out.update({r["key"]: json.loads(r["value"]) for r in q("SELECT key, value FROM settings")})
    return out


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
