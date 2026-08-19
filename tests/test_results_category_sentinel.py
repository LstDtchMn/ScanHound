"""The category '__none__' sentinel contract (category-switch fix, review round 2).

An OMITTED category parameter means "no category filter" (show everything), so
the frontend cannot express "every known category deselected" by omission --
buildResultParams (frontend stores/results.ts) sends '__none__' instead.
Contract: the sentinel hides every _KNOWN_CATEGORIES item while items with an
unknown/'search' effective category keep their always-show behavior.
"""
from backend.api.routes.results import (
    CATEGORY_NONE_SENTINEL, _csv, _filter_and_sort,
)


def _it(**kw):
    base = dict(title="A", status="missing", category=None, season=None,
                genres=[], language="English", resolution="1080p", hdr="",
                dovi=False, plex_versions="[]", year=2020, rating=5.0,
                size="4.5 GB", posted_date="June 8, 2026 at 12:56 AM",
                group_key="a-2020")
    base.update(kw)
    return base


def _deck():
    return [
        _it(title="K", category="4k"),
        _it(title="M", category="remux"),
        _it(title="T", season=1),               # inferred 'tv'
        _it(title="S", category="search"),      # unknown: always shows
    ]


def test_sentinel_hides_all_known_categories_but_keeps_unknowns():
    out = _filter_and_sort(_deck(), category=[CATEGORY_NONE_SENTINEL])
    assert {i["title"] for i in out} == {"S"}


def test_sentinel_is_inert_when_mixed_with_real_categories():
    # Never sent by the frontend, but the contract must stay sane if it is:
    # the sentinel adds nothing and removes nothing beyond the empty set.
    out = _filter_and_sort(_deck(), category=["4k", CATEGORY_NONE_SENTINEL])
    assert {i["title"] for i in out} == {"K", "S"}


def test_omitted_category_still_means_no_filter():
    # The pre-existing meaning the sentinel exists to NOT collide with.
    out = _filter_and_sort(_deck(), category=None)
    assert {i["title"] for i in out} == {"K", "M", "T", "S"}


def test_csv_carries_the_sentinel_through_the_query_layer():
    assert _csv(CATEGORY_NONE_SENTINEL) == [CATEGORY_NONE_SENTINEL]
