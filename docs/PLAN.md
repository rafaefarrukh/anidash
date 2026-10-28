# anidash for Windows — implementation plan

Goal: anyone on Windows 10/11 installs one `setup.exe`, connects AniList, picks a folder, and it works. No second app to install or configure. Linux keeps working.

## Decisions (settled — don't relitigate)
- **Stay on Python + pywebview + the existing single-file React dashboard.** No Angular/.NET rewrite. Friction comes from packaging and dependencies, not the language.
- **Built-in torrent engine (libtorrent)** is the default; qBittorrent remains an optional backend for people who already use it.
- **Signing:** apply to SignPath Foundation (free for OSS) after the first public release. Azure Artifact Signing is unavailable to individuals outside the US/Canada; no paid certificate.
- **WebView2:** installer detects it and installs it with Microsoft's Evergreen bootstrapper when missing; the browser fallback stays. No Fixed Version bundling (≈150+ MB and goes stale every ~6 weeks).
- **Updates:** check GitHub Releases once a month (plus a "Check now" button). No silent auto-install.
- **Player:** user-selectable. Default *Auto* = a real anime-capable player (VLC/mpv/MPC-HC) if installed, else the system default player, with a dismissable hint to install VLC (Windows' built-in player renders ASS subtitles badly).
- **Files:** the library location is classified (local / removable / network / cloud-synced); downloads stage on the same volume; volumes without an OS trash use an anidash-managed trash folder. Nothing is ever deleted outright.

## How the work is run
- Each phase = one fresh Claude Code cloud session = one PR into `main`. Start the next phase after the previous PR is merged (exceptions under *Parallel*).
- The session reads `CLAUDE.md` and this file, implements its phase, runs `ruff` + `pytest`, pushes, opens a PR, subscribes to PR activity and drives CI (Linux **and Windows** jobs) to green.
- Windows behaviour is verified by CI on `windows-latest`; anything needing a human goes in the phase's **manual Windows checks**, which Rafae runs on his Windows 11 partition before tagging a release.
- Parallel: Phase 5 (player) touches no files Phase 3/4 need beyond `api.py`/dashboard settings; it can run alongside Phase 3 if you accept a small merge.

## Manual tasks (Rafae)
- [x] **M0** Make sure GitHub Actions is enabled (repo → Settings → Actions → General → *Allow all actions*). Every phase relies on the Windows CI jobs; when Phase 0 was pushed, GitHub showed no workflows or runs for the repo.
- [x] **M1** Merge the Phase 0 PR (v2 import + this plan + env hook) into `main`.
- [x] **M2** AniList client registered: name `AniDash`, **client ID `55203`**, redirect `https://anilist.co/api/v2/oauth/pin`. anidash uses the implicit grant, so the client secret is never needed and must never be committed.
- [x] **M3** PyPI: skipped for now. Phase 1 makes the PyPI job opt-in (repo variable `PUBLISH_PYPI=true`).
- [ ] **M4** Cloud environment network access must allow PyPI and GitHub (the default *Trusted* policy does). Nothing else is needed: tests don't hit AniList/Nyaa.
- [ ] **M5** Before each release: run the phase's manual Windows checks on real Windows 11 (and Windows 10 if you can borrow one).
- [ ] **M6** After v2.0.0 is out: apply to SignPath Foundation (signpath.org/apply). Phase 6 prepares the code signing policy page they require.
- [ ] **M7** After v2.0.0: submit the winget manifest Phase 6 generates (PR to `microsoft/winget-pkgs`).

---

## Phase 0 — import v2, environment, plan *(done in the planning session)*
- [x] v2 source replaces v1 in the repo (v1 stays in history at `bc49977`).
- [x] `.claude/hooks/session-start.sh` + `.claude/settings.json`: venv with `-e ".[dev]"`, ruff, pyinstaller, qbittorrent-nox (best effort).
- [x] `CLAUDE.md`, this plan.
- [x] CI also runs on pushes to `claude/**` branches.

## Phase 1 — release plumbing
- [x] 1. `anilist.PUBLIC_CLIENT_ID` = the ID from M2. Setup step 1 no longer asks for a client ID unless the user opens *Use my own client* (keep that path).
- [x] 2. `release.yml`: `publish` depends only on the Windows job; the PyPI job runs only when repo variable `PUBLISH_PYPI == 'true'`, and the wheel/sdist are still attached to the GitHub Release. Generate `SHA256SUMS.txt` for all release assets (Phase 6 verifies downloads with it).
- [x] 3. **Frozen-window check:** add a hidden CLI command `anidash selftest` that imports everything the frozen app needs at runtime but the service smoke test never touches (the pywebview Windows backend `webview.platforms.winforms` + pythonnet/`clr`, `pystray`, `keyring` Windows backend, `send2trash`), prints JSON, exits non-zero on failure. Run it on the frozen exe in `ci.yml` and `release.yml`. Fix the PyInstaller spec until it passes (likely `collect_all("clr_loader")`/`pythonnet`, `collect_data_files`). Note: the exe is windowed (`console=False`), so have `selftest` write its result to a file path given as an argument, and have CI read the file.
- [x] 4. Runtime dir: on Windows put `RUNTIME_DIR` under `DATA_DIR` (not `%TEMP%`, which Storage Sense cleans). Linux unchanged.
- [x] 5. Ruff: add `[tool.ruff]` to `pyproject.toml` (target py311, line-length 130, select `E,F,W,I,B,UP,SIM`; ignore what the existing style deliberately does) so results don't depend on any user-level config, fix or `noqa` with reason, and add a ruff step to `ci.yml`.
- [x] 6. Bump `__version__` to `2.0.0rc1`; CHANGELOG entry.

Manual Windows checks: none yet (CI covers it).

Notes from the Phase 1 session (changes from the plan):
- Setup step 1 keeps the own-client path behind a *Use my own client* link (shown only when a built-in ID exists); saving an empty ID returns to the built-in client.
- `release.yml`: `publish` needs the Windows job and the *build-only* `python` job (so wheel/sdist get attached); the PyPI upload is a separate `pypi` job that `publish` doesn't wait on. `rc` tags are published as pre-releases (Phase 6 ignores pre-releases).
- `selftest` checks are required on Windows only (Linux falls back to the browser/file secrets); CI additionally requires the window backend to be `winforms/edgechromium`. The spec now `collect_all`s `pythonnet` + `clr_loader` and collects `send2trash` submodules.
- Ruff ignores `E501` (long SQL/log lines are the house style; line-length 130 still drives import wrapping) and `B904` (`HTTPException` in `except` is the reply). Other findings were fixed; two deliberate long-lived `open()`s carry `noqa: SIM115`. Ruff is pinned in CI.
- The test for `RUNTIME_DIR` needed a small refactor: `paths._runtime_dir(data_dir)`.

## Phase 2 — Windows installer & runtime
- [x] 1. **WebView2:** in `anidash.iss` `[Code]`, detect the runtime (registry `pv` value under `EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`, both `HKLM\SOFTWARE\WOW6432Node\...` and `HKCU\Software\...`; treat empty or `0.0.0.0` as missing — verify against Microsoft's "Distribute your app and the WebView2 Runtime" doc). If missing, run the bundled Evergreen **bootstrapper** (`MicrosoftEdgeWebview2Setup.exe`, downloaded in CI from Microsoft's fwlink, not committed) with `/silent /install`. If that fails, continue and tell the user the dashboard will open in their browser.
- [x] 2. In the app: when pywebview has no backend on Windows, open the browser (exists) **and** show a one-time notice in the dashboard explaining how to install WebView2 for the in-app window and automatic AniList login.
- [x] 3. **Uninstall:** add an uninstall page/checkbox "Also remove my settings, database and saved logins". When ticked: run `anidash.exe forget --yes` (new CLI command: deletes keyring entries via `vault.delete` for all known keys, the secrets file, the webview profile) then remove `%LOCALAPPDATA%\anidash`. Never touch the library folder. Default unticked.
- [x] 4. Installer polish: closes a running anidash before upgrading (`anidash quit`, then `CloseApplications`), keeps the autostart choice on upgrade, Start-menu shortcut, version shown in *Apps & features*.
- [x] 5. CI: build the installer in `ci.yml` too (not just on tags), run it silently on the runner (`/VERYSILENT /SUPPRESSMSGBOXES`), check `anidash.exe selftest` from the installed location, then uninstall silently and check the app folder is gone.

Manual Windows checks: install on a clean user profile; install with WebView2 removed (or on a VM without it); upgrade over an existing install; uninstall with and without "remove my settings".
Also: with WebView2 missing *and* offline, setup shows the "couldn't install WebView2" message and the dashboard opens in the browser with the one-time notice (Dismiss hides it for good); an interactive upgrade while anidash runs closes it without a Restart Manager prompt; Apps & features shows "anidash" + version; the autostart checkbox is not offered on upgrade and a choice made in Settings survives it.

Notes from the Phase 2 session (changes from the plan):
- Sources: registry detection and `/silent /install` from Microsoft's "Distribute your app and the WebView2 Runtime" (learn.microsoft.com/microsoft-edge/webview2/concepts/distribution, page updated 2026-10-02). The bootstrapper fwlink (`https://go.microsoft.com/fwlink/p/?LinkId=2124703`) is the doc's "Get the Link" target and is what Microsoft's own WebView2Samples WiX sample uses; the download page renders the link with JS, so it couldn't be read from the page itself. CI checks the downloaded file's Authenticode signature is Microsoft's. HKLM is read through Inno's `HKLM32` (= WOW6432Node on x64, the plain key on 32-bit Windows), exactly the doc's two paths.
- Item 2: pywebview doesn't raise without WebView2; it silently falls back to MSHTML (IE), which can't run the dashboard. So `desktop.webview2_version()` does the same registry check before starting the window. The window process tells the service (`POST /api/ui/no-webview`); the banner shows until dismissed (`webview_notice_dismissed` setting). `selftest` gained a non-required `webview2` check that CI requires on its runners, so the Python registry code runs on real Windows.
- Item 3: the uninstall "checkbox" is a Yes/No question defaulting to No (Inno's uninstaller has no wizard pages); `/REMOVEDATA` answers yes for silent uninstalls. `forget` also stops a running instance first and exits 3 when the library is inside the data folder, in which case the folder is kept. The uninstaller always deletes the HKCU Run value (Settings may have created it, which the old `uninsdeletevalue` missed).
- Item 4: `anidash quit --wait` (exit 0 stopped / 1 not running / 2 timed out) waits for the service *process* (psutil), not just its port: the first CI run showed the port closing a few seconds before the exe unlocked, so an uninstall could leave files behind. The service also waits for its window child after terminating it, and a window whose service has been gone for 30 s closes itself (a crashed or killed service no longer leaves a window holding the install folder). A second CI run still caught DLLs locked ~0.5 s after the service exited, so `quit --wait` also waits for every other process running the same `anidash.exe` (terminating leftovers after 5 s), and the uninstaller waits (≤15 s) until `anidash.exe` is deletable before Inno starts removing files. Autostart: the task is offered on fresh installs only, so upgrades never touch the Run value. A silent upgrade (the Phase 6 updater) restarts the instance it stopped (`service`). `UninstallDisplayName=anidash`; setup.exe gets a numeric `VersionInfoVersion` from `make_version.py --print-num`.
- Item 5: the install test lives in `packaging/windows/ci_installer_test.ps1`: fresh install (Apps & features entry, Start-menu shortcut, Run value, selftest from the install folder), upgrade over a running copy with autostart turned off in between (old instance stopped and restarted, autostart still off), uninstall keeping data, reinstall + `/REMOVEDATA` with a secret planted in Credential Manager (folder and secret gone). `release.yml` downloads the bootstrapper too. ISCC is located by glob, so a choco bump to a new Inno major doesn't break the path.

## Phase 3 — built-in torrent engine (libtorrent)
- [x] 1. New `engine.py`: a small interface the rest of the code uses — `add(infohash, name, save_path, anilist_id)`, `torrents()` (hash, name, state, progress, files, save_path, eta), `rename_file`, `remove(hashes)`, `interfaces()`, `bind_interface(value)`, `health()`. Two implementations: `LibtorrentEngine` (default) and `QbitEngine` (wraps today's `qbit.py`). Setting `engine` = `builtin` | `qbittorrent`; existing installs that already completed qBittorrent setup keep `qbittorrent`.
- [x] 2. `LibtorrentEngine` (`libtorrent>=2.0`, wheels exist for Windows/Linux CPython 3.9–3.13): one session owned by the service process; DHT, PEX, LSD, encryption on; listen on a random high port; trackers as in `qbit.TRACKERS`; **no seeding** (stop and remove once the file is in place, matching current rules); resume data saved on alerts and at shutdown to `DATA_DIR/torrents/`; restored on start. VPN kill switch = `outgoing_interfaces` + `listen_interfaces` bound to the chosen interface (list interfaces with `psutil.net_if_addrs`); if the interface disappears, transfers stop — never fall back.
- [x] 3. `jobs.py` talks only to `engine`. Keep behaviour identical: rename to the final name, v2 replacement and its timeouts, removal after completion.
- [x] 4. Setup wizard step 2 becomes **Downloads**: *Built-in (recommended)* with the optional VPN interface picker, or *Use my qBittorrent* (today's auto/manual flow, unchanged). Activity page shows engine progress.
- [x] 5. Add `libtorrent` to dependencies; PyInstaller spec collects its binaries (OpenSSL DLLs on Windows); `selftest` imports it and creates/destroys a session.
- [x] 6. Tests: a fake engine for `jobs` tests; an **offline integration test** that makes a small torrent of a generated `.mkv`, seeds it from one libtorrent session on 127.0.0.1 and downloads it with `LibtorrentEngine` (no DHT/trackers, direct `connect_peer`), then checks rename + removal. Keep the qBittorrent integration test.
- [x] 7. Update README, Outline (process model, setup step 2), CHANGELOG.

Manual Windows checks: first-run setup with built-in engine; a real episode downloads; Windows Firewall prompt behaviour on first listen (document what the user sees; consider adding a per-user firewall rule from the installer if the prompt is confusing); VPN binding stops transfers when the VPN disconnects.

Notes from the Phase 3 session (changes from the plan):
- Interface: `files(hash)` is its own method (qBittorrent needs one request per torrent for it, and jobs only asks for finished ones); `torrents()` fills `files` when it's free (libtorrent). Added `stop(hash)` (jobs pauses a torrent whose rename is still pending) and `close()`. `Torrent.state` uses qBittorrent's state names for both engines, so the dashboard's labels work unchanged. `TRACKERS`/`magnet()` moved from `qbit.py` to `engine.py`.
- Engine choice: setting `engine`; migration 2 sets `qbittorrent` for databases with `setup_done` (and v1 upgrades). New settings `bt_port` (random 49152–65000, picked once) and `bt_interface`. `engine.get()` switches when the setting changes (Settings → Downloads, or setup's two buttons via `POST /api/setup/engine`). Interface endpoints moved to `/api/engine/interfaces` + `/api/engine/interface` and work for both engines.
- No seeding: on `torrent_finished_alert` the torrent is paused (auto-managed off) until `watch_downloads` renames and removes it, so it doesn't upload between finishing and the next job tick.
- VPN kill switch: binding uses the interface's IPv4 address (portable across Windows/Linux device naming) for `listen_interfaces` and `outgoing_interfaces`, with UPnP/NAT-PMP/LSD off while bound. The alert loop re-checks the interface every second: gone → `session.pause()`; new address → re-bind; back → resume. Bound at start, the session starts paused on loopback until the check passes.
- The service starts the engine in the background right after the API is up (saved torrents resume without waiting for a job) and closes it on exit (resume data). The forced-exit timer went from 6 s to 12 s to leave room for that.
- libtorrent wheels (Windows/Linux, CPython 3.9–3.13) link OpenSSL statically: the Windows wheel is one `.pyd`, no separate DLLs; the spec `collect_all`s `libtorrent`. Pinned `>=2.0.11,<2.2`; 2.1's deprecated `torrent_info.files()` is avoided (`layout()` with a 2.0 fallback). AUR depends on `libtorrent-rasterbar` (its Python bindings).
- Tests: `conftest.py` has an autouse `FakeEngine`, so no test starts a real session by accident; `test_integration_libtorrent.py` covers download → rename → removal with two real sessions on 127.0.0.1 (magnet + direct `connect_peer`, no DHT/trackers), stop-on-finish, resume across an engine restart, and the VPN pause/rebind logic (with `_iface_ip` faked). The qBittorrent integration test now runs through `QbitEngine`.


## Phase 4 — library location & file routing (items 8–10)
- [x] 1. New `storage.py`: `classify(path) -> {kind: local|removable|network|cloud, cloud_provider, fs_type, free_bytes, has_os_trash, writable}`.
   - Windows: `GetDriveTypeW` via ctypes (fixed/removable/remote), UNC paths → network, `GetVolumeInformationW` for fs type; cloud = under `%OneDrive%`, `%OneDriveConsumer%`, `%OneDriveCommercial%`, Dropbox (`%APPDATA%\Dropbox\info.json` / `%LOCALAPPDATA%\Dropbox\info.json`), Google Drive for desktop (volume label/fs `GoogleDriveFS` or path under the mounted drive), iCloud Drive (`%USERPROFILE%\iCloudDrive`). OS trash: fixed local drives only.
   - Linux: `/proc/self/mountinfo` fs type (`nfs*`, `cifs`, `smb*`, `fuse.sshfs`, `fuse.rclone`, `fuse.gvfsd-fuse` → network/cloud), `/sys/block/*/removable`. OS trash: whatever send2trash supports for that mount.
- [x] 2. **Default library:** `Videos\Anime` unless Videos is cloud-synced, then `%USERPROFILE%\Anime` (Linux: `~/Videos/Anime`, `~/Anime` likewise).
- [x] 3. **Setup & Settings:** show the classification under the folder picker. Cloud-synced → blocked unless the user ticks "I understand this will upload every episode". Network/removable → warning (no Recycle Bin, may be offline). Show free space; warn under 10 GB.
- [x] 4. **Staging:** downloads go to `<library>\.anidash-incoming\` (same volume ⇒ the final move is an atomic rename). Hide the folder on Windows (`FILE_ATTRIBUTE_HIDDEN`). Clean up orphans on start.
- [x] 5. **Trash policy:** OS trash when `has_os_trash`, else move into `<library>\.anidash-trash\<date>\` and empty entries older than `managed_trash_days` (setting, default 14) — that last step is the only true delete, it is opt-in by setting, and it is confined to `.anidash-trash`. Update CLAUDE.md invariant wording accordingly.
- [x] 6. **Locked files:** `PermissionError` / `winerror 32` on move, rename or trash → record in a `pending_ops` table (new migration) and retry on each poll; never mark the episode failed for this. Activity shows "waiting: file in use".
- [x] 7. **Startup health check:** library missing (unplugged drive / unmounted share), unwritable, or now classified differently → pause downloads for that reason and show a banner with the fix; resume automatically when it's back.
- [x] 8. Tests for every classification branch (monkeypatched Win32/procfs), staging, managed trash, pending ops.

Manual Windows checks: library on C:, on a second internal drive, on a USB stick, on a mapped network drive, inside OneDrive; play an episode in a player while it's due for purge.
Also: a library on a removable drive with the drive unplugged at start (banner, no `Anime` folder created on C:), then plugged back in (resumes).

Notes from the Phase 4 session (changes from the plan):
- `classify()` also returns `mount` (Linux mount point / Windows drive root or UNC share) and `exists`. The health check's fingerprint is `{kind, fs_type, mount}`: without the mount point, an unmounted second ext4 disk would look like the (ext4) system disk and the library would be recreated there. `library_root()` no longer creates the folder; `jobs.check_library()` does, once the volume matches. For installs without a fingerprint yet, a missing library with downloaded episodes counts as unavailable rather than being recreated.
- OS trash on Linux: local and removable mounts (send2trash's per-mount `.Trash-<uid>`); network/cloud use the managed trash, and a `TrashPermissionError` from send2trash also falls back to it. Windows: fixed drives only, as planned.
- `managed_trash_days` defaults to 14 as the plan says; "opt-in" is read as: it's a setting, and 0 turns permanent deletion off entirely. Only `.anidash-trash/<YYYY-MM-DD>/` folders are removed, never symlinks.
- Completion order changed with staging: remove the torrent (files stay, no seeding), trash v1 if this is a replacement, then `os.replace` from `.anidash-incoming/<hash>/` into the show folder. The engine's `rename_file` is no longer used by jobs. On Windows the engine can hold the file for a moment after removal, so that move retries for ~2 s before it is queued in `pending_op`. Torrents already in flight at upgrade (saved in the show folder) finish through the same move.
- Windows CI caught a real race: libtorrent reports a torrent complete once every piece is hash-verified, while some can still be in its write buffer, and an immediate remove + rename captured a short file (851,968 of 1,048,576 bytes). New engine method `finalize(hash)`: libtorrent flushes (`flush_cache`, which also closes its file handles), waits for `cache_flushed_alert`, then removes the torrent; if the flush isn't confirmed the torrent stays paused and jobs retries next tick. jobs also waits for the on-disk size to reach the torrent's before moving (covers qBittorrent) and records the torrent's size.
- Pending ops keep order per file: a move into a path whose old file is waiting to be trashed waits behind it. A pending op whose file vanished is dropped. Activity lists them as "waiting: file in use".
- A cloud library is accepted by the API only with `library_ack` equal to that path (the dashboard's "I understand" checkbox). Saving the folder again re-records the fingerprint, which is the fix the "different storage" banner suggests. Fixed alongside: a refused settings save no longer discards the unsaved form.
- Tests: `test_storage.py` covers every Windows drive type, UNC, Google Drive volume, each cloud-folder source and the Linux mount kinds (fake mountinfo + sysfs tree; the Linux-only ones are skipped on Windows, where that code never runs); `test_file_routing.py` covers managed trash, moves, locked files and ordering, purge of a file in use, the health check (unplugged, unmounted second disk, reclassified, unwritable, upgraded install) and orphan cleanup. The libtorrent integration test now downloads into `.anidash-incoming`, so the Windows CI job exercises the remove-then-move race with a real engine.


## Phase 5 — player choice
1. Settings → **Player**: Auto / System default / VLC / mpv / MPC-HC / MPC-BE / Custom (path + arguments). Detected players are listed with their paths; undetected ones greyed out.
2. Detection: Windows registry + Program Files + PATH (VLC: existing logic; mpv: PATH, `%LOCALAPPDATA%\Programs\mpv`, scoop/winget shims; MPC-HC: `HKCU\Software\MPC-HC\MPC-HC` `ExePath`, Program Files; MPC-BE similarly). Linux: PATH + Flatpak (`org.videolan.VLC`, `io.mpv.Mpv`).
3. Auto = first of VLC → mpv → MPC-HC → MPC-BE, else system default (`os.startfile` / `xdg-open`).
4. If Auto falls back to the system default on Windows, show a dismissable hint once: subtitles may render incorrectly; "Install VLC" runs `winget install -e --id VideoLAN.VLC` (argument list, never a shell) when winget exists, else opens videolan.org.
5. Migration: `vlc_path`/`vlc_args` → `player=custom` (if a path was set) or `auto`.
6. Tests for selection order, migration, argument handling (`shlex` posix vs Windows).

Manual Windows checks: Play with each installed player, with none installed, with a path containing spaces and non-ASCII characters.

## Phase 6 — updates, docs, release prep
1. **Update check:** monthly (setting: monthly / off; stores `update_checked_at`), plus *Check now* in Settings. `GET https://api.github.com/repos/rafaefarrukh/anidash/releases/latest`, compare versions (ignore pre-releases unless the running version is one). Banner: version, release notes link.
   - Windows: *Update* downloads the `setup.exe` asset to a temp folder, verifies it against `SHA256SUMS.txt`, launches it with `/SILENT`, and quits anidash. Linux: show the right command (`paru -Syu anidash` / `pipx upgrade anidash`).
2. `docs/code-signing-policy.md` (what SignPath requires: who builds, who approves, privacy note) and a README link to it.
3. `packaging/winget/` manifest templates (version, installer URL, SHA256, `InstallerType: inno`, `Scope: user`) and a small script that fills them from a release.
4. README: rewrite *Install* and *First run* for the new flow (no qBittorrent, no client ID); add a Windows FAQ (SmartScreen "More info → Run anyway" until signing lands, WebView2, firewall prompt, where files go). CHANGELOG for 2.0.0. `Outline.md` §8–10 updated.
5. Bump to `2.0.0`. Do **not** tag; Rafae tags `v2.0.0` after M5.

Manual Windows checks: update flow from rc to 2.0.0 (publish a test pre-release on a fork or use a draft release).

---

## Prompts

Paste one into a new Claude Code cloud session on this repository after the previous phase's PR is merged. Replace `<…>` placeholders. Each prompt points the session at *Session rules* below, so one paste is enough.

### Session rules (every phase session follows these)
```
Read CLAUDE.md and docs/PLAN.md first. You are implementing exactly one phase of docs/PLAN.md; the decisions section is settled, don't revisit it. Work on the branch this session gives you, starting from the latest main.

Rules:
- Match the existing code style; keep diffs focused on this phase.
- Before every push: `ruff check src tests` and `pytest -q` must pass locally.
- This container is Linux. Verify Windows-specific code through the Windows CI jobs (read runs and logs with the GitHub MCP tools). Never claim something was tested on Windows that wasn't; list it under "Manual Windows checks" in the PR body instead.
- Update docs/PLAN.md: tick the items you finished in this phase, and note anything you changed from the plan and why.
- When done: push, open a PR to main (title "Phase N: …"; body = summary, test results, manual Windows checks), subscribe to its PR activity and drive CI to green on both Linux and Windows. Tell me when it's green and what I need to check by hand.
```

### Phase 1
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 1 (release plumbing) of docs/PLAN.md. The AniList client ID to ship is 55203. PyPI publishing: skip for now.
```

### Phase 2
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 2 (Windows installer & runtime) of docs/PLAN.md. The installer must stay per-user (no admin). Check Microsoft's current WebView2 distribution docs for the registry detection and bootstrapper URL before writing the [Code] section, and cite the doc in a comment.
```

### Phase 3
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 3 (built-in libtorrent engine) of docs/PLAN.md. Keep every download rule from Outline.md §6 identical, including no seeding and the v2 replacement timeouts. qBittorrent must keep working as the optional backend, and its existing tests must still pass. The offline two-session libtorrent integration test is required.
```

### Phase 4
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 4 (library location & file routing) of docs/PLAN.md. "Nothing is deleted outright" stays the rule: the only permanent delete is emptying .anidash-trash after managed_trash_days, confined to that folder. Unit-test every storage classification branch with monkeypatching so it runs on Linux.
```

### Phase 5
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 5 (player choice) of docs/PLAN.md. Migrate existing vlc_path/vlc_args settings with a DB migration and a test.
```

### Phase 6
```
Read CLAUDE.md and docs/PLAN.md, and follow the "Session rules" in docs/PLAN.md.
Implement Phase 6 (updates, docs, release prep) of docs/PLAN.md. Don't create a tag or release; stop at a green PR with version 2.0.0.
```

### Release (after M5 passes)
```
Read CLAUDE.md and docs/PLAN.md. main is at 2.0.0 and manual Windows checks passed. Check that release.yml will produce: the Windows setup.exe, SHA256SUMS.txt and (if PUBLISH_PYPI is set) the PyPI upload. Fix anything wrong via a PR. Then tell me the exact commands to tag v2.0.0; after I push the tag, watch the Release workflow and fill in packaging/winget from the published assets.
```
