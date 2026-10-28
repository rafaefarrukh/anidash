# Changelog

## 2.0.0rc1 — unreleased

Release plumbing for the Windows app.

- AniList login works out of the box with the built-in public client (ID 55203). Setup only asks for a client ID under *Use my own client*.
- Windows: the runtime folder (lock + `runtime.json`) moved from `%TEMP%` to `%LOCALAPPDATA%\anidash\run`, so Storage Sense can't delete it while anidash runs.
- Hidden `anidash selftest [out.json]` checks the window, tray, keyring and trash backends; CI and releases run it on the frozen exe. The PyInstaller spec now bundles pythonnet/clr_loader.
- Releases: GitHub Release no longer waits on PyPI (opt-in via repo variable `PUBLISH_PYPI=true`), ships `SHA256SUMS.txt`, and marks `rc` tags as pre-releases.
- Ruff config in `pyproject.toml` and a lint job in CI.

### Player choice

- Settings → Player: *Auto* (VLC → mpv → MPC-HC → MPC-BE, else the system default), a specific player, the system default, or a custom program with arguments (`{file}` places the episode). Installed players are listed with their paths; missing ones are greyed out.
- Detection covers PATH, the Windows registry, Program Files, scoop/winget shims, and Flatpak on Linux.
- If Auto falls back to Windows' own player, a one-time hint offers to install VLC (winget, or the VideoLAN page).
- Your VLC settings carry over (database migration 4): a VLC path you set becomes a custom player with your arguments; otherwise Auto.

### Library location & file routing

- Setup and Settings show where the library lives (local disk, removable drive, network share, cloud-synced folder) with free space. A OneDrive/Dropbox/Google Drive/iCloud folder needs an explicit "I understand this will upload every episode"; network and removable folders get a warning; under 10 GB free gets a warning.
- The default library avoids a cloud-synced Videos folder (`~/Anime` instead).
- Downloads go to a hidden `.anidash-incoming` folder in the library and are moved into place when finished (one rename, no half-written files in the library). Leftover staging folders are cleaned up at start.
- Where there is no Recycle Bin/Trash (network shares, cloud folders), deleted episodes go to a hidden `.anidash-trash` folder in the library, emptied after 14 days (Settings, 0 = never).
- A file that is in use (e.g. open in a player when it's due for deletion) no longer causes errors: the move or deletion waits and retries on every check (database migration 3).
- If the library's drive is unplugged, its share isn't mounted, or it becomes unwritable, downloads pause with a banner and resume by themselves once it's back. anidash no longer recreates a missing library folder on the system disk.

### Built-in download engine

- **No qBittorrent needed:** anidash now downloads with its own BitTorrent engine (libtorrent: DHT, peer exchange, local discovery, encryption). Setup step 2 is *Downloads*: *Built-in (recommended)* or *Use my qBittorrent* (the old flow, unchanged). Switch any time in Settings → Downloads.
- Existing installs that finished setup keep using qBittorrent (database migration 2).
- Same rules as before: no seeding (a finished torrent stops at once and is removed after the rename), v2 replacement and its timeouts, renaming into the library.
- Torrents resume after a restart (resume data in the data folder).
- VPN kill switch for the built-in engine: bind to an interface; if it goes down, transfers pause and never fall back to another connection.
- `anidash selftest` starts and stops a libtorrent session; the Windows build bundles libtorrent.

### Windows installer & runtime

- The installer installs the Microsoft Edge WebView2 Runtime when it is missing (Evergreen bootstrapper, per-user, no admin). If that fails it says so and anidash uses the browser.
- Windows without WebView2: anidash now detects that itself (pywebview would fall back to Internet Explorer's engine), opens the dashboard in the browser and shows a one-time notice on how to get the window and one-click AniList login.
- Uninstall asks "Also remove my settings, database and saved logins?" (default no; `/REMOVEDATA` for silent uninstalls) and always removes the autostart entry. The library folder is never touched.
- Upgrades stop a running anidash first (and restart it after a silent upgrade), keep your autostart choice, and *Apps & features* shows "anidash" with its version.
- New CLI: `anidash forget --yes` (delete saved logins and the window profile) and `anidash quit --wait`.
- CI builds the installer on every push and installs, upgrades and uninstalls it silently on Windows.

## 2.0.0 — unreleased

v2 makes anidash installable by other people, on Linux and Windows.

### New
- **Windows 10/11** support: per-user installer, Recycle Bin, Credential Manager, VLC found in Program Files/registry.
- **Packaging:** pipx (`anidash` on PyPI), AUR PKGBUILD, Windows installer (PyInstaller + Inno Setup), GitHub Actions CI on Linux + Windows.
- **Tray icon** (open, check now, pause, quit) replaces the systemd service; **start at login** via XDG autostart / the Run key. `anidash install-systemd` remains for headless boxes.
- **First-run setup in the app** (was a terminal wizard):
  - AniList login in a child window with the token captured automatically (no copy-paste). A built-in public client ID can be shipped; users can still use their own.
  - qBittorrent auto-setup: finds the install (native, nox, Flatpak, Windows), enables the Web UI on 127.0.0.1 with a random PBKDF2 login, backs up the config, restarts qBittorrent.
  - Optional VPN interface binding (kill switch).
- **Backlog handling:** tracking mid-season skips episodes up to your AniList progress and fetches the aired rest; their give-up clock starts when you track.
- Pause automation; quit from the dashboard; secrets fall back to a mode-600 file when there is no keyring.
- Falls back to the system's default player when VLC is missing; falls back to the browser when no webview backend exists.

### Fixed (v1 issues)
- Tracking mid-season no longer marks old episodes failed or re-downloads watched ones.
- One slow AniList call no longer flips the whole dashboard to demo data; every panel loads on its own, and *Track* returns at once.
- A v2 replacement that vanishes from qBittorrent or stalls for a day is abandoned (v1 kept) instead of looping forever.
- The season defaults to the current one instead of FALL 2026.
- The UI works offline: React and the Archivo font are bundled (v1 loaded them from unpkg and Google Fonts).
- Nyaa is no longer polled twice per interval (the systemd kick timer is gone; a resume watchdog catches up after sleep), and one query now serves a whole backlog.
- Per-episode errors no longer abort the whole poll; Nyaa network errors back off for 5 minutes instead of logging an error every poll.
- Leftover seeding settings removed; `anitomy-ng` dropped in favour of the tested regex parser (22 fixtures).
- Names are Windows-safe everywhere (reserved names, `:"<>|?*`, trailing dots).
- Only one instance can run; a second launch focuses the first.

### Migration
- On Linux the v1 database and `config.toml` are imported from `~/.local/share/anime-dl` on first start. If the v1 systemd service is still running, v2 pauses itself and says how to stop v1.
