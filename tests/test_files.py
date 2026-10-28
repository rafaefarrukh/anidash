from pathlib import Path

import pytest

from anidash import files


@pytest.mark.parametrize("raw,clean", [
    ("Re:Zero kara Hajimeru", "ReZero kara Hajimeru"),
    ("Kaguya-sama: Love Is War", "Kaguya-sama - Love Is War"),
    ('A "quoted" <title>?', "A 'quoted' (title)"),
    ("a/b\\c", "abc"),
    ("..hidden", "hidden"),
    ("trailing dots...", "trailing dots"),
    ("CON", "_CON"),
    ("com1.mkv", "_com1.mkv"),
    ("", "Untitled"),
    ("   ", "Untitled"),
    ("Tab\tand\nnewline", "Tabandnewline"),
    ("x" * 300, "x" * 150),
])
def test_sanitize(raw, clean):
    assert files.sanitize(raw) == clean


def test_sanitized_names_are_windows_safe():
    bad = set('<>:"/\\|?*')
    for raw in ['Fate/stay night: UBW', 'What?! *Really*', 'A|B', 'Dr. STONE: NEW WORLD.']:
        s = files.sanitize(raw)
        assert not bad & set(s) and not s.endswith((".", " ")), s


def test_episode_name():
    assert files.episode_name("Show: Two", 5, 12, ".MKV") == "Show - Two - 05.mkv"
    assert files.episode_name("Long", 7, 150, ".mp4") == "Long - 007.mp4"
    assert files.episode_name("Unknown", 7, None, ".mkv") == "Unknown - 07.mkv"


def test_confine(home):
    root = files.library_root()
    assert files.confine(root / "Show" / "a.mkv") == (root / "Show" / "a.mkv").resolve()
    assert files.confine(root) == root
    for outside in (root.parent, root / ".." / "x", Path("/etc/passwd"), Path(str(root) + "-evil") / "x"):
        with pytest.raises(files.PathError):
            files.confine(outside)


def test_confine_symlink_escape(home):
    root = files.library_root()
    (root / "link").symlink_to(home)  # points outside the library
    with pytest.raises(files.PathError):
        files.confine(root / "link" / "anything")


def test_show_dir_rejects_traversal(home):
    assert files.show_dir("../../etc").parent == files.library_root()


def test_trash_moves_only_inside_library(home, monkeypatch):
    moved = []
    monkeypatch.setattr(files, "send2trash", lambda p: moved.append(p))
    f = files.library_root() / "Show" / "Show - 01.mkv"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    assert files.trash(f) is True
    assert moved == [str(f.resolve())]
    assert files.trash(files.library_root() / "missing.mkv") is False
    with pytest.raises(files.PathError):
        files.trash(home / "outside.txt")
