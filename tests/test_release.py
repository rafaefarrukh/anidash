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


def _winget_fill():
    import importlib.util
    spec = importlib.util.spec_from_file_location("winget_fill", Path(__file__).parents[1] / "packaging" / "winget" / "fill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_winget_manifests_from_release_sums(tmp_path):
    fill = _winget_fill()
    h = "ab" * 32
    sums = f"{'cd' * 32}  anidash-2.0.0.tar.gz\n{h}  anidash-2.0.0-setup.exe\n"
    dest = fill.fill("2.0.0", sums, "2026-11-02", tmp_path)
    assert dest == tmp_path / "manifests" / "r" / "RafaeFarrukh" / "anidash" / "2.0.0"
    files = {p.name: p.read_text() for p in dest.iterdir()}
    assert set(files) == {"RafaeFarrukh.anidash.yaml", "RafaeFarrukh.anidash.installer.yaml", "RafaeFarrukh.anidash.locale.en-US.yaml"}
    inst = files["RafaeFarrukh.anidash.installer.yaml"]
    assert f"InstallerSha256: {h.upper()}" in inst and "InstallerType: inno" in inst and "Scope: user" in inst
    assert "https://github.com/rafaefarrukh/anidash/releases/download/v2.0.0/anidash-2.0.0-setup.exe" in inst
    assert "ReleaseDate: 2026-11-02" in inst
    assert not any("{version}" in t or "{sha256}" in t for t in files.values())
    with pytest.raises(SystemExit):
        fill.fill("2.0.0rc1", sums, "2026-11-02", tmp_path)  # winget gets final releases only
    with pytest.raises(SystemExit):
        fill.fill("2.0.1", sums, "2026-11-02", tmp_path)  # not in SHA256SUMS.txt
