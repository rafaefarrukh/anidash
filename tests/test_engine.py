"""Engine selection and the engine endpoints (the libtorrent engine itself: test_integration_libtorrent.py)."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from anidash import api, db, engine, scheduler

REAL_GET = engine.get  # captured at import, before the autouse fake replaces it
TOKEN = "t" * 43
H = {"X-Anidash-Token": TOKEN}


def test_get_follows_the_setting_and_closes_the_old_engine(home, monkeypatch):
    closed = []

    class Fake(SimpleNamespace):
        def close(self):
            closed.append(self.name)
    monkeypatch.setattr(engine, "LibtorrentEngine", lambda: Fake(name="builtin"))
    monkeypatch.setattr(engine, "QbitEngine", lambda: Fake(name="qbittorrent"))
    monkeypatch.setattr(engine, "_current", None)
    assert REAL_GET().name == "builtin"  # default for new installs
    first = REAL_GET()
    assert REAL_GET() is first  # one session per process
    db.set_settings({"engine": "qbittorrent"})
    assert REAL_GET().name == "qbittorrent" and closed == ["builtin"]
    engine.close()
    assert closed == ["builtin", "qbittorrent"] and engine._current is None


def test_magnet_has_trackers():
    m = engine.magnet("ab" * 20, "[Group] Show - 01 (1080p).mkv")
    assert m.startswith("magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=%5BGroup%5D%20Show")
    assert m.count("&tr=") == len(engine.TRACKERS)


def client(monkeypatch):
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    api.configure(8787, TOKEN)
    return TestClient(api.app, base_url="http://127.0.0.1:8787")


def test_setup_engine_choice(home, monkeypatch, fake_engine):
    with client(monkeypatch) as c:
        assert c.post("/api/setup/engine", json={"engine": "transmission"}, headers=H).status_code == 422
        r = c.post("/api/setup/engine", json={"engine": "qbittorrent"}, headers=H)
        assert r.status_code == 200 and db.setting("engine") == "qbittorrent"
        s = c.get("/api/setup").json()
        assert s["downloads"]["engine"] == "qbittorrent" and s["qbit"]["ok"] is True  # qbit mirrors the engine's health
        assert c.patch("/api/settings", json={"engine": "nope"}, headers=H).status_code == 422
        assert c.patch("/api/settings", json={"engine": "builtin"}, headers=H).json()["engine"] == "builtin"


def test_engine_interface_binding(home, monkeypatch, fake_engine):
    with client(monkeypatch) as c:
        r = c.get("/api/engine/interfaces").json()
        assert r["engine"] == "builtin" and r["list"][0]["value"] == "wg0"
        assert c.post("/api/engine/interface", json={"value": "eth9"}, headers=H).status_code == 422
        assert c.post("/api/engine/interface", json={"value": "wg0"}, headers=H).status_code == 200
        assert fake_engine.bound == "wg0"
        assert c.post("/api/engine/interface", json={"value": "wg0"}).status_code == 403  # token required


def test_downloads_lists_engine_progress(home, monkeypatch, fake_engine):
    fake_engine.items = [engine.Torrent("a" * 40, "x.mkv", "downloading", 0.5, "/tmp", 500, eta=3725, dlspeed=2_500_000),
                         engine.Torrent("b" * 40, "y.mkv", "uploading", 1.0, "/tmp", 0)]
    with client(monkeypatch) as c:
        assert c.get("/api/downloads").json() == [{"name": "x.mkv", "progress": 0.5, "speed": "2.5 MB/s", "eta": "1:02"}]
