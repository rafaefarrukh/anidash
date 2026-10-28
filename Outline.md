# anidash — Outline v2

v2 makes anidash usable by other people: Linux first, Windows second, with an in-app first-run setup. Rules and data model carry over from v1 unless noted. v1's outline is in git history (`v1` tag / commit `bc49977`).

## 1. Decisions locked
- **Platforms:** Linux (any desktop, plus headless) and Windows 10/11. macOS is not a target.
- **Distribution:** PyPI (installed with pipx), an AUR PKGBUILD, and a Windows per-user installer (PyInstaller onedir + Inno Setup). CI builds and tests on both OSes.
- **Torrent engine:** built-in libtorrent (`engine.LibtorrentEngine`, default); qBittorrent 4.4+/5.x stays available as an optional backend (`engine.QbitEngine`), set up automatically by the app.
- **Shell:** a background process with a tray icon (pystray). The dashboard is a separate pywebview window process. No systemd in the default path; autostart uses XDG autostart or the HKCU Run key.
- **Extras (MAL, other players, LAN access, other indexers):** out of scope for v2.
- Unchanged from v1: AniList is the source of truth, *watched = downloaded*, 30-day retention, fallback to preference 2 after 24 h, deletes go to the Trash only (or anidash's own `.anidash-trash` where the volume has none), no seeding.

## 2. Process model
```
anidash (launcher / autostart)
 ├─ single-instance lock ($RUNTIME/instance.lock); 2nd launch → POST /api/ui/show → exit
 ├─ main thread: tray icon  (falls back to "headless" when no tray host)
 ├─ thread: uvicorn + FastAPI on 127.0.0.1:<8787 or next free>
 ├─ thread: APScheduler jobs + resume watchdog (wall-clock vs monotonic jump > 2 min → catch-up)
 ├─ thread: libtorrent session alerts (built-in engine: resume data, stop-on-finish, VPN binding check)
 └─ child process: `anidash window` (pywebview; WebKitGTK / Qt / WebView2; browser fallback; on Windows
                    WebView2 is checked in the registry first, since pywebview would fall back to MSHTML)
runtime.json (mode 600): {port, token, pid, version}: how the window and the CLI find the service
```
Why separate processes: GTK/WinForms and the tray each want the main thread, so mixing them in one process is fragile.

## 3. Paths (platformdirs)
| | Linux | Windows |
|---|---|---|
| data (DB, webview session) | `~/.local/share/anidash` | `%LOCALAPPDATA%\anidash` |
| config (secrets fallback, v1 import) | `~/.config/anidash` | `%LOCALAPPDATA%\anidash` |
| logs (rotating 3×2 MB) | `~/.local/state/anidash/log` | `%LOCALAPPDATA%\anidash\Logs` |
| runtime | `$XDG_RUNTIME_DIR/anidash` | `%LOCALAPPDATA%\anidash\run` (not `%TEMP%`: Storage Sense empties it) |
| library default | `~/Videos/Anime` | `Videos\Anime` |

`ANIDASH_HOME` puts everything under one folder (tests, portable use).

## 4. Data model changes (migration 1, `PRAGMA user_version`)
- `anime.skip_through`: AniList progress when the show was tracked. Episodes ≤ this are inserted as `skipped`.
- `episode.replace_started`: timestamp for the v2-replacement timeout.
- New episode state `skipped` (never fetched, hidden).
- Settings: v1 `config.toml` (port, client ID, qBit user) is folded into the `settings` table. `seed_ratio` and `seed_hours` are dropped. Added: `backlog`, `vlc_path`, `qbit_username`, `anilist_client_id`, `token_expires`, `setup_done`.

## 5. First-run setup (in the dashboard)
1. **AniList:** implicit grant with redirect `…/oauth/pin`. In the app window, a child window opens the authorize URL; the token is read from the redirect fragment (`#access_token=…&expires_in=…`), with the pin page's textarea as a fallback. In a plain browser: new tab plus paste. Uses the built-in public client (`anilist.PUBLIC_CLIENT_ID` = 55203); setup step 1 only asks for an ID when the user opens *Use my own client* (or a build ships without one).
2. **Downloads:** *Built-in (recommended)*: nothing to configure; optional VPN interface. Or *Use my qBittorrent*:
   - *detect* finds: native (`~/.config/qBittorrent/qBittorrent.conf`), nox, Flatpak (`~/.var/app/org.qbittorrent.qBittorrent/…`), Windows (`%APPDATA%\qBittorrent\qBittorrent.ini`, exe from the registry or Program Files).
   - *wait until closed*: qBit rewrites its config on exit, so the page polls every 3 s.
   - *write `[Preferences]`*: `WebUI\Enabled=true`, `Address=127.0.0.1`, `Port`, `Username=anidash`, `Password_PBKDF2="@ByteArray(salt:dk)"` (PBKDF2-HMAC-SHA512, 100k iterations, 16-byte salt), `LocalHostAuth=true`, `HostHeaderValidation=true`, `CSRFProtection=true`. Also `[LegalNotice] Accepted=true`.
   - Line-based INI edit; a timestamped backup is kept.
   - *start* qBit, then log in.
   - Manual path for people who already use the Web UI.
   - Optional: bind to an interface via `app/setPreferences current_network_interface`.
   - The choice is the `engine` setting (`builtin` | `qbittorrent`); migration 2 keeps `qbittorrent` for installs that had finished setup.
3. **Library:** folder, backlog toggle, autostart toggle.

Finishing sets `setup_done` and runs `sync_schedule` (imports the Watching list).

## 6. Rules: v2 changes
- **Mid-season tracking:** new rows are `skipped` when `number ≤ skip_through`, or when backlog is off and the episode aired before tracking. Give-up clock = `search_from` or `max(airs_at, added_at)`.
- **Query cache:** one Nyaa query per (group, alias, resolution) per poll run, so a 10-episode backlog costs 1–2 requests.
- **Download engine (`engine.py`):** jobs and the API only call `engine.get()` (`add`, `torrents`, `files`, `rename_file`, `stop`, `remove`, `interfaces`, `bind_interface`, `health`). Built-in: one libtorrent session in the service process; DHT, PEX, LSD, encryption on; a random high listen port picked once (`bt_port`); trackers as in `engine.TRACKERS`; resume data in `DATA_DIR/torrents/*.fastresume`, saved on add, on metadata, on finish, every minute if changed and at shutdown, restored at start. **No seeding:** a finished torrent is paused at once and removed after the rename. **VPN kill switch:** with `bt_interface` set, listen and outgoing sockets bind to that interface's IPv4 address (UPnP/NAT-PMP/LSD off); when the interface is down the session pauses, never falling back; it follows address changes and resumes when it's back. Bound at start, the session starts paused on loopback until the interface is checked.
- **Error isolation:** a failure on one episode is logged and the poll continues. `NyaaBusy` stops the run. Network errors back off 5 min; 429/403/503 back off 15 min.
- **Replacement timeout:** if the v2 torrent vanishes for more than 10 min, or isn't finished within 24 h, keep v1, put the v2 hash in `skipped`, and log a warning.
- **Naming:** Windows-safe on every OS: `Title: Sub` → `Title - Sub`; `:"<>|?*` removed or replaced; no trailing dots or spaces; reserved device names get a `_` prefix; 150-character cap. Path confinement compares `normcase`d resolved paths.
- **Trash:** send2trash (freedesktop Trash, including the per-mount `.Trash-uid` on shared partitions; Recycle Bin on Windows) when `storage.classify(library).has_os_trash` (Windows: fixed drives; Linux: local and removable mounts); otherwise, or when send2trash refuses (`TrashPermissionError`), `<library>/.anidash-trash/<YYYY-MM-DD>/<relative path>` (name clashes get ` (2)`). `purge` empties dated folders older than `managed_trash_days` (default 14, 0 = never): the only permanent delete, confined to `.anidash-trash`, never following symlinks.
- **Library location (`storage.py`):** `classify(path)` → `kind` local|removable|network|cloud, `cloud_provider`, `fs_type`, `mount`, `free_bytes`, `has_os_trash`, `writable`, `exists`. Windows: `GetDriveTypeW`/`GetVolumeInformationW` (UNC → network; Google Drive's `GoogleDriveFS` volume → cloud). Linux: the longest matching mount in `/proc/self/mountinfo` (nfs/cifs/smb/sshfs/gvfs → network; rclone/onedriver → cloud) and `/sys/class/block` (removable flag or a USB parent → removable). Cloud folders by path on both: `%OneDrive*%`, Dropbox's `info.json`, iCloud Drive, and on Linux the sync clients' default folders. A cloud library needs `library_ack` = that path. Default library: `Videos/Anime`, or `~/Anime` if Videos is cloud-synced.
- **Staging:** torrents download into `<library>/.anidash-incoming/<infohash>/` (hidden on Windows). On completion the torrent is removed (no seeding), then the file is moved with `os.replace` (same volume ⇒ atomic) to `<Title>/<Title> - NN.ext`; an existing target goes to the trash first, never overwritten. Staging folders no torrent or waiting move uses go to the trash once per start.
- **Files in use:** a move or trash that hits `PermissionError` / winerror 5, 32, 33 becomes a `pending_op` row (migration 3), retried in order at every `watch_downloads`; later operations on the same file wait behind it. The episode is never failed for this; Activity shows "waiting: file in use".
- **Library health:** the volume's fingerprint (`kind`, `fs_type`, `mount`) is recorded when the folder is chosen. Before each poll/download/purge job: folder missing on a different volume (unplugged drive, unmounted share, or an empty mount-point folder), different storage, or unwritable → those jobs skip and a banner explains; they resume on their own. The library folder is created only when the volume matches (never on the system disk in place of a missing drive).
- **Player:** `vlc_path` → `vlc` on PATH → Windows registry/Program Files → Flatpak VLC → system default player.
- **Pause:** a tray or dashboard toggle; scheduled jobs skip while paused, and *Run now* still works.
- **v1 guard:** if v1's systemd unit is present and something non-v2 answers on :8787, start paused with a banner.

## 7. API additions
`GET /api/health` → `{app, version, setup_done, v1_conflict, paused, no_webview}` · `POST /api/ui/show`, `POST /api/ui/no-webview` (the Windows window process fell back to the browser; the dashboard shows a one-time WebView2 notice) and `/dismiss`, `GET /api/ui/seq` (the window raises itself when the seq changes) · `POST /api/app/quit`, `POST /api/app/pause` · `GET /api/setup` · `GET /api/setup/qbit` · `POST /api/setup/qbit/auto {index}` · `GET|POST /api/setup/qbit/interface(s)` · `POST /api/setup/finish` · `POST /api/settings/test-qbit {url, username, password}` (tests and saves) · `POST /api/account/token {token, expires_in}`. Other changes:
- `/api/season` defaults to the current season, returns `airs_at` (ms) and the AniList `list_status`, and returns 502 on AniList errors (v1 returned `[]`).
- `/api/anime` returns `airs_at` (ms) instead of a server-formatted label; the browser formats it in local time.

## 8. Packaging
- `pyproject.toml` (hatchling): GUI entry point `anidash`, console `anidash-cli`; `[qt]` extra for a pip-only Linux window. `libtorrent` (2.0.11–2.1, wheels with OpenSSL linked in) is a dependency; the PyInstaller spec `collect_all`s it and `selftest` starts and stops a session.
- Linux pipx needs the distro's PyGObject + WebKitGTK 4.1 + Ayatana AppIndicator (`--system-site-packages`). The first run writes a per-user `.desktop` entry and icon.
- AUR: `packaging/arch/PKGBUILD`. `python-pywebview`, `python-pystray` and `python-qbittorrent-api` are assumed to be AUR; verify before publishing.
- Windows: `packaging/windows/anidash.spec` (onedir, windowed, version resource) + `anidash.iss`: per-user install to `%LOCALAPPDATA%\Programs\anidash`, optional autostart (fresh installs only; upgrades keep the Run value as it is) and desktop icon, *Apps & features* name + version. Installs the WebView2 Runtime with the bundled Evergreen bootstrapper (`/silent /install`, per-user) when the registry shows none; if that fails, setup says the dashboard will open in the browser. Upgrades run `anidash quit --wait` first (then Restart Manager), and a silent upgrade restarts the instance it stopped. Uninstall stops anidash, removes the Run value and keeps user data unless the user answers yes to "Also remove my settings, database and saved logins" (or passes `/REMOVEDATA`): then `anidash forget --yes` (keyring entries, secrets file, webview profile) and `%LOCALAPPDATA%\anidash` is deleted, unless the library lives inside it. The library is never touched.
- CI (`.github/workflows/ci.yml`): pytest on Ubuntu + Windows × Python 3.11/3.13 (the Ubuntu job also runs the real-qbittorrent-nox integration test), plus `ruff check`, then the frozen exe runs `anidash selftest <out.json>` (imports the WebView2/pythonnet window backend, tray, keyring and trash backends, and runs the WebView2 registry check; hidden command) and a service smoke test, and the installer is built and exercised silently (`packaging/windows/ci_installer_test.ps1`: install, selftest from the install folder, upgrade over a running copy, uninstall keeping and removing data). `release.yml` on `v*` tags: Windows installer (after the same selftest), sdist/wheel and `SHA256SUMS.txt` attached to the GitHub Release (`rc` tags are pre-releases); PyPI upload only when the repo variable `PUBLISH_PYPI` is `true`.

## 9. Before the first public release
1. ~~Register the public AniList client and set `PUBLIC_CLIENT_ID`.~~ Done: 55203.
2. Confirm the license (MIT assumed) and that bundling Archivo (OFL) and React (MIT) is noted.
3. Verify the AUR dependency names; fill in `sha256sums` with `updpkgsums`.
4. Set up the PyPI trusted publisher for `rafaefarrukh/anidash`.
5. Manual test on real desktops: KDE + GNOME (with and without the AppIndicator extension), Windows 11 (WebView2, tray, Recycle Bin, qBittorrent auto-setup with the desktop client).
6. Consider signing the Windows installer; unsigned builds trigger SmartScreen warnings.

## 10. Known limits
- GNOME without the AppIndicator extension has no tray icon (the app still runs; use the launcher).
- qBittorrent running as a system service (`qbittorrent-nox@user`) must be stopped and started by the user around auto-setup.
- Auto-setup replaces an existing Web UI login, and the UI warns first. People who already use the Web UI should use the manual path.
- Nyaa can be ISP-blocked (a mirror is configurable). The RSS cap of 75 items per query still applies.
