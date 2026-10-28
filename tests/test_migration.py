import json
import sqlite3

from anidash import db, paths

V1_SCHEMA = """
CREATE TABLE anime(anilist_id INTEGER PRIMARY KEY, title_en TEXT, title_romaji TEXT, title_native TEXT,
  description TEXT, cover_url TEXT, site_url TEXT, total_episodes INTEGER, season TEXT, season_year INTEGER,
  airing_status TEXT, display_title TEXT NOT NULL, search_alias TEXT NOT NULL, episode_offset INTEGER NOT NULL DEFAULT 0,
  trusted_only INTEGER NOT NULL DEFAULT 1, probe_json TEXT, active INTEGER NOT NULL DEFAULT 1, added_at REAL NOT NULL);
CREATE TABLE episode(anilist_id INTEGER NOT NULL, number INTEGER NOT NULL, airs_at REAL, state TEXT NOT NULL DEFAULT 'scheduled',
  chosen_group TEXT, version INTEGER, infohash TEXT, torrent_name TEXT, file_path TEXT, size_bytes INTEGER, downloaded_at REAL,
  delete_after REAL, anilist_synced INTEGER NOT NULL DEFAULT 0, replacing INTEGER NOT NULL DEFAULT 0, old_hash TEXT,
  old_path TEXT, queued_at REAL, search_from REAL, PRIMARY KEY(anilist_id, number));
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO anime(anilist_id, title_en, display_title, search_alias, added_at) VALUES(1, 'Old Show', 'Old Show', 'Old Show', 0);
INSERT INTO episode(anilist_id, number, state, replacing) VALUES(1, 1, 'done', 1);
INSERT INTO settings VALUES('library_path', '"/mnt/shared/Anime"');
INSERT INTO settings VALUES('seed_ratio', '1.0');
"""


def test_v1_database_upgrades(home):
    db.close()
    paths.DB_PATH.unlink()
    for ext in ("-wal", "-shm"):
        paths.DB_PATH.with_name(paths.DB_PATH.name + ext).unlink(missing_ok=True)
    c = sqlite3.connect(paths.DB_PATH)
    c.executescript(V1_SCHEMA)
    c.close()
    (paths.CONFIG_DIR / "v1-config.toml").write_text('port = 8787\nanilist_client_id = "12345"\nqbit_username = "rafae"\n')
    db.init()
    assert db.one("PRAGMA user_version")[0] == len(db.MIGRATIONS)
    assert db.one("SELECT skip_through FROM anime WHERE anilist_id=1")["skip_through"] == 0
    assert db.one("SELECT replace_started FROM episode")["replace_started"] is not None  # stuck v1 replacements time out
    assert db.setting("library_path") == "/mnt/shared/Anime"
    assert db.setting("anilist_client_id") == "12345" and db.setting("qbit_username") == "rafae"
    assert db.setting("setup_done") is True
    assert db.setting("engine") == "qbittorrent"  # set up with qBittorrent in v1: keeps it
    assert db.one("SELECT 1 FROM settings WHERE key='seed_ratio'") is None
    db.init()  # idempotent
    assert json.loads(db.one("SELECT value FROM settings WHERE key='port'")["value"]) == 8787


def _at_schema_1(setup_done: bool) -> None:
    db.close()
    c = sqlite3.connect(paths.DB_PATH)
    c.execute("DELETE FROM settings")
    if setup_done:
        c.execute("INSERT INTO settings VALUES('setup_done', 'true')")
    c.execute("PRAGMA user_version=1")
    c.commit()
    c.close()
    db.init()


def test_engine_migration_keeps_qbittorrent_for_finished_setups(home):
    _at_schema_1(setup_done=True)
    assert db.setting("engine") == "qbittorrent"
    _at_schema_1(setup_done=False)
    assert db.setting("engine") == "builtin"  # new users get the built-in engine
    assert db.one("PRAGMA user_version")[0] == len(db.MIGRATIONS)


def test_pending_op_table(home):
    _at_schema_1(setup_done=False)  # runs migrations 2 and 3 again from schema 1
    cols = {r["name"] for r in db.q("PRAGMA table_info(pending_op)")}
    assert {"kind", "src", "dst", "anilist_id", "number", "created", "tries", "last_error"} <= cols
    assert db.setting("managed_trash_days") == 14
