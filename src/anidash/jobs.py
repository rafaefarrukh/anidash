"""Scheduled jobs (Outline §5) and release matching (§6)."""
import contextlib
import functools
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import anilist, db, engine, files, nyaa, shows, storage
from .parser import parse

log = logging.getLogger("anidash.jobs")
DAY, HOUR, MIN = 86400, 3600, 60
_locks: dict[str, threading.Lock] = {}
LAST: dict[str, float] = {}
RUNNING: set[str] = set()
MANUAL: set[str] = set()  # runs started from the dashboard; these get start/finish entries in the log
FINISHED: dict[str, tuple[float, str | None]] = {}  # name -> (end time, error or None)


def job(fn):
    name = fn.__name__
    _locks[name] = threading.Lock()

    @functools.wraps(fn)
    def run(*a, **kw):
        with _locks[name]:
            t0, err = time.time(), None
            RUNNING.add(name)
            manual, label = name in MANUAL, name.replace("_", " ").capitalize()
            MANUAL.discard(name)
            if manual:
                db.event("info", f"{label} started")
            try:
                return fn(*a, **kw)
            except anilist.AuthError as e:
                err = str(e)
                db.event("error", f"{name}: {e}")
            except nyaa.NyaaBusy as e:
                err = str(e)
                db.event("warn", f"{name}: {e}")
            except Exception as e:  # keep the scheduler alive, but surface it
                err = f"{type(e).__name__}: {e}"
                log.exception("%s failed", name)
                db.event("error", f"{name} failed: {err}")
            finally:
                RUNNING.discard(name)
                FINISHED[name] = (time.time(), err)
                if manual:
                    db.event("info", f"{label} {'ended with an error' if err else 'finished'} ({time.time() - t0:.0f} s)")
                LAST[name] = t0
                db.set_settings({f"job_last_{name}": t0})
    return run


# -- sync_schedule ----------------------------------------------------------
@job
def sync_schedule(ids: list[int] | None = None) -> None:
    if ids is None:
        me = anilist.viewer()
        tracked = {r["anilist_id"] for r in db.q("SELECT anilist_id FROM anime")}
        for e in anilist.current_list(me["id"]):
            m = e["media"]
            if m["id"] not in tracked and m["status"] in ("RELEASING", "NOT_YET_RELEASED"):
                shows.upsert_media(m, progress=e.get("progress") or 0)
                db.event("info", f"Imported {(m['title'] or {}).get('english') or m['title']['romaji']} from your AniList Watching list", m["id"])
        ids = [r["anilist_id"] for r in db.q("SELECT anilist_id FROM anime WHERE active=1")]
    if not ids:
        return
    upcoming, backlog = 0, bool(db.setting("backlog"))
    for s in anilist.schedules(ids):
        a = db.one("SELECT skip_through, added_at FROM anime WHERE anilist_id=?", s["id"])
        if not a:
            continue
        with db.tx() as c:
            c.execute("UPDATE anime SET total_episodes=?, airing_status=? WHERE anilist_id=?", (s["episodes"], s["status"], s["id"]))
            for n in s["nodes"]:
                # New rows: already watched on AniList -> skipped; aired before you tracked the show ->
                # fetched only with the backlog setting on (its give-up clock starts at tracking time).
                skip = n["episode"] <= a["skip_through"] or (not backlog and n["airingAt"] < a["added_at"])
                c.execute("INSERT OR IGNORE INTO episode(anilist_id, number, airs_at, state) VALUES(?,?,?,?)",
                          (s["id"], n["episode"], n["airingAt"], "skipped" if skip else "scheduled"))
                c.execute("UPDATE episode SET airs_at=? WHERE anilist_id=? AND number=? AND state IN ('scheduled','searching')",
                          (n["airingAt"], s["id"], n["episode"]))
                upcoming += n["airingAt"] > time.time()
    db.event("info", f"Schedule synced · {len(ids)} shows, {upcoming} upcoming episodes")


