"""The service process owns at most one dashboard window, which runs as its own process
(`anidash window`). GUI toolkits want the main thread, and the tray already has it here."""
import logging
import subprocess
import sys
import threading

from . import autostart

log = logging.getLogger("anidash")
_proc: subprocess.Popen | None = None
_seq = 0
_lock = threading.Lock()
quit_event = threading.Event()
_on_quit = []  # callbacks (tray.stop)


def raise_seq() -> int:
    return _seq


def window_open() -> bool:
    return _proc is not None and _proc.poll() is None


def open_window() -> None:
    """Open the dashboard, or bring the open one to the front."""
    global _proc, _seq
    with _lock:
        if window_open():
            _seq += 1  # the window polls /api/ui/seq and raises itself
            return
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            _proc = subprocess.Popen(autostart.launch_command("window"), **kw)
        except OSError as e:
            log.error("could not open the dashboard window: %s", e)


def close_window() -> None:
    if window_open():
        _proc.terminate()


def request_quit() -> None:
    close_window()
    quit_event.set()
    for cb in list(_on_quit):
        try:
            cb()
        except Exception:
            log.exception("quit callback failed")
