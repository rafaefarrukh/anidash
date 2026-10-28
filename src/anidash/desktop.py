"""The dashboard window (pywebview: WebKitGTK or Qt on Linux, WebView2 on Windows).
Runs as its own process; finds the service through runtime.json."""
import contextlib
import logging
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
import webview

from . import paths, runtime

log = logging.getLogger("anidash")
webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
WV2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_version() -> str | None:
    """The installed Evergreen WebView2 Runtime, or None. pywebview doesn't fail without it: it falls back
    to MSHTML (IE), which can't run the dashboard, so check first. Per Microsoft's "Distribute your app and
    the WebView2 Runtime" (learn.microsoft.com/microsoft-edge/webview2/concepts/distribution): `pv` under
    HKLM's 32-bit view (WOW6432Node on 64-bit Windows) or under HKCU; missing, empty or 0.0.0.0 = not installed."""
    import winreg
    for root, sub, view in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WV2_CLIENT, winreg.KEY_WOW64_32KEY),
                            (winreg.HKEY_CURRENT_USER, "Software\\" + WV2_CLIENT, 0)):
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as k:
                v = str(winreg.QueryValueEx(k, "pv")[0] or "").strip()
        except OSError:
            continue
        if v and v != "0.0.0.0":
            return v
    return None


class Bridge:
    """Exposed to the dashboard as window.pywebview.api.*"""

    def __init__(self):
        self.main = None

    def open_url(self, url: str):
        u = urlparse(url)  # https anywhere; plain http only for a local service such as the qBittorrent Web UI
        if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
            webbrowser.open(url)

    def anilist_login(self, url: str) -> dict:
        """Open AniList's authorize page in a child window and capture the token from the redirect
        (https://anilist.co/api/v2/oauth/pin#access_token=...&expires_in=...). No copy-paste."""
        u = urlparse(url)
        if u.scheme != "https" or u.hostname != "anilist.co" or not u.path.startswith("/api/v2/oauth/authorize"):
            return {"error": "bad authorize URL"}
        closed = threading.Event()
        win = webview.create_window("Connect AniList", url, width=520, height=760)
        win.events.closed += lambda: closed.set()
        deadline = time.time() + 600
        while not closed.is_set() and time.time() < deadline:
            time.sleep(0.4)
            try:
                cur = win.get_current_url() or ""
            except Exception:
                continue
            frag = parse_qs(urlparse(cur).fragment)
            token = (frag.get("access_token") or [None])[0]
            if not token and "/oauth/pin" in cur:  # some builds drop the fragment; the pin page shows it in a textarea
                try:
                    token = win.evaluate_js("(document.querySelector('textarea')||{}).value||''") or None
                except Exception:
                    token = None
            if token:
                with contextlib.suppress(Exception):
                    win.destroy()
                exp = (frag.get("expires_in") or [None])[0]
                return {"token": token, "expires_in": int(exp) if exp and exp.isdigit() else None}
        return {"error": "AniList window closed before login finished"}


def _raise_loop(win, info):
    seen = None
    while True:
        time.sleep(1)
        try:
            seq = httpx.get(f"http://127.0.0.1:{info['port']}/api/ui/seq", timeout=2).json()["seq"]
        except Exception:
            continue
        if seen is not None and seq != seen:
            try:
                win.restore()
                win.show()
                win.on_top = True
                win.on_top = False
            except Exception:
                pass
        seen = seq


def main():
    info = None
    for _ in range(40):
        info = runtime.running()
        if info:
            break
        time.sleep(0.25)
    if not info:
        webview.create_window("anidash", html="<body style='font:15px sans-serif;padding:32px'><h2>anidash is not running</h2>"
                              f"<p>Start it from your app menu. Logs: <code>{paths.LOG_DIR}</code></p></body>")
        webview.start()
        return
    if sys.platform == "win32" and not webview2_version():
        return _browser_fallback(info, "the WebView2 Runtime is not installed")
    bridge = Bridge()
    win = webview.create_window("anidash", f"http://127.0.0.1:{info['port']}/", js_api=bridge,
                                width=1440, height=920, min_size=(1040, 640))
    bridge.main = win
    paths.WEBVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(lambda: _raise_loop(win, info), private_mode=False, storage_path=str(paths.WEBVIEW_DIR),
                      icon=str(paths.ASSETS_DIR / "icon.png"))
    except webview.WebViewException as e:  # no GTK/Qt/WebView2: the dashboard works in any browser too
        _browser_fallback(info, str(e))


def _browser_fallback(info: dict, why: str) -> None:
    log.warning("no webview backend (%s); opening the dashboard in your browser", why)
    if sys.platform == "win32":  # the dashboard then explains how to get the window back (once)
        with contextlib.suppress(Exception):
            runtime.call("POST", "/api/ui/no-webview", info)
    webbrowser.open(f"http://127.0.0.1:{info['port']}/")
