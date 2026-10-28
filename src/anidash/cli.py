"""anidash                   open the dashboard (starts the background service if needed)
anidash service [--no-tray]   run in the background (what autostart uses)
anidash window            the dashboard window only (started by the service)
anidash status            print whether it is running, and where its files are
anidash autostart on|off  start at login
anidash quit              stop the running instance
anidash install-systemd   headless Linux: write a systemd user unit (no tray)
anidash --version
"""
import json
import sys

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
        from . import runtime
        info = runtime.running()
        if info:
            runtime.call("POST", "/api/app/quit", info)
            print("stopping")
        else:
            print("not running")
    elif cmd == "autostart" and len(argv) > 1 and argv[1] in ("on", "off"):
        from . import autostart
        autostart.set_enabled(argv[1] == "on")
        print(f"autostart {'enabled' if autostart.is_enabled() else 'disabled'}")
    elif cmd == "install-systemd":
        from . import autostart
        unit = autostart.install_systemd()
        print(f"wrote {unit}\nthen: systemctl --user daemon-reload && systemctl --user enable --now {unit.name}\n"
              "and, to start at boot without logging in: loginctl enable-linger $USER")
    else:
        sys.exit(__doc__)
