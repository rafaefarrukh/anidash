"""The background process: API server + scheduler in threads, tray icon on the main thread."""
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

from . import APP, __version__, paths, runtime

log = logging.getLogger("anidash")


def setup_logging() -> None:
    for name in ("stdout", "stderr"):  # windowed PyInstaller builds have no console streams
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # noqa: SIM115  lives as long as the process
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.LOG_DIR / "anidash.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.INFO)
    for noisy in ("apscheduler", "httpx", "httpcore", "qbittorrentapi", "uvicorn.access", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _v1_active() -> bool:
    """v1 ran as a systemd user service on :8787; running both would download everything twice."""
    if sys.platform == "win32" or not paths.V1_UNIT.exists():
        return False
    h = runtime.probe(8787, timeout=0.5)
    return bool(h) and h.get("app") != APP


def run(tray: bool = True, open_window: bool = False) -> int:
    paths.ensure()
    setup_logging()
    if not runtime.acquire_lock():  # second launch: show the running instance's window instead
        for _ in range(40):
            live = runtime.running()
            if live:
                if open_window:
                    runtime.call("POST", "/api/ui/show", live)
                return 0
            time.sleep(0.25)
        log.error("another anidash is starting but never came up; see %s", paths.LOG_DIR)
        return 1
    if sys.platform != "win32":
        from . import autostart
        autostart.install_desktop_entry()

    import uvicorn

    from . import api, db, scheduler, ui
    db.init()
    port = runtime.free_port(int(db.setting("port") or 8787))
    token = runtime.new_token()
    api.configure(port, token)
    if _v1_active():
        api.STATE["v1_conflict"] = True
        scheduler.paused.set()
        log.error("anidash v1 is still running (systemd). Automation is paused until it is stopped: "
                  "systemctl --user disable --now anidash.service anidash-kick.timer")

    config = uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="api", daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        if not t.is_alive():
            log.error("API server failed to start")
            return 1
        time.sleep(0.05)
    runtime.write(port, token)
    log.info("anidash %s listening on http://127.0.0.1:%d", __version__, port)

    def shutdown(*_):
        ui.request_quit()

    def exit_soon():  # some tray backends ignore stop() when they never docked; don't hang on Quit
        def force():
            server.should_exit = True
            runtime.clear()
            log.info("anidash stopped (forced)")
            os._exit(0)
        timer = threading.Timer(6, force)
        timer.daemon = True
        timer.start()
    ui._on_quit.append(exit_soon)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, shutdown)

    if open_window or not db.setting("setup_done"):
        ui.open_window()
    try:
        if not (tray and _tray_run()):
            while not ui.quit_event.wait(1):
                pass
    finally:
        server.should_exit = True
        t.join(timeout=10)
        runtime.clear()
        log.info("anidash stopped")
    return 0


def _tray_run() -> bool:
    from . import tray
    return tray.run()
