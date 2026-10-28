"""AniList GraphQL client.

Live limit is ~30 req/min plus a burst limiter; we stay at 25/min, 2 in flight.
429s may be Cloudflare HTML, so status is checked before parsing.
"""
import logging
import re
import threading
import time

import httpx

from . import __version__, db, vault

log = logging.getLogger("anidash.anilist")
URL = "https://graphql.anilist.co"
UA = f"anidash/{__version__} (+https://github.com/rafaefarrukh/anidash)"

# Public client registered for anidash (implicit grant, redirect https://anilist.co/api/v2/oauth/pin).
# Implicit grant needs no client secret, so shipping the ID is safe. Users can override it in Settings.
PUBLIC_CLIENT_ID = "55203"
PIN_REDIRECT = "https://anilist.co/api/v2/oauth/pin"


def client_id() -> str:
    return str(db.setting("anilist_client_id") or PUBLIC_CLIENT_ID or "")


def authorize_url(cid: str | None = None) -> str | None:
    cid = cid or client_id()
    return f"https://anilist.co/api/v2/oauth/authorize?client_id={cid}&response_type=token" if cid else None


class AuthError(Exception):
    pass


class AniListError(Exception):
    pass


class _Bucket:
    def __init__(self, per_min=25, burst=4):
        self.rate, self.cap, self.tokens = per_min / 60, burst, burst
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


_bucket = _Bucket()
_sem = threading.BoundedSemaphore(2)
_locked_until = 0.0
_http = httpx.Client(timeout=20, headers={"User-Agent": UA, "Accept": "application/json"})


def gql(query: str, variables: dict | None = None, token: str | None = None, auth: bool = True) -> dict:
    global _locked_until
    headers = {}
    if auth:
        token = token or vault.get("anilist_token")
        if not token:
            raise AuthError("AniList is not connected")
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(4):
        wait = _locked_until - time.time()
        if wait > 0:
            time.sleep(wait)
        _bucket.take()
        with _sem:
            try:
                r = _http.post(URL, json={"query": query, "variables": variables or {}}, headers=headers)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise AniListError(f"network error: {e}") from e
                time.sleep(5 * (attempt + 1))
                continue
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "")
            secs = int(ra) if ra.isdigit() else 65
            _locked_until = time.time() + secs
            log.warning("AniList 429, backing off %ss", secs)
            continue
        if r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        if "json" not in r.headers.get("content-type", ""):
            raise AniListError(f"HTTP {r.status_code} with non-JSON body (Cloudflare?)")
        body = r.json()
        errors = body.get("errors") or []
        if auth and r.status_code in (400, 401) and any(
                e.get("status") == 401 or "token" in (e.get("message") or "").lower() for e in errors):
            raise AuthError("AniList refused the token (expired?). Reconnect in Settings.")
        if errors:
            raise AniListError("; ".join(e.get("message", "?") for e in errors))
        return body["data"]
    raise AniListError("AniList rate limit, gave up after retries")


MEDIA = """id siteUrl episodes season seasonYear status
  title { english romaji native } description(asHtml: false) coverImage { large }
  nextAiringEpisode { episode airingAt }"""
MEDIA_AUTH = MEDIA + " mediaListEntry { status progress }"


def clean_desc(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def viewer(token: str | None = None) -> dict:
    return gql("query { Viewer { id name mediaListOptions { scoreFormat } } }", token=token)["Viewer"]


def media(mid: int) -> dict:
    return gql(f"query($id:Int){{ Media(id:$id, type:ANIME){{ {MEDIA_AUTH} }} }}", {"id": mid})["Media"]


def season(season: str, year: int, pages: int = 3) -> list[dict]:
    out = []
    for p in range(1, pages + 1):
        d = gql("""query($s:MediaSeason,$y:Int,$p:Int){ Page(page:$p, perPage:25){ pageInfo{ hasNextPage }
          media(season:$s, seasonYear:$y, type:ANIME, isAdult:false, sort:POPULARITY_DESC, format_in:[TV,TV_SHORT,ONA]){
            id title{ english romaji } episodes nextAiringEpisode{ episode airingAt } mediaListEntry{ status } } } }""", {"s": season, "y": year, "p": p})["Page"]
        out += d["media"]
        if not d["pageInfo"]["hasNextPage"]:
            break
    return out


def current_list(user_id: int) -> list[dict]:
    d = gql(f"""query($u:Int){{ MediaListCollection(userId:$u, type:ANIME, status:CURRENT){{
        lists{{ entries{{ progress media{{ {MEDIA} }} }} }} }} }}""", {"u": user_id})
    return [e for lst in d["MediaListCollection"]["lists"] for e in lst["entries"]]


def schedules(ids: list[int]) -> list[dict]:
    """All episodes (aired and upcoming) for up to 25 shows, batched."""
    out = []
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        d = gql("""query($ids:[Int]){ Page(perPage:10){ media(id_in:$ids, type:ANIME){
            id episodes status airingSchedule(perPage:25){ pageInfo{ hasNextPage } nodes{ episode airingAt } } } } }""",
                {"ids": chunk})
        for m in d["Page"]["media"]:
            nodes = m["airingSchedule"]["nodes"]
            page = 2
            while m["airingSchedule"]["pageInfo"]["hasNextPage"] and page < 6:
                more = gql("""query($id:Int,$p:Int){ Media(id:$id){ airingSchedule(page:$p, perPage:25){
                    pageInfo{ hasNextPage } nodes{ episode airingAt } } } }""", {"id": m["id"], "p": page})["Media"]
                m["airingSchedule"] = more["airingSchedule"]
                nodes += more["airingSchedule"]["nodes"]
                page += 1
            out.append({"id": m["id"], "episodes": m["episodes"], "status": m["status"], "nodes": nodes})
    return out


def save_entry(mid: int, status: str | None = None, progress: int | None = None, score: float | None = None) -> dict:
    vars_, decl, args = {"mediaId": mid}, ["$mediaId:Int"], ["mediaId:$mediaId"]
    for name, typ, val in (("status", "MediaListStatus", status), ("progress", "Int", progress), ("score", "Float", score)):
        if val is not None:
            vars_[name] = val
            decl.append(f"${name}:{typ}")
            args.append(f"{name}:${name}")
    q = f"mutation({','.join(decl)}){{ SaveMediaListEntry({','.join(args)}){{ id status progress }} }}"
    return gql(q, vars_)["SaveMediaListEntry"]


def push_progress(mid: int, number: int) -> int:
    d = gql("query($id:Int){ Media(id:$id){ mediaListEntry{ progress } } }", {"id": mid})["Media"]
    current = (d.get("mediaListEntry") or {}).get("progress") or 0
    target = max(current, number)
    if target != current:
        save_entry(mid, progress=target)
    return target
