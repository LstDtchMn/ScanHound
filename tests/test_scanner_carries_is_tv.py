"""`is_tv` is decided once and carried, not re-derived per consumer.

`_process_post` settles the question properly -- the scraper's own answer OR
the source's declared type -- and then the matcher threw that away and asked
`item.season is not None` instead.

Those are not the same question. A complete-series pack is television with no
season number. So is any TV release whose season the title regex failed to
parse. Every one of them answered False and was routed to `find_movie_matches`,
to be compared against the film library.

The axis under test is therefore TV WITH NO SEASON. A fixture whose TV items
all carry a season cannot fail against the old code: `season is not None` and
the real answer agree everywhere except the case that was broken.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import MediaItem, ScannerService, ScanStatus


def _scanner(matching):
    s = ScannerService.__new__(ScannerService)
    s.plex = MagicMock()
    s.plex.plex_index = {"all_items": [object()]}
    s.matching = matching
    # stop_scan_flag is a property over this event, not a plain attribute.
    s._stop_event = threading.Event()
    s._log = MagicMock()
    s._progress = MagicMock()
    s.items = []
    return s


def _matching():
    m = MagicMock()
    m.find_tv_season_matches = MagicMock(return_value=([], False))
    m.find_movie_matches = MagicMock(return_value=([], False))
    return m


def _run(scanner, items):
    return asyncio.run(scanner._match_against_plex("Deep Scan", items))


def _item(**kw) -> MediaItem:
    base = dict(
        id="1", title="Some Show", year=2026, season=None, episodes=None,
        status=ScanStatus.MISSING, resolution="2160p", url="https://x/1",
        web_data={},
    )
    base.update(kw)
    return MediaItem(**base)


class TestTelevisionWithNoSeasonNumber:
    """The case the old derivation got wrong, and the only one that can fail."""

    def test_a_tv_item_with_no_season_goes_to_the_tv_matcher(self):
        m = _matching()
        _run(_scanner(m), [_item(is_tv=True, season=None)])
        assert m.find_tv_season_matches.called, (
            "a TV release with no parsed season was routed to the movie matcher"
        )
        assert not m.find_movie_matches.called

    def test_a_film_still_goes_to_the_movie_matcher(self):
        """The other side of the same branch: carrying `is_tv` must not send
        every item to the TV matcher."""
        m = _matching()
        _run(_scanner(m), [_item(is_tv=False, season=None, title="Some Film")])
        assert m.find_movie_matches.called
        assert not m.find_tv_season_matches.called

    def test_a_tv_item_with_a_season_is_unaffected(self):
        """The case that already worked. Kept so a fix cannot regress it."""
        m = _matching()
        _run(_scanner(m), [_item(is_tv=True, season=2)])
        assert m.find_tv_season_matches.called
        assert not m.find_movie_matches.called

    def test_the_recorded_value_wins_over_the_season_heuristic(self):
        """Pins WHICH source of truth is consulted.

        Both fields are present and they disagree. Under the old code the
        season decided; under the fix the recorded value does. A fixture where
        they agree cannot tell the two implementations apart.
        """
        m = _matching()
        _run(_scanner(m), [_item(is_tv=False, season=3, title="Film With A 3")])
        assert m.find_movie_matches.called, (
            "the season heuristic overrode the value the scanner actually recorded"
        )
        assert not m.find_tv_season_matches.called


class TestItSurvivesTheCache:
    """Deciding correctly and then losing it on the way to disk fixes nothing.

    `rematch_cache` reconstructs items from JSON, and that is the path which
    re-matches every cached row -- 4,068 of them on the live instance.
    """

    def test_is_tv_round_trips_through_the_cache_serialisation(self):
        from backend.api.routes.scanner import _media_item_to_dict

        svc = ScannerService.__new__(ScannerService)
        d = _media_item_to_dict(_item(is_tv=True, season=None))
        assert d["is_tv"] is True, "the field never reached the serialised dict"

        restored = svc._media_item_from_dict(d)
        assert restored is not None
        assert restored.is_tv is True

    def test_a_row_written_before_this_field_existed_keeps_todays_behaviour(self):
        """Every cached row on the live instance predates this field.

        Defaulting them to False would route all of them to the movie matcher
        -- strictly worse than the bug being fixed. They fall back to the old
        derivation instead, so the change is additive.
        """
        svc = ScannerService.__new__(ScannerService)
        old_tv = {"id": "1", "title": "Old Show", "year": 2026, "season": 4}
        old_film = {"id": "2", "title": "Old Film", "year": 2026, "season": None}

        assert "is_tv" not in old_tv
        assert svc._media_item_from_dict(old_tv).is_tv is True
        assert svc._media_item_from_dict(old_film).is_tv is False

    def test_an_explicit_false_is_not_overridden_by_a_season(self):
        """`'is_tv' in d` rather than a truthiness check: a row that recorded
        False must stay False even when a season is present, or the fallback
        silently re-introduces the heuristic it replaced."""
        svc = ScannerService.__new__(ScannerService)
        d = {"id": "3", "title": "Film", "year": 2026, "season": 2, "is_tv": False}
        assert svc._media_item_from_dict(d).is_tv is False
