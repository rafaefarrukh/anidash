"""Per-user directories (platformdirs) and one-time migration from v1's ~/.local/share/anime-dl."""
import logging
import os
import shutil
import sys
from pathlib import Path

import platformdirs

from . import APP

log = logging.getLogger("anidash")
IS_WIN = sys.platform == "win32"


def _runtime_dir(data_dir: Path) -> Path:
    # Windows' runtime dir is under %TEMP%, which Storage Sense empties while we run.
    return data_dir / "run" if IS_WIN else Path(platformdirs.user_runtime_dir(APP, appauthor=False))


_override = os.environ.get("ANIDASH_HOME")  # tests and portable installs: everything under one folder
if _override:
    _root = Path(_override)
    CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR = _root / "config", _root / "data", _root / "logs", _root / "run"
else:
    CONFIG_DIR = Path(platformdirs.user_config_dir(APP, appauthor=False))
    DATA_DIR = Path(platformdirs.user_data_dir(APP, appauthor=False))
    LOG_DIR = Path(platformdirs.user_log_dir(APP, appauthor=False))
    RUNTIME_DIR = _runtime_dir(DATA_DIR)

DB_PATH = DATA_DIR / "anidash.db"
RUNTIME_FILE = RUNTIME_DIR / "runtime.json"   # port + per-start API token, mode 600
SECRETS_FILE = CONFIG_DIR / "secrets.json"    # only when no OS keyring exists
WEBVIEW_DIR = DATA_DIR / "webview"


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):  # PyInstaller
        return Path(sys._MEIPASS) / "anidash"
    return Path(__file__).resolve().parent


WEB_DIR = Path(os.environ.get("ANIDASH_WEB") or _bundle_dir() / "web")
ASSETS_DIR = _bundle_dir() / "assets"


def default_library() -> Path:
    try:
        videos = Path(platformdirs.user_videos_dir())
    except Exception:
        videos = Path.home() / "Videos"
    return videos / "Anime"


def ensure() -> None:
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not IS_WIN:
        for d in (CONFIG_DIR, DATA_DIR, RUNTIME_DIR):
            os.chmod(d, 0o700)
    _migrate_v1()


def _migrate_v1() -> None:
    """v1 kept its database in ~/.local/share/anime-dl and a bootstrap config.toml beside it."""
    if IS_WIN or DB_PATH.exists() or _override:
        return
    old = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "anime-dl" / "anidash.db"
    if not old.exists():
        return
    shutil.copy2(old, DB_PATH)
    for ext in ("-wal", "-shm"):
        if old.with_name(old.name + ext).exists():
            shutil.copy2(old.with_name(old.name + ext), DB_PATH.with_name(DB_PATH.name + ext))
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anime-dl" / "config.toml"
    if cfg.exists():
        shutil.copy2(cfg, CONFIG_DIR / "v1-config.toml")  # read once by db.migrate()
    log.info("Imported v1 database from %s", old)


V1_UNIT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/anidash.service"
