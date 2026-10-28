"""Library classification (Phase 4): every Windows and Linux branch, with the OS calls faked so it runs anywhere."""
import json
import sys
from pathlib import Path, PureWindowsPath

import pytest

from anidash import paths, storage

W = storage
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Linux mount table: POSIX paths and sysfs symlinks; never runs on Windows")


@pytest.fixture()
def no_cloud(monkeypatch, tmp_path):
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


# -- Windows ----------------------------------------------------------------------------
@pytest.mark.parametrize("dtype, fs, label, kind, trash", [
    (W.DRIVE_FIXED, "NTFS", "Windows", "local", True),
    (W.DRIVE_FIXED, "exFAT", "Data", "local", True),             # a second internal drive
    (W.DRIVE_REMOVABLE, "FAT32", "USB", "removable", False),
    (W.DRIVE_CDROM, "UDF", "DVD", "removable", False),
    (W.DRIVE_REMOTE, "NTFS", "share", "network", False),          # a mapped network drive
])
def test_windows_drive_types(monkeypatch, dtype, fs, label, kind, trash):
    seen = []
    monkeypatch.setattr(W, "_win_volume", lambda root: seen.append(root) or (dtype, fs, label))
    info = W._classify_win(PureWindowsPath("D:/Anime"))
    assert seen == ["D:\\"] and info["mount"] == "D:" and info["kind"] == kind and info["has_os_trash"] is trash and info["fs_type"] == fs


def test_windows_unc_is_network_without_win32_calls(monkeypatch):
    monkeypatch.setattr(W, "_win_volume", lambda root: pytest.fail("UNC paths need no drive lookup"))
    assert W._classify_win(PureWindowsPath("//nas/media/Anime")) == {"kind": "network", "fs_type": "smb", "has_os_trash": False,
                                                                       "mount": "\\\\nas\\media"}


@pytest.mark.parametrize("fs, label", [("GoogleDriveFS", "Google Drive"), ("FAT32", "Google Drive")])
def test_windows_google_drive_volume(monkeypatch, fs, label):
    monkeypatch.setattr(W, "_win_volume", lambda root: (W.DRIVE_FIXED, fs, label))
    info = W._classify_win(PureWindowsPath("G:/My Drive/Anime"))
    assert info["kind"] == "cloud" and info["cloud_provider"] == "Google Drive" and not info["has_os_trash"]


def test_windows_classify_goes_through_the_win_branch(monkeypatch, tmp_path, no_cloud):
    monkeypatch.setattr(W, "IS_WIN", True)
    monkeypatch.setattr(W, "_classify_win", lambda p: {"kind": "removable", "fs_type": "exFAT", "has_os_trash": False})
    info = W.classify(tmp_path / "Anime")
    assert info["kind"] == "removable" and info["exists"] is False and info["writable"] and info["free_bytes"] > 0


# -- cloud folders -------------------------------------------------------------------------
@pytest.mark.parametrize("env", ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"])
def test_onedrive_env(monkeypatch, tmp_path, no_cloud, env):
    monkeypatch.setenv(env, str(tmp_path / "OneDrive - Contoso"))
    assert W.cloud_provider(tmp_path / "OneDrive - Contoso" / "Videos" / "Anime") == "OneDrive"
    assert W.cloud_provider(tmp_path / "OneDrive - Contoso2") is None  # a prefix of the name is not inside it


@pytest.mark.parametrize("where", ["APPDATA", "LOCALAPPDATA", "home"])
def test_dropbox_info_json(monkeypatch, tmp_path, no_cloud, where):
    box = tmp_path / "Dropbox (Personal)"
    info = {"personal": {"path": str(box), "host": 1}, "business": {"path": str(tmp_path / "Work")}}
    f = (tmp_path / "home" / ".dropbox" / "info.json") if where == "home" else (tmp_path / where / "Dropbox" / "info.json")
    if where != "home":
        monkeypatch.setenv(where, str(tmp_path / where))
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(info))
    assert W.cloud_provider(box / "Anime") == "Dropbox" and W.cloud_provider(tmp_path / "Work") == "Dropbox"
    assert W.cloud_provider(tmp_path / "Elsewhere") is None


def test_icloud_and_linux_sync_folders(monkeypatch, tmp_path, no_cloud):
    home = tmp_path / "home"
    assert W.cloud_provider(home / "iCloudDrive" / "Anime") == "iCloud Drive"
    monkeypatch.setattr(W, "IS_WIN", False)
    assert W.cloud_provider(home / "Google Drive" / "Anime") == "Google Drive"
    assert W.cloud_provider(home / "Videos" / "Anime") is None
    monkeypatch.setattr(W, "IS_WIN", True)  # on Windows those names mean nothing by themselves
    assert W.cloud_provider(home / "Google Drive" / "Anime") is None


def test_cloud_folder_wins_over_the_volume(monkeypatch, tmp_path, no_cloud):
    monkeypatch.setattr(W, "IS_WIN", True)
    monkeypatch.setattr(W, "_classify_win", lambda p: {"kind": "local", "fs_type": "NTFS", "has_os_trash": True})
    monkeypatch.setenv("OneDrive", str(tmp_path))
    info = W.classify(tmp_path / "Anime")
    assert info["kind"] == "cloud" and info["cloud_provider"] == "OneDrive"


