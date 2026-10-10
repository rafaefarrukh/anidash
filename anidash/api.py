"""FastAPI app: JSON API for the dashboard + static dashboard files. Bound to 127.0.0.1 only."""
import hmac
import json
import os
import secrets
import shlex
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import anilist, config, db, files, jobs, nyaa, qbit, scheduler, shows, vault
from .parser import parse

def _load_token() -> str:
    """Reuse the token across service restarts (the runtime dir is cleared at reboot)."""
    try:
        t = config.TOKEN_FILE.read_text().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    return secrets.token_urlsafe(32)


TOKEN = _load_token()
PORT = config.load()["port"]
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_season_cache: dict[tuple, tuple[float, list]] = {}
_account_cache: dict = {}


@asynccontextmanager
async def lifespan(app):
    db.init()
    config.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TOKEN)
    scheduler.start()
    db.event("info", "Service started")
    yield
    scheduler.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "").rsplit(":", 1)[0] not in ALLOWED_HOSTS:  # DNS-rebinding guard
        return JSONResponse({"detail": "bad host"}, 403)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        if not hmac.compare_digest(request.headers.get("x-anidash-token", ""), TOKEN):
            return JSONResponse({"detail": "missing or bad token"}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "bad origin"}, 403)
    return await call_next(request)


def ms(t):
    return int(t * 1000) if t else None


def airs_label(t):
    return datetime.fromtimestamp(t).strftime("%a %H:%M") if t else None


# -- tracked shows ----------------------------------------------------------
def anime_view(a, now) -> dict:
    mid = a["anilist_id"]
    prefs = [{"group": p["grp"], "res": p["resolution"]} for p in db.q("SELECT * FROM preference WHERE anilist_id=? ORDER BY rank", mid)]
    eps = db.q("SELECT * FROM episode WHERE anilist_id=? ORDER BY number", mid)
    aired = [e for e in eps if e["airs_at"] and e["airs_at"] <= now]
    nxt = next((e for e in eps if e["airs_at"] and e["airs_at"] > now), None)
    ref = nxt["airs_at"] if nxt else (aired[-1]["airs_at"] if aired else None)
    torrents = {}
    if any(e["state"] in ("downloading", "queued") for e in eps):
        try:
            torrents = {t.hash.lower(): t for t in qbit.client().torrents_info(category=qbit.CATEGORY)}
        except Exception:
            pass
    visible = []
    for e in eps:
        t = torrents.get(e["infohash"] or "")
        if e["state"] == "deleted" or not (e["airs_at"] and e["airs_at"] <= now or e["state"] != "scheduled"):
            continue
        visible.append({"number": e["number"], "state": "searching" if e["state"] == "scheduled" else e["state"],
                        "file_path": e["file_path"], "size_bytes": e["size_bytes"], "downloaded_at": ms(e["downloaded_at"]),
                        "delete_after": ms(e["delete_after"]), "progress": t.progress if t else 0, "qbit_state": t.state if t else None})
    return {"id": mid, "en": a["title_en"] or a["title_romaji"], "romaji": a["title_romaji"], "desc": a["description"],
            "airs": airs_label(ref), "total": a["total_episodes"], "aired": len(aired),
            "next_ep": nxt["number"] if nxt else None, "next_at": ms(nxt["airs_at"]) if nxt else None,
            "alias": a["search_alias"], "display": a["display_title"], "offset": a["episode_offset"],
            "trusted": bool(a["trusted_only"]), "prefs": prefs,
            "probe": json.loads(a["probe_json"]) if a["probe_json"] else None, "episodes": visible}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/anime")
def list_anime():
    now = time.time()
    return [anime_view(a, now) for a in db.q("SELECT * FROM anime WHERE active=1 ORDER BY title_en COLLATE NOCASE")]


@app.post("/api/anime/{mid}")
def track(mid: int):
    try:
        shows.track(mid)
    except anilist.AuthError as e:
        raise HTTPException(401, str(e))
    jobs.sync_schedule([mid])
    return {"ok": True}


