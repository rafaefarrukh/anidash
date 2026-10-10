# Anime Automation Dashboard — Outline v1

## 1. Decisions locked
- **Platform:** EndeavourOS (Arch). Local only, single user.
- **Stack:** Python service (FastAPI + APScheduler + SQLite) + HTML dashboard served on `127.0.0.1`. Optional pywebview/Tauri wrapper later.
- **Torrent client:** qBittorrent-nox via Web API.
- **Source:** Nyaa RSS (no HTML scraping), scoped per uploader where possible.
- **Player:** external VLC, launched by the service.
- **Watched = downloaded.** AniList progress is updated when a download completes.
- **Retention:** episode files are deleted 30 days after download.
- **Fallback:** switch to the next-preferred group 24 h after airing.
- **Pages:** Season (replaces Search), Library (replaces View), Activity, Settings, AniList. The AniList tab opens anilist.co in a second native window of the wrapped app (pywebview/Tauri) so login and the full site work; it cannot be iframed. No VLC page.

## 2. Architecture
```
systemd --user
 └─ anidash.service  (always on)
     ├─ FastAPI ── serves dashboard + JSON API on 127.0.0.1:8787
     ├─ Scheduler ─ jobs (§5)
     ├─ SQLite ─── ~/.local/share/anidash/anidash.db
     ├─ AniList client (GraphQL, OAuth token in keyring)
     ├─ Nyaa client (RSS + anitomy-ng parser)
     ├─ qBit client (qbittorrent-api lib, qBit ≥ 5.0)
     └─ Player launcher (subprocess → vlc)
Library: ~/Anime/<Title>/<Title> - ##.<ext>
```

## 3. Data model
**anime** — one row per tracked show
- `anilist_id` (PK), `title_en`, `title_romaji`, `title_native`, `description`, `cover_url`
- `total_episodes` (nullable), `season`, `season_year`, `airing_status`
- `display_title`: used for folder and file names; defaults to the English title, romaji fallback (editable, sanitized)
- `search_alias`: the Nyaa query (defaults to the romaji title)
- `episode_offset`: int, maps release numbering to AniList numbering (e.g. S2 "13" → 1)
- `active`: bool, `added_at`

**preference** — ordered release choices per anime
- `anilist_id`, `rank` (1, 2…), `group` (e.g. SubsPlease), `resolution` (480/720/1080), `trusted_only` (bool)

**episode**
- `anilist_id`, `number`, `airs_at` (from AniList airingSchedule)
- `state`: `scheduled → searching → queued → downloading → done → deleted` | `failed`
- `chosen_group`, `infohash`, `torrent_name`, `file_path`, `size_bytes`
- `downloaded_at`, `delete_after` (= downloaded_at + 30 d), `anilist_synced` (bool)

**event** — activity log
- `ts`, `level` (info/warn/error), `anilist_id?`, `episode?`, `message`

**settings** — key/value
- library path, check cadence, fallback hours (24), retention days (30), VLC args, qBit URL and credentials ref

## 4. Secrets
- The AniList token and the qBit API key (or password) are stored in the system keyring (libsecret), never in the DB or config files.
- AniList login uses the **auth pin** flow: register a client with redirect `https://anilist.co/api/v2/oauth/pin`, open `…/oauth/authorize?client_id=…&response_type=token`, paste the token. No client secret is needed.
- Tokens last 1 year with no refresh and no scopes (full account access). Store the issue date; Settings warns 14 days before expiry.

## 5. Scheduled jobs
| Job | When | Does |
|---|---|---|
| `sync_schedule` | daily 06:00 + on demand | Pull airingSchedule for active anime and upsert episode rows |
| `poll_releases` | every 20 min | For each episode with `airs_at + 30 min ≤ now` and state `scheduled`/`searching`: query RSS, pick a release (§6), add it to qBit |
| `watch_downloads` | every 1 min | Poll qBit for queued infohashes; on completion → rename/move, set `done`, push AniList progress |
| `retry_sync` | every 15 min | Retry AniList updates where `anilist_synced = false` |
| `purge` | daily 04:00 | For `delete_after < now`: remove torrent via qBit (keep files), move file to trash, set state `deleted` |
| `seed_limit` | none | No seeding: the torrent is removed from qBit (files kept) as soon as the episode is renamed |

The systemd timer uses `Persistent=true` so missed runs fire after sleep.

## 6. Rules
**Release matching**
1. RSS query: `search_alias` + resolution, category Anime – English-translated.
2. Parse each item with anitomy-ng → group, title, episode, resolution, version.
3. Accept only if:
   - the group matches a preference,
   - the resolution matches,
   - the trusted flag is set (if `trusted_only`),
   - parsed episode − offset = target number,
   - the file is a single video (.mkv or .mp4),
   - the size is between 50 MB and 3 GB.
