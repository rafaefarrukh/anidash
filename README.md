# anidash

**Set-and-forget anime downloads for Arch-based Linux.** anidash watches your [AniList](https://anilist.co) "Watching" list, finds new episodes on [Nyaa](https://nyaa.si) from the release groups you prefer, downloads them with qBittorrent, names them `~/Anime/<Title>/<Title> - NN.mkv`, updates your AniList progress, and moves each file to the Trash 30 days after download. You watch in VLC, from the dashboard or however you like.

It runs entirely on your machine: a local background service plus a small desktop window. There is no cloud component and no account other than your own AniList.

## Contents

- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [First-time setup details](#first-time-setup-details)
- [Using the dashboard](#use)
- [Where things live](#files)
- [Logs and control](#logs-and-control)
- [How it decides what to download](#rules-in-short)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Uninstall](#uninstall)

## What you need

- **Linux with pacman and systemd** (written for EndeavourOS; other Arch-based distros should work). The installer uses `pacman`, so other distros need to install the equivalent packages by hand.
- **An AniList account.** You will create a free API client in step 1 of setup (about two minutes).
- **sudo access**, used only for installing packages and enabling qBittorrent.
- A desktop session (GTK/WebKit) for the dashboard window.

The installer pulls in everything else: `python`, `python-gobject`, `webkit2gtk-4.1`, `vlc`, `qbittorrent-nox`, `libsecret`, `glib2`.

## Quick start

```bash
git clone https://github.com/rafaefarrukh/anidash.git
cd anidash
chmod +x install.sh
./install.sh
```

Run it as your normal user (not root). Then open **anidash** from your app menu.

The installer:
1. installs missing packages with pacman;
2. copies the app to `~/.local/share/anime-dl/app` and builds a Python venv beside it;
3. offers to enable `qbittorrent-nox@$USER` and shows its first-run temporary password;
4. runs `anidash setup` (AniList login, qBittorrent login, library folder);
5. enables `anidash.service` and `anidash-kick.timer`, then adds an **anidash** launcher to your app menu.

Re-running `install.sh` upgrades in place and keeps your data.

## First-time setup details

### AniList login (asked during setup)
1. Go to <https://anilist.co/settings/developer> → **Create New Client**.
2. Name: `anidash`. Redirect URL: `https://anilist.co/api/v2/oauth/pin`.
3. Paste the Client ID into setup. A browser tab opens; approve it and paste the token it shows.

Tokens last a year. Settings warns you 14 days before expiry; **Reconnect** there repeats step 3.

### qBittorrent
- Web UI at `http://127.0.0.1:8080` (change it in Settings). Use a real password and leave *Bypass authentication for localhost* **off**.
- Bind qBittorrent to your VPN interface (Options → Advanced → Network interface). Leave the rest of the system off the VPN: Nyaa blocks most VPN exit IPs, and the RSS feed is fetched over your normal connection.

## Use

- **Season:** pick the season, then **Track** shows (this sets them to Watching on AniList). Use **Probe all** to see which groups and resolutions exist, then pick preference 1 and 2 for each show and **Save**. Shows already on your AniList Watching list are imported by **Sync AniList**.
- **Library:** the Today card lists the last 24 hours. Use **Play** for the latest episode; the episode list shows how many days are left before each one is deleted.
- **Activity:** downloads in progress, job status with **Run now**, and the log.
- **Remove a show:** turn on Remove mode → ✕ → choose a status and score. AniList is updated first. If that fails, nothing is deleted.

## Files

| What | Where |
|---|---|
| Bootstrap config (port, client ID, qBit user) | `~/.config/anime-dl/config.toml` |
| Database (settings, shows, episodes, log) | `~/.local/share/anime-dl/anidash.db` |
| App + venv | `~/.local/share/anime-dl/app`, `…/venv` |
| AniList window session | `~/.local/share/anime-dl/webview` |
| Secrets (AniList token, qBit password) | system keyring, service `anidash` |
| Library | `~/Anime` (Settings) |

## Logs and control

```bash
journalctl --user -u anidash -f          # live log (also shown on the Activity page)
systemctl --user restart anidash
systemctl --user list-timers anidash-kick.timer
~/.local/share/anime-dl/venv/bin/python -m anidash kick poll_releases   # run a job now
```

The service runs its own schedule: release polling every 20 minutes, download checks every minute, AniList retries every 15 minutes, schedule sync at 06:00 and purge at 04:00. `anidash-kick.timer` is `Persistent=true`, so after suspend or a reboot it fires once and the service catches up on anything it missed.

## Rules in short

- Preference 1 only for the first 24 h after airing, then preference 2. After 7 days the episode is marked failed; **Retry** restarts the clock with both preferences allowed.
- An episode is accepted only when it is a single `.mkv`/`.mp4` between 50 MB and 3 GB, from a trusted uploader if *Trusted only* is on, and its number after the offset matches.
- If a v2 appears within 3 days, it silently replaces v1 (v1 goes to the Trash) and the 30-day clock restarts.
- Nothing is ever deleted outright: purge and removal use `gio trash`, only inside the library folder.

## Security

- Listens on `127.0.0.1:8787` only, and checks the Host header against DNS rebinding.
- Every mutating API call needs a per-start random token (injected into the dashboard page; the timer reads it from `$XDG_RUNTIME_DIR/anidash.token`, mode 600) plus a same-origin check.
- VLC and `gio` are run with argument lists, never a shell. Downloaded files are never executed.

## Troubleshooting

- **"Demo data · service not reachable" in the header:** the service is down. Run `systemctl --user status anidash`.
- **Keyring errors at boot:** the service starts before your wallet is unlocked. It recovers on the next job once you log in. On KDE, make sure KWallet's Secret Service integration is enabled.
- **Nyaa 429/403 in the log:** your connection is on a VPN or shared IP. The service backs off for 15 minutes; you can set a mirror in Settings → Nyaa base URL.
- **Run the tests:** `cd ~/.local/share/anime-dl/app && ../venv/bin/python -m pytest -q tests`

## Development

```
anidash/        Python service: api.py (FastAPI), scheduler.py + jobs.py, anilist.py, nyaa.py,
                qbit.py, parser.py, db.py (SQLite), vault.py (keyring), cli.py, desktop.py
Dashboard.dc.html, support.js, _ds/   Front-end served by the service
systemd/        User service and the catch-up timer
tests/          Parser tests
Outline.md      Design notes and architecture
Wireframes.dc.html   UI wireframes
```

To hack on it without installing, create a venv with `--system-site-packages` (so `python-gobject` is visible), `pip install -r requirements.txt`, then run `python -m anidash serve` (service only) or `python -m anidash window` (desktop window). See `Outline.md` for the design and data model.

## Uninstall

`./uninstall.sh` removes the app, units and launcher but keeps your library, database and config. `./uninstall.sh --purge` also deletes the database, config and keyring entries. `~/Anime` is never touched.
