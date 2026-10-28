import pytest

from anidash.nyaa import parse_size
from anidash.parser import parse

# (name, group, episode, episode_end, resolution, version, ext)
CASES = [
    ("[SubsPlease] Kusuriya no Hitorigoto - 05 (480p) [A1B2C3D4].mkv", "SubsPlease", 5, None, 480, 1, ".mkv"),
    ("[SubsPlease] Kusuriya no Hitorigoto - 05v2 (1080p) [A1B2C3D4].mkv", "SubsPlease", 5, None, 1080, 2, ".mkv"),
    ("[SubsPlease] Kaiju No. 8 - 12 (1080p) [5E5B2C3F].mkv", "SubsPlease", 12, None, 1080, 1, ".mkv"),
    ("[SubsPlease] Mob Psycho 100 III - 07 (720p) [ABCD1234].mkv", "SubsPlease", 7, None, 720, 1, ".mkv"),
    ("[SubsPlease] 86 - Eighty Six - 03 (1080p) [ABCD1234].mkv", "SubsPlease", 3, None, 1080, 1, ".mkv"),
    ("[SubsPlease] Sousou no Frieren (2026) - 01 (1080p) [ABCD1234].mkv", "SubsPlease", 1, None, 1080, 1, ".mkv"),
    ("[SubsPlease] Title - Subtitle Part - 11 (1080p) [ABCD].mkv", "SubsPlease", 11, None, 1080, 1, ".mkv"),
    ("[Erai-raws] Kusuriya no Hitorigoto - 05 [720p][Multiple Subtitle][ABCD1234]", "Erai-raws", 5, None, 720, 1, None),
    ("[Erai-raws] Some Show 2nd Season - 13 [1080p][HEVC][Multiple Subtitle]", "Erai-raws", 13, None, 1080, 1, None),
    ("[Erai-raws] Show - 05v2 [1080p CR WEB-DL AVC AAC][MultiSub][ABCD1234]", "Erai-raws", 5, None, 1080, 2, None),
    ("[ASW] Show - 05 [1080p HEVC x265 10Bit][AAC]", "ASW", 5, None, 1080, 1, None),
    ("[SubsPlease] Show - 01-02 (480p) [ABCD].mkv", "SubsPlease", 1, 2, 480, 1, ".mkv"),
    ("[SubsPlease] Show - 05 - 05 (480p) [ABCD].mkv", "SubsPlease", 5, None, 480, 1, ".mkv"),
    ("[ToonsHub] Show S01E05 1080p WEB-DL", "ToonsHub", 5, None, 1080, 1, None),
    ("[ToonsHub] Show S02E05E06 1080p WEB-DL", "ToonsHub", 5, 6, 1080, 1, None),
    ("[Group] Show - 105 [1920x1080].mp4", "Group", 105, None, 1080, 1, ".mp4"),
    ("[Group] Show - 05 (1080p).exe", "Group", 5, None, 1080, 1, ".exe"),
]
REJECT = [
    "Show - 05 [1080p]",                                    # no group
    "[SubsPlease] Show (01-12) (1080p) [Batch]",            # batch
    "[Erai-raws] Show - 01 ~ 12 [1080p][Batch][Multiple Subtitle]",
    "[Group] Show Complete Series [1080p]",
    "",
]


@pytest.mark.parametrize("name,group,ep,end,res,ver,ext", CASES)
def test_parse(name, group, ep, end, res, ver, ext):
    p = parse(name)
    assert p is not None, name
    assert (p.group, p.episode, p.episode_end, p.resolution, p.version, p.ext) == (group, ep, end, res, ver, ext)


@pytest.mark.parametrize("name", REJECT)
def test_reject(name):
    assert parse(name) is None


def test_size():
    assert parse_size("1.1 GiB") == int(1.1 * 1024 ** 3)
    assert parse_size("350.2 MiB") == int(350.2 * 1024 ** 2)
    assert parse_size("700 MB") == 700_000_000
    assert parse_size("") == 0
    assert parse_size("junk") == 0
