"""FastAPI app: JSON API for the dashboard + the static dashboard. Bound to 127.0.0.1 only.

configure(port, token) must be called before the app starts serving."""
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    APP,
    __version__,
    anilist,
    autostart,
    db,
    files,
    jobs,
    nyaa,
    paths,
    player,
    qbit,
    qbit_setup,
    scheduler,
    shows,
    ui,
    vault,
)
from .parser import parse

log = logging.getLogger("anidash")
TOKEN = ""
PORT = 0
ALLOWED_ORIGINS: set[str] = set()
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_season_cache: dict[tuple, tuple[float, list]] = {}
_account_cache: dict = {}
STATE = {"v1_conflict": False}


def configure(port: int, token: str) -> None:
    global TOKEN, PORT, ALLOWED_ORIGINS
    TOKEN, PORT = token, port
    ALLOWED_ORIGINS = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


@asynccontextmanager
async def lifespan(app):
    db.init()
    scheduler.start()
    db.event("info", f"anidash {__version__} started")
    yield
    scheduler.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "").rsplit(":", 1)[0] not in ALLOWED_HOSTS:  # DNS-rebinding guard
        return JSONResponse({"detail": "bad host"}, 403)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        if not TOKEN or not hmac.compare_digest(request.headers.get("x-anidash-token", ""), TOKEN):
            return JSONResponse({"detail": "missing or bad token"}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "bad origin"}, 403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def ms(t):
    return int(t * 1000) if t else None


def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def current_season(now: datetime | None = None) -> tuple[str, int]:
    now = now or datetime.now()
    return ("WINTER", "WINTER", "SPRING", "SPRING", "SPRING", "SUMMER", "SUMMER", "SUMMER", "FALL", "FALL", "FALL",
            "WINTER")[now.month - 1], now.year + (1 if now.month == 12 else 0)


# -- app / window -----------------------------------------------------------
@app.get("/api/health")
def health():
    return {"app": APP, "version": __version__, "setup_done": bool(db.setting("setup_done")),
            "v1_conflict": STATE["v1_conflict"], "paused": scheduler.paused.is_set()}


@app.post("/api/ui/show")
def ui_show():
    ui.open_window()
    return {"ok": True}


@app.get("/api/ui/seq")
def ui_seq():
    return {"seq": ui.raise_seq()}


@app.post("/api/app/quit")
def app_quit():
    _bg(ui.request_quit)
    return {"ok": True}


@app.post("/api/app/pause")
def app_pause(body: dict):
    (scheduler.paused.set if body.get("paused") else scheduler.paused.clear)()
    db.event("info", "Automation paused" if scheduler.paused.is_set() else "Automation resumed")
    return {"paused": scheduler.paused.is_set()}


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
        with suppress(Exception):
            torrents = {t.hash.lower(): t for t in qbit.client().torrents_info(category=qbit.CATEGORY)}
    visible = []
    for e in eps:
        if e["state"] in ("deleted", "skipped") or not (e["airs_at"] and e["airs_at"] <= now or e["state"] != "scheduled"):
            continue
        t = torrents.get(e["infohash"] or "")
        visible.append({"number": e["number"], "state": "searching" if e["state"] == "scheduled" else e["state"],
                        "file_path": e["file_path"], "size_bytes": e["size_bytes"], "downloaded_at": ms(e["downloaded_at"]),
                        "delete_after": ms(e["delete_after"]), "progress": t.progress if t else 0, "qbit_state": t.state if t else None})
    return {"id": mid, "en": a["title_en"] or a["title_romaji"], "romaji": a["title_romaji"], "desc": a["description"],
            "airs_at": ms(ref), "total": a["total_episodes"], "aired": len(aired), "skip_through": a["skip_through"],
            "next_ep": nxt["number"] if nxt else None, "next_at": ms(nxt["airs_at"]) if nxt else None,
            "alias": a["search_alias"], "display": a["display_title"], "offset": a["episode_offset"],
            "trusted": bool(a["trusted_only"]), "prefs": prefs, "url": a["site_url"] or f"https://anilist.co/anime/{mid}",
            "probe": json.loads(a["probe_json"]) if a["probe_json"] else None, "episodes": visible}


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
    except anilist.AniListError as e:
        raise HTTPException(502, str(e))
    _bg(jobs.sync_schedule, [mid])  # rate-limited AniList calls; don't hold the request open
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
        try:
            files.trash(folder)
        except OSError as e:
            db.event("warn", f"Could not move {folder} to the Trash: {e}", mid)
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
def season(season: str | None = None, year: int | None = None):
    cs, cy = current_season()
    season, year = (season or cs).upper(), year or cy
    if season not in ("WINTER", "SPRING", "SUMMER", "FALL") or not 1990 <= year <= 2100:
        raise HTTPException(422)
    key, now = (season, year), time.time()
    if key not in _season_cache or now - _season_cache[key][0] > 900:
        try:
            _season_cache[key] = (now, anilist.season(season, year))
        except (anilist.AniListError, anilist.AuthError) as e:
            db.event("warn", f"Season browse failed: {e}")
            raise HTTPException(502, str(e))
    tracked = {r["anilist_id"] for r in db.q("SELECT anilist_id FROM anime")}
    return [{"id": m["id"], "en": m["title"].get("english") or m["title"]["romaji"], "romaji": m["title"]["romaji"],
             "airs_at": ms((m.get("nextAiringEpisode") or {}).get("airingAt")), "tracked": m["id"] in tracked,
             "list_status": (m.get("mediaListEntry") or {}).get("status")}
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
    try:
        used = player.play(path)
    except OSError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "player": used}


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
    at = time.time()
    try:
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