4. Rank by preference rank; within a rank, prefer a v2 over v1, then most seeders.

**Fallback**
- Rank 1 only until `airs_at + 24 h`; after that, any ranked preference is allowed.
- 7 days with nothing found → `failed`, logged.

**Revisions**
- If a v2 appears after v1 is done: replace the file silently (v1 to trash), keep the counters, reset `delete_after` to now + 30 d.

**Naming**
- `~/Anime/<display_title>/<display_title> - <NN>.<ext>`, zero-padded to 2 digits (3 if total > 99).
- Sanitize names: strip `/ \0`, control characters and leading dots; collapse `..`.
- Resolve the real path and refuse anything outside the library root.

**AniList progress**
- On `done`: `SaveMediaListEntry(progress = max(current, number))`.
- If progress reaches `total_episodes`, leave the status alone; the user decides at removal.

**Removal (delete anime)**
- Read `scoreFormat` from the Viewer and render the matching score input.
- Confirm → `SaveMediaListEntry(status, score)` → remove torrents (keep files) → move the folder to trash (`gio trash`, freedesktop spec) → delete DB rows.
- If AniList fails, abort before deleting anything.

## 7. API (dashboard ↔ service)
Every mutating call requires an `X-Anidash-Token` header and checks the Origin.
- `GET  /api/season?season=FALL&year=2026`: AniList season browse, with watching-list flags
- `POST /api/anime/{id}`: track a show (immediately sets AniList status CURRENT)
- `GET  /api/anime`: tracked list with preferences and episode counts
- `PATCH /api/anime/{id}`: alias, display title, offset, preferences
- `POST /api/anime/{id}/probe`: search Nyaa and return available groups × resolutions
- `DELETE /api/anime/{id}`: body `{status, score}`; runs the removal flow
- `GET  /api/library`: per-anime episodes on disk
- `GET  /api/today`: episodes completed in the last 24 h
- `POST /api/play/{anilist_id}/{number}`: launch VLC
- `POST /api/jobs/{name}/run`: manual trigger
- `GET  /api/events?level=&limit=`: activity log
- `GET/PATCH /api/settings`
- `POST /api/settings/test-qbit`: connection check
- `GET  /api/account`: AniList user, score format, token expiry, disk usage
- `POST /api/account/token`: store a new AniList token (validated first)
- `GET  /api/downloads`, `GET /api/jobs`: Activity page
- `POST /api/anime/{id}/episodes/{n}/retry`: restart the give-up clock, allow every preference
- `POST /api/jobs/catchup/run`: run overdue jobs (used by `anidash-kick.timer`)

## 8. Pages
**Global header (nav):** Season · Library · Activity · Settings · AniList ↗, plus a service status dot and the next scheduled check.

**Season**
- Toolbar:
  - season/year switcher
  - "Sync AniList" (pull CURRENT list)
  - "Probe all" (search Nyaa for each tracked show)
  - "Remove mode" toggle
- One card per row, fixed height:
  - **Column 1:** English title, Japanese subtitle, summary clamped to 4 lines, airing day/time, "Open on AniList" link.
  - **Column 2:** preference 1 and preference 2 (group dropdown + resolution dropdown, filled from probe results), trusted-only toggle.
  - **Column 2, collapsed "Advanced":** search alias, display title, episode offset.
- Untracked season shows appear in a secondary list with a "Track" action.
- Remove mode: an ✕ on each card opens the removal dialog (status, score, confirm).

**Library**
- **Frozen top card "Today":** links to episodes downloaded in the last 24 h, one click to play.
- One card per anime, fixed height:
  - **Column 1:** title; "Ep NN of TT aired · NN downloaded"; primary button "Play Ep NN" (latest); next airing countdown.
  - **Column 2:** scrollable episode list (number, file name, size, downloaded date, days until delete, play action). Missing or failed episodes show inline with a retry action.

**Activity**
- A table of current downloads (progress, speed, ETA) plus the event log filtered by level. The failure count is mirrored in the nav.

**AniList**
- Opens (or focuses) a second app window on anilist.co with a persistent session. The dashboard tab shows "Bring to front", "Sync now", "Open in browser instead". Closing that window triggers an AniList sync.

**Settings**
- Library path, retention, fallback hours, poll interval, VLC args, AniList connect/reconnect, qBit connection test, disk usage.

