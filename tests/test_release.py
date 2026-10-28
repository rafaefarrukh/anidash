"""Phase 1 release plumbing: built-in AniList client, Windows runtime dir, `anidash selftest`."""
import json
import sys
from pathlib import Path

import pytest

from anidash import anilist, cli, db, paths, selftest


def test_builtin_client_and_override(home):
    assert anilist.PUBLIC_CLIENT_ID == "55203" and anilist.client_id() == "55203"
    assert "client_id=55203&" in anilist.authorize_url()
    db.set_settings({"anilist_client_id": "777"})
    assert anilist.client_id() == "777"
    db.set_settings({"anilist_client_id": ""})  # clearing goes back to the built-in one
    assert anilist.client_id() == "55203"


def test_runtime_dir_not_in_temp_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WIN", True)
    assert paths._runtime_dir(tmp_path / "data") == tmp_path / "data" / "run"
    monkeypatch.setattr(paths, "IS_WIN", False)
    monkeypatch.setattr(paths.platformdirs, "user_runtime_dir", lambda *a, **k: "/run/user/1000/anidash")
    assert paths._runtime_dir(tmp_path / "data") == Path("/run/user/1000/anidash")


def test_selftest_writes_json(tmp_path):
    out = tmp_path / "st.json"
    rc = selftest.run(str(out))
    res = json.loads(out.read_text())
    assert set(res["checks"]) == set(selftest.CHECKS) and res["ok"] == (rc == 0)
    assert res["checks"]["modules"]["ok"] and res["checks"]["send2trash"]["ok"]
    if sys.platform == "win32":  # the real backends, unfrozen; CI checks the frozen exe too
        assert rc == 0, res
        assert res["checks"]["window"]["detail"].startswith("winforms/")


def test_selftest_required_failure_sets_exit_code(tmp_path, monkeypatch):
    def boom():
        raise ImportError("no clr")
    monkeypatch.setattr(selftest, "CHECKS", {"window": (boom, True), "tray": (boom, False)})
    out = tmp_path / "st.json"
    with pytest.raises(SystemExit) as e:
        cli.main(["selftest", str(out)])
    res = json.loads(out.read_text())
    assert e.value.code == 1 and res["ok"] is False
    assert res["checks"]["window"] == {"ok": False, "detail": "ImportError: no clr", "required": True}
