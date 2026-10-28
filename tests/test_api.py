from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from anidash import api, scheduler, vault

TOKEN = "t" * 43


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    monkeypatch.setattr(scheduler, "set_poll_minutes", lambda m: None)
    monkeypatch.setattr(api.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(api.autostart, "set_enabled", lambda on: None)
    api.configure(8787, TOKEN)
    with TestClient(api.app, base_url="http://127.0.0.1:8787") as c:
        yield c


H = {"X-Anidash-Token": TOKEN}


def test_health_and_index_token(client):
    h = client.get("/api/health").json()
    assert h["app"] == "anidash" and h["setup_done"] is False
    html = client.get("/").text
    assert f'<meta name="anidash-token" content="{TOKEN}">' in html
    assert "vendor/react.production.min.js" in html and "unpkg.com" not in html
    assert TOKEN in client.get("/Dashboard.dc.html").text  # the direct file path also gets the token


def test_mutations_need_token_and_origin(client):
    assert client.patch("/api/settings", json={"poll_minutes": 30}).status_code == 403
    assert client.patch("/api/settings", json={"poll_minutes": 30}, headers={"X-Anidash-Token": "nope"}).status_code == 403
    r = client.patch("/api/settings", json={"poll_minutes": 30}, headers={**H, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.patch("/api/settings", json={"poll_minutes": 30}, headers={**H, "Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 200 and r.json()["poll_minutes"] == 30


def test_dns_rebinding_host_check(client):
    assert client.get("/api/health", headers={"Host": "attacker.example:8787"}).status_code == 403


def test_settings_validation(client):
    bad = [{"poll_minutes": 1}, {"retention_days": 0}, {"nyaa_base": "ftp://x"}, {"anilist_client_id": "abc"}, {"poll_minutes": "x"}]
    for body in bad:
        assert client.patch("/api/settings", json=body, headers=H).status_code == 422, body
    r = client.patch("/api/settings", json={"backlog": "false", "seed_ratio": 5}, headers=H).json()
    assert r["backlog"] is False and "seed_ratio" not in r


def test_setup_status_shape(client):
    s = client.get("/api/setup").json()
    assert set(s) >= {"done", "anilist", "downloads", "qbit", "library", "autostart", "backlog"}
    assert s["downloads"] == {"engine": "builtin", "ok": True, "version": "fake", "error": None}
    assert s["qbit"]["ok"] is False and s["library"]["writable"] is True


def test_token_endpoint_validates_with_anilist(client, monkeypatch):
    monkeypatch.setattr(api.anilist, "viewer", lambda token=None: {"name": "rafae", "id": 1, "mediaListOptions": {"scoreFormat": "POINT_10"}})
    stored = {}
    monkeypatch.setattr(vault, "set", lambda k, v: stored.__setitem__(k, v))
    r = client.post("/api/account/token", json={"token": "x" * 40, "expires_in": 86400}, headers=H)
    assert r.json() == {"user": "rafae"} and stored == {"anilist_token": "x" * 40}


@pytest.mark.parametrize("date,expect", [((2026, 1, 5), ("WINTER", 2026)), ((2026, 4, 1), ("SPRING", 2026)),
                                         ((2026, 10, 25), ("FALL", 2026)), ((2026, 12, 20), ("WINTER", 2027))])
def test_current_season(date, expect):
    assert api.current_season(datetime(*date)) == expect
