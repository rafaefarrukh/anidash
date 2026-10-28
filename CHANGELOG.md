# Changelog

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