# -- Linux ----------------------------------------------------------------------------------
MOUNTINFO = """\
22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
40 22 0:50 / /mnt/nas rw,relatime shared:30 - nfs4 nas:/media rw
41 22 0:51 / /mnt/smb rw,relatime shared:31 - cifs //nas/share rw
42 22 0:52 / /home/u/remote rw,nosuid shared:32 - fuse.sshfs u@host:/ rw
43 22 0:53 / /home/u/gdrive rw,nosuid shared:33 - fuse.rclone gdrive: rw
44 22 8:17 / /run/media/u/My\\040Stick rw,nosuid shared:34 - vfat /dev/sdb1 rw
45 22 8:33 / /mnt/data rw,relatime shared:35 - ntfs3 /dev/sdc1 rw
46 22 0:54 / /run/user/1000/gvfs rw,nosuid shared:36 - fuse.gvfsd-fuse gvfsd-fuse rw
"""


@pytest.fixture()
def linux(monkeypatch, tmp_path, no_cloud):
    monkeypatch.setattr(W, "IS_WIN", False)
    mi = tmp_path / "mountinfo"
    mi.write_text(MOUNTINFO)
    real_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: real_read(mi if str(self) == "/proc/self/mountinfo" else self, *a, **k))
    sysb = tmp_path / "sys"  # sdb: removable flag set; sdc: an internal disk
    for dev, removable in (("sdb", "1"), ("sdc", "0")):
        (sysb / "devices" / dev / f"{dev}1").mkdir(parents=True)
        (sysb / "devices" / dev / "removable").write_text(removable + "\n")
        (sysb / "class").mkdir(exist_ok=True)
        (sysb / "class" / f"{dev}1").symlink_to(sysb / "devices" / dev / f"{dev}1")
    monkeypatch.setattr(W, "SYS_BLOCK", sysb / "class")
    return sysb


@posix_only
def test_mountinfo_parsing_unescapes_spaces(linux):
    assert ("/run/media/u/My Stick", "vfat", "/dev/sdb1") in W._mountinfo()


@posix_only
@pytest.mark.parametrize("path, kind, fs, trash, provider", [
    ("/home/u/Videos/Anime", "local", "ext4", True, None),
    ("/mnt/nas/Anime", "network", "nfs4", False, None),
    ("/mnt/smb/Anime", "network", "cifs", False, None),
    ("/home/u/remote/Anime", "network", "fuse.sshfs", False, None),
    ("/run/user/1000/gvfs/smb-share:server=nas/Anime", "network", "fuse.gvfsd-fuse", False, None),
    ("/home/u/gdrive/Anime", "cloud", "fuse.rclone", False, "rclone"),
    ("/run/media/u/My Stick/Anime", "removable", "vfat", True, None),
    ("/mnt/data/Anime", "local", "ntfs3", True, None),  # an internal disk shared with Windows
])
def test_linux_mounts(linux, path, kind, fs, trash, provider):
    info = W._classify_linux(Path(path))
    assert path.startswith(info["mount"])
    assert (info["kind"], info["fs_type"], info["has_os_trash"], info.get("cloud_provider")) == (kind, fs, trash, provider)


@posix_only
def test_usb_disk_counts_as_removable_even_without_the_flag(linux):
    usb = linux / "devices" / "pci0000:00" / "usb2" / "2-1" / "sdd"
    (usb / "sdd1").mkdir(parents=True)
    (usb / "removable").write_text("0\n")
    (linux / "class" / "sdd1").symlink_to(usb / "sdd1")
    assert W._block_removable("/dev/sdd1") and not W._block_removable("/dev/sdc1") and not W._block_removable("nas:/x")


# -- warnings, fingerprint, default library ---------------------------------------------
def info(**kw):
    return {"kind": "local", "cloud_provider": None, "fs_type": "ext4", "free_bytes": 500e9, "writable": True, **kw}


@pytest.mark.parametrize("kw, expect", [
    ({}, []),
    ({"kind": "cloud", "cloud_provider": "OneDrive"}, ["synced by OneDrive"]),
    ({"kind": "network"}, ["Network folder"]),
    ({"kind": "removable"}, ["Removable drive"]),
    ({"writable": False}, ["cannot write"]),
    ({"free_bytes": 4e9}, ["Only 4.0 GB free"]),
])
def test_warnings(kw, expect):
    got = W.warnings(info(**kw))
    assert len(got) == len(expect) and all(e in g for e, g in zip(expect, got, strict=True))


def test_fingerprint_is_the_volume_not_its_free_space():
    assert W.fingerprint(info(free_bytes=1, mount="/mnt/d")) == W.fingerprint(info(mount="/mnt/d")) == {"kind": "local", "fs_type": "ext4", "mount": "/mnt/d"}
    assert W.fingerprint(info(mount="/")) != W.fingerprint(info(mount="/mnt/d"))


def test_default_library_avoids_a_synced_videos_folder(monkeypatch, tmp_path, no_cloud):
    videos = tmp_path / "OneDrive" / "Videos"
    monkeypatch.setattr(paths.platformdirs, "user_videos_dir", lambda: str(videos))
    assert paths.default_library() == videos / "Anime"
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    assert paths.default_library() == tmp_path / "home" / "Anime"
