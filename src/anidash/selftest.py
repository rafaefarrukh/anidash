"""`anidash selftest [OUT]`: import what the frozen app loads only at runtime (window backend, tray,
keyring and trash backends), which the service smoke test never touches. Prints JSON, or writes it
to OUT because the Windows exe is windowed and has no stdout. Exit code 1 if a required check fails."""
import json
import sys
from collections.abc import Callable

IS_WIN = sys.platform == "win32"


def _window() -> str:
    if IS_WIN:
        import clr  # noqa: F401  pythonnet; winforms imports it too, but a failure here is clearer
        from webview.platforms import winforms
        return f"winforms/{winforms.renderer}"
    try:
        from webview.platforms import gtk  # noqa: F401
        return "gtk"
    except Exception:
        from webview.platforms import qt  # noqa: F401
        return "qt"


def _webview2() -> str:
    if not IS_WIN:
        return "n/a"
    from .desktop import webview2_version
    v = webview2_version()
    if not v:
        raise RuntimeError("WebView2 Runtime not found in the registry")
    return v


def _tray() -> str:
    import pystray
    mod = pystray.Icon.__module__
    if IS_WIN and mod != "pystray._win32":
        raise RuntimeError(f"wrong backend {mod}")
    return mod


def _keyring() -> str:
    import keyring
    kr = keyring.get_keyring()
    mod = f"{type(kr).__module__}.{type(kr).__name__}"
    if IS_WIN and "Windows" not in mod:
        raise RuntimeError(f"wrong backend {mod}")
    return mod


def _trash() -> str:
    from send2trash import send2trash
    return send2trash.__module__


def _modules() -> str:
    from . import api, desktop, service, tray  # noqa: F401
    return "ok"


# name -> (check, required). Linux falls back to the browser and file secrets, so only Windows insists.
# webview2 isn't required (users without it get the browser); CI requires it on its runners, which have it.
CHECKS: dict[str, tuple[Callable[[], str], bool]] = {
    "window": (_window, IS_WIN), "webview2": (_webview2, False), "tray": (_tray, IS_WIN), "keyring": (_keyring, IS_WIN),
    "send2trash": (_trash, True), "modules": (_modules, True)}


def run(out: str | None = None) -> int:
    from . import __version__
    res: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "checks": {}}
    ok = True
    for name, (check, required) in CHECKS.items():
        try:
            res["checks"][name] = {"ok": True, "detail": check(), "required": required}
        except BaseException as e:  # an import can raise anything, SystemExit included
            res["checks"][name] = {"ok": False, "detail": f"{type(e).__name__}: {e}", "required": required}
            ok = ok and not required
    res["ok"] = ok
    text = json.dumps(res, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    elif sys.stdout:
        print(text)
    return 0 if ok else 1
