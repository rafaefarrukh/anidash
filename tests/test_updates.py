"""Phase 6: update check against GitHub Releases, verified download and the API. GitHub is faked."""
import hashlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from anidash import api, db, jobs, scheduler, updates

DL = "https://github.com/rafaefarrukh/anidash/releases/download"


def rel(tag, prerelease=False, draft=False, assets=True):
    names = [f"anidash-{tag.lstrip('v')}-setup.exe", "SHA256SUMS.txt", f"anidash-{tag.lstrip('v')}.tar.gz"] if assets else []
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "html_url": f"https://github.com/rafaefarrukh/anidash/releases/tag/{tag}",
            "published_at": "2026-11-01T00:00:00Z", "assets": [{"name": n, "browser_download_url": f"{DL}/{tag}/{n}"} for n in names]}


@pytest.mark.parametrize("a, b", [("2.0.0rc1", "2.0.0"), ("2.0.0a2", "2.0.0b1"), ("2.0.0b3", "2.0.0rc1"), ("2.0.0", "2.0.1"),
                                  ("2.0.9", "2.1.0"), ("v1.9.9", "2.0.0rc1"), ("2.0.0rc1", "2.0.0rc2")])
def test_version_order(a, b):
    assert updates.parse(a) < updates.parse(b)


def test_unparseable_versions_are_ignored():
    assert updates.parse("nightly") is None and updates.parse("2.0") is None
    assert updates._pick([rel("nightly"), rel("v2.0.0")], "2.0.0rc1")["tag_name"] == "v2.0.0"


def test_prereleases_only_for_prerelease_users():
    rels = [rel("v2.1.0rc1", prerelease=True), rel("v2.0.1"), rel("v2.2.0", draft=True)]
    assert updates._pick(rels, "2.0.0")["tag_name"] == "v2.0.1"
    assert updates._pick(rels, "2.0.0rc1")["tag_name"] == "v2.1.0rc1"
    assert updates._pick([rel("v2.2.0", draft=True)], "2.0.0") is None


@pytest.fixture()
def github(monkeypatch):
    calls, releases = [], [rel("v2.0.0"), rel("v2.0.0rc1", prerelease=True)]
    monkeypatch.setattr(updates, "_fetch", lambda: calls.append(1) or releases)
    monkeypatch.setattr(updates, "__version__", "2.0.0rc1")
    return calls, releases


def test_monthly_check_and_off(home, github):
    calls, _ = github
    info = updates.check()
    assert info["version"] == "2.0.0" and info["setup"].endswith("anidash-2.0.0-setup.exe") and info["sums"].endswith("SHA256SUMS.txt")
    assert updates.check() == info and len(calls) == 1  # within the month: cached
    db.set_settings({"update_checked_at": time.time() - 31 * 86400})
    updates.check()
    assert len(calls) == 2
    db.set_settings({"update_check": "off", "update_checked_at": 0})
    updates.check()
    assert len(calls) == 2  # off: never asks by itself
    updates.check(force=True)
    assert len(calls) == 3  # "Check now" still works


def test_available_and_dismiss(home, github, monkeypatch):
    updates.check(force=True)
    assert updates.available()["version"] == "2.0.0"
    db.set_settings({"update_dismissed": "2.0.0"})
    assert updates.available() is None
    db.set_settings({"update_dismissed": ""})
    monkeypatch.setattr(updates, "__version__", "2.0.0")
    assert updates.available() is None  # not newer


