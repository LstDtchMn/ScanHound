"""R4-94-3: a suppressed route must not come back as the row's own verdict.

C1, THE FINDING. Conflict suppression was ORDER-DEPENDENT.

``cached_type_evidence`` blanks the crawl route when the row records a
``category_conflict``. ``cached_verdict_evidence`` never consulted that flag --
and a stored PROVISIONAL verdict is BY DEFINITION the route's own answer
("provisional means nothing above ROUTE spoke", its own docstring). So the
route's output re-entered at ROUTE authority, unopposed, and SURVIVED the
suppression of the exact route that produced it.

Reproduced through the real ``/scan/rescan-item`` at 1965399::

    seed {category:'tv', is_tv:False}
    rescan                             -> media_type 'tv', provisional, persisted
    mark_scan_category_conflict([url])   the in-place blob write that exists
                                         precisely for rows a crawl SKIPS as
                                         already cached
    rescan                             -> media_type 'tv', is_tv True,
                                          category_conflict True

``web_item_facts`` sets ``is_tv = media_type == 'tv'`` and
``_match_against_plex`` branches on it, so the conflicted release WAS compared
against the TV library.

THE DISCRIMINATING CONTROL IS ORDER ALONE. The identical final row with the
conflict recorded BEFORE any rescan gives 'ambiguous', stably. Every conflict
test on this branch seeds a row with NO stored media_type, so none of them can
see this. PRE-EXISTING, not a regression: the same probe at c5a5ab4 gives 'tv'
with provisional=False, so R4-94-2 improved it without closing it.

Everything below drives PRODUCTION code -- the HTTP route, the production
database writer that records a conflict, or the shared composition functions.
Nothing here re-implements the rule.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.routes.scanner import rescan_classification
from backend.database import DatabaseManager
from backend.scanner_service import (
    cached_media_type,
    cached_verdict_evidence,
    conflict_suppresses_stored_verdict,
)


URL = "https://hdencode.org/r4-94-3-order-independent/"

# Deliberately silent. A title carrying a season token is TITLE-authority TV
# evidence and would satisfy most of this file by itself -- the exact fixture
# defect R4-94-2 found in test_rescan_preserves_classification.py.
NEUTRAL_TITLE = "Quiet Neutral Title"


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed(**data):
    """One background_scan_cache row shaped like the live ones: the SOURCE NAME
    in the column, the crawl category inside the JSON."""
    row = {
        "url": URL, "title": NEUTRAL_TITLE, "year": 2026, "status": "missing",
        "category": "", "is_tv": False, "season": None,
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


def _details(*, is_tv=False, season=None):
    """A freshly-scraped detail page carrying nothing. ``is_tv=False`` is the
    honest reading of a filename with no season token, NOT a claim of film."""
    return {
        "display_title": NEUTRAL_TITLE, "year": 2026, "rating": "-", "url": URL,
        "imdb_id": "tt0000001", "size": "23.9 GB", "res": "2160p", "hdr": "SDR",
        "dovi": False, "is_tv": is_tv, "season": season,
        "episode_number": None, "episodes": None, "posted_date": None,
    }


def _rescan(client, details=None):
    """Drive the REAL route end to end."""
    import backend.api.dependencies as deps
    with patch.object(deps.registry.scanner.scrapers, "scrape_details",
                      return_value=details or _details()):
        resp = client.post("/scan/rescan-item", json={"url": URL})
    assert resp.status_code == 200, resp.text
    return resp.json()["item"]


def _persisted():
    dm = DatabaseManager()
    row = dm.get_background_cache_by_url(URL)
    dm.close()
    assert row is not None, "the rescan did not write the row back"
    return json.loads(row["data"])


def _record_conflict():
    """PRODUCTION writer. This is the in-place blob write that exists precisely
    for a release the crawl SKIPS as already cached -- the only way a conflict
    is ever recorded AFTER the row already carries a verdict, which is the
    whole ordering the finding is about."""
    dm = DatabaseManager()
    marked = dm.mark_scan_category_conflict([URL])
    dm.close()
    assert marked == 1, "the production conflict writer did not mark the row"


def _verdict_of(item):
    return item["media_type"], item["media_type_provisional"], item["is_tv"]


# The seeds are chosen so the FIRST rescan reaches a routable verdict and
# persists it -- otherwise there is no stored verdict for the conflict to be
# recorded against and the ordering cannot differ. `expected` is pinned by name
# as well as by the pair-equality below: two arms agreeing on a WRONG answer
# would satisfy equality alone, which is exactly the coincidence that makes a
# matched pair worthless on its own.
ORDERING_CASES = [
    # id                    seed                                  expected
    ("tv_route_only",       {"category": "tv"},                   ("ambiguous", True, False)),
    ("movie_route_only",    {"category": "4k"},                   ("ambiguous", True, False)),
    ("remux_route_only",    {"category": "remux"},                ("ambiguous", True, False)),
    # CONTROL, and the reason this fix is not "a conflict always means
    # ambiguous": a recorded season is TITLE evidence, the first rescan decides
    # on it, and a conflict about which LISTING carried the release says
    # nothing about the filename. The decided verdict survives.
    ("season_decided",      {"category": "tv", "season": 3},      ("tv", False, True)),
]


class TestOrderIsNoLongerAVariable:
    """C1. The same final row must give the same answer whichever order the
    conflict and the rescan happened in."""

    @pytest.mark.parametrize(
        "seed,expected", [(s, e) for _n, s, e in ORDERING_CASES],
        ids=[n for n, _s, _e in ORDERING_CASES])
    def test_conflict_after_a_rescan_matches_conflict_before(
            self, client, seed, expected):
        # ARM A -- the finding. Rescan first, so the route's own verdict is in
        # the row; then the crawl discovers the disagreement.
        _seed(**seed)
        first = _rescan(client)
        assert first["media_type"] in ("tv", "movie"), (
            "fixture check: the first rescan must reach a routable verdict, "
            "otherwise there is no stored verdict for the conflict to be "
            "recorded against and this case cannot test ordering at all"
        )
        _record_conflict()
        after = _rescan(client)

        # ARM B -- the control. The identical row, conflict recorded first.
        _seed(category_conflict=True, **seed)
        _rescan(client)
        before = _rescan(client)

        assert _verdict_of(after) == _verdict_of(before), (
            "recording the conflict AFTER a rescan gives a different answer "
            "than recording it BEFORE: the stored provisional verdict IS the "
            "suppressed route's own answer, re-entering at ROUTE authority "
            "above the suppression that removed it"
        )
        assert _verdict_of(after) == expected
        assert after["category_conflict"] is True
        assert _persisted()["media_type"] == expected[0], (
            "the answer was written back into the cache, so the next reader "
            "inherits it"
        )

    def test_the_conflicted_release_stops_reaching_the_tv_library(self, client):
        """The harm, stated in the terms the matcher uses. ``web_item_facts``
        sets ``is_tv = media_type == 'tv'`` and ``_match_against_plex`` branches
        on it, so a 'tv' verdict here IS a comparison against the TV library."""
        _seed(category="tv")
        assert _rescan(client)["is_tv"] is True     # the route's own answer
        _record_conflict()
        item = _rescan(client)
        assert item["is_tv"] is False, (
            "a release whose two listings disagree about its type was compared "
            "against the TV library, off the very route the conflict suppresses"
        )
        assert item["media_type"] == "ambiguous", (
            "and media_type must keep the distinction so the library query can "
            "REFUSE rather than defaulting to Movies"
        )

    def test_repeated_rescans_do_not_walk_the_answer_back(self, client):
        """The ordering fix must be a fixpoint, not a one-rescan delay."""
        _seed(category="tv")
        _rescan(client)
        _record_conflict()
        seen = [_verdict_of(_rescan(client)) for _ in range(4)]
        assert seen == [("ambiguous", True, False)] * 4, seen


class TestAConflictedRowWithAStoredVerdict:
    """C2. Every provisional value, on a row that RECORDS a media_type.

    Absent and True are the route's own answer and are suppressed; False had
    TITLE-or-better evidence behind it and survives. Every conflict test on the
    branch before this one seeded a row with no stored media_type at all.
    """

    @pytest.mark.parametrize("provisional,expected", [
        (True, ("ambiguous", True)),
        # ABSENT is not "decided". cached_media_type reads a missing flag as
        # provisional, so it must be suppressed for the same reason True is --
        # a legacy-shaped current-format row is the likeliest live shape.
        (None, ("ambiguous", True)),
        # The control that keeps the rule narrow.
        (False, ("tv", False)),
    ], ids=["provisional", "flag_absent", "decided"])
    def test_cached_media_type_on_a_conflicted_row(self, provisional, expected):
        row = {"category": "tv", "category_conflict": True, "is_tv": True,
               "media_type": "tv", "title": NEUTRAL_TITLE}
        if provisional is not None:
            row["media_type_provisional"] = provisional
        assert cached_media_type(row) == expected

    @pytest.mark.parametrize("provisional,suppressed", [
        (True, True), (None, True), (False, False)],
        ids=["provisional", "flag_absent", "decided"])
    def test_cached_verdict_evidence_on_a_conflicted_row(
            self, provisional, suppressed):
        row = {"category": "tv", "category_conflict": True, "media_type": "tv"}
        if provisional is not None:
            row["media_type_provisional"] = provisional
        evidence = cached_verdict_evidence(row)
        assert (evidence is None) is suppressed
        if evidence is not None:
            assert evidence.media_type.value == "tv"

    @pytest.mark.parametrize("provisional", [True, None, False],
                             ids=["provisional", "flag_absent", "decided"])
    def test_an_unconflicted_row_is_untouched(self, provisional):
        """The suppression must key on the CONFLICT, not on the flag. Without
        this, a mutant that suppresses every provisional verdict passes
        everything above."""
        row = {"category": "tv", "media_type": "tv", "title": NEUTRAL_TITLE}
        if provisional is not None:
            row["media_type_provisional"] = provisional
        assert cached_media_type(row) == ("tv", provisional is not False)
        assert cached_verdict_evidence(row) is not None

    def test_rev38_four_shapes(self):
        """The four shapes rev3.8's justification was checked against. Two move
        and two do not, and the two that do not are correct rather than
        unfixed: neither rests on the route. Row 1 is a LEGACY row whose is_tv
        is the detail scraper's own Sxx match (DETAIL); row 2 additionally
        records a season (TITLE). A conflict is about which listing carried the
        release, not about what the filename said."""
        assert cached_media_type(
            {"category": "tv", "category_conflict": True,
             "is_tv": True}) == ("tv", True)
        assert cached_media_type(
            {"category": "tv", "category_conflict": True, "is_tv": True,
             "season": 3}) == ("tv", True)
        assert cached_media_type(
            {"category": "tv", "category_conflict": True, "is_tv": True,
             "media_type": "tv",
             "media_type_provisional": True}) == ("ambiguous", True)
        assert cached_media_type(
            {"category": "4k", "category_conflict": True,
             "media_type": "movie"}) == ("ambiguous", True)

    def test_a_stored_ambiguous_is_not_suppressed(self):
        """'ambiguous' is not a routable answer, so there is nothing to
        suppress -- and re-deriving over it would let a CONFLICTED row become
        MORE decided than the row itself recorded."""
        row = {"category": "tv", "category_conflict": True,
               "media_type": "ambiguous", "season": 3}
        assert conflict_suppresses_stored_verdict(row) is False
        assert cached_media_type(row) == ("ambiguous", True)

    def test_the_conflicted_stored_verdict_through_the_real_route(self, client):
        """C2 where it is reachable: the route reads the same functions."""
        _seed(category="tv", media_type="tv", media_type_provisional=True,
              is_tv=True)
        item = _rescan(client)
        assert (item["media_type"], item["is_tv"]) == ("tv", True), (
            "fixture check: with no conflict the stored provisional verdict is "
            "carried, so the conflicted arm below is not testing a row that "
            "resolves 'ambiguous' anyway"
        )
        _seed(category="tv", category_conflict=True, media_type="tv",
              media_type_provisional=True, is_tv=True)
        item = _rescan(client)
        assert (item["media_type"], item["is_tv"]) == ("ambiguous", False)

    def test_fresh_detail_evidence_still_overrules_a_conflicted_row(self, client):
        """Suppression must not become refusal. A season token on the freshly
        fetched detail page is DETAIL evidence the conflict says nothing
        about, and it still decides."""
        _seed(category="tv", category_conflict=True, media_type="tv",
              media_type_provisional=True)
        item = _rescan(client, _details(is_tv=True, season=2))
        assert (item["media_type"], item["media_type_provisional"]) == ("tv", False)


class TestTheInvariantHoldsInBothReaders:
    """C3. ``is_tv is (media_type == 'tv')`` -- the invariant the route
    enforces -- was violated in the sibling cache->item reader that R4-94-2's
    own rationale cites as the authority for what a cached row means."""

    @pytest.fixture
    def scanner(self, client):
        import backend.api.dependencies as deps
        return deps.registry.scanner

    @pytest.mark.parametrize("row,expected_type", [
        ({"category": "", "is_tv": True, "media_type": "ambiguous"}, "ambiguous"),
        ({"category": "4k", "is_tv": True, "media_type": "movie"}, "movie"),
    ], ids=["ambiguous_with_is_tv", "movie_with_is_tv"])
    def test_the_two_executed_shapes(self, scanner, row, expected_type):
        item = scanner._media_item_from_dict(
            dict(row, url=URL, title=NEUTRAL_TITLE))
        assert item is not None
        assert item.media_type == expected_type, (
            "fixture check: the verdict itself must not move -- this test is "
            "about the boolean beside it, and a changed media_type would make "
            "the assertion below pass for the wrong reason"
        )
        assert item.is_tv is False, (
            "the reader carried the stored is_tv verbatim while setting "
            "media_type independently, so it produced exactly the "
            "contradiction R4-94-2 removed one route over"
        )

    @pytest.mark.parametrize("row", [
        {"category": "", "is_tv": True, "media_type": "ambiguous"},
        {"category": "4k", "is_tv": True, "media_type": "movie"},
        {"category": "tv", "is_tv": False, "media_type": "tv"},
        {"category": "4k", "is_tv": True},
        {"category": "", "is_tv": False, "season": 3},
        {"category": "tv", "category_conflict": True, "media_type": "tv",
         "media_type_provisional": True, "is_tv": True},
        {"category": "", "is_tv": False},
    ])
    def test_the_invariant_over_every_shape(self, scanner, row):
        item = scanner._media_item_from_dict(
            dict(row, url=URL, title=NEUTRAL_TITLE))
        assert item is not None
        assert item.is_tv is (item.media_type == "tv"), (
            f"{row} -> media_type={item.media_type!r} is_tv={item.is_tv!r}"
        )

    def test_a_legacy_is_tv_row_still_resolves_tv(self, scanner):
        """Nothing is lost. On a LEGACY row -- no media_type at all -- the
        stored is_tv is genuine recovered observation and reaches the verdict
        as DETAIL evidence, so it still resolves 'tv' and still yields True.
        Without this, deriving the boolean could be 'satisfied' by a reader
        that always answers False."""
        item = scanner._media_item_from_dict(
            {"category": "", "is_tv": True, "url": URL, "title": NEUTRAL_TITLE})
        assert (item.media_type, item.is_tv) == ("tv", True)


class TestTheCrawlAttestationIsCarried:
    """C4. ``category_attested`` was dropped on the rescan path.

    ``attest_scan_categories`` writes the flag ONLY where the key is absent,
    and ``get_scan_category`` reads its absence as NEVER CHECKED and returns
    None -- deliberately. So dropping it downgraded an attested clean row to
    unverifiable and withheld the server-owned media kind, until some future
    crawl happened to observe the release again.
    """

    def _attest(self):
        dm = DatabaseManager()
        n = dm.attest_scan_categories([URL])
        dm.close()
        assert n == 1, "the production attestation writer did not mark the row"

    def _scan_category(self):
        dm = DatabaseManager()
        try:
            return dm.get_scan_category(URL)
        finally:
            dm.close()

    def test_rescan_classification_returns_the_attestation(self):
        # THREE states, and the third lives in the KEY. False means "checked
        # and not attested"; a row nobody ever checked must not claim to have
        # been. Returning False here is what let the rescan persist False and
        # withdraw the row from attest_scan_categories permanently.
        assert rescan_classification({"data": json.dumps(
            {"category": "tv", "category_attested": True})}) == ("tv", False, True)
        assert rescan_classification({"data": json.dumps(
            {"category": "tv", "category_attested": False})}) == ("tv", False, False)
        assert rescan_classification({"data": json.dumps(
            {"category": "tv"})}) == ("tv", False, None)

    def test_an_attested_row_survives_a_rescan(self, client):
        _seed(category="tv")
        self._attest()
        assert self._scan_category() == "tv", "fixture check: attested and clean"
        _rescan(client)
        assert _persisted()["category_attested"] is True
        assert self._scan_category() == "tv", (
            "the rescan silently downgraded an attested row to NEVER CHECKED, "
            "withholding the media kind that authorises Keep-best"
        )

    def test_an_unattested_row_is_not_invented_into_attestation(self, client):
        """Carried, not manufactured. A rescan observes nothing about which
        listings carried the release, so it must not create the flag either.

        This assertion used to read ``is False`` -- which contradicted the
        sentence above it. Writing False IS creating the flag: it is the exact
        state attest_scan_categories skips on, so one rescan permanently
        withdrew a never-checked row from the ONLY writer that reaches a
        release the crawl skips as already cached. Measured, with a no-rescan
        control on the identical row: control -> attest=1, get_scan_category
        'tv'; after one rescan -> attest=0, get_scan_category None, forever.
        """
        _seed(category="tv")
        assert self._scan_category() is None, "fixture check: never attested"
        _rescan(client)
        assert "category_attested" not in _persisted(), (
            "the rescan wrote category_attested=%r onto a row that never had "
            "the key; that is the state attest_scan_categories skips on"
            % (_persisted().get("category_attested"),)
        )
        assert self._scan_category() is None

        # The harm itself, not just its cause: the row must still be reachable
        # by the attestation writer afterwards. This is what the old behaviour
        # destroyed, and destroyed permanently.
        self._attest()
        assert self._scan_category() == "tv", (
            "a rescan left the row unattestable: attest_scan_categories could "
            "not reach it, so the server-owned media kind is withheld forever"
        )

    def test_a_conflicted_row_stays_unverifiable(self, client):
        """The two carried facts together: attestation is present, and the
        conflict still withholds the answer."""
        _seed(category="tv")
        self._attest()
        _record_conflict()
        _rescan(client)
        row = _persisted()
        assert (row["category_attested"], row["category_conflict"]) == (True, True)
        assert self._scan_category() is None
