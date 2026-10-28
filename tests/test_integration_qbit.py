"""Against a real qbittorrent-nox (skipped when it isn't installed): auto-setup writes a config
qBittorrent accepts, then a finished torrent is renamed into the library and removed from qBit."""
import hashlib
import shutil
import socket
import subprocess
import time

import pytest

from anidash import db, jobs, qbit, qbit_setup, shows, vault

NOX = shutil.which("qbittorrent-nox")
pytestmark = pytest.mark.skipif(not NOX, reason="qbittorrent-nox not installed")


def bencode(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, (bytes, str)):
        x = x.encode() if isinstance(x, str) else x
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(map(bencode, x)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(x.items())) + b"e"


def make_torrent(path) -> tuple[bytes, str]:
    data, plen = path.read_bytes(), 16384
    pieces = b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))
    info = {"name": path.name, "length": len(data), "piece length": plen, "pieces": pieces, "private": 1}
    return bencode({"info": info, "announce": "http://127.0.0.1:1/announce"}), hashlib.sha1(bencode(info)).hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def qb(home, monkeypatch, tmp_path):
    profile = tmp_path / "qbprofile"
    cfg = profile / "qBittorrent" / "config" / "qBittorrent.conf"
    port = free_port()
    monkeypatch.setattr(qbit_setup, "running", lambda: [])  # other qBittorrents on this machine don't matter here
    inst = qbit_setup.Install("nox", str(cfg), [NOX, f"--profile={profile}"])
    res = qbit_setup.configure(inst, "anidash", "integration-pw", port)
    qbit_setup.set_keys(cfg, "BitTorrent", {"Session\\Port": str(free_port()), "Session\\DHTEnabled": "false",
                                            "Session\\LSDEnabled": "false", "Session\\PeXEnabled": "false"})
    proc = subprocess.Popen(inst.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    secrets = {"qbit_password": "integration-pw"}
    monkeypatch.setattr(vault, "get", lambda k: secrets.get(k))
    db.set_settings({"qbit_url": res["url"], "qbit_username": "anidash"})
    qbit.reset()
    for _ in range(60):
        try:
            qbit.make()
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("qbittorrent-nox did not accept the generated login")
    yield qbit.client()
    qbit.reset()
    proc.terminate()
    proc.wait(10)


def test_finished_torrent_is_renamed_and_removed(qb, home, monkeypatch):
    mid = 77
    shows.upsert_media({"id": mid, "title": {"english": "Real Show: Part 2", "romaji": "Riaru"}, "episodes": 12})
    folder = qbit_setup.Path(db.setting("library_path")) / "Real Show - Part 2"
    folder.mkdir(parents=True)
    src = folder / "[SubsPlease] Riaru - 05 (1080p) [ABCD].mkv"
    src.write_bytes(bytes(range(256)) * 400)
    torrent, h = make_torrent(src)
    qb.torrents_add(torrent_files=torrent, save_path=str(folder), category=qbit.CATEGORY, content_layout="NoSubfolder",
                    use_auto_torrent_management=False)
    db.x("INSERT INTO episode(anilist_id, number, airs_at, state, infohash, queued_at) VALUES(?,?,?,?,?,?)",
         mid, 5, time.time() - 3600, "queued", h, time.time())
    for _ in range(80):  # qBit hash-checks the existing file and finds it complete
        t = next(iter(qb.torrents_info(torrent_hashes=h)), None)
        if t and t.progress >= 1:
            break
        time.sleep(0.25)
    assert t and t.progress >= 1, "torrent never completed its check"
    monkeypatch.setattr(jobs.anilist, "push_progress", lambda m, n: n)
    jobs.watch_downloads()
    r = db.one("SELECT * FROM episode WHERE anilist_id=? AND number=5", mid)
    assert r["state"] == "done", dict(r)
    final = folder / "Real Show - Part 2 - 05.mkv"
    assert r["file_path"] == str(final.resolve()) and final.exists() and not src.exists()
    assert not qb.torrents_info(torrent_hashes=h)  # no seeding: removed, file kept
