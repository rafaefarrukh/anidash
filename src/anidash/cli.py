"""anidash                   open the dashboard (starts the background service if needed)
anidash service [--no-tray]   run in the background (what autostart uses)
anidash window            the dashboard window only (started by the service)
anidash status            print whether it is running, and where its files are
anidash autostart on|off  start at login
anidash quit [--wait]     stop the running instance (--wait: until it has exited; exit code 1 if it wasn't running)
anidash forget --yes      delete saved logins (keyring), the secrets file and the window profile; keeps the library
anidash install-systemd   headless Linux: write a systemd user unit (no tray)
anidash --version
"""
import json
import shutil
import sys
from pathlib import Path

from . import __version__


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "open"
    if cmd in ("-V", "--version", "version"):
        print(f"anidash {__version__}")
    elif cmd == "open":
        from . import service
        sys.exit(service.run(tray=True, open_window=True))
    elif cmd == "service":
        from . import service
        sys.exit(service.run(tray="--no-tray" not in argv, open_window="--open" in argv))
    elif cmd == "window":
        from . import desktop
        desktop.main()
    elif cmd == "status":
        from . import paths, runtime
        info = runtime.running()
        print(json.dumps({"running": bool(info), "port": info and info["port"], "data": str(paths.DATA_DIR),
                          "config": str(paths.CONFIG_DIR), "logs": str(paths.LOG_DIR)}, indent=2))
    elif cmd == "quit":
        sys.exit(quit_running(wait="--wait" in argv))
    elif cmd == "forget":
        if "--yes" not in argv:
            sys.exit("anidash forget deletes your saved logins and window profile. Run `anidash forget --yes` to confirm.")
        sys.exit(forget())
    elif cmd == "autostart" and len(argv) > 1 and argv[1] in ("on", "off"):
        from . import autostart
        autostart.set_enabled(argv[1] == "on")
        print(f"autostart {'enabled' if autostart.is_enabled() else 'disabled'}")
    elif cmd == "selftest":  # hidden: CI runs it on the frozen exe
        from . import selftest
        sys.exit(selftest.run(argv[1] if len(argv) > 1 else None))
    elif cmd == "install-systemd":
        from . import autostart
        unit = autostart.install_systemd()
        print(f"wrote {unit}\nthen: systemctl --user daemon-reload && systemctl --user enable --now {unit.name}\n"
              "and, to start at boot without logging in: loginctl enable-linger $USER")
    else:
        sys.exit(__doc__)


def quit_running(wait: bool = False, timeout: float = 15) -> int:
    """0 = stopped (or asked to), 1 = wasn't running, 2 = still running after `timeout` (--wait only).
    The installer uses --wait so files aren't in use when it replaces or removes them."""
    from . import runtime
    info = runtime.running()
    if not info:
        print("not running")
        return 1 if wait else 0
    runtime.call("POST", "/api/app/quit", info)
    print("stopping")
    if wait:  # the process, not the port: it closes its server a few seconds before it exits and unlocks its files
        import psutil
        try:
            psutil.Process(info["pid"]).wait(timeout)
        except psutil.TimeoutExpired:
            return 2
        except psutil.NoSuchProcess:
            pass
    return 0


def forget() -> int:
    """Stop anidash, then delete what an uninstall would otherwise leave behind outside the data folder
    (keyring entries) plus the secrets file and the webview profile. The uninstaller removes the rest of
    %LOCALAPPDATA%\\anidash after this returns 0; 3 means the library lives inside it, so it must stay."""
    from . import db, paths, vault
    if quit_running(wait=True) == 2:
        print("anidash is still running; quit it first")
        return 2
    vault.forget_all()
    shutil.rmtree(paths.WEBVIEW_DIR, ignore_errors=True)
    print("saved logins and window profile deleted")
    lib = None
    if paths.DB_PATH.exists():
        lib = db.setting("library_path")
        db.close()
    if lib and _inside(Path(lib), paths.DATA_DIR, paths.CONFIG_DIR, paths.LOG_DIR):
        print(f"your library {lib} is inside anidash's data folder; that folder is kept")
        return 3
    return 0


def _inside(p, *roots) -> bool:
    p = p.expanduser().resolve()
    return any(p == r.resolve() or r.resolve() in p.parents for r in roots)