## 9. Key flows
1. **Season start:** Season page → pick season → Track shows → Probe all → set preferences → done.
2. **Episode lifecycle:** airs → `sync_schedule` knows it → `poll_releases` picks a release → qBit downloads → renamed → AniList +1 → appears in Today → purged at 30 days.
3. **Watch:** Library → Play → service runs `vlc --fullscreen <path>`.
4. **End of show:** Remove mode → ✕ → status + score → confirm → AniList updated, files and settings gone.

## 10. Security checklist
- The service binds to 127.0.0.1 only; mutating requests need the token and an Origin check.
- qBit Web UI is localhost-only with a password; the "bypass auth" option stays off.
- VPN with a kill switch; qBit is bound to the VPN network interface.
- Never execute downloaded content; extension allow-list; trusted uploaders.
- Subprocess calls use argument lists, never `shell=True`.
- All deletes go to trash only, confined to the library root after resolving real paths.
- Secrets are stored in the keyring.

## 11. Edge cases to handle
- **Air dates:** delays, recaps and breaks; airingSchedule shifts are re-synced daily.
- **Release format:** batch releases (ignored for weekly), double episodes ("01-02"), split cours with continued numbering.
- **Unknowns:** `total_episodes` unknown (show "?").
- **Access:** Nyaa unreachable or ISP-blocked (configurable base URL, backoff); AniList rate limit (≤ 30 req/min, with batched queries).
- **Disk:** disk full before adding a torrent → skip and log an error.

## 12. Build milestones
1. **Core:** DB schema, AniList OAuth + season fetch, tracking.
2. **Fetching:** Nyaa RSS + parser + probe; preference editing (Season page).
3. **Downloads:** qBit integration, rename, AniList progress (end-to-end for one show).
4. **Library + Today:** Library page, VLC launch.
5. **Housekeeping:** purge, fallback, revisions, Activity page.
6. **Removal and setup:** removal flow, Settings, systemd units, hardening.

## 13. Verified constraints (checked Oct 2026)
**AniList**
- The live limit is **30 req/min** (docs say 90, but the API runs degraded), plus a burst limiter; exceeding either locks you out for 1 min.
  → Use one global token-bucket (≤ 25/min, ≤ 2 in flight) and batch queries (`Page.airingSchedules(mediaId_in: […])`, `MediaListCollection` once).
- A 429 can come back as **Cloudflare HTML**, not JSON → check the status before parsing; honour `Retry-After`, otherwise back off 65 s.
- Cloudflare **rejects generic client User-Agents** (403/1010 seen for `Python-urllib`) → always send `User-Agent: anidash/<ver> (+contact)`.
- A 400 means a refused or expired token → don't retry; flag re-auth.
- Page size: request ≤ 25 (some fields clamp silently).
- `airingSchedules(notYetAired: false)` returns *only* past episodes; querying both needs two calls or no filter.

**Nyaa RSS**
- Feed: `https://nyaa.si/?page=rss&q=<query>&c=1_2&f=0`.
- Fields per item: `nyaa:infoHash`, `nyaa:seeders`, `nyaa:leechers`, `nyaa:downloads`, `nyaa:size`, `nyaa:trusted`, `nyaa:categoryId`.
- `nyaa:size` is a **string** (`"1.1 GiB"`) → parse the units yourself.
- `nyaa:trusted` is `Yes`/`No`.
- Capped at **75 items, no paging** → keep queries narrow (alias + group + resolution) so the target episode is always in the window.
- Nyaa returns 429/blocks **datacenter and shared VPN exit IPs** → the service fetches RSS over your normal connection; only qBit is bound to the VPN interface. If your whole system is on a VPN, expect 429s: back off and make the base URL configurable.

**qBittorrent (≥ 5.0)**
- `pause/resume` were renamed `stop/start` → use `qbittorrent-api` (current release 2026.10.0), which tracks this.
- `torrents/delete` must be a **POST with form data**, not query params; always send `deleteFiles=false` (we trash files ourselves).
- qBit ≥ 5.2 supports an **API key** → prefer it over username/password.
- Add torrents with `savepath`, `category=anidash`, `tags=<anilist_id>`, content layout `NoSubfolder`, so files are found deterministically.

**Filename parser**
- `anitopy` is unmaintained (last release ~4 years ago) → use **anitomy-ng** (Rust port, Python wheels, conformance-tested against anitomy and anitopy fixtures). Pin the version and keep a test set of real SubsPlease and Erai-raws titles.

## 14. Resolved
1. Display title: English, romaji fallback.
2. "Track" sets AniList status CURRENT immediately.
3. All deletions (purge and removal) move to trash first.
4. A v2 replacement restarts the 30-day clock.