# -- poll_releases ----------------------------------------------------------
def _prefs(mid: int):
    return db.q("SELECT rank, grp, resolution FROM preference WHERE anilist_id=? ORDER BY rank", mid)


_run_cache: dict[str, list] = {}


def _search(query: str) -> list:
    """Queries are per show (group + alias + resolution), not per episode: one request serves a
    whole backlog. The cache lives for one poll run."""
    if query not in _run_cache:
        _run_cache[query] = nyaa.search(query)
    return _run_cache[query]


def _candidates(a, number: int, prefs, min_version: int = 1):
    seen = {r["infohash"] for r in db.q("SELECT infohash FROM skipped WHERE anilist_id=? AND number=?", a["anilist_id"], number)}
    out = []
    for pr in prefs:
        for it in _search(f"{pr['grp']} {a['search_alias']} {pr['resolution']}p"):
            if it.infohash in seen:
                continue
            seen.add(it.infohash)
            p = parse(it.title)
            if not p or p.episode_end is not None:  # batches and double episodes are skipped for weekly
                continue
            if p.episode - a["episode_offset"] != number or p.version < min_version:
                continue
            for r in prefs:
                if p.group.lower() == r["grp"].lower() and p.resolution == r["resolution"]:
                    ok = ((it.trusted or not a["trusted_only"]) and 50e6 <= it.size <= 3e9
                          and (p.ext is None or p.ext in (".mkv", ".mp4")))
                    if ok:
                        out.append((r["rank"], -p.version, -it.seeders, it, p))
                    break
    out.sort(key=lambda c: c[:3])
    return out


def _disk_ok(size: int) -> bool:
    return shutil.disk_usage(storage.existing(files.library_root())).free > size * 1.2 + 1e9


# -- library health ------------------------------------------------------------
LIBRARY: dict = {"problem": None, "cleaned": False}


def check_library() -> str | None:
    """Is the library where it was chosen? An unplugged drive or unmounted share pauses downloads
    (instead of creating an empty Anime folder on the system disk); they resume by themselves once it's back."""
    root = files.library_root()
    info = storage.classify(root)
    fp, now_fp = db.setting("library_fp"), storage.fingerprint(info)
    had_files = db.one("SELECT 1 FROM episode WHERE file_path IS NOT NULL")
    problem = None
    if not info["exists"] and (fp and fp != now_fp or not fp and had_files):
        problem = f"The library folder {root} is not available (drive unplugged or network share not mounted). Downloads are paused until it is back."
    elif fp and fp != now_fp:
        problem = (f"The library folder {root} is now on different storage ({fp['kind']} {fp['fs_type']} → {info['kind']} {info['fs_type']}). "
                   "Downloads are paused; if this is intended, save the folder again in Settings.")
    elif not info["writable"]:
        problem = f"anidash cannot write to the library folder {root}. Fix its permissions; downloads resume by themselves."
    else:
        if not fp:
            db.set_settings({"library_fp": now_fp})
        root.mkdir(parents=True, exist_ok=True)
    if problem != LIBRARY["problem"]:
        db.event("error" if problem else "info", problem or "Library folder is available again; downloads resume")
        LIBRARY["problem"] = problem
    if not problem and not LIBRARY["cleaned"]:
        LIBRARY["cleaned"] = True
        _clean_incoming()
    return problem


def _clean_incoming() -> None:
    """Once per start: staging folders no torrent or waiting move uses any more go to the trash."""
    base = files.library_root() / files.INCOMING
    live = {r["infohash"].lower() for r in db.q("""SELECT infohash FROM episode WHERE infohash IS NOT NULL
                                                AND (state IN ('queued','downloading') OR replacing=1)""")}
    waiting = [r["src"] for r in db.q("SELECT src FROM pending_op")]
    for d in base.iterdir() if base.is_dir() else []:
        if d.name.lower() not in live and not any(w == str(d) or w.startswith(str(d) + os.sep) for w in waiting):
            file_op("trash", d)


