"""System tray icon (pystray). Linux: AppIndicator/StatusNotifier (KDE, Cinnamon, XFCE; GNOME
needs the AppIndicator extension) with an X11 fallback. Windows: the notification area."""
import logging

from . import paths, scheduler, ui

log = logging.getLogger("anidash")


def run() -> bool:
    """Blocks until Quit. Returns False if no tray is available (caller runs headless)."""
    try:
        import pystray
        from PIL import Image
    except Exception as e:  # missing GUI libraries
        log.warning("tray unavailable (%s); running without it", e)
        return False

    def check_now(icon, item):
        scheduler.run_now("poll_releases")

    def toggle_pause(icon, item):
        (scheduler.paused.clear if scheduler.paused.is_set() else scheduler.paused.set)()

    def quit_(icon, item):
        ui.request_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda i, it: ui.open_window(), default=True),
        pystray.MenuItem("Check for episodes now", check_now),
        pystray.MenuItem("Pause automation", toggle_pause, checked=lambda it: scheduler.paused.is_set()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit anidash", quit_),
    )
    try:
        icon = pystray.Icon("anidash", Image.open(paths.ASSETS_DIR / "tray.png"), "anidash", menu)
        ui._on_quit.append(icon.stop)
        icon.run()
        return True
    except Exception as e:
        log.warning("tray failed to start (%s); running without it", e)
        return False