# -- settings & account -----------------------------------------------------
def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


EDITABLE = {"library_path": str, "retention_days": int, "fallback_hours": int, "poll_minutes": int, "give_up_days": int,
            "backlog": _bool, "nyaa_base": str, "qbit_url": str, "qbit_username": str, "vlc_path": str, "vlc_args": str,
            "anilist_client_id": str}
RANGES = {"retention_days": (1, 365), "fallback_hours": (0, 168), "poll_minutes": (5, 720), "give_up_days": (1, 60)}


@app.get("/api/settings")
def get_settings():
    s = db.settings_all()
    out = {k: s[k] for k in EDITABLE}
    out["autostart"] = autostart.is_enabled()
    return out


@app.patch("/api/settings")
def patch_settings(body: dict):
    clean = {}
    for k, v in body.items():
        if k == "autostart":
            autostart.set_enabled(_bool(v))
            continue
        if k not in EDITABLE:
            continue
        try:
            clean[k] = EDITABLE[k](v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{k}: bad value")
        if k in RANGES and not RANGES[k][0] <= clean[k] <= RANGES[k][1]:
            raise HTTPException(422, f"{k} must be between {RANGES[k][0]} and {RANGES[k][1]}")
    for k in ("nyaa_base", "qbit_url"):
        if k in clean and not clean[k].startswith(("http://", "https://")):
            raise HTTPException(422, f"{k} must be an http(s) URL")
    if "anilist_client_id" in clean and clean["anilist_client_id"] and not clean["anilist_client_id"].isdigit():
        raise HTTPException(422, "AniList client ID is a number")
    if "library_path" in clean:
        p = Path(clean["library_path"]).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(422, f"Library folder: {e}")
        clean["library_path"] = str(p)
    db.set_settings(clean)
    if "poll_minutes" in clean:
        scheduler.set_poll_minutes(clean["poll_minutes"])
    if {"qbit_url", "qbit_username"} & clean.keys():
        qbit.reset()
    return get_settings()


class QbitLogin(BaseModel):
    url: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, max_length=500)


def _qbit_error(e: Exception, user: str) -> HTTPException:
    import qbittorrentapi
    if isinstance(e, qbittorrentapi.Forbidden403Error):
        return HTTPException(502, "qBittorrent has banned 127.0.0.1 after failed logins. Restart qBittorrent, then try again.")
    if isinstance(e, (qbittorrentapi.LoginFailed, qbittorrentapi.Unauthorized401Error)):
        return HTTPException(502, f"Wrong username or password for '{user}'.")
    if isinstance(e, qbittorrentapi.APIConnectionError):
        return HTTPException(502, "No qBittorrent Web UI answers at that address. Is qBittorrent running with the Web UI on?")
    return HTTPException(502, f"{type(e).__name__}: {e}")


@app.post("/api/settings/test-qbit")
def test_qbit(body: QbitLogin):
    """Test (and, with a password, save) a qBittorrent login."""
    url = body.url or db.setting("qbit_url")
    user = body.username or db.setting("qbit_username")
    try:
        c = qbit.make(url, user, body.password)
        v = c.app.version.lstrip("v")
    except Exception as e:
        raise _qbit_error(e, user)
    if body.password:
        vault.set("qbit_password", body.password)
    db.set_settings({"qbit_url": url, "qbit_username": user})
    qbit.reset()
    return {"version": v}


@app.get("/api/account")
def account():
    root = files.library_root()
    used = db.one("SELECT COALESCE(SUM(size_bytes),0) n FROM episode WHERE state='done'")["n"]
    out = {"client_id": anilist.client_id(), "public_client": bool(anilist.PUBLIC_CLIENT_ID), "disk_used": used,
           "disk_free": shutil.disk_usage(root).free, "user": None, "score_format": "POINT_100",
           "token_expires": None, "secrets": vault.backend_name(), "secrets_in_file": vault.using_file(),
           "version": __version__, "data_dir": str(paths.DATA_DIR), "log_dir": str(paths.LOG_DIR)}
    exp = float(db.setting("token_expires") or 0) or (float(db.setting("token_issued") or 0) + 365 * 86400)
    if exp > 365 * 86400:
        out["token_expires"] = ms(exp)
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
    expires_in: int | None = Field(default=None, ge=0, le=10 * 365 * 86400)