# -- file operations that can wait for a file in use ---------------------------
def _run_op(kind: str, src: str, dst: str | None) -> None:
    if kind == "move":
        files.move(Path(src), Path(dst))
    else:
        files.trash(Path(src))


def file_op(kind: str, src, dst=None, mid=None, n=None, attempts: int = 1) -> bool:
    """Move or trash now; if the file is in use (a player has it open on Windows), queue it in pending_op
    and retry on every poll, in order. Never fails the episode. Returns True if done now."""
    src, dst = str(src), (str(dst) if dst else None)
    keys = [k for k in (src, dst) if k]
    marks = ",".join("?" * len(keys))
    waiting = db.one(f"SELECT 1 FROM pending_op WHERE src IN ({marks}) OR dst IN ({marks})", *keys, *keys)
    err = "waiting for an earlier operation on this file"
    for i in range(0 if waiting else attempts):  # an earlier op on the same file goes first
        try:
            _run_op(kind, src, dst)
            return True
        except files.Locked as e:
            err = str(e)
            if i + 1 < attempts:
                time.sleep(0.25)
    db.x("INSERT INTO pending_op(kind, src, dst, anilist_id, number, created, last_error) VALUES(?,?,?,?,?,?,?)",
         kind, src, dst, mid, n, time.time(), err)
    db.event("warn", f"Waiting: {Path(src).name} is in use; {'moving' if kind == 'move' else 'trashing'} it once it is free", mid, n)
    return False


def run_pending() -> None:
    blocked: set[str] = set()
    for op in db.q("SELECT * FROM pending_op ORDER BY id"):
        keys = {op["src"], op["dst"]} - {None}
        if keys & blocked:
            continue
        try:
            _run_op(op["kind"], op["src"], op["dst"])
        except FileNotFoundError:
            pass  # gone meanwhile (moved or deleted by the user): nothing left to do
        except Exception as e:  # Locked, or anything else: keep it, retry next poll, keep later ops on it waiting
            blocked |= keys
            db.x("UPDATE pending_op SET tries=tries+1, last_error=? WHERE id=?", f"{type(e).__name__}: {e}", op["id"])
            continue
        db.x("DELETE FROM pending_op WHERE id=?", op["id"])
        db.event("info", f"{Path(op['src']).name}: {'moved into the library' if op['kind'] == 'move' else 'moved to the trash'}",
                 op["anilist_id"], op["number"])


@job
def poll_releases() -> None:
    if check_library():
        return
    _run_cache.clear()
    now = time.time()
    fb_h, give_up = float(db.setting("fallback_hours")), float(db.setting("give_up_days"))
    rows = db.q("""SELECT e.number, e.airs_at, e.state, e.search_from, a.* FROM episode e JOIN anime a USING(anilist_id)
                   WHERE a.active=1 AND e.state IN ('scheduled','searching') AND e.airs_at + 1800 <= ?
                   ORDER BY e.airs_at""", now)
    for r in rows:
        mid, n, title = r["anilist_id"], r["number"], r["title_en"] or r["title_romaji"]
        if now > (r["search_from"] or max(r["airs_at"], r["added_at"])) + give_up * DAY:
            db.x("UPDATE episode SET state='failed' WHERE anilist_id=? AND number=?", mid, n)
            db.event("error", f"{title} – {n:02d}: no matching release after {give_up:g} days", mid, n)
            continue
        prefs = _prefs(mid)
        if not prefs:
            continue
        try:
            _poll_one(r, now, prefs, fb_h, title)
        except nyaa.NyaaBusy:
            raise  # every later query would fail too
        except Exception as e:  # one bad show must not stop the others
            log.exception("poll failed for %s %s", title, n)
            db.event("error", f"{title} – {n:02d}: {type(e).__name__}: {e}", mid, n)
    _check_revisions()


