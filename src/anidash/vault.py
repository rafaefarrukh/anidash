"""Secrets: the OS keyring (Secret Service / KWallet on Linux, Credential Manager on Windows).
Headless Linux boxes often have no keyring at all; then secrets go to a mode-600 file and
Settings says so."""
import contextlib
import json
import logging
import os

import keyring
import keyring.backend
from keyring.errors import KeyringError, NoKeyringError

from . import paths

SERVICE = "anidash"
KEYS = ("anilist_token", "qbit_password")  # every key anidash stores; `anidash forget` deletes them all
log = logging.getLogger("anidash")
_file_mode = False


def _redetect() -> None:
    # keyring picks its backends once per process. Started at login, the service can come up
    # before KWallet/GNOME Keyring is reachable, and the keyring would read as empty for good.
    with contextlib.suppress(Exception):
        keyring.backend.get_all_keyring.reset()
    with contextlib.suppress(Exception):
        keyring.core.init_backend()


def _no_keyring() -> bool:
    kr = keyring.get_keyring()
    name = type(kr).__module__ + "." + type(kr).__name__
    return "fail" in name.lower() or "null" in name.lower()


def backend_name() -> str:
    if using_file():
        return f"file ({paths.SECRETS_FILE})"
    kr = keyring.get_keyring()
    return getattr(kr, "name", type(kr).__name__)


def using_file() -> bool:
    return _file_mode or (paths.SECRETS_FILE.exists() and _no_keyring())


def _file_read() -> dict:
    try:
        return json.loads(paths.SECRETS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _file_write(data: dict) -> None:
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def get(key: str):
    if using_file():
        return _file_read().get(key)
    try:
        v = keyring.get_password(SERVICE, key)
        if v is None:
            _redetect()
            v = keyring.get_password(SERVICE, key)
        return v
    except NoKeyringError:
        return _file_read().get(key)
    except KeyringError as e:
        log.error("keyring unavailable (%s): is your wallet unlocked?", e)
        return None


def set(key: str, value: str) -> None:
    global _file_mode
    if not using_file():
        try:
            keyring.set_password(SERVICE, key, value)
            return
        except NoKeyringError:
            pass
    _file_mode = True
    log.warning("no system keyring found; storing secrets in %s (mode 600)", paths.SECRETS_FILE)
    data = _file_read()
    data[key] = value
    _file_write(data)


def delete(key: str) -> None:
    data = _file_read()
    if key in data:
        del data[key]
        _file_write(data)
    with contextlib.suppress(KeyringError, Exception):
        keyring.delete_password(SERVICE, key)


def forget_all() -> None:
    """Delete every stored secret, and the secrets file itself."""
    for key in KEYS:
        delete(key)
    paths.SECRETS_FILE.unlink(missing_ok=True)
