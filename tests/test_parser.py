from anidash.nyaa import parse_size
from anidash.parser import parse

CASES = [
    ("[SubsPlease] Kusuriya no Hitorigoto - 05 (480p) [A1B2C3D4].mkv", "SubsPlease", 5, None, 480, 1),
    ("[SubsPlease] Kusuriya no Hitorigoto - 05v2 (1080p) [A1B2C3D4].mkv", "SubsPlease", 5, None, 1080, 2),
    ("[Erai-raws] Kusuriya no Hitorigoto - 05 [720p][Multiple Subtitle][ABCD1234]", "Erai-raws", 5, None, 720, 1),
    ("[Erai-raws] Some Show 2nd Season - 13 [1080p][HEVC][Multiple Subtitle]", "Erai-raws", 13, None, 1080, 1),
    ("[SubsPlease] Show - 01-02 (480p) [ABCD].mkv", "SubsPlease", 1, 2, 480, 1),
    ("[ToonsHub] Show S01E05 1080p WEB-DL", "ToonsHub", 5, None, 1080, 1),
    ("Show - 05 [1080p]", None, None, None, None, None),
]


def test_parse():
    for name, group, ep, end, res, ver in CASES:
        p = parse(name)
        if group is None:
            assert p is None, name
            continue
        assert (p.group, p.episode, p.episode_end, p.resolution, p.version) == (group, ep, end, res, ver), name


def test_size():
    assert parse_size("1.1 GiB") == int(1.1 * 1024 ** 3)
    assert parse_size("350.2 MiB") == int(350.2 * 1024 ** 2)
    assert parse_size("") == 0
