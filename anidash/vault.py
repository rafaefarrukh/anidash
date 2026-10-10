"""Secrets live in the system keyring (Secret Service: GNOME Keyring or KWallet)."""
import contextlib
import logging

import keyring
import keyring.backend
from keyring.errors import KeyringError

SERVICE = "anidash"
log = logging.getLogger("anidash")


def _redetect() -> None:
    # keyring picks its backends once per process. The service starts at boot, before KWallet is
    # running, so KWallet gets left out for good and every secret reads as missing. Look again.
    with contextlib.suppress(KeyError):  # nothing cached yet
        keyring.backend.get_all_keyring.reset()
    keyring.core.init_backend()


def get(key: str):
    try:
        v = keyring.get_password(SERVICE, key)
        if v is None:
            _redetect()
            v = keyring.get_password(SERVICE, key)
        return v
    except KeyringError as e:
        log.error("keyring unavailable (%s): is your wallet unlocked?", e)
        return None


def set(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)


def delete(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except KeyringError:
        pass
