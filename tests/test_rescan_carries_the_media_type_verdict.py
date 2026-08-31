"""R4-94-1: the verdict a rescan carries must reach the field that DECIDES.

`/scan/rescan-item` recovered the cached positive TV signal into the legacy
`is_tv` field (round 11's fix, still correct) and then computed the new
authoritative `media_type` WITHOUT it. The matcher no longer branches on
`is_tv`; it selects a library from `media_type`. So the preserved decision sat
in a field nothing reads while the field that authorises the actual comparison
had been re-derived from a strictly smaller set of evidence.

    cached row: category='4k' (or blank), is_tv=True, season=None,
                neutral title
    fresh page: is_tv=False (no season token in the filename)

    rescan_classification  -> is_tv True    (correct, and preserved)
    old composition        -> media_type 'movie' -- route evidence only

The object was internally contradictory, and the route serialises it straight
back into background_scan_cache, so the re-derived 'movie' became the next
row's carried verdict. One rescan of a season pack was enough to lose the
classification permanently.

WHY THE EXISTING TESTS MISSED IT. tests/test_rescan_preserves_classification.py
drives `rescan_classification` and then ORs its answer with the fresh detail
`is_tv` -- which is exactly the LEGACY half. It never executes the
`resolve_*_media_type` composition that now carries the authority. A stale
consumer test after a model migration: correct about the code it calls, blind
to the code that decides.

Everything below drives PRODUCTION composition -- either the HTTP route itself
(cache write, verdict, matcher-facing object, cache write-back) or the shared
`resolve_rescan_media_type` that the route calls. Nothing here re-implements
the rule.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import release_grammar as grammar
from backend.api.main import create_app
from backend.database import DatabaseManager
from backend.scanner_service import resolve_rescan_media_type


URL = "https://hdencode.org/r4-94-1-carried-verdict/"


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed(**data):
    """Write one background_scan_cache row shaped like the live ones: the
    SOURCE NAME in the column, the crawl category inside the JSON."""
    row = {
        "url": URL,
        "title": "Quiet Neutral Title",
        "year": 2026,
        "status": "missing",
        "category": "",
        "is_tv": False,
        "season": None,
    }
    row.update(data)
    dm = DatabaseManager()
    dm.upsert_background_cache([{
        "url": URL, "title": row["title"], "year": row["year"],
        "status": "missing", "source_category": "HDEncode",
        "data": json.dumps(row),
    }])
    dm.close()
    return row


def _details(*, is_tv=False, season=None, title="Quiet Neutral Title"):
    """A freshly-scraped detail page. `is_tv=False` is the honest reading of a
    filename with no season token -- it is NOT a claim that this is a film."""
    return {
        "display_title": title, "year": 2026, "rating": "-", "url": URL,
        "imdb_id": "tt0000001", "size": "23.9 GB", "res": "2160p",
        "hdr": "SDR", "dovi": False, "is_tv": is_tv, "season": season,
        "episode_number": None, "episodes": None, "posted_date": None,
    }


def _rescan(client, details):
    """Drive the REAL route end to end."""
    import backend.api.dependencies as deps
    with patch.object(deps.registry.scanner.scrapers, "scrape_details",
                      return_value=details):
        resp = client.post("/scan/rescan-item", json={"url": URL})
    assert resp.status_code == 200, resp.text
    return resp.json()["item"]


def _persisted():
    dm = DatabaseManager()
    row = dm.get_background_cache_by_url(URL)
    dm.close()
    assert row is not None, "the rescan did not write the row back"
    return json.loads(row["data"])


# ── The regression ──────────────────────────────────────────────────────────

class TestTheCarriedVerdictReachesMediaType:
    """The reviewer's required regression, through the live HTTP route."""

    @pytest.mark.parametrize("category", ["4k", ""])
    def test_a_recorded_is_tv_survives_into_media_type(self, client, category):
        _seed(category=category, is_tv=True, season=None)
        item = _rescan(client, _details(is_tv=False))

        assert item["media_type"] == "tv", (
            "the cached is_tv=True was carried into the legacy field and then "
            "discarded when media_type was re-derived -- the matcher routes on "
            "media_type, so this release goes to the FILM library"
        )
        assert item["is_tv"] is True
        assert _persisted()["media_type"] == "tv", (
            "the re-derived verdict was written back into the cache, so the "
            "next reader inherits the wrong answer"
        )

    @pytest.mark.parametrize("row,fresh_is_tv", [
        ({"category": "4k", "is_tv": True}, False),
        ({"category": "", "is_tv": True}, False),
        ({"category": "tv", "is_tv": False}, False),
        ({"category": "4k", "is_tv": False, "season": 3}, False),
        ({"category": "4k", "is_tv": False}, False),
        ({"category": "", "is_tv": False}, False),
        ({"category": "4k", "is_tv": False}, True),
        ({"category": "tv", "category_conflict": True, "is_tv": False}, False),
        ({"category": "4k", "category_conflict": True, "is_tv": True}, False),
    ])
    def test_the_two_type_fields_never_contradict_each_other(
            self, client, row, fresh_is_tv):
        """The shape of the defect, stated directly: one object must not claim
        television in one field and film in the other.

        R4-94-2 REWROTE this test. It asserted only

            not (item['is_tv'] and item['media_type'] == 'movie')

        which is blind to the third media_type. A row recording a
        category_conflict came back media_type='ambiguous' WITH is_tv=True --
        an item that refused to decide its own type while asserting TV in the
        legacy field, and the route persisted both. The old form could not see
        it, and the two rows it ran on could not produce it.

        The assertion is now equality, not an excluded pair: is_tv is the
        SHADOW of the verdict (web_item_facts' rule, and _process_posts'), so
        'tv' and only 'tv' may set it. Run over every row shape this file
        exercises, including the two conflicted ones the old form never saw.
        """
        _seed(**row)
        item = _rescan(client, _details(is_tv=fresh_is_tv))
        assert item["is_tv"] is (item["media_type"] == "tv"), (
            f"is_tv={item['is_tv']} beside media_type={item['media_type']!r}: "
            "one object, two answers"
        )
        persisted = _persisted()
        assert persisted["is_tv"] is (persisted["media_type"] == "tv"), (
            "the contradiction was written back into the cache, where the next "
            "reader inherits it"
        )

    def test_a_cached_season_also_reaches_media_type(self, client):
        """The second carried positive signal. TITLE authority, so it still
        beats a movie route."""
        _seed(category="4k", is_tv=False, season=3)
        item = _rescan(client, _details(is_tv=False))
        assert item["media_type"] == "tv"
        assert _persisted()["media_type"] == "tv"

    def test_a_cached_tv_route_reaches_media_type(self, client):
        _seed(category="tv", is_tv=False, season=None)
        item = _rescan(client, _details(is_tv=False))
        assert item["media_type"] == "tv"
        assert item["media_type_provisional"] is True, (
            "a route-only verdict is provisional -- it may route and display, "
            "but it must not authorise anything autonomous"
        )


