"""Release matching, tracking mid-season, give-up clocks and replacements, with AniList, Nyaa
and the download engine faked."""
import time
from types import SimpleNamespace

import pytest

from anidash import db, jobs, nyaa, shows
from anidash.engine import File, Torrent

DAY = 86400
MID = 4242


def item(title, size=1.4e9, seeders=50, trusted=True, h=None):
    h = h or format(abs(hash(title)) % (16 ** 40), "040x")
    return nyaa.Item(title=title, infohash=h, seeders=seeders, size=int(size), trusted=trusted)


@pytest.fixture()
def show(home):
    m = {"id": MID, "title": {"english": "Test Show", "romaji": "Tesuto Sho"}, "episodes": 12, "status": "RELEASING"}
    shows.upsert_media(m)
    with db.tx() as c:
        c.execute("INSERT INTO preference VALUES(?,?,?,?)", (MID, 1, "SubsPlease", 1080))
        c.execute("INSERT INTO preference VALUES(?,?,?,?)", (MID, 2, "Erai-raws", 1080))
    return db.one("SELECT * FROM anime WHERE anilist_id=?", MID)


@pytest.fixture()
def fake_nyaa(monkeypatch):
    jobs._run_cache.clear()
    feed = {}
    calls = []

    def search(q):
        calls.append(q)
        return [it for key, items in feed.items() if key in q for it in items]
    monkeypatch.setattr(nyaa, "search", search)
    return SimpleNamespace(feed=feed, calls=calls)


@pytest.fixture()
def fake_qbit(monkeypatch, fake_engine):
    monkeypatch.setattr(jobs, "_disk_ok", lambda size: True)
    return fake_engine.added


def ep(n):
    return db.one("SELECT * FROM episode WHERE anilist_id=? AND number=?", MID, n)


def add_ep(n, airs_at, state="scheduled", **kw):
    cols = {"anilist_id": MID, "number": n, "airs_at": airs_at, "state": state, **kw}
    db.x(f"INSERT INTO episode({','.join(cols)}) VALUES({','.join('?' * len(cols))})", *cols.values())


def test_candidates_rank_and_filters(show, fake_nyaa):
    fake_nyaa.feed["SubsPlease"] = [
        item("[SubsPlease] Tesuto Sho - 05 (1080p) [A].mkv", seeders=10),
        item("[SubsPlease] Tesuto Sho - 05v2 (1080p) [B].mkv", seeders=5),
        item("[SubsPlease] Tesuto Sho - 05 (720p) [C].mkv"),                  # wrong resolution
        item("[SubsPlease] Tesuto Sho - 06 (1080p) [D].mkv"),                 # wrong episode
        item("[SubsPlease] Tesuto Sho - 05 (1080p) [E].mkv", trusted=False),  # untrusted
        item("[SubsPlease] Tesuto Sho - 05 (1080p) [F].mkv", size=10e6),      # too small
        item("[SubsPlease] Tesuto Sho - 05 (1080p) [G].exe"),                 # not a video
        item("[SubsPlease] Tesuto Sho - 04-05 (1080p) [H].mkv"),              # double episode
    ]
    fake_nyaa.feed["Erai-raws"] = [item("[Erai-raws] Tesuto Sho - 05 [1080p][Multiple Subtitle]", seeders=999)]
    c = jobs._candidates(show, 5, jobs._prefs(MID))
    assert [x[3].title for x in c] == ["[SubsPlease] Tesuto Sho - 05v2 (1080p) [B].mkv",
                                       "[SubsPlease] Tesuto Sho - 05 (1080p) [A].mkv",
                                       "[Erai-raws] Tesuto Sho - 05 [1080p][Multiple Subtitle]"]


def test_offset(show, fake_nyaa):
    db.x("UPDATE anime SET episode_offset=12 WHERE anilist_id=?", MID)
    a = db.one("SELECT * FROM anime WHERE anilist_id=?", MID)
    fake_nyaa.feed["SubsPlease"] = [item("[SubsPlease] Tesuto Sho - 13 (1080p) [A].mkv")]
    assert len(jobs._candidates(a, 1, jobs._prefs(MID))) == 1
    assert jobs._candidates(a, 13, jobs._prefs(MID)) == []


def test_fallback_only_after_24h(show, fake_nyaa, fake_qbit):
    now = time.time()
    add_ep(5, now - 3600)          # aired 1 h ago: preference 1 only
    fake_nyaa.feed["Erai-raws"] = [item("[Erai-raws] Tesuto Sho - 05 [1080p]")]
    jobs.poll_releases()
    assert ep(5)["state"] == "searching" and not fake_qbit
    db.x("UPDATE episode SET airs_at=? WHERE anilist_id=? AND number=5", now - 25 * 3600, MID)
    jobs.poll_releases()
    assert ep(5)["state"] == "queued" and ep(5)["chosen_group"] == "Erai-raws" and len(fake_qbit) == 1