class Pref(BaseModel):
    group: str = Field(min_length=1, max_length=60)
    res: int


class AnimePatch(BaseModel):
    alias: str = Field(min_length=1, max_length=200)
    display: str = Field(min_length=1, max_length=200)
    offset: int = Field(ge=-2000, le=2000)
    trusted: bool
    prefs: list[Pref] = Field(max_length=2)  # empty until a group is picked


@app.patch("/api/anime/{mid}")
def patch_anime(mid: int, body: AnimePatch):
    if not db.one("SELECT 1 FROM anime WHERE anilist_id=?", mid):
        raise HTTPException(404)
    if any(p.res not in (480, 720, 1080) for p in body.prefs):
        raise HTTPException(422, "resolution must be 480, 720 or 1080")
    display = files.sanitize(body.display)
    files.show_dir(display)  # validates confinement
    with db.tx() as c:
        c.execute("UPDATE anime SET search_alias=?, display_title=?, episode_offset=?, trusted_only=? WHERE anilist_id=?",
                  (body.alias.strip(), display, body.offset, int(body.trusted), mid))
        c.execute("DELETE FROM preference WHERE anilist_id=?", (mid,))
        for rank, p in enumerate(body.prefs, 1):
            c.execute("INSERT INTO preference VALUES(?,?,?,?)", (mid, rank, p.group.strip(), p.res))
    return {"ok": True}


@app.post("/api/anime/{mid}/probe")
def probe(mid: int):
    a = db.one("SELECT search_alias, title_en, title_romaji FROM anime WHERE anilist_id=?", mid)
    if not a:
        raise HTTPException(404)
    title = a["title_en"] or a["title_romaji"]
    try:
        items = nyaa.search(a["search_alias"])
    except nyaa.NyaaBusy as e:
        db.event("warn", f"Probe for {title} stopped: {e}", mid)
        raise HTTPException(503, str(e))
    found: dict[str, set] = {}
    for it in items:
        p = parse(it.title)
        if p and p.group and p.resolution in (480, 720, 1080):
            found.setdefault(p.group, set()).add(p.resolution)
    out = {g: sorted(r) for g, r in sorted(found.items(), key=lambda kv: -len(kv[1]))}
    shows.save_probe(mid, out)
    db.event("info", f"Probe for {title} finished · {len(out)} group{'' if len(out) == 1 else 's'} found", mid)
    return out


class Removal(BaseModel):
    status: str
    score: float | None = None


@app.delete("/api/anime/{mid}")
def remove(mid: int, body: Removal):
    a = db.one("SELECT * FROM anime WHERE anilist_id=?", mid)
    if not a:
        raise HTTPException(404)
    if body.status not in ("COMPLETED", "DROPPED", "PAUSED", "PLANNING"):
        raise HTTPException(422, "bad status")
    try:  # AniList first; if it fails nothing is deleted
        anilist.save_entry(mid, status=body.status, score=body.score)
    except (anilist.AniListError, anilist.AuthError) as e:
        raise HTTPException(502, str(e))
    hashes = [r["infohash"] for r in db.q("SELECT infohash FROM episode WHERE anilist_id=? AND infohash IS NOT NULL", mid)]
    try:
        qbit.remove(hashes)
    except Exception as e:
        db.event("warn", f"Could not remove torrents for {a['title_en']}: {e}", mid)
    folder = files.show_dir(a["display_title"])
    if folder != files.library_root():
        files.trash(folder)
    db.x("DELETE FROM anime WHERE anilist_id=?", mid)
    db.event("info", f"Removed {a['title_en'] or a['title_romaji']} · AniList → {body.status.title()}, files moved to Trash")
    return {"ok": True}


@app.post("/api/anime/{mid}/episodes/{n}/retry")
def retry(mid: int, n: int):
    jobs.retry_episode(mid, n)
    scheduler.run_now("poll_releases")
    return {"ok": True}