def test_how_to_update(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    assert updates.how_to_update() == {"kind": "installer", "command": None}
    monkeypatch.setattr(updates.sys, "frozen", False, raising=False)
    assert updates.how_to_update()["command"] in ("pipx upgrade anidash", "paru -Syu anidash")


# -- verified download -------------------------------------------------------------------
SETUP = b"MZ fake installer bytes" * 1000


@pytest.fixture()
def assets(monkeypatch):
    """Serves the release assets; `files` can be changed by a test."""
    files = {"anidash-2.0.0-setup.exe": SETUP,
             "SHA256SUMS.txt": f"{hashlib.sha256(SETUP).hexdigest()}  anidash-2.0.0-setup.exe\nabc  anidash-2.0.0.tar.gz\n".encode()}

    def handler(req: httpx.Request):
        return httpx.Response(200, content=files[req.url.path.rsplit("/", 1)[1]])
    real = httpx.Client
    monkeypatch.setattr(updates.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return files


INFO = {"version": "2.0.0", "setup": f"{DL}/v2.0.0/anidash-2.0.0-setup.exe", "sums": f"{DL}/v2.0.0/SHA256SUMS.txt"}


def test_download_is_verified(tmp_path, assets):
    p = updates.download_verified(INFO, tmp_path)
    assert p.read_bytes() == SETUP and p.name == "anidash-2.0.0-setup.exe"


def test_tampered_installer_is_never_kept(tmp_path, assets):
    assets["anidash-2.0.0-setup.exe"] = SETUP + b"evil"
    with pytest.raises(ValueError, match="does not match"):
        updates.download_verified(INFO, tmp_path)
    assert not list(tmp_path.iterdir())


def test_unlisted_installer_is_refused(tmp_path, assets):
    assets["SHA256SUMS.txt"] = b"abc  something-else.exe\n"
    with pytest.raises(ValueError, match="not listed"):
        updates.download_verified(INFO, tmp_path)


@pytest.mark.parametrize("url", ["https://evil.example/rafaefarrukh/anidash/releases/download/v2/x.exe",
                                 "http://github.com/rafaefarrukh/anidash/releases/download/v2/x.exe",
                                 "https://github.com/someone/else/releases/download/v2/x.exe", None])
def test_only_this_repos_release_assets(url, tmp_path):
    with pytest.raises(ValueError):
        updates.download_verified({**INFO, "setup": url}, tmp_path)


def test_install_runs_the_verified_setup_silently(tmp_path, assets, monkeypatch):
    launched = []
    monkeypatch.setattr(updates.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    monkeypatch.setattr(updates.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    setup = updates.install(INFO)
    assert launched == [[str(setup), "/SILENT", "/NORESTART"]] and setup.read_bytes() == SETUP


# -- jobs and API -----------------------------------------------------------------------------
def test_daily_job_logs_a_new_version_once(home, github):
    jobs.check_updates()
    jobs.check_updates()
    assert [r["message"] for r in db.q("SELECT message FROM event WHERE message LIKE '%is available%'")] == [
        "anidash 2.0.0 is available (https://github.com/rafaefarrukh/anidash/releases/tag/v2.0.0)"]


@pytest.fixture()
def client(home, monkeypatch):
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    api.configure(8787, "t" * 43)
    with TestClient(api.app, base_url="http://127.0.0.1:8787") as c:
        yield c


H = {"X-Anidash-Token": "t" * 43}


def test_api_check_dismiss_and_install_off_windows(client, github, monkeypatch):
    monkeypatch.setattr(api.updates, "__version__", "2.0.0rc1")
    assert client.get("/api/health").json()["update"] is None  # nothing checked yet
    r = client.post("/api/update/check", headers=H).json()
    assert r["available"]["version"] == "2.0.0" and client.get("/api/health").json()["update"]["version"] == "2.0.0"
    monkeypatch.setattr(updates, "how_to_update", lambda: {"kind": "command", "command": "pipx upgrade anidash"})
    r = client.post("/api/update/install", headers=H)
    assert r.status_code == 409 and "pipx upgrade anidash" in r.json()["detail"]
    assert client.post("/api/update/dismiss", headers=H).status_code == 200
    assert client.get("/api/health").json()["update"] is None
    assert client.patch("/api/settings", json={"update_check": "weekly"}, headers=H).status_code == 422
    assert client.patch("/api/settings", json={"update_check": "off"}, headers=H).json()["update_check"] == "off"


def test_api_check_reports_github_errors(client, monkeypatch):
    def down():
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(updates, "_fetch", down)
    r = client.post("/api/update/check", headers=H)
    assert r.status_code == 502 and "Could not reach GitHub" in r.json()["detail"]
