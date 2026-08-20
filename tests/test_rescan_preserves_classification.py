"""A manual rescan must not destroy the classification it cannot re-observe.

Peer review round 11. `/scan/rescan-item` read `source_category` twice, and
that column is the SOURCE NAME -- "HDEncode" on all 4,145 live rows -- not the
crawl category ('4k' | 'remux' | 'tv'):

    details['category'] = source_category
        wrote "HDEncode" into the rebuilt row and persisted it back to
        background_scan_cache, replacing a good category with one that maps to
        no media kind. Fails closed, but it silently destroys the server-owned
        classification that authorises a destructive action for that release.

    post_source_is_tv = (source_category == 'tv')
        ALWAYS False. So rescanning a TV row whose season does not parse
        produced is_tv=False with season=None -- recreating the exact defect
        this branch exists to fix, on a live route.

The second was added by the #93 fix itself, which is the part worth noticing: a
fix for "re-derived a fact that was already known" re-derived it again, one
route over, from a field that never held it.

A rescan re-fetches the DETAIL page. It observes nothing about which listing
the release came from, so it must carry that evidence forward.
"""
from __future__ import annotations

import json

import pytest

from backend.scanner_service import ScannerService


def _cached(category, *, is_tv=False, season=None):
    """A cached row shaped like the ones on disk: source NAME in the column,
    crawl category inside the JSON."""
    return {
        "url": "https://hdencode.org/a-show-s02-1080p/",
        "source_category": "HDEncode",
        "data": json.dumps({
            "url": "https://hdencode.org/a-show-s02-1080p/",
            "title": "A Show S02", "year": 2026,
            "category": category, "is_tv": is_tv, "season": season,
        }),
    }


def _rebuild(existing, details):
    """Drive the REAL route logic.

    The first version of this helper reimplemented the classification here, so
    a mutation restoring the production bug killed nothing -- the test could
    not see the code it was written for. `rescan_classification` is now
    imported from the route module and is the only implementation.
    """
    from backend.api.routes.scanner import rescan_classification

    category, is_tv_from_cache = rescan_classification(existing)
    details = dict(details)
    details["category"] = category
    return details, (details.get("is_tv", False) or is_tv_from_cache)


class TestTheCrawlCategorySurvives:
    def test_the_source_name_is_never_written_as_a_category(self):
        """The corruption. "HDEncode" maps to no media kind, so the release
        loses its authorisation until a full re-crawl."""
        details, _is_tv = _rebuild(_cached("tv"), {"is_tv": False})
        assert details["category"] == "tv"
        assert details["category"] != "HDEncode"

    @pytest.mark.parametrize("category", ["4k", "remux", "tv"])
    def test_every_category_round_trips(self, category):
        details, _ = _rebuild(_cached(category), {"is_tv": False})
        assert details["category"] == category

    def test_an_unreadable_cached_row_yields_no_category(self):
        """Fail closed rather than inventing one."""
        details, _ = _rebuild({"source_category": "HDEncode", "data": "{bad"},
                              {"is_tv": False})
        assert details["category"] == ""


class TestTheTVSignalSurvivesARescan:
    def test_a_tv_row_with_no_season_stays_TV(self):
        """The reviewer's required regression, and the exact shape #93 exists
        for: source_category="HDEncode", data.category="tv", season=None, and
        a detail page that does not positively parse TV."""
        _details, is_tv = _rebuild(_cached("tv", season=None),
                                   {"is_tv": False})
        assert is_tv is True, (
            "a rescan routed a TV release with no parsed season to the movie "
            "matcher -- the bug this branch fixes, via a different door")

    def test_the_cached_is_tv_verdict_is_honoured(self):
        """The item's own recorded verdict outranks a re-derivation: it was
        decided at crawl time with evidence this route does not have."""
        _d, is_tv = _rebuild(_cached("", is_tv=True), {"is_tv": False})
        assert is_tv is True

    def test_a_cached_season_is_positive_TV_evidence(self):
        _d, is_tv = _rebuild(_cached("", season=2), {"is_tv": False})
        assert is_tv is True

    def test_a_film_stays_a_film(self):
        """POSITIVE CONTROL. Without it, 'always True' would satisfy the tests
        above and every rescanned movie would go to the TV matcher."""
        _d, is_tv = _rebuild(_cached("4k", is_tv=False, season=None),
                             {"is_tv": False})
        assert is_tv is False

    def test_a_fresh_detail_parse_can_still_prove_TV(self):
        """The detail page remains able to contribute positive evidence."""
        _d, is_tv = _rebuild(_cached("4k"), {"is_tv": True})
        assert is_tv is True
