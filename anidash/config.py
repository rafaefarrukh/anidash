"""Paths and the small bootstrap config file (~/.config/anime-dl/config.toml)."""
import os
import tomllib
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "anime-dl"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "anime-dl"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", DATA_DIR))
CONFIG_FILE = CONFIG_DIR / "config.toml"
DB_PATH = DATA_DIR / "anidash.db"
TOKEN_FILE = RUNTIME_DIR / "anidash.token"
WEB_DIR = Path(os.environ.get("ANIDASH_WEB", DATA_DIR / "app" / "web"))

DEFAULTS = {"host": "127.0.0.1", "port": 8787, "anilist_client_id": "", "qbit_username": "admin"}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            cfg.update(tomllib.load(f))
    cfg["host"] = "127.0.0.1"  # never bind anything else
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# anidash bootstrap config. Everything else lives in the dashboard's Settings page.\n"]
    for k in ("port", "anilist_client_id", "qbit_username"):
        v = cfg.get(k, DEFAULTS[k])
        lines.append(f"{k} = {v}\n" if isinstance(v, int) else f'{k} = "{str(v).replace(chr(34), "")}"\n')
    CONFIG_FILE.write_text("".join(lines))
    CONFIG_FILE.chmod(0o600)


def base_url() -> str:
    return f"http://127.0.0.1:{load()['port']}"
