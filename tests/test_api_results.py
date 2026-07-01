"""Tests for server-side results filtering, sorting, pagination."""
from backend.api.routes.results import (
    _filter_and_sort, _effective_category, _has_plex_copy,
    _parse_size_to_bytes, _parse_posted_date,
)


def _it(**kw):
    base = dict(title="A", status="missing", category=None, season=None,
                genres=[], language="English", resolution="1080p", hdr="",
                dovi=False, plex_versions="[]", year=2020, rating=5.0,
                size="4.5 GB", posted_date="June 8, 2026 at 12:56 AM",
                group_key="a-2020")
    base.update(kw)
    return base


def test_effective_category_rules():
    assert _effective_category(_it(category="remux")) == "remux"
    assert _effective_category(_it(category=None, season=2)) == "tv"
    assert _effective_category(_it(category=None, season=None)) == "4k"


def test_category_filter_shows_enabled_and_unknown():
    items = [_it(title="M", category="remux"), _it(title="T", season=1),
             _it(title="S", category="search")]
    out = _filter_and_sort(items, category=["4k"])
    titles = {i["title"] for i in out}
    assert titles == {"S"}  # remux+tv hidden; unknown 'search' always shows


def test_quick_inplex_and_hdrdv():
    inplex = _it(title="P", plex_versions='[{"v":1}]')
    dv = _it(title="D", dovi=True)
    plain = _it(title="X")
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["inplex"])} == {"P"}
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["hdrdv"])} == {"D"}


def test_typed_sort_size_and_posted():
    a = _it(title="A", size="9 GB", posted_date="June 8, 2026 at 12:00 AM")
    b = _it(title="B", size="10 GB", posted_date="July 3, 2026 at 12:00 AM")
    by_size = _filter_and_sort([a, b], sort="size", order="desc")
    assert [i["title"] for i in by_size] == ["B", "A"]  # 10GB > 9GB (not lexical)
    by_posted = _filter_and_sort([a, b], sort="posted_date", order="desc")
    assert [i["title"] for i in by_posted] == ["B", "A"]  # July after June


def test_filter_missing_keeps_only_missing_status():
    missing = _it(title="M", status="Missing")
    have = _it(title="H", status="library")
    out = _filter_and_sort([missing, have], filter="missing")
    assert {i["title"] for i in out} == {"M"}


def test_search_matches_title_substring_case_insensitively():
    match = _it(title="The XYZ Movie")
    nomatch = _it(title="Something Else")
    out = _filter_and_sort([match, nomatch], search="xyz")
    assert {i["title"] for i in out} == {"The XYZ Movie"}


def test_genre_filter_keeps_only_intersecting_items():
    action = _it(title="A", genres=["Action", "Thriller"])
    drama = _it(title="D", genres=["Drama"])
    none_genre = _it(title="N", genres=[])
    out = _filter_and_sort([action, drama, none_genre], genre=["Action"])
    assert {i["title"] for i in out} == {"A"}


def test_language_filter_keeps_only_matching_language():
    fr = _it(title="F", language="French")
    en = _it(title="E", language="English")
    out = _filter_and_sort([fr, en], language=["French"])
    assert {i["title"] for i in out} == {"F"}


def test_sort_title_casefold_orders_apple_before_banana():
    apple = _it(title="apple")
    banana = _it(title="Banana")
    out = _filter_and_sort([banana, apple], sort="title", order="asc")
    assert [i["title"] for i in out] == ["apple", "Banana"]


def test_sort_year_numeric_desc():
    old = _it(title="Old", year=1999)
    new = _it(title="New", year=2024)
    out = _filter_and_sort([old, new], sort="year", order="desc")
    assert [i["title"] for i in out] == ["New", "Old"]


def test_sort_rating_numeric_desc():
    low = _it(title="Low", rating=2.0)
    high = _it(title="High", rating=9.0)
    out = _filter_and_sort([low, high], sort="rating", order="desc")
    assert [i["title"] for i in out] == ["High", "Low"]


def test_parse_size_to_bytes_failsafe_branches():
    assert _parse_size_to_bytes("") == 0.0
    assert _parse_size_to_bytes("garbage") == 0.0
    # Regex allows multiple dots ("[\d.]+"), so this reaches float() and must
    # not raise ValueError -- guarded to fail safe and return 0.0.
    assert _parse_size_to_bytes("..5 GB") == 0.0


def test_parse_posted_date_failsafe_branches():
    assert _parse_posted_date("") == 0.0
    assert _parse_posted_date("not a date") == 0.0


def test_has_plex_copy_failsafe_branches():
    assert _has_plex_copy(_it(plex_versions="{bad json")) is False
    assert _has_plex_copy(_it(plex_versions=None)) is False
