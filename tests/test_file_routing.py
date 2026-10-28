"""Phase 4 file routing: staging, managed trash, locked files (pending_op) and the library health check."""
import os
import time
from datetime import date, timedelta

import pytest
from send2trash.exceptions import TrashPermissionError

from anidash import db, files, jobs, storage


@pytest.fixture()
def lib(home, monkeypatch):
    jobs.LIBRARY.update(problem=None, cleaned=True)
    return files.library_root()


def no_os_trash(monkeypatch):
    real = storage.classify
    monkeypatch.setattr(storage, "classify", lambda p: {**real(p), "kind": "network", "has_os_trash": False})


def locked_once(monkeypatch, times=1, winerror=32):
    """os.replace fails like Windows does while a player has the file open."""
    real, left = os.replace, [times]

    def replace(a, b):
        if left[0] > 0:
            left[0] -= 1
            e = PermissionError(13, "The process cannot access the file because it is being used by another process")
            e.winerror = winerror
            raise e
        return real(a, b)
    monkeypatch.setattr(files.os, "replace", replace)
    return left


# -- trash -------------------------------------------------------------------------------
def test_os_trash_when_available(lib, monkeypatch):
    sent = []
    monkeypatch.setattr(files, "send2trash", lambda p: sent.append(p))
    (lib / "a.mkv").write_text("x")
    assert files.trash(lib / "a.mkv") and sent == [str(lib / "a.mkv")]


def test_managed_trash_without_os_trash(lib, monkeypatch):
    no_os_trash(monkeypatch)
    monkeypatch.setattr(files, "send2trash", lambda p: pytest.fail("no OS trash on this volume"))
    for _ in range(2):  # same name twice: both kept
        (lib / "Show").mkdir(exist_ok=True)
        (lib / "Show" / "Show - 01.mkv").write_text("x")
        files.trash(lib / "Show" / "Show - 01.mkv")
    day = lib / files.TRASH / date.today().isoformat() / "Show"
    assert sorted(f.name for f in day.iterdir()) == ["Show - 01 (2).mkv", "Show - 01.mkv"]


def test_falls_back_to_managed_trash_when_send2trash_cannot(lib, monkeypatch):
    def refuse(p):
        raise TrashPermissionError(p)
    monkeypatch.setattr(files, "send2trash", refuse)
    (lib / "a.mkv").write_text("x")
    files.trash(lib / "a.mkv")
    assert (lib / files.TRASH / date.today().isoformat() / "a.mkv").exists()


def test_trash_refuses_outside_the_library(lib, home):
    (home / "outside.mkv").write_text("x")
    with pytest.raises(files.PathError):
        files.trash(home / "outside.mkv")


def test_empty_managed_trash_only_old_dated_folders(lib, home):
    t = lib / files.TRASH
    old, recent = date.today() - timedelta(days=20), date.today() - timedelta(days=3)
    for d in (old, recent):
        (t / d.isoformat()).mkdir(parents=True)
        (t / d.isoformat() / "x.mkv").write_text("x")
    (t / "notes").mkdir()  # not a date: never touched
    (home / "keep").mkdir()
    (t / (old - timedelta(days=1)).isoformat()).symlink_to(home / "keep", target_is_directory=True)  # never followed out of the trash
    assert files.empty_managed_trash(0) == 0 and (t / old.isoformat()).exists()  # 0 = never empty
    assert files.empty_managed_trash(14) == 1
    assert not (t / old.isoformat()).exists() and (t / recent.isoformat() / "x.mkv").exists()
    assert (t / "notes").exists() and (home / "keep").exists()