@app.post("/api/anime/{mid}/episodes/{n}/cancel")
def cancel(mid: int, n: int):
    try:
        ok = jobs.cancel_episode(mid, n)
    except Exception as e:
        raise HTTPException(502, f"qBittorrent: {type(e).__name__}: {e}")
    if not ok:
        raise HTTPException(409, "Episode is not queued or downloading")
    scheduler.run_now("poll_releases")
    return {"ok": True}


# -- season browse ----------------------------------------------------------
@app.get("/api/season")
def season(season: str = "FALL", year: int = 2026):
    season = season.upper()
    if season not in ("WINTER", "SPRING", "SUMMER", "FALL"):
        raise HTTPException(422)
    key, now = (season, year), time.time()
    if key not in _season_cache or now - _season_cache[key][0] > 900:
        try:
            _season_cache[key] = (now, anilist.season(season, year))
        except (anilist.AniListError, anilist.AuthError) as e:
            db.event("warn", f"Season browse failed: {e}")
            return []
    tracked = {r["anilist_id"] for r in db.q("SELECT anilist_id FROM anime")}
    return [{"id": m["id"], "en": m["title"].get("english") or m["title"]["romaji"], "romaji": m["title"]["romaji"],
             "airs": airs_label((m.get("nextAiringEpisode") or {}).get("airingAt")), "tracked": m["id"] in tracked}
            for m in _season_cache[key][1]]


