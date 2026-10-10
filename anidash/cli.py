"""Command line: serve | window | kick <job> | setup."""
import getpass
import sys
import time
import webbrowser

import httpx

from . import config


def kick(name: str = "catchup") -> None:
    token = config.TOKEN_FILE.read_text().strip()
    r = httpx.post(f"{config.base_url()}/api/jobs/{name}/run", headers={"X-Anidash-Token": token}, timeout=10)
    r.raise_for_status()


def serve() -> None:
    import logging
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")  # journald adds timestamps
    for noisy in ("apscheduler", "httpx", "httpcore", "qbittorrentapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run("anidash.api:app", host="127.0.0.1", port=config.load()["port"], log_level="warning", access_log=False)


def _ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def setup() -> None:
    from . import anilist, db, qbit, vault
    db.init()
    cfg = config.load()
    print("\n— AniList —")
    print("1. Open https://anilist.co/settings/developer → Create New Client")
    print("2. Name: anidash   Redirect URL: https://anilist.co/api/v2/oauth/pin")
    cfg["anilist_client_id"] = _ask("Client ID", str(cfg["anilist_client_id"]))
    if cfg["anilist_client_id"]:
        url = f"https://anilist.co/api/v2/oauth/authorize?client_id={cfg['anilist_client_id']}&response_type=token"
        print(f"3. Approve access in the browser, then copy the token shown.\n   {url}")
        webbrowser.open(url)
        while True:
            tok = getpass.getpass("Paste token (hidden, empty to skip): ").strip()
            if not tok:
                break
            try:
                v = anilist.viewer(token=tok)
                vault.set("anilist_token", tok)
                db.set_settings({"token_issued": time.time()})
                print(f"   ✓ Connected as @{v['name']} (token valid ~1 year)")
                break
            except Exception as e:
                print(f"   ✕ {e}")
    print("\n— qBittorrent —")
    url = _ask("Web UI address", db.setting("qbit_url"))
    cfg["qbit_username"] = _ask("Username", cfg["qbit_username"])
    pw = getpass.getpass("Password (hidden, empty keeps current): ")
    if pw:
        vault.set("qbit_password", pw)
    db.set_settings({"qbit_url": url})
    config.save(cfg)
    try:
        print(f"   ✓ qBittorrent {qbit.make(url).app.version}")
    except Exception as e:
        print(f"   ✕ Could not connect ({e}). Fix later in Settings → Test connection.")
    print("\n— Library —")
    db.set_settings({"library_path": _ask("Library folder", db.setting("library_path"))})
    print(f"\nSaved. Config: {config.CONFIG_FILE}   Data: {config.DATA_DIR}")


def main(argv=None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "window":
        from . import desktop
        desktop.main()
    elif cmd == "kick":
        kick(argv[1] if len(argv) > 1 else "catchup")
    elif cmd == "setup":
        setup()
    else:
        sys.exit("usage: python -m anidash [serve|window|kick <job>|setup]")
