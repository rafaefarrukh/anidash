"""The dashboard window (pywebview: WebKitGTK or Qt on Linux, WebView2 on Windows).
Runs as its own process; finds the service through runtime.json."""
import logging
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
import webview

from . import paths, runtime

log = logging.getLogger("anidash")
webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True


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
                try:
                    win.destroy()
                except Exception:
                    pass
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
    bridge = Bridge()
    win = webview.create_window("anidash", f"http://127.0.0.1:{info['port']}/", js_api=bridge,
                                width=1440, height=920, min_size=(1040, 640))
    bridge.main = win
    paths.WEBVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(lambda: _raise_loop(win, info), private_mode=False, storage_path=str(paths.WEBVIEW_DIR),
                      icon=str(paths.ASSETS_DIR / "icon.png"))
    except webview.WebViewException as e:  # no GTK/Qt/WebView2: the dashboard works in any browser too
        log.warning("no webview backend (%s); opening the dashboard in your browser", e)
        webbrowser.open(f"http://127.0.0.1:{info['port']}/")
