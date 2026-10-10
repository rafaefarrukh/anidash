"""Tracked-show operations shared by the API and the jobs."""
import json
import time

from . import anilist, db, files


def upsert_media(m: dict, active: bool = True) -> None:
    t = m.get("title") or {}
    display = files.sanitize(t.get("english") or t.get("romaji") or str(m["id"]))
    with db.tx() as c:
        c.execute("""INSERT INTO anime(anilist_id, title_en, title_romaji, title_native, description, cover_url, site_url,
                       total_episodes, season, season_year, airing_status, display_title, search_alias, active, added_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(anilist_id) DO UPDATE SET title_en=excluded.title_en, title_romaji=excluded.title_romaji,
                       title_native=excluded.title_native, description=excluded.description, cover_url=excluded.cover_url,
                       site_url=excluded.site_url, total_episodes=excluded.total_episodes, season=excluded.season,
                       season_year=excluded.season_year, airing_status=excluded.airing_status""",
                  (m["id"], t.get("english"), t.get("romaji"), t.get("native"), anilist.clean_desc(m.get("description")),
                   (m.get("coverImage") or {}).get("large"), m.get("siteUrl"), m.get("episodes"), m.get("season"),
                   m.get("seasonYear"), m.get("status"), display, t.get("romaji") or display, int(active), time.time()))
        # no default release groups: nothing downloads until you probe and pick one


def track(mid: int) -> None:
    m = anilist.media(mid)
    anilist.save_entry(mid, status="CURRENT")  # Track = Watching, immediately
    upsert_media(m)
    title = (m["title"] or {}).get("english") or m["title"]["romaji"]
    db.event("info", f"Tracking {title} · AniList status → Watching", mid)


def save_probe(mid: int, found: dict) -> None:
    db.x("UPDATE anime SET probe_json=? WHERE anilist_id=?", json.dumps(found), mid)
