"""Offline, two real libtorrent sessions on 127.0.0.1: one seeds a generated .mkv, LibtorrentEngine downloads
it from a magnet (no DHT, trackers or LSD; the peer is connected directly). Then watch_downloads moves it
from .anidash-incoming into the library and removes the torrent (no seeding), and resume data survives an engine restart."""
import socket
import time

import pytest

from anidash import db, engine, jobs, shows

lt = pytest.importorskip("libtorrent")
OFFLINE = {"enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
           "announce_to_all_trackers": False, "enable_outgoing_utp": False, "enable_incoming_utp": False}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(cond, timeout=30, msg="timed out"):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            pytest.fail(msg)
        time.sleep(0.1)


@pytest.fixture()
def seeder(tmp_path):
    src = tmp_path / "seed" / "[SubsPlease] Riaru - 05 (1080p) [ABCD].mkv"
    src.parent.mkdir()
    src.write_bytes(bytes(range(256)) * 4096)  # 1 MiB, several pieces
    if hasattr(lt, "list_files"):  # libtorrent 2.1
        ct = lt.create_torrent(lt.list_files(str(src)), 65536)
    else:
        fs = lt.file_storage()
        lt.add_files(fs, str(src))
        ct = lt.create_torrent(fs, 65536)
    lt.set_piece_hashes(ct, str(src.parent))
    ti = lt.torrent_info(ct.generate())
    port = free_port()
    s = lt.session({**OFFLINE, "listen_interfaces": f"127.0.0.1:{port}"})
    p = lt.add_torrent_params()
    p.ti, p.save_path, p.flags = ti, str(src.parent), lt.torrent_flags.seed_mode
    s.add_torrent(p)
    yield {"hash": str(ti.info_hashes().v1), "name": src.name, "port": port, "size": src.stat().st_size}
    s.pause()


def make_engine(tmp_path):
    return engine.LibtorrentEngine(resume_dir=tmp_path / "resume", interface="", port=free_port(),
                                   overrides={**OFFLINE, "listen_interfaces": "127.0.0.1:0"})


def test_download_rename_and_remove(home, tmp_path, monkeypatch, seeder):
    eng = make_engine(tmp_path)
    monkeypatch.setattr(engine, "get", lambda: eng)
    mid = 77
    shows.upsert_media({"id": mid, "title": {"english": "Real Show: Part 2", "romaji": "Riaru"}, "episodes": 12})
    folder = jobs.files.show_dir("Real Show - Part 2")
    try:
        staging = jobs.files.incoming(seeder["hash"])
        eng.add(seeder["hash"], seeder["name"], str(staging), mid)
        eng._handle(seeder["hash"]).connect_peer(("127.0.0.1", seeder["port"]))
        db.x("INSERT INTO episode(anilist_id, number, airs_at, state, infohash, queued_at) VALUES(?,?,?,?,?,?)",
             mid, 5, time.time() - 3600, "queued", seeder["hash"], time.time())

        def done():
            t = next(iter(eng.torrents()), None)
            return t and t.progress >= 1 and t.amount_left == 0
        wait(done, msg=f"download never finished: {eng.torrents()}")
        t = eng.torrents()[0]
        assert t.hash == seeder["hash"] and [f.name for f in t.files] == [seeder["name"]]
        wait(lambda: eng.torrents()[0].state == "uploading" and eng._handle(seeder["hash"]).status().flags & lt.torrent_flags.paused,
             msg="finished torrent was not stopped (it would seed)")

        monkeypatch.setattr(jobs.anilist, "push_progress", lambda m, n: n)
        jobs.watch_downloads()
        r = db.one("SELECT * FROM episode WHERE anilist_id=? AND number=5", mid)
        assert r["state"] == "done", dict(r)
        final = folder / "Real Show - Part 2 - 05.mkv"
        assert r["file_path"] == str(final.resolve()) and final.stat().st_size == seeder["size"]
        assert not staging.exists()  # moved out of .anidash-incoming, folder removed
        assert eng.torrents() == []  # no seeding: removed, file kept
        assert not list((tmp_path / "resume").glob("*.fastresume"))
    finally:
        eng.close()


def test_resume_data_survives_restart(home, tmp_path, seeder):
    eng = make_engine(tmp_path)
    try:
        eng.add(seeder["hash"], seeder["name"], str(tmp_path / "dl"), 1)
        eng._handle(seeder["hash"]).connect_peer(("127.0.0.1", seeder["port"]))
        wait(lambda: eng.torrents() and eng.torrents()[0].files, msg="no metadata from the seeder")
    finally:
        eng.close()
    assert (tmp_path / "resume" / f"{seeder['hash']}.fastresume").exists()
    eng2 = make_engine(tmp_path)
    try:
        t = eng2.torrents()
        assert [x.hash for x in t] == [seeder["hash"]] and t[0].files  # restored with its metadata
    finally:
        eng2.close()


def test_vpn_interface_gone_stops_everything(home, tmp_path, monkeypatch):
    ips = {"wg0": "10.2.0.2"}
    monkeypatch.setattr(engine, "_iface_ip", lambda name: ips.get(name))
    eng = engine.LibtorrentEngine(resume_dir=tmp_path / "resume", interface="wg0", port=free_port(), overrides=OFFLINE)
    try:
        assert not eng.session.is_paused() and eng.bound_ip == "10.2.0.2" and eng.health()["ok"]
        assert eng.session.get_settings()["outgoing_interfaces"] == "10.2.0.2"
        ips.clear()  # VPN dropped
        eng._apply_binding()
        assert eng.session.is_paused() and not eng.health()["ok"] and "wg0" in eng.health()["error"]
        assert eng.session.get_settings()["outgoing_interfaces"] == "10.2.0.2"  # never falls back to another interface
        ips["wg0"] = "10.2.0.9"  # back, with a new address
        eng._apply_binding()
        assert not eng.session.is_paused() and eng.session.get_settings()["outgoing_interfaces"] == "10.2.0.9"
    finally:
        eng.close()


def test_vpn_down_at_start_never_listens_outside(home, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_iface_ip", lambda name: None)
    eng = engine.LibtorrentEngine(resume_dir=tmp_path / "resume", interface="wg0", port=free_port(), overrides=OFFLINE)
    try:
        assert eng.session.is_paused() and eng.blocked
        assert eng.session.get_settings()["listen_interfaces"].startswith("127.0.0.1:")
    finally:
        eng.close()
