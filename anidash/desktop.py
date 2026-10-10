"""pywebview wrapper for the dashboard window. External sites (AniList, Nyaa, qBittorrent) open in the system browser."""
import subprocess
import time
import webbrowser
from urllib.parse import urlparse

import httpx
import webview

from . import config

webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True


class Bridge:
    """Exposed to the dashboard as window.pywebview.api.*"""

    def open_url(self, url: str):
        u = urlparse(url)  # https anywhere; plain http only for a local service such as the qBittorrent Web UI
        if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
            webbrowser.open(url)


def _wait_for_service(url: str) -> bool:
    for i in range(40):
        try:
            if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                return True
        except httpx.HTTPError:
            if i == 0:
                subprocess.run(["systemctl", "--user", "start", "anidash.service"], check=False)
        time.sleep(0.25)
    return False


def main():
    url = config.base_url()
    if not _wait_for_service(url):
        webview.create_window("anidash", html="<body style='font:15px sans-serif;padding:32px'><h2>Service not running</h2>"
                              "<p>Check <code>journalctl --user -u anidash -e</code></p></body>")
    else:
        webview.create_window("anidash", url, js_api=Bridge(), width=1440, height=920, min_size=(1040, 640))
    config.DATA_DIR.joinpath("webview").mkdir(parents=True, exist_ok=True)
    webview.start(gui="gtk", private_mode=False, storage_path=str(config.DATA_DIR / "webview"))