def _poll_one(r, now: float, prefs, fb_h: float, title: str) -> None:
    mid, n = r["anilist_id"], r["number"]
    allowed = prefs if (r["search_from"] or now >= r["airs_at"] + fb_h * HOUR) else prefs[:1]
    if r["state"] == "scheduled":
        db.x("UPDATE episode SET state='searching' WHERE anilist_id=? AND number=?", mid, n)
    cands = _candidates(r, n, allowed)
    if not cands:
        return
    rank, _, _, it, p = cands[0]
    if not _disk_ok(it.size):
        db.event("error", f"{title} – {n:02d}: disk nearly full, skipped", mid, n)
        return
    files.show_dir(r["display_title"])  # confinement check for the final name
    engine.get().add(it.infohash, it.title, str(files.incoming(it.infohash)), mid)
    db.x("""UPDATE episode SET state='queued', chosen_group=?, version=?, infohash=?, torrent_name=?, size_bytes=?, queued_at=?
            WHERE anilist_id=? AND number=?""", p.group, p.version, it.infohash, it.title, it.size, now, mid, n)
    note = f" · fell back to preference {rank}" if rank > 1 else ""
    db.event("warn" if rank > 1 else "info", f"{title} – {n:02d}: queued {p.group} {p.resolution}p{note}", mid, n)


def _check_revisions() -> None:
    """A v2 for a done episode (within 3 days) replaces v1 silently and restarts the 30-day clock."""
    rows = db.q("""SELECT e.number, e.version, e.chosen_group, e.infohash, e.file_path, a.* FROM episode e JOIN anime a USING(anilist_id)
                   WHERE e.state='done' AND e.replacing=0 AND e.downloaded_at > ?""", time.time() - 3 * DAY)
    for r in rows:
        prefs = [p for p in _prefs(r["anilist_id"]) if p["grp"].lower() == (r["chosen_group"] or "").lower()]
        if not prefs:
            continue
        cands = _candidates(r, r["number"], prefs, min_version=(r["version"] or 1) + 1)
        if not cands:
            continue
        _, _, _, it, p = cands[0]
        engine.get().add(it.infohash, it.title, str(files.incoming(it.infohash)), r["anilist_id"])
        db.x("""UPDATE episode SET replacing=1, old_hash=infohash, old_path=file_path, infohash=?, torrent_name=?, version=?,
                replace_started=? WHERE anilist_id=? AND number=?""", it.infohash, it.title, p.version, time.time(),
             r["anilist_id"], r["number"])
        db.event("info", f"{r['title_en'] or r['title_romaji']} – {r['number']:02d}: v{p.version} found, replacing", r["anilist_id"], r["number"])


# -- watch_downloads --------------------------------------------------------
def _sync_progress(mid: int, n: int, title: str) -> None:
    try:
        p = anilist.push_progress(mid, n)
        db.x("UPDATE episode SET anilist_synced=1 WHERE anilist_id=? AND number=?", mid, n)
        db.event("info", f"{title} – {n:02d} downloaded · AniList progress → {p}", mid, n)
    except (anilist.AniListError, anilist.AuthError) as e:
        db.event("warn", f"{title} – {n:02d} downloaded · AniList update queued ({e})", mid, n)