# ── Negative controls ───────────────────────────────────────────────────────

class TestItDoesNotJustAnswerTV:
    """Without these, `media_type = 'tv'` passes everything above."""

    def test_no_positive_signal_and_a_movie_route_is_a_provisional_movie(
            self, client):
        _seed(category="4k", is_tv=False, season=None)
        item = _rescan(client, _details(is_tv=False))
        assert item["media_type"] == "movie"
        assert item["media_type_provisional"] is True
        assert item["is_tv"] is False
        assert _persisted()["media_type"] == "movie"

    def test_no_signals_at_all_is_refused_not_guessed(self, client):
        """KEPT from round 13 and endorsed on review: zero evidence must not be
        compared against the film library any more than the TV one."""
        _seed(category="", is_tv=False, season=None)
        item = _rescan(client, _details(is_tv=False))
        assert item["media_type"] == "ambiguous"
        assert _persisted()["media_type"] == "ambiguous"

    def test_stronger_fresh_evidence_still_overrules_a_carried_route(
            self, client):
        """NOT "old always wins". The fresh detail filename is DETAIL
        authority; the carried 4k crawl category is only ROUTE. The season
        token the rescan just read decides."""
        _seed(category="4k", is_tv=False, season=None)
        item = _rescan(client, _details(is_tv=True, season=1))
        assert item["media_type"] == "tv"
        assert item["media_type_provisional"] is False, (
            "a DETAIL-authority verdict is not provisional"
        )

    def test_a_recorded_conflict_suppresses_the_route_it_conflicts_over(
            self, client):
        """A crawl-time category conflict is not a route to trust, and a rescan
        re-reads a detail page -- it learns nothing about which LISTINGS
        carried the release, so it cannot clear the conflict either."""
        _seed(category="4k", category_conflict=True, is_tv=False, season=None)
        item = _rescan(client, _details(is_tv=False))
        assert item["media_type"] == "ambiguous"
        assert item["category_conflict"] is True


