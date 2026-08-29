"""The media-type decision is made once and CARRIED, not re-derived per consumer.

`season is not None` and "is this television" are not the same question. A
complete-series pack is television with no season number; so is any TV release
whose season the title regex failed to parse. The original bug routed every one
of them to `find_movie_matches`, to be compared against the film library.

The axis under test is therefore TV WITH NO SEASON. A fixture whose TV items
all carry a season cannot fail against the old code: `season is not None` and
the real answer agree everywhere except the case that was broken.

MERGE 2026-08-28 (PR #94 x main). On main the carried fact was the boolean
``is_tv`` and the matcher branched on it. On this branch the carried fact is
``media_type`` from the release-grammar resolver, and the matcher selects
tri-state (tv / movie / refuse). These tests now drive the same guarantees
through the PRODUCTION reconstruction path — ``_media_item_from_dict``, which
resolves ``media_type`` from exactly the facts main recorded (``is_tv``,
``season``, ``category``, title) — and then through the matcher, so the
precedence questions this file pinned (round 10 Q8 among them) are still
answered by production code, not by a fixture's hand-set field.

ONE ASSERTION CHANGED SIDES, deliberately and visibly:
``test_neither_signal_means_movie`` became
``test_neither_signal_is_refused_not_guessed``. Main defaulted a no-evidence
item to the movie matcher because a boolean has no third value. Refusing to
guess is PR #94's round-13 fix (see the tri-state comment in
``_match_against_plex`` and docs/reviews/2026-08-05-round13-relay-block.md) —
matching a release against the film library on zero evidence is the exact
failure this file exists to prevent, one library over.
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


def _cached(**kw) -> dict:
    """A cached-row dict of exactly the facts main recorded — no media_type,
    so ``_media_item_from_dict`` must resolve it, which is the code under
    test."""
    base = dict(
        id="1", title="Some Show", year=2026, season=None, episodes=None,
        status="missing", resolution="2160p", url="https://x/1",
        web_data={},
    )
    base.update(kw)
    return base


def _item(scanner, **kw) -> MediaItem:
    item = scanner._media_item_from_dict(_cached(**kw))
    assert item is not None, "production reconstruction refused the fixture"
    return item


class TestTelevisionWithNoSeasonNumber:
    """The case the old derivation got wrong, and the only one that can fail."""

    def test_a_tv_item_with_no_season_goes_to_the_tv_matcher(self):
        m = _matching()
        s = _scanner(m)
        item = _item(s, is_tv=True, season=None)
        assert item.media_type == "tv", (
            "a recorded is_tv=True was discarded by reconstruction — main's "
            "decided verdict must be carried, not re-derived from season"
        )
        _run(s, [item])
        assert m.find_tv_season_matches.called, (
            "a TV release with no parsed season was routed to the movie matcher"
        )
        assert not m.find_movie_matches.called

    def test_a_film_still_goes_to_the_movie_matcher(self):
        """The other side of the same branch: carrying the verdict must not
        send every item to the TV matcher. The film's route evidence is its
        crawl category, exactly as the live cache records it."""
        m = _matching()
        s = _scanner(m)
        _run(s, [_item(s, is_tv=False, season=None, title="Some Film",
                       category="4k")])
        assert m.find_movie_matches.called
        assert not m.find_tv_season_matches.called

    def test_a_tv_item_with_a_season_is_unaffected(self):
        """The case that already worked. Kept so a fix cannot regress it."""
        m = _matching()
        s = _scanner(m)
        _run(s, [_item(s, is_tv=True, season=2)])
        assert m.find_tv_season_matches.called
        assert not m.find_movie_matches.called

    def test_a_recorded_season_alongside_is_tv_False_is_treated_as_TV(self):
        """Peer review round 10, Q8. The previous version of this test pinned
        the opposite -- False wins -- and that was the wrong precedence.

        `False` is normally the ABSENCE of positive TV evidence: the detail
        scraper initialises it False and raises it only on an Sxx match. A
        recorded season is an affirmative observation. Letting the absence
        outrank the observation is how a show ends up compared against the film
        library, which is the whole failure this file exists for.

        In the merged design the resolver encodes exactly this: only a True
        is_tv is evidence, and a recorded season is TITLE-authority TV.
        """
        m = _matching()
        s = _scanner(m)
        _run(s, [_item(s, is_tv=False, season=3, title="Show With A Season")])
        assert m.find_tv_season_matches.called, (
            "a recorded season was overridden by an is_tv=False that only means "
            "'no positive TV evidence was seen'"
        )
        assert not m.find_movie_matches.called

    def test_is_tv_True_with_no_season_is_still_TV(self):
        """The signal that must not be lost -- guarded again here because the
        cached-is-tv evidence line in _media_item_from_dict is the one most
        likely to be 'simplified' away."""
        m = _matching()
        s = _scanner(m)
        _run(s, [_item(s, is_tv=True, season=None)])
        assert m.find_tv_season_matches.called
        assert not m.find_movie_matches.called

    def test_neither_signal_is_refused_not_guessed(self):
        """CHANGED at the 2026-08-28 merge — see the module docstring.

        Main asserted that no-evidence items reach the film matcher, because a
        boolean branch has nowhere else to send them. The tri-state matcher
        refuses instead: zero evidence must not be compared against the film
        library any more than against the TV library. This pins PR #94's
        round-13 behaviour and will fail loudly if anyone restores the movie
        default."""
        m = _matching()
        s = _scanner(m)
        item = _item(s, is_tv=False, season=None, title="A Film")
        _run(s, [item])
        assert not m.find_movie_matches.called, (
            "a zero-evidence item was matched against the film library — the "
            "movie default this branch removed has been restored"
        )
        assert not m.find_tv_season_matches.called
        assert item.status is ScanStatus.MEDIA_TYPE_UNRESOLVED


class TestItSurvivesTheCache:
    """Deciding correctly and then losing it on the way to disk fixes nothing.

    `rematch_cache` reconstructs items from JSON, and that is the path which
    re-matches every cached row -- 4,068 of them on the live instance.

    RESTORED at the R4-94-1 review. The merge dropped this class because its
    three assertions were written about the boolean `is_tv`; two of them
    genuinely no longer describe the merged design and stayed dropped. This one
    does not: the carried fact simply changed name, from `is_tv` to
    `media_type`, and losing it in serialisation is exactly as fatal as before.

    The fixture is chosen so the heuristics DISAGREE with the stored verdict --
    a neutral title, a `4k` crawl category and no season, all of which re-derive
    to MOVIE. The assertion therefore proves the stored verdict is being
    CARRIED, not coincidentally re-derived to the same answer.
    """

    def test_media_type_round_trips_through_the_cache_serialisation(self):
        from backend.api.routes.scanner import _media_item_to_dict

        svc = ScannerService.__new__(ScannerService)
        decided = MediaItem(
            id="1", title="Quiet Neutral Title", year=2026, season=None,
            url="https://x/1", category="4k", is_tv=True,
            media_type="tv", media_type_provisional=False,
        )
        d = _media_item_to_dict(decided)
        assert d["media_type"] == "tv", "the verdict never reached the dict"
        assert d["media_type_provisional"] is False

        restored = svc._media_item_from_dict(d)
        assert restored is not None
        assert restored.media_type == "tv", (
            "a decided TV verdict was lost on the cache round trip -- the row's "
            "own heuristics (neutral title, 4k route, no season) re-derive to "
            "movie, so this release would be matched against the film library"
        )
        assert restored.media_type_provisional is False, (
            "a decided verdict came back marked provisional, which withdraws "
            "its authority to act"
        )

    def test_the_fixture_really_does_disagree_with_the_heuristics(self):
        """CONTROL. If the row's own evidence happened to resolve TV, the test
        above would pass without carrying anything."""
        svc = ScannerService.__new__(ScannerService)
        without_verdict = {
            "id": "1", "title": "Quiet Neutral Title", "year": 2026,
            "season": None, "url": "https://x/1", "category": "4k",
            "is_tv": False,
        }
        rederived = svc._media_item_from_dict(without_verdict)
        assert rederived is not None
        assert rederived.media_type == "movie"