def test_one_query_per_show_for_a_backlog(show, fake_nyaa, fake_qbit):
    now = time.time()
    for n in range(1, 6):
        add_ep(n, now - (8 - n) * DAY)
    fake_nyaa.feed["SubsPlease"] = [item(f"[SubsPlease] Tesuto Sho - {n:02d} (1080p) [{n}].mkv") for n in range(1, 6)]
    jobs.poll_releases()
    assert all(ep(n)["state"] == "queued" for n in range(1, 6))
    assert len([q for q in fake_nyaa.calls if q.startswith("SubsPlease")]) == 1


def test_give_up_clock_starts_at_tracking(show, fake_nyaa, fake_qbit):
    now = time.time()
    db.x("UPDATE anime SET added_at=? WHERE anilist_id=?", now - DAY, MID)  # tracked yesterday
    add_ep(1, now - 30 * DAY)   # aired a month ago: must not fail on the first poll
    jobs.poll_releases()
    assert ep(1)["state"] == "searching"
    db.x("UPDATE anime SET added_at=? WHERE anilist_id=?", now - 8 * DAY, MID)
    jobs.poll_releases()
    assert ep(1)["state"] == "failed"


def test_sync_schedule_skips_watched_and_respects_backlog(home, monkeypatch):
    now = time.time()
    shows.upsert_media({"id": MID, "title": {"romaji": "Tesuto"}, "episodes": 12}, progress=3)
    db.x("UPDATE anime SET added_at=? WHERE anilist_id=?", now, MID)
    nodes = [{"episode": n, "airingAt": int(now + (n - 6.5) * DAY)} for n in range(1, 9)]  # 1..6 aired, 7..8 upcoming
    monkeypatch.setattr(jobs.anilist, "schedules", lambda ids: [{"id": MID, "episodes": 12, "status": "RELEASING", "nodes": nodes}])
    jobs.sync_schedule([MID])
    states = {r["number"]: r["state"] for r in db.q("SELECT number, state FROM episode")}
    assert [states[n] for n in range(1, 9)] == ["skipped"] * 3 + ["scheduled"] * 5

    db.x("DELETE FROM episode")
    db.set_settings({"backlog": False})
    jobs.sync_schedule([MID])
    states = {r["number"]: r["state"] for r in db.q("SELECT number, state FROM episode")}
    assert [states[n] for n in range(1, 9)] == ["skipped"] * 6 + ["scheduled"] * 2


def test_stalled_replacement_keeps_v1(show, monkeypatch, home):
    now = time.time()
    add_ep(5, now - DAY, state="done", infohash="b" * 40, old_hash="a" * 40, old_path=str(home / "Anime/x.mkv"),
           file_path=str(home / "Anime/x.mkv"), replacing=1, replace_started=now - 3600, version=2, downloaded_at=now - DAY)
    jobs.watch_downloads()  # the v2 is not in the engine
    r = ep(5)
    assert r["replacing"] == 0 and r["infohash"] == "a" * 40 and r["state"] == "done" and r["version"] == 1
    assert db.one("SELECT 1 FROM skipped WHERE infohash=?", "b" * 40)


def test_watch_downloads_renames_and_syncs(show, monkeypatch, home, fake_engine):
    now = time.time()
    folder = home / "Anime" / "Test Show"
    folder.mkdir(parents=True)
    add_ep(5, now - DAY, state="downloading", infohash="c" * 40, queued_at=now - 600)
    src = "[SubsPlease] Tesuto Sho - 05 (1080p) [A].mkv"
    (folder / src).write_bytes(b"\0" * 1000)
    pushed = []
    fake_engine.on_rename = lambda old, new: (folder / old).rename(folder / new)
    fake_engine.items = [Torrent("c" * 40, src, "uploading", 1.0, str(folder), 0, files=[File(src, 1000)])]
    monkeypatch.setattr(jobs.anilist, "push_progress", lambda mid, n: pushed.append(n) or n)
    jobs.watch_downloads()
    r = ep(5)
    assert r["state"] == "done" and r["file_path"].endswith("Test Show - 05.mkv") and r["anilist_synced"] == 1
    assert fake_engine.renamed == ["Test Show - 05.mkv"] and fake_engine.removed == ["c" * 40] and pushed == [5]
    assert r["delete_after"] - r["downloaded_at"] == pytest.approx(30 * DAY)