@job
def watch_downloads() -> None:
    if check_library():
        return
    run_pending()
    rows = db.q("""SELECT e.*, a.display_title, a.total_episodes, a.title_en, a.title_romaji FROM episode e JOIN anime a USING(anilist_id)
                   WHERE e.infohash IS NOT NULL AND (e.state IN ('queued','downloading') OR e.replacing=1)""")
    if not rows:
        return
    eng = engine.get()
    infos = {t.hash: t for t in eng.torrents()}
    now = time.time()
    for r in rows:
        mid, n, title = r["anilist_id"], r["number"], r["title_en"] or r["title_romaji"]
        t = infos.get(r["infohash"])
        if r["replacing"] and (t is None and now - (r["replace_started"] or now) > 10 * MIN
                               or now - (r["replace_started"] or now) > DAY):
            _abort_replacement(r, t)
            continue
        if t is None:
            if now - (r["queued_at"] or now) > 10 * MIN and not r["replacing"]:
                db.x("UPDATE episode SET state='searching', infohash=NULL WHERE anilist_id=? AND number=?", mid, n)
                db.event("warn", f"{title} – {n:02d}: torrent vanished from the download engine, searching again", mid, n)
            continue
        if t.progress < 1 or t.amount_left > 0:
            if r["state"] == "queued" and t.progress > 0:
                db.x("UPDATE episode SET state='downloading' WHERE anilist_id=? AND number=?", mid, n)
            continue
        fl = t.files or eng.files(t.hash)
        if len(fl) != 1 or not files.is_video(fl[0].name):
            eng.remove(t.hash)
            db.x("UPDATE episode SET state='searching', infohash=NULL, replacing=0 WHERE anilist_id=? AND number=?", mid, n)
            db.event("error", f"{title} – {n:02d}: torrent was not a single .mkv/.mp4, discarded", mid, n)
            continue
        src = files.confine(Path(t.save_path) / fl[0].name)
        final = files.show_dir(r["display_title"]) / files.episode_name(r["display_title"], n, r["total_episodes"], src.suffix)
        try:  # don't seed: drop the torrent first (its files stay), then move the file into the library
            eng.remove(t.hash)
        except Exception as e:
            log.warning("could not remove finished torrent %s: %s", t.hash, e)
        if r["replacing"]:
            if r["old_hash"]:
                with contextlib.suppress(Exception):
                    eng.remove(r["old_hash"])
            if r["old_path"]:
                file_op("trash", r["old_path"], mid=mid, n=n)
        # the engine may hold the file for a moment after removal; queued (not failed) if it stays in use
        file_op("move", src, final, mid, n, attempts=8)
        if src.parent.parent.name == files.INCOMING:
            with contextlib.suppress(OSError):  # the staging folder is empty now (rmdir refuses otherwise)
                src.parent.rmdir()
        size = final.stat().st_size if final.exists() else fl[0].size
        ret = float(db.setting("retention_days"))
        db.x("""UPDATE episode SET state='done', file_path=?, size_bytes=?, downloaded_at=?, delete_after=?, replacing=0,
                old_hash=NULL, old_path=NULL, replace_started=NULL WHERE anilist_id=? AND number=?""", str(final), size, now, now + ret * DAY, mid, n)
        if r["replacing"]:
            db.event("info", f"{title} – {n:02d}: replaced with v{r['version']}, deletes in {ret:g} d", mid, n)
        else:
            _sync_progress(mid, n, title)


def _abort_replacement(r, t) -> None:
    """The v2 vanished from the engine or never finished: keep v1, and never try that v2 again."""
    mid, n = r["anilist_id"], r["number"]
    if t is not None:
        try:
            engine.get().remove(t.hash)
        except Exception as e:
            log.warning("could not remove stalled replacement %s: %s", t.hash, e)
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO skipped VALUES(?,?,?)", (mid, n, r["infohash"]))
        c.execute("""UPDATE episode SET replacing=0, infohash=old_hash, file_path=COALESCE(old_path, file_path), old_hash=NULL,
                     old_path=NULL, replace_started=NULL, version=COALESCE(version, 2) - 1 WHERE anilist_id=? AND number=?""", (mid, n))
    db.event("warn", f"{r['title_en'] or r['title_romaji']} – {n:02d}: replacement release stalled, keeping the current file", mid, n)


# -- retry_sync / purge -----------------------------------------------------
@job
def retry_sync() -> None:
    for r in db.q("""SELECT e.anilist_id, e.number, a.title_en, a.title_romaji FROM episode e JOIN anime a USING(anilist_id)
                     WHERE e.state='done' AND e.anilist_synced=0 ORDER BY e.number"""):
        _sync_progress(r["anilist_id"], r["number"], r["title_en"] or r["title_romaji"])