# ── The composition itself, without HTTP ────────────────────────────────────

class TestTheSharedCompositionIsTheOnlyRule:
    """`resolve_rescan_media_type` is imported from production; the route calls
    the same function on the same inputs. These pin the authority mapping so a
    later edit cannot quietly flatten it back into an OR."""

    def test_carried_is_tv_is_detail_authority(self):
        v = resolve_rescan_media_type(
            {"category": "4k", "is_tv": True, "season": None,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.TV
        assert v.provisional is False
        assert any("cached-is-tv" in b for b in v.because)

    def test_carried_season_is_title_authority(self):
        v = resolve_rescan_media_type(
            {"category": "4k", "is_tv": False, "season": 2,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.TV
        assert any("cached-season" in b for b in v.because)

    def test_the_listing_title_is_used_when_the_row_has_none(self):
        """The route passes the DB column as a fallback: a legacy row's JSON
        may predate the title being stored inside it."""
        v = resolve_rescan_media_type(
            {"category": "4k"}, {"is_tv": False},
            listing_title="Some Show Complete Series")
        assert v.media_type is grammar.MediaType.TV
        assert any("cached-title" in b for b in v.because)

    def test_a_stored_provisional_movie_verdict_does_not_outrank_fresh_tv(self):
        """A stored verdict re-enters at the authority its own provisional flag
        records. Provisional means "route alone decided this", so a freshly
        observed season token overrules it."""
        v = resolve_rescan_media_type(
            {"category": "4k", "media_type": "movie",
             "media_type_provisional": True, "title": "Quiet Neutral Title"},
            {"is_tv": True})
        assert v.media_type is grammar.MediaType.TV

    def test_a_stored_non_provisional_tv_verdict_is_carried(self):
        """The row that was already rescanned once. Its decided TV verdict must
        not be lost the second time round."""
        v = resolve_rescan_media_type(
            {"category": "4k", "media_type": "tv",
             "media_type_provisional": False, "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.TV
        assert any("cached-verdict" in b for b in v.because)

    def test_a_stored_ambiguous_verdict_is_not_evidence_for_tv(self):
        """"We could not decide" is a record of having decided nothing. Two
        cases, because a mutation that admits 'ambiguous' as evidence has to
        pick a side and either side must be caught.

        Here the row is a plain 4k film carrying a DECIDED 'ambiguous'. If that
        were read as TV evidence at DETAIL authority it would beat the route."""
        v = resolve_rescan_media_type(
            {"category": "4k", "media_type": "ambiguous",
             "media_type_provisional": False, "is_tv": False, "season": None,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.MOVIE
        assert v.provisional is True, (
            "the verdict is carried by the crawl route alone; a stored "
            "'ambiguous' must not lend it authority it never had"
        )
        assert not any("cached-verdict" in b for b in v.because)

    def test_a_stored_ambiguous_verdict_is_not_evidence_for_movie(self):
        """The other side. A decided 'ambiguous' read as MOVIE at DETAIL would
        overrule the season this row actually records."""
        v = resolve_rescan_media_type(
            {"category": "", "media_type": "ambiguous",
             "media_type_provisional": False, "is_tv": False, "season": 2,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.TV
        assert not any("cached-verdict" in b for b in v.because)


# ── The feedback loop ───────────────────────────────────────────────────────

class TestTheRouteDoesNotLaunderItsOwnVerdict:
    """R4-94-2. The route WRITES the legacy is_tv field from its own verdict and
    persists it; cached_type_evidence then re-admitted that same boolean at
    DETAIL authority on the next read. The system's own conclusion re-entered as
    an observation, one authority level above the evidence it was built from --
    so a second rescan, with no new information whatsoever, manufactured a
    stronger answer than the first.

    What it costs, traced rather than assumed. The reviewer cited
    backend/hdencode_action_service.py:505, which raises
    'auto_media_type_provisional' to refuse autonomous action -- but that gate
    reads hdencode_candidates, fed by the RSS feed parser and
    hdencode_candidate_service, NOT background_scan_cache, which is the table
    this route writes. There is no path from here to that raise on this head.
    The demonstrated cost is what these tests assert: a verdict that gets
    stronger every click without observing anything, and a deliberately
    suppressed category conflict that reverses into a decided TV verdict --
    which changes the library the release is compared against. The doctrine
    ("provisional must not authorise anything autonomous") is this branch's own,
    and a flag that clears itself violates it wherever it is read.

    The rule these pin: a row that records a media_type is CURRENT-FORMAT, and
    its is_tv is then a derived shadow of that verdict -- not independent detail
    evidence. Only a legacy row (no stored media_type) carries an is_tv that is
    genuine recovered observation.
    """

    def test_a_second_rescan_of_the_same_row_reaches_the_same_verdict(
            self, client):
        """L1, through the real route. Nothing about the release changed between
        the two clicks and nothing new was observed, so nothing about the answer
        may change either."""
        _seed(category="tv", is_tv=False, season=None)

        first = _rescan(client, _details(is_tv=False))
        assert first["media_type"] == "tv"
        assert first["media_type_provisional"] is True, (
            "a route-only verdict is provisional"
        )

        second = _rescan(client, _details(is_tv=False))
        assert second["media_type"] == "tv"
        assert second["media_type_provisional"] is True, (
            "the second rescan observed NOTHING new -- it re-read the is_tv the "
            "first rescan itself wrote, admitted it at DETAIL authority, and "
            "cleared the provisional flag that gates autonomous action"
        )
        assert _persisted()["media_type_provisional"] is True

    def test_a_suppressed_conflict_stays_suppressed_across_rescans(
            self, client):
        """L2. The behaviour change this branch deliberately introduced -- a
        recorded category conflict resolves to 'ambiguous' and is refused --
        reversed itself on the second click, from evidence the route
        manufactured on the first."""
        _seed(category="tv", category_conflict=True, is_tv=False, season=None)

        first = _rescan(client, _details(is_tv=False))
        assert first["media_type"] == "ambiguous"
        assert first["media_type_provisional"] is True

        second = _rescan(client, _details(is_tv=False))
        assert second["media_type"] == "ambiguous", (
            "the conflict this branch suppresses became a DECIDED tv verdict "
            "two clicks later, out of the route's own persisted is_tv"
        )
        assert second["media_type_provisional"] is True
        assert _persisted()["media_type"] == "ambiguous"

    def test_a_stored_verdicts_row_does_not_readmit_is_tv_as_detail(self):
        """The rule itself, on the production function. The same row twice, the
        only difference being whether it records a verdict.

        Legacy (no media_type): is_tv=True is genuine recovered observation from
        main's #93 rows -- DETAIL, and it decides.
        Current-format: is_tv is a shadow of the recorded verdict, so the stored
        verdict alone speaks, at the authority its own provisional flag
        records."""
        legacy = resolve_rescan_media_type(
            {"category": "tv", "is_tv": True, "season": None,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert legacy.media_type is grammar.MediaType.TV
        assert legacy.provisional is False, (
            "the recorded is_tv is DETAIL evidence on a legacy row, so the "
            "verdict is not decided by the route alone"
        )
        assert any("cached-is-tv" in b for b in legacy.because)

        # Exactly the row the first rescan in the test above writes back: a tv
        # route, a provisional tv verdict, and the is_tv shadow of that verdict.
        current = resolve_rescan_media_type(
            {"category": "tv", "is_tv": True, "season": None,
             "media_type": "tv", "media_type_provisional": True,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert current.media_type is grammar.MediaType.TV
        assert current.provisional is True, (
            "the row's own recorded verdict was provisional; its is_tv shadow "
            "must not promote it to a decided one"
        )
        assert not any("cached-is-tv" in b for b in current.because), (
            "a current-format row's is_tv is derived from media_type, so "
            "admitting it as DETAIL evidence is the system reading its own "
            "conclusion back in as an observation"
        )

    def test_a_stored_ambiguous_row_does_not_readmit_is_tv_either(self):
        """'ambiguous' is a recorded verdict too -- the record of having decided
        nothing. A row that refused to decide, carrying is_tv=True, must not
        resolve TV off that shadow."""
        v = resolve_rescan_media_type(
            {"category": "tv", "category_conflict": True, "is_tv": True,
             "season": None, "media_type": "ambiguous",
             "media_type_provisional": True, "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.AMBIGUOUS
        assert v.provisional is True

    def test_a_legacy_row_still_recovers_its_recorded_is_tv(self):
        """The control that keeps the gate from being a blanket removal. Rows
        written by main since #93 record is_tv and NO media_type; for them the
        boolean is the only surviving trace of a real decision and must still be
        honoured at DETAIL."""
        v = resolve_rescan_media_type(
            {"category": "", "is_tv": True, "season": None,
             "title": "Quiet Neutral Title"},
            {"is_tv": False})
        assert v.media_type is grammar.MediaType.TV
        assert v.provisional is False
        assert any("cached-is-tv" in b for b in v.because)
