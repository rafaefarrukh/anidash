"""Phase 2: WebView2 detection + browser-fallback notice, `anidash forget`, `anidash quit --wait`."""
import contextlib
import json
import sys
import types

import psutil
import pytest

from anidash import cli, db, desktop, paths, runtime, vault

HKLM, HKCU = 1, 2
WOW64_32 = 0x200


def fake_winreg(values: dict):
    """values: {(root, subkey, view): pv}; anything else is a missing key."""
    m = types.SimpleNamespace(HKEY_LOCAL_MACHINE=HKLM, HKEY_CURRENT_USER=HKCU, KEY_READ=0x20019, KEY_WOW64_32KEY=WOW64_32)
    m.opened = []

    @contextlib.contextmanager
    def open_key(root, sub, reserved, access):
        k = (root, sub, access & WOW64_32)
        m.opened.append(k)
        if k not in values:
            raise FileNotFoundError(sub)
        yield k

    m.OpenKey = open_key
    m.QueryValueEx = lambda k, name: (values[k], 1)
    return m


LM = (HKLM, "SOFTWARE\\" + desktop.WV2_CLIENT, WOW64_32)
CU = (HKCU, "Software\\" + desktop.WV2_CLIENT, 0)


@pytest.mark.parametrize("values, expect", [
    ({LM: "130.0.2849.80"}, "130.0.2849.80"),            # per-machine (32-bit view = WOW6432Node on x64)
    ({CU: "129.0.1.2"}, "129.0.1.2"),                    # per-user
    ({LM: "0.0.0.0", CU: "131.0.0.1"}, "131.0.0.1"),     # one placeholder, one real
    ({LM: "", CU: "0.0.0.0"}, None),                     # Microsoft: empty or 0.0.0.0 = not installed
    ({LM: None}, None),
    ({}, None),
])
def test_webview2_detection(monkeypatch, values, expect):
    reg = fake_winreg(values)
    monkeypatch.setitem(sys.modules, "winreg", reg)
    assert desktop.webview2_version() == expect
    assert reg.opened[0] == LM and desktop.WV2_CLIENT.endswith("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")


def test_window_without_webview2_opens_browser_and_tells_service(monkeypatch):
    info = {"port": 8799, "token": "x"}
    calls, opened = [], []
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "webview2_version", lambda: None)
    monkeypatch.setattr(runtime, "running", lambda: info)
    monkeypatch.setattr(runtime, "call", lambda method, path, i=None, **kw: calls.append((method, path)))
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    monkeypatch.setattr(desktop.webview, "create_window", lambda *a, **k: pytest.fail("must not use MSHTML"))
    desktop.main()
    assert calls == [("POST", "/api/ui/no-webview")] and opened == ["http://127.0.0.1:8799/"]


def test_no_webview_notice_is_one_time(home, monkeypatch):
    from fastapi.testclient import TestClient

    from anidash import api, scheduler
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    monkeypatch.setitem(api.STATE, "no_webview", False)
    api.configure(8787, "t" * 43)
    h = {"X-Anidash-Token": "t" * 43}
    with TestClient(api.app, base_url="http://127.0.0.1:8787") as c:
        assert c.get("/api/health").json()["no_webview"] is False
        assert c.post("/api/ui/no-webview").status_code == 403  # token required like every mutation
        assert c.post("/api/ui/no-webview", headers=h).status_code == 200
        assert c.get("/api/health").json()["no_webview"] is True
        assert c.post("/api/ui/no-webview/dismiss", headers=h).status_code == 200
        assert c.get("/api/health").json()["no_webview"] is False
        c.post("/api/ui/no-webview", headers=h)  # a later fallback, even after a restart, stays quiet
        assert c.get("/api/health").json()["no_webview"] is False


def test_forget_needs_yes(home):
    with pytest.raises(SystemExit) as e:
        cli.main(["forget"])
    assert "--yes" in str(e.value.code)


def test_forget_deletes_secrets_and_profile(home, monkeypatch):
    deleted = []
    monkeypatch.setattr(vault.keyring, "delete_password", lambda svc, key: deleted.append((svc, key)))
    monkeypatch.setattr(runtime, "running", lambda: None)
    paths.SECRETS_FILE.write_text(json.dumps({"anilist_token": "secret", "qbit_password": "pw"}))
    (paths.WEBVIEW_DIR / "EBWebView").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        cli.main(["forget", "--yes"])
    assert e.value.code == 0
    assert sorted(deleted) == [("anidash", "anilist_token"), ("anidash", "qbit_password")]
    assert not paths.SECRETS_FILE.exists() and not paths.WEBVIEW_DIR.exists()
    assert paths.DB_PATH.exists()  # the uninstaller removes the folder; forget alone keeps settings


def test_forget_keeps_data_folder_when_library_is_inside(home, monkeypatch):
    monkeypatch.setattr(vault.keyring, "delete_password", lambda *a: None)
    monkeypatch.setattr(runtime, "running", lambda: None)
    monkeypatch.setattr(paths, "WEBVIEW_DIR", paths.DATA_DIR / "webview")
    db.set_settings({"library_path": str(paths.DATA_DIR / "Anime")})
    assert cli.forget() == 3


class FakeProc:
    """psutil.Process stand-in: `alive` says whether wait() times out."""
    def __init__(self, alive):
        self.alive = alive

    def wait(self, timeout=None):
        if self.alive:
            raise psutil.TimeoutExpired(timeout)


def test_forget_stops_the_running_instance_first(home, monkeypatch):
    info = {"port": 8799, "token": "x", "pid": 4242}
    state = {"up": True}
    monkeypatch.setattr(vault.keyring, "delete_password", lambda *a: None)
    monkeypatch.setattr(runtime, "running", lambda: info if state["up"] else None)
    monkeypatch.setattr(runtime, "call", lambda *a, **k: state.update(up=False))
    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc(alive=False))
    assert cli.forget() == 0 and state["up"] is False


def test_quit_wait_waits_for_the_process(monkeypatch):
    info = {"port": 8799, "token": "x", "pid": 4242}
    monkeypatch.setattr(runtime, "running", lambda: None)
    assert cli.quit_running(wait=True) == 1 and cli.quit_running() == 0
    monkeypatch.setattr(runtime, "running", lambda: info)
    monkeypatch.setattr(runtime, "call", lambda *a, **k: None)
    waited = []
    monkeypatch.setattr(psutil, "Process", lambda pid: waited.append(pid) or FakeProc(alive=False))
    assert cli.quit_running(wait=True) == 0 and waited == [4242]
    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc(alive=True))  # never exits
    assert cli.quit_running(wait=True, timeout=0) == 2

    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", gone)  # already exited
    assert cli.quit_running(wait=True) == 0


def test_orphaned_window_closes_itself(monkeypatch):
    destroyed = []
    win = types.SimpleNamespace(destroy=lambda: destroyed.append(True))
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(desktop.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    desktop._raise_loop(win, {"port": 8799}, gone_after=3)  # returns instead of polling forever
    assert destroyed == [True]


def test_close_window_waits_for_the_child(monkeypatch):
    from anidash import ui
    calls = []
    proc = types.SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append("terminate"),
                                 wait=lambda timeout: calls.append(("wait", timeout)))
    monkeypatch.setattr(ui, "_proc", proc)
    ui.close_window()
    assert calls == ["terminate", ("wait", 5)]