# -- move ----------------------------------------------------------------------------------
def test_move_trashes_an_existing_target(lib, monkeypatch):
    sent = []
    monkeypatch.setattr(files, "send2trash", lambda p: sent.append(p) or os.remove(p))  # stand-in for the OS trash
    (lib / "new.mkv").write_text("v2")
    (lib / "Show").mkdir()
    (lib / "Show" / "ep.mkv").write_text("v1")
    files.move(lib / "new.mkv", lib / "Show" / "ep.mkv")
    assert (lib / "Show" / "ep.mkv").read_text() == "v2" and sent == [str(lib / "Show" / "ep.mkv")]


@pytest.mark.parametrize("winerror", [32, 33, 5])
def test_locked_file_raises_locked(lib, monkeypatch, winerror):
    (lib / "a.mkv").write_text("x")
    locked_once(monkeypatch, winerror=winerror)
    with pytest.raises(files.Locked):
        files.move(lib / "a.mkv", lib / "b.mkv")


def test_incoming_is_confined_and_created(lib):
    d = files.incoming("AB" * 20)
    assert d == lib / files.INCOMING / ("ab" * 20) and (lib / files.INCOMING).is_dir()


# -- pending ops ----------------------------------------------------------------------------
def test_file_in_use_waits_and_retries_in_order(lib, monkeypatch):
    no_os_trash(monkeypatch)
    (lib / "Show").mkdir()
    (lib / "Show" / "ep.mkv").write_text("v1")  # a player has v1 open
    (lib / "new.mkv").write_text("v2")
    left = locked_once(monkeypatch, times=3)
    assert not jobs.file_op("trash", lib / "Show" / "ep.mkv", mid=1, n=1)
    assert not jobs.file_op("move", lib / "new.mkv", lib / "Show" / "ep.mkv", 1, 1)  # waits behind the trash
    assert db.one("SELECT COUNT(*) n FROM pending_op")["n"] == 2
    jobs.run_pending()  # still open: nothing moves, the move does not jump the queue
    assert (lib / "Show" / "ep.mkv").read_text() == "v1" and db.one("SELECT tries FROM pending_op WHERE kind='trash'")["tries"] == 1
    jobs.run_pending()
    assert left[0] == 0
    jobs.run_pending()  # player closed: v1 to the trash, then v2 in place
    assert (lib / "Show" / "ep.mkv").read_text() == "v2" and not db.q("SELECT * FROM pending_op")
    assert any("moved into the library" in r["message"] for r in db.q("SELECT message FROM event"))


def test_pending_op_on_a_vanished_file_is_dropped(lib):
    db.x("INSERT INTO pending_op(kind, src, created) VALUES('trash', ?, ?)", str(lib / "gone.mkv"), time.time())
    jobs.run_pending()
    assert not db.q("SELECT * FROM pending_op")


def test_purge_of_a_file_in_use_never_fails_the_episode(lib, monkeypatch):
    no_os_trash(monkeypatch)
    db.x("INSERT INTO anime(anilist_id, display_title, search_alias, added_at) VALUES(1,'S','S',0)")
    (lib / "S").mkdir()
    (lib / "S" / "S - 01.mkv").write_text("x")
    db.x("INSERT INTO episode(anilist_id, number, state, file_path, delete_after) VALUES(1,1,'done',?,?)",
         str(lib / "S" / "S - 01.mkv"), time.time() - 1)
    locked_once(monkeypatch, times=1)
    jobs.purge()
    assert db.one("SELECT state FROM episode")["state"] == "deleted" and db.one("SELECT kind FROM pending_op")["kind"] == "trash"
    jobs.run_pending()
    assert not (lib / "S" / "S - 01.mkv").exists() and not db.q("SELECT * FROM pending_op")


# -- library health ------------------------------------------------------------------------
def test_first_check_records_the_volume_and_creates_the_folder(home, tmp_path):
    jobs.LIBRARY.update(problem=None, cleaned=True)
    lib = tmp_path / "Lib2"
    db.set_settings({"library_path": str(lib)})
    assert jobs.check_library() is None and lib.is_dir()
    assert db.setting("library_fp") == storage.fingerprint(storage.classify(lib))


