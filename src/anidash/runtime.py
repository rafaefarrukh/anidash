"""The running instance publishes its port and API token in runtime.json (mode 600).
Other processes (the window, the CLI, a second launch) read it to find and talk to it."""
import json
import os
import secrets
import socket
import sys

import httpx

from . import APP, __version__, paths


_lock_fh = None


def acquire_lock() -> bool:
    """One instance per user. Held for the life of the process; the OS drops it if we crash."""
    global _lock_fh
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(paths.RUNTIME_DIR / "instance.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_fh = fh
    return True


def write(port: int, token: str) -> None:
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.RUNTIME_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid(), "version": __version__}, f)
    os.replace(tmp, paths.RUNTIME_FILE)


def read() -> dict | None:
    try:
        return json.loads(paths.RUNTIME_FILE.read_text())
    except (OSError, ValueError):
        return None


def clear() -> None:
    info = read()
    if info and info.get("pid") == os.getpid():
        paths.RUNTIME_FILE.unlink(missing_ok=True)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def probe(port: int, timeout: float = 1.0) -> dict | None:
    """What answers on this port? {'app': 'anidash', 'version': ...} for v2, {'ok': True} for v1, else None."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def running() -> dict | None:
    """The live instance from runtime.json, or None."""
    info = read()
    if not info:
        return None
    h = probe(info["port"])
    return info if h and h.get("app") == APP else None


def call(method: str, path: str, info: dict | None = None, **kw):
    info = info or running()
    if not info:
        raise RuntimeError("anidash is not running")
    return httpx.request(method, f"http://127.0.0.1:{info['port']}{path}",
                         headers={"X-Anidash-Token": info["token"]}, timeout=kw.pop("timeout", 10), **kw)