# -- playback ---------------------------------------------------------------
@app.post("/api/play/{mid}/{n}")
def play(mid: int, n: int):
    e = db.one("SELECT file_path FROM episode WHERE anilist_id=? AND number=? AND state='done'", mid, n)
    if not e or not e["file_path"]:
        raise HTTPException(404, "episode not on disk")
    path = files.confine(Path(e["file_path"]))
    if not path.exists() or not files.is_video(path.name):
        raise HTTPException(404, "file missing")
    vlc = shutil.which("vlc")
    if not vlc:
        raise HTTPException(500, "vlc not found")
    subprocess.Popen([vlc, *shlex.split(str(db.setting("vlc_args"))), "--", str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True}


# -- activity ---------------------------------------------------------------
@app.get("/api/downloads")
def downloads():
    labels = {r["infohash"]: f"{r['title_en'] or r['title_romaji']} – {r['number']:02d} · {r['chosen_group']}"
              for r in db.q("""SELECT e.infohash, e.number, e.chosen_group, a.title_en, a.title_romaji FROM episode e
                               JOIN anime a USING(anilist_id) WHERE e.infohash IS NOT NULL AND (e.state IN ('queued','downloading') OR e.replacing=1)""")}
    try:
        ts = qbit.client().torrents_info(category=qbit.CATEGORY)
    except Exception:
        return []
    out = []
    for t in ts:
        if t.progress >= 1:
            continue
        eta = t.eta if 0 <= t.eta < 8640000 else None
        out.append({"name": labels.get(t.hash.lower(), t.name), "progress": t.progress,
                    "speed": f"{t.dlspeed / 1e6:.1f} MB/s", "eta": f"{eta // 3600}:{eta % 3600 // 60:02d}" if eta is not None else "∞"})
    return out


@app.get("/api/jobs")
def list_jobs():
    return scheduler.describe()


@app.post("/api/jobs/{name}/run")
def run_job(name: str):
    try:
        at = time.time()
        if name in jobs.JOBS:
            jobs.MANUAL.add(name)
        scheduler.run_now(name)
    except KeyError:
        raise HTTPException(404, "unknown job")
    return {"ok": True, "at": at * 1000}


@app.get("/api/events")
def events(level: str | None = None, limit: int = 200):
    rows = db.q("SELECT * FROM event WHERE (? IS NULL OR level=?) ORDER BY ts DESC LIMIT ?", level, level, min(limit, 1000))
    return [{"ts": ms(r["ts"]), "level": r["level"], "anilist_id": r["anilist_id"], "episode": r["episode"],
             "message": r["message"], "action": "retry" if r["level"] == "error" and r["episode"] else None} for r in rows]


@app.get("/api/today")
def today():
    return [dict(r) for r in db.q("SELECT anilist_id, number, file_path FROM episode WHERE state='done' AND downloaded_at > ?", time.time() - 86400)]


# -- settings & account -----------------------------------------------------
EDITABLE = {"library_path": str, "retention_days": int, "fallback_hours": int, "poll_minutes": int, "give_up_days": int,
            "nyaa_base": str, "qbit_url": str, "seed_ratio": float, "seed_hours": int, "vlc_args": str}


@app.get("/api/settings")
def get_settings():
    s = db.settings_all()
    return {k: s[k] for k in EDITABLE}


@app.patch("/api/settings")
def patch_settings(body: dict):
    clean = {}
    for k, v in body.items():
        if k not in EDITABLE:
            continue
        try:
            clean[k] = EDITABLE[k](v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{k}: bad value")
    for k in ("nyaa_base", "qbit_url"):
        if k in clean and not clean[k].startswith(("http://", "https://")):
            raise HTTPException(422, f"{k} must be an http(s) URL")
    if "library_path" in clean:
        Path(clean["library_path"]).expanduser().mkdir(parents=True, exist_ok=True)
    db.set_settings(clean)
    if "poll_minutes" in clean:
        scheduler.set_poll_minutes(clean["poll_minutes"])
    if "qbit_url" in clean:
        qbit.reset()
    return get_settings()


class QbitTest(BaseModel):
    url: str | None = None


@app.post("/api/settings/test-qbit")
def test_qbit(body: QbitTest):
    import qbittorrentapi
    user = config.load()["qbit_username"]
    try:
        v = qbit.make(body.url).app.version.lstrip("v")
        qbit.reset()
        return {"version": v}
    except qbittorrentapi.Forbidden403Error:
        raise HTTPException(502, "qBittorrent has banned 127.0.0.1 after failed logins. Restart qBittorrent, then fix the password.")
    except (qbittorrentapi.LoginFailed, qbittorrentapi.Unauthorized401Error):
        raise HTTPException(502, f"Wrong login for user '{user}'. Password saved: {'yes' if vault.get('qbit_password') else 'NO'}.")
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/api/account")
def account():
    cfg = config.load()
    root = files.library_root()
    used = db.one("SELECT COALESCE(SUM(size_bytes),0) n FROM episode WHERE state='done'")["n"]
    out = {"client_id": cfg["anilist_client_id"], "disk_used": used, "disk_free": shutil.disk_usage(root).free,
           "user": None, "score_format": "POINT_100", "token_expires": None}
    issued = float(db.setting("token_issued") or 0)
    if issued:
        out["token_expires"] = ms(issued + 365 * 86400)
    if vault.get("anilist_token"):
        if not _account_cache or time.time() - _account_cache.get("t", 0) > 3600:
            try:
                v = anilist.viewer()
                _account_cache.update(t=time.time(), user=v["name"], score_format=v["mediaListOptions"]["scoreFormat"])
            except Exception:
                pass
        out.update({k: _account_cache[k] for k in ("user", "score_format") if k in _account_cache})
    return out


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=4000)


@app.post("/api/account/token")
def set_token(body: TokenIn):
    try:
        v = anilist.viewer(token=body.token)
    except (anilist.AniListError, anilist.AuthError) as e:
        raise HTTPException(400, f"AniList rejected the token: {e}")
    vault.set("anilist_token", body.token)
    db.set_settings({"token_issued": time.time()})
    _account_cache.clear()
    db.event("info", f"AniList connected as @{v['name']}")
    return {"user": v["name"]}


# -- dashboard --------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = (config.WEB_DIR / "Dashboard.dc.html").read_text(encoding="utf-8")
    return html.replace("<head>", f'<head>\n<meta name="anidash-token" content="{TOKEN}">', 1)


if config.WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=config.WEB_DIR), name="web")