def test_unplugged_drive_pauses_and_resumes(lib, monkeypatch, fake_engine):
    real = storage.classify
    db.set_settings({"library_fp": {"kind": "removable", "fs_type": "exfat", "mount": real(lib)["mount"]}})
    gone = lib.parent / "gone"
    lib.rename(gone)
    monkeypatch.setattr(storage, "classify", lambda p: {**real(p), "kind": "local", "fs_type": "ext4"} if not lib.exists()
                        else {**real(p), "kind": "removable", "fs_type": "exfat"})
    db.x("INSERT INTO anime(anilist_id, display_title, search_alias, added_at) VALUES(1,'S','S',0)")
    db.x("INSERT INTO episode(anilist_id, number, state, infohash, queued_at) VALUES(1,1,'queued',?,?)", "a" * 40, time.time())
    jobs.watch_downloads()
    assert "not available" in jobs.LIBRARY["problem"] and not lib.exists()  # no empty folder on the system disk
    gone.rename(lib)
    jobs.watch_downloads()
    assert jobs.LIBRARY["problem"] is None
    msgs = [r["message"] for r in db.q("SELECT message FROM event ORDER BY id")]
    assert any("not available" in m for m in msgs) and any("available again" in m for m in msgs)


def test_upgraded_install_with_files_does_not_recreate_a_missing_library(home, tmp_path):
    jobs.LIBRARY.update(problem=None, cleaned=True)
    db.set_settings({"library_path": str(tmp_path / "media" / "usb" / "Anime")})  # no fingerprint yet (pre-Phase 4)
    db.x("INSERT INTO anime(anilist_id, display_title, search_alias, added_at) VALUES(1,'S','S',0)")
    db.x("INSERT INTO episode(anilist_id, number, state, file_path) VALUES(1,1,'done','/x.mkv')")
    assert "not available" in jobs.check_library() and not (tmp_path / "media").exists()


def test_unmounted_second_disk_is_not_recreated_on_the_system_disk(home, monkeypatch, tmp_path):
    """Same kind and fs type as the root disk, but /mnt/disk2 is just an empty mount-point folder now."""
    jobs.LIBRARY.update(problem=None, cleaned=True)
    lib = tmp_path / "disk2" / "Anime"
    (tmp_path / "disk2").mkdir()
    db.set_settings({"library_path": str(lib), "library_fp": {"kind": "local", "fs_type": "ext4", "mount": str(tmp_path / "disk2")}})
    monkeypatch.setattr(storage, "_classify_linux", lambda p: {"kind": "local", "fs_type": "ext4", "has_os_trash": True, "mount": "/"})
    monkeypatch.setattr(storage, "IS_WIN", False)
    assert "not available" in jobs.check_library() and not lib.exists()


def test_reclassified_library_pauses(lib, monkeypatch):
    db.set_settings({"library_fp": {"kind": "network", "fs_type": "cifs"}})
    assert "different storage" in jobs.check_library()


def test_unwritable_library_pauses(lib, monkeypatch):
    real = storage.classify
    monkeypatch.setattr(storage, "classify", lambda p: {**real(p), "writable": False})
    assert "cannot write" in jobs.check_library()


def test_orphaned_staging_folders_go_to_the_trash_once(lib, monkeypatch):
    sent = []
    monkeypatch.setattr(files, "send2trash", lambda p: sent.append(os.path.basename(p)))
    jobs.LIBRARY["cleaned"] = False
    db.x("INSERT INTO anime(anilist_id, display_title, search_alias, added_at) VALUES(1,'S','S',0)")
    db.x("INSERT INTO episode(anilist_id, number, state, infohash) VALUES(1,1,'downloading',?)", "a" * 40)
    for h in ("a" * 40, "b" * 40):
        files.incoming(h).mkdir()
    jobs.check_library()
    assert sent == ["b" * 40]  # the live download stays
    jobs.check_library()
    assert sent == ["b" * 40]  # once per start