@job
def purge() -> None:
    if check_library():
        return
    emptied = files.empty_managed_trash(int(db.setting("managed_trash_days")))
    if emptied:
        db.event("info", f"Emptied {emptied} day{'s' if emptied > 1 else ''} of anidash's trash folder ({files.TRASH})")
    rows = db.q("""SELECT e.*, a.title_en, a.title_romaji FROM episode e JOIN anime a USING(anilist_id)
                   WHERE e.state='done' AND e.delete_after < ?""", time.time())
    names = []
    for r in rows:
        try:
            engine.get().remove(r["infohash"])
        except Exception as e:
            log.warning("could not remove torrent %s: %s", r["infohash"], e)
        if r["file_path"]:
            file_op("trash", r["file_path"], mid=r["anilist_id"], n=r["number"])  # waits if a player has it open
        db.x("UPDATE episode SET state='deleted', file_path=NULL WHERE anilist_id=? AND number=?", r["anilist_id"], r["number"])
        names.append(f"{r['title_en'] or r['title_romaji']} – {r['number']:02d}")
    if names:
        db.event("info", f"Purged {len(names)} file{'s' if len(names) > 1 else ''} to Trash ({', '.join(names[:6])}{'…' if len(names) > 6 else ''})")


# -- manual retry -----------------------------------------------------------
def retry_episode(mid: int, n: int) -> None:
    """Restart the give-up clock and allow every ranked preference immediately."""
    db.x("""UPDATE episode SET state='searching', infohash=NULL, search_from=?
            WHERE anilist_id=? AND number=? AND state IN ('failed','searching','scheduled')""", time.time(), mid, n)


def cancel_episode(mid: int, n: int) -> bool:
    """Drop the chosen release for good (it is skipped from now on), trash any partial file and search again
    with every ranked preference. Returns False if the episode is not queued or downloading."""
    with _locks["watch_downloads"]:  # don't race a download that is completing right now
        r = db.one("""SELECT e.infohash, e.state, e.replacing, e.torrent_name, a.title_en, a.title_romaji
                     FROM episode e JOIN anime a USING(anilist_id) WHERE e.anilist_id=? AND e.number=?""", mid, n)
        if not r or r["state"] not in ("queued", "downloading") or r["replacing"] or not r["infohash"]:
            return False
        h, eng = r["infohash"], engine.get()
        t = next((t for t in eng.torrents() if t.hash == h.lower()), None)
        partial = [Path(t.save_path) / f.name for f in (t.files or eng.files(h))] if t else []
        eng.remove(h)
        with db.tx() as x:
            x.execute("INSERT OR IGNORE INTO skipped VALUES(?,?,?)", (mid, n, h))
            x.execute("""UPDATE episode SET state='searching', infohash=NULL, chosen_group=NULL, version=NULL, torrent_name=NULL,
                         size_bytes=NULL, queued_at=NULL, search_from=? WHERE anilist_id=? AND number=?""", (time.time(), mid, n))
    for p in partial:
        for f in (p, p.with_name(p.name + ".!qB")):  # qBit's incomplete-file suffix, if enabled
            try:
                if f.exists():
                    file_op("trash", f, mid=mid, n=n, attempts=8)
            except Exception as e:
                log.warning("could not trash partial file %s: %s", f, e)
    db.event("warn", f"{r['title_en'] or r['title_romaji']} – {n:02d}: cancelled {r['torrent_name']}, searching for another release", mid, n)
    return True


JOBS = {"sync_schedule": sync_schedule, "poll_releases": poll_releases, "watch_downloads": watch_downloads,
        "retry_sync": retry_sync, "purge": purge}


def last_run(name: str) -> float:
    return LAST.get(name) or float(db.setting(f"job_last_{name}") or 0)


def stamp(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"
