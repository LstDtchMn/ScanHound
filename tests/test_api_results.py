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
