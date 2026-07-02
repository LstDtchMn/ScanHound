import pytest
from backend.rename.dv_paths import normalize_path, same_target

MAP = [
    (r"Y:", r"\\TURTLELANDSRV2\Share"),
    (r"P:", r"\\TURTLELANDSRV2\Plex\4K Magellan"),
]


@pytest.mark.parametrize("raw,expected", [
    # separator unify + casefold
    (r"E:\4K\Movie (2020)\file.MKV", "e:/4k/movie (2020)/file.mkv"),
    ("E:/4K/Movie (2020)/file.mkv", "e:/4k/movie (2020)/file.mkv"),
    # trailing junk trimmed, dup separators collapsed
    (r"E:\4K\\Movie\\ ", "e:/4k/movie"),
    (r"E:\4K\Movie\.", "e:/4k/movie"),
])
def test_sep_case_trim(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", [
    r"Y:\Movies\A\f.mkv",
    r"\\TURTLELANDSRV2\Share\Movies\A\f.mkv",
    "y:/movies/a/f.mkv",
    r"\\turtlelandsrv2\share\Movies\A\f.MKV",
])
def test_drive_and_unc_collapse_to_one_canonical(raw):
    # every variant of the same file maps to a single canonical string
    assert normalize_path(raw, MAP) == normalize_path(r"Y:\Movies\A\f.mkv", MAP)


def test_longest_prefix_wins():
    # P: is a deeper UNC root than Y:; must not be shadowed by a shorter match
    a = normalize_path(r"P:\Film\x.mkv", MAP)
    b = normalize_path(r"\\TURTLELANDSRV2\Plex\4K Magellan\Film\x.mkv", MAP)
    assert a == b


def test_two_different_roots_do_not_collide():
    a = normalize_path(r"Y:\Movies\A\f.mkv", MAP)
    b = normalize_path(r"Z:\Movies\A\f.mkv", MAP)
    assert a != b


def test_same_target_guard():
    assert same_target(r"Y:\Movies\A\f.mkv",
                       r"\\TURTLELANDSRV2\Share\Movies\A\f.mkv", MAP) is True
    assert same_target(r"Y:\Movies\A\f.mkv",
                       r"Z:\Movies\A\f.mkv", MAP) is False
