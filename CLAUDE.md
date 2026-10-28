# anidash — notes for Claude Code

anidash follows the user's AniList *Watching* list, finds new episodes on Nyaa, downloads them, names them `<Library>/<Title>/<Title> - 05.mkv`, syncs AniList progress and moves files to the Trash after 30 days. Python service + tray + pywebview window; dashboard served on 127.0.0.1.

**Current work:** making it a low-friction Windows app (Linux stays supported). The phased plan, decisions and per-phase status live in `docs/PLAN.md`. Read it before starting; tick your phase's checklist there as you go.

## Layout
- `src/anidash/` — the package. `service.py` (process entry: API thread + scheduler + tray), `api.py` (FastAPI, all endpoints), `jobs.py` (poll/finish/purge logic), `qbit.py` + `qbit_setup.py` (torrent client), `files.py` (naming, path confinement, trash), `player.py`, `paths.py` (platformdirs), `vault.py` (keyring), `autostart.py`, `desktop.py` (pywebview window, separate process), `db.py` (SQLite + migrations via `PRAGMA user_version`).
- `src/anidash/web/Dashboard.dc.html` — the whole UI: one HTML file, React (vendored) with JSX inside `<script type="text/x-dc">`, compiled in-browser by `support.js`. **No build step.** Opened as a plain file it shows demo data, so it can be checked with Playwright (Chromium is preinstalled; `require('playwright')` / `from playwright.sync_api import ...` if installed).
- `packaging/windows/` — PyInstaller spec (onedir, windowed) + Inno Setup script (per-user, no admin). `packaging/arch/` — PKGBUILD.
- `.github/workflows/ci.yml` — pytest on Ubuntu + Windows × Py 3.11/3.13, then a frozen-exe build and smoke test on Windows. Runs on pushes to `claude/**` branches and on PRs.
- `Outline.md` — v2 design spec (process model, data model, rules). Keep it in sync when you change behaviour.

## Commands
```bash
pytest -q                          # whole suite (~4 s); the qBittorrent integration test needs qbittorrent-nox
ruff check src tests               # lint (config in pyproject.toml; CI pins the version)
anidash service --no-tray          # run headless, then http://127.0.0.1:8787
ANIDASH_HOME=/tmp/x anidash ...    # isolate all data in one folder
```
The SessionStart hook (`.claude/hooks/session-start.sh`) creates `.venv` with `-e ".[dev]"`, ruff and pyinstaller, and puts it on PATH.

## Windows can't be run here
This container is Linux. Windows-only code (`winreg`, `os.startfile`, `msvcrt`, ctypes Win32 calls, Inno Setup, PyInstaller output) is verified by the **Windows CI jobs**: push, then read the run with the GitHub MCP tools (`actions_list`, `get_job_logs`). Guard Windows imports behind `sys.platform == "win32"` and unit-test the logic with monkeypatching so it also runs on Linux. Anything that truly needs a human on Windows goes in the phase's "manual Windows checks" list in `docs/PLAN.md` — never claim it was tested.

## Invariants (don't break these)
- Listen on 127.0.0.1 only; mutating API calls need the per-start token + same origin; Host header checked.
- Never run a shell: subprocess with argument lists only. Never execute downloaded files; only `.mkv`/`.mp4`.
- Nothing is deleted outright: deletions go to the OS trash (or the anidash-managed trash once Phase 4 lands), and only inside the library (`files.confine`).
- File names are Windows-safe on every OS (`files.sanitize`).
- AniList is the source of truth; on removal, update AniList first and delete nothing if that fails.
- Secrets live in the OS keyring (`vault`), never in the DB or logs.
- DB changes go through a new numbered migration in `db.py` with a test in `tests/test_migration.py`.

## Style
Match the surrounding code: dense, short functions, brief comments that say *why*, type hints, no new heavy dependencies without a reason in the PR. Tests sit in `tests/` next to the existing ones and use the fixtures in `conftest.py`.