@app.post("/api/account/token")
def set_token(body: TokenIn):
    try:
        v = anilist.viewer(token=body.token)
    except (anilist.AniListError, anilist.AuthError) as e:
        raise HTTPException(400, f"AniList rejected the token: {e}")
    vault.set("anilist_token", body.token)
    now = time.time()
    db.set_settings({"token_issued": now, "token_expires": now + (body.expires_in or 365 * 86400)})
    _account_cache.clear()
    db.event("info", f"AniList connected as @{v['name']}")
    return {"user": v["name"]}


# -- first-run setup ----------------------------------------------------------
@app.get("/api/setup")
def setup_status():
    a = account()
    qb = {"ok": False, "error": None, "version": None}
    try:
        qb["version"] = qbit.client().app.version.lstrip("v")
        qb["ok"] = True
    except Exception as e:
        qb["error"] = str(e) or type(e).__name__
    lib = Path(db.setting("library_path"))
    return {"done": bool(db.setting("setup_done")), "anilist": {"ok": bool(a["user"]), "user": a["user"],
            "client_id": a["client_id"], "authorize_url": anilist.authorize_url()},
            "qbit": qb, "library": {"path": str(lib), "writable": os.access(lib if lib.exists() else lib.parent, os.W_OK)},
            "autostart": autostart.is_enabled(), "backlog": bool(db.setting("backlog"))}


@app.get("/api/setup/qbit")
def setup_qbit_report():
    r = qbit_setup.report()
    for i in r["installs"]:
        sec = qbit_setup.read_section(Path(i["config"]), "Preferences")
        i["webui_enabled"] = sec.get("WebUI\\Enabled", "false") == "true"
        i["webui_user"] = sec.get("WebUI\\Username") if "WebUI\\Password_PBKDF2" in sec else None
        i["webui_port"] = int(sec.get("WebUI\\Port", "8080") or 8080)
    return r


class AutoQbit(BaseModel):
    index: int = Field(ge=0, le=10)


@app.post("/api/setup/qbit/auto")
def setup_qbit_auto(body: AutoQbit):
    insts = qbit_setup.detect()
    if body.index >= len(insts):
        raise HTTPException(404, "qBittorrent installation not found")
    inst = insts[body.index]
    if qbit_setup.running():
        raise HTTPException(409, "qBittorrent is running. Quit it completely (also from the tray), then try again.")
    port = int(qbit_setup.read_section(Path(inst.config), "Preferences").get("WebUI\\Port", "8080") or 8080)
    user, pw = "anidash", secrets.token_urlsafe(24)
    try:
        res = qbit_setup.configure(inst, user, pw, port)
    except OSError as e:
        raise HTTPException(500, f"Could not write {inst.config}: {e}")
    vault.set("qbit_password", pw)
    db.set_settings({"qbit_url": res["url"], "qbit_username": user})
    qbit.reset()
    started = qbit_setup.start(inst)
    version, err = None, None
    for _ in range(40 if started else 1):
        try:
            version = qbit.make(res["url"], user, pw).app.version.lstrip("v")
            break
        except Exception as e:  # still starting
            err = e
            time.sleep(0.5)
    db.event("info", f"qBittorrent Web UI configured ({inst.kind}){' · backup: ' + res['backup'] if res['backup'] else ''}")
    return {"started": started, "version": version, "backup": res["backup"], "url": res["url"],
            "error": None if version else (f"qBittorrent did not answer yet: {err}" if started else
                                           "Start qBittorrent yourself, then press Test.")}


@app.get("/api/setup/qbit/interfaces")
def setup_qbit_interfaces():
    try:
        return qbit.interfaces()
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/setup/qbit/interface")
def setup_qbit_interface(body: dict):
    value = str(body.get("value", ""))
    try:
        if value and value not in {i["value"] for i in qbit.interfaces()["list"]}:
            raise HTTPException(422, "unknown interface")
        qbit.bind_interface(value)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))
    db.event("info", f"qBittorrent bound to {value or 'any interface'}")
    return {"ok": True}


@app.post("/api/setup/finish")
def setup_finish():
    db.set_settings({"setup_done": True})
    db.event("info", "Setup finished")
    if vault.get("anilist_token"):
        jobs.MANUAL.add("sync_schedule")
        scheduler.run_now("sync_schedule")  # imports your AniList Watching list
    return {"ok": True}


# -- dashboard --------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = (paths.WEB_DIR / "Dashboard.dc.html").read_text(encoding="utf-8")
    return html.replace("<head>", f'<head>\n<meta name="anidash-token" content="{TOKEN}">', 1)


@app.get("/Dashboard.dc.html", response_class=HTMLResponse)
def index_alias():
    return index()


if paths.WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=paths.WEB_DIR), name="web")
