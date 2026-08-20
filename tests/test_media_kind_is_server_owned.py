"""The media kind must be the SERVER's answer, never the client's.

Peer review round 10, finding M1. The kind was taken from
`DownloadRequest.category`, which is declared `category: str = ""`, documented
as unvalidated, and forwarded straight into `download_item(category=...)`. The
frontend fills it from `ScanResult.category`, so the real chain was:

    server scan  ->  frontend object  ->  JSON request  ->  unvalidated field
                 ->  downloads.media_kind  ->  identity_kind
                 ->  destructive Keep-best authority in the UI

The reviewer's phrasing of the defect is the one worth keeping:

    package provenance != media-kind provenance

Knowing which release a package came from does not certify what KIND of thing
that release is. The server scanned it and already recorded the listing it came
from, so the server answers; the client's value is only ever allowed to
CONTRADICT.

WHY A WRONG VALUE IS WORSE THAN A MISSING ONE: a missing kind fails closed --
the row groups but never authorizes. A recognized wrong kind authorizes. This
stack has already shipped one signal-propagation bug (batched grabs dropped the
category entirely until #92), so the mechanism is not hypothetical.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_service import DownloadService


URL = "https://hdencode.org/some-release-2026-2160p/"


@pytest.fixture
def svc(tmp_path):
    db = DatabaseManager(str(tmp_path / "m1.db"))
    service = DownloadService.__new__(DownloadService)
    service.db = db
    yield service
    db.close()


def _scanned_as(db, category, url=URL):
    """Record what THIS SERVER saw, the way a background scan does."""
    db.upsert_background_cache([{
        "url": url,
        "title": "Some Release",
        "year": 2026,
        "status": "missing",
        "source_category": "HDEncode",
        # category_attested: a conflict-aware crawl checked this release.
        # Without it the lookup refuses, because a row written before
        # conflict detection cannot prove it was ever checked.
        "data": json.dumps({"url": url, "title": "Some Release",
                            "category": category, "season": None,
                            "category_attested": True}),
    }])


class TestTheServerAnswers:
    def test_the_recorded_category_decides(self, svc):
        _scanned_as(svc.db, "tv")
        assert svc.verified_media_kind(URL, "tv") == "tv"

    def test_a_silent_client_still_gets_the_servers_answer(self, svc):
        """The client's value is a cross-check, not the supply. Sending nothing
        is not a disagreement."""
        _scanned_as(svc.db, "4k")
        assert svc.verified_media_kind(URL, "") == "movie"
        assert svc.verified_media_kind(URL, None) == "movie"

    def test_remux_is_a_movie_and_tv_is_tv(self, svc):
        _scanned_as(svc.db, "remux", url=URL + "a")
        _scanned_as(svc.db, "tv", url=URL + "b")
        assert svc.verified_media_kind(URL + "a", "remux") == "movie"
        assert svc.verified_media_kind(URL + "b", "tv") == "tv"


class TestAClientCannotSupplyTheAnswer:
    """The finding itself."""

    def test_a_recognized_but_WRONG_category_records_nothing(self, svc):
        """The exact hazard. 'tv' is a recognized value, so the old code mapped
        it happily; the server scanned this release from a 4K listing."""
        _scanned_as(svc.db, "4k")
        assert svc.verified_media_kind(URL, "tv") is None

    def test_the_reverse_disagreement_also_records_nothing(self, svc):
        _scanned_as(svc.db, "tv")
        assert svc.verified_media_kind(URL, "4k") is None

    def test_an_unscanned_url_records_nothing_however_confident_the_client(self, svc):
        """No server record is 'cannot verify', not 'trust the caller'."""
        assert svc.verified_media_kind(URL, "tv") is None
        assert svc.verified_media_kind(URL, "4k") is None

    def test_a_disagreement_is_logged_not_silent(self, svc, caplog):
        """A conflict means a stale UI, a mismatched row, or a call-site bug.
        Failing closed silently would hide the bug that caused it."""
        _scanned_as(svc.db, "4k")
        with caplog.at_level("WARNING"):
            assert svc.verified_media_kind(URL, "tv") is None
        # getMessage() applies the args; r.message alone is the raw format
        # string, so a %-placeholder assertion silently tests the wrong text.
        messages = [r.getMessage() for r in caplog.records]
        assert any("media kind NOT recorded" in m for m in messages), messages
        assert any("category='tv'" in m and "'4k'" in m for m in messages), (
            "the log must name BOTH values; 'a mismatch happened' is not "
            "actionable when the point is to find which side is wrong")


class TestUnreadableEvidenceIsNotPermission:
    def test_a_failing_lookup_records_nothing(self, svc):
        """An exception from the evidence source must not fall through to
        trusting the client -- that would make a broken database the most
        permissive state."""
        svc.db = MagicMock()
        svc.db.get_scan_category.side_effect = RuntimeError("db down")
        assert svc.verified_media_kind(URL, "tv") is None

    def test_no_database_at_all_records_nothing(self, svc):
        svc.db = None
        assert svc.verified_media_kind(URL, "tv") is None

    def test_an_undecodable_cached_row_records_nothing(self, svc):
        svc.db.upsert_background_cache([{
            "url": URL, "title": "x", "year": 2026, "status": "missing",
            "source_category": "HDEncode", "data": "{not json",
        }])
        assert svc.verified_media_kind(URL, "tv") is None


class TestTheResolutionHappensOnce:
    def test_every_history_path_consumes_one_resolved_value(self):
        """Four call sites wrote the kind. Resolving per site would be four
        chances for one of them to keep using the raw client category -- the
        defect class that let batched grabs drop it entirely until #92.
        """
        import io as _io
        import re

        source = _io.open("backend/download_service.py", encoding="utf-8").read()
        assert source.count("_verified_kind = self.verified_media_kind(") == 1, (
            "the kind must be resolved exactly once per download_item call")
        assert source.count("media_kind=_verified_kind") == 4, (
            "every history-writing path must consume the resolved value")
        assert not re.search(r"media_kind=self\.media_kind_for_category\(category\)", source), (
            "a history path is still mapping the raw client category")


# ── the other half of M1: the crawl's own classification ──────────────────
#
# Fixing the client round trip is not enough on its own. The server's category
# was ALSO first-source-wins: one `seen_post_urls` set spans every source, and
# the movie listings are crawled before TV Packs (4K -> Remux -> TV Packs), so a
# release visible in both was recorded as a movie and the TV listing was skipped
# entirely, its evidence discarded with no trace.
#
# The post is still processed ONCE. What changes is that a disagreeing second
# listing is recorded instead of dropped.

import asyncio
import threading

from backend.scanner_service import ScannerService


def _page(entries):
    rows = "".join(
        '<div class="data"><h5><a href="%s">%s</a></h5></div>' % (u, t)
        for u, t in entries)
    return ("<html><body>%s</body></html>" % rows).encode()


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self.content = body


class _Scraper:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def get(self, *_a, **_kw):
        body = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return _Resp(body)


def _shell():
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._last_crawl_seen_urls = set()
    s._last_crawl_early_stopped = False
    s._last_crawl_request_count = 0
    s._last_crawl_policy_excluded_new = []
    s._last_crawl_policy_excluded_count = 0
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = {}
    s.db = None
    return s


def _source(name, kind, category):
    return {"name": name, "base": f"https://hdencode.org/{category}/",
            "suffix": "", "type": kind, "source": "hdencode",
            "category": category}


def _crawl(sources, scraper, monkeypatch):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)

    async def run():
        loop = asyncio.get_running_loop()
        return await _shell()._crawl_pages(
            sources, pages=1, base_url="https://hdencode.org",
            scraper=scraper, loop=loop, previously_scanned=set(),
            early_stop=False, policy_excluded=None, skip_full_disc=False)
    return asyncio.run(run())


SHARED = "https://hdencode.org/some-show-s02-2160p/"


class TestTwoListingsThatDisagree:
    def test_a_release_in_both_a_movie_and_a_tv_listing_is_marked_conflicted(
            self, monkeypatch):
        """The case that silently became a movie."""
        scraper = _Scraper([_page([(SHARED, "Some Show S02 2160p")])])
        posts = _crawl([_source("4K Movies", "movie", "4k"),
                        _source("TV Packs", "tv", "tv")], scraper, monkeypatch)
        assert len(posts) == 1, "the post must still be processed exactly once"
        assert posts[0]["category_conflict"] is True

    def test_the_first_listing_still_supplies_the_category(self, monkeypatch):
        """The conflict is recorded ALONGSIDE the original value, not instead
        of it -- downstream needs to know what was seen, only not to trust it."""
        scraper = _Scraper([_page([(SHARED, "Some Show S02 2160p")])])
        posts = _crawl([_source("4K Movies", "movie", "4k"),
                        _source("TV Packs", "tv", "tv")], scraper, monkeypatch)
        assert posts[0]["category"] == "4k"
        assert posts[0]["type"] == "movie"

    def test_two_movie_listings_are_NOT_a_conflict(self, monkeypatch):
        """'4k' and 'remux' both mean movie. A collision between them says
        nothing contradictory about the KIND, and marking it would make the
        conflict signal fire constantly and mean nothing."""
        scraper = _Scraper([_page([(SHARED, "A Film 2026 2160p")])])
        posts = _crawl([_source("4K Movies", "movie", "4k"),
                        _source("Remux Movies", "movie", "remux")],
                       scraper, monkeypatch)
        assert len(posts) == 1
        assert posts[0]["category_conflict"] is False

    def test_an_uncontested_release_is_not_marked(self, monkeypatch):
        scraper = _Scraper([_page([(SHARED, "A Film 2026 2160p")])])
        posts = _crawl([_source("4K Movies", "movie", "4k")], scraper, monkeypatch)
        assert posts[0]["category_conflict"] is False


class TestAConflictedReleaseHasNoRecordedKind:
    def _cache(self, db, *, category, conflict):
        db.upsert_background_cache([{
            "url": URL, "title": "Some Show", "year": 2026, "status": "missing",
            "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": category,
                                "category_attested": True,
                                "category_conflict": conflict}),
        }])

    def test_a_conflicted_row_answers_nothing(self, svc):
        self._cache(svc.db, category="4k", conflict=True)
        assert svc.db.get_scan_category(URL) is None
        assert svc.verified_media_kind(URL, "4k") is None

    def test_the_same_row_without_the_conflict_answers_normally(self, svc):
        """Positive control. Without it the test above would pass even if the
        lookup were broken for every row."""
        self._cache(svc.db, category="4k", conflict=False)
        assert svc.db.get_scan_category(URL) == "4k"
        assert svc.verified_media_kind(URL, "4k") == "movie"

    def test_agreeing_with_the_first_listing_does_not_rescue_it(self, svc):
        """A client echoing the movie category cannot resolve a conflict the
        server recorded -- that is the first-source-wins outcome coming back in
        through the other door."""
        self._cache(svc.db, category="4k", conflict=True)
        assert svc.verified_media_kind(URL, "4k") is None
        assert svc.verified_media_kind(URL, "tv") is None
        assert svc.verified_media_kind(URL, "") is None


# ── M1a: a conflict must RETRACT an already-persisted kind ────────────────
#
# Peer review round 11. verified_media_kind() refuses to RECORD a kind once a
# conflict appears -- but the destructive identity does not read the cache. It
# reads the already-persisted downloads.media_kind through
# get_release_identity(). So a kind written BEFORE the conflict was discovered
# stayed authoritative and Keep-best stayed available on it.
#
# Reproduced before fixing: T0 record movie -> T1 conflict appears ->
# verified_media_kind returns None (correct) -> persisted media_kind is STILL
# "movie". A later status-only write with media_kind=None preserved it too,
# because add_to_history COALESCEs.
#
# Why one value cannot carry both meanings:
#     None = "this write carries no media-kind observation, keep what you had"
#     None = "the evidence that justified the old value has been withdrawn"
# Hence a named operation that only ever erases.


class TestAConflictRetractsAPersistedKind:
    def _persist(self, db, kind):
        db.add_to_history(URL, "Some Release", None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind=kind)

    def test_the_durable_row_is_retracted(self, svc):
        _scanned_as(svc.db, "4k")
        self._persist(svc.db, svc.verified_media_kind(URL, "4k"))
        assert svc.db.get_release_identity([URL])[URL]["media_kind"] == "movie"

        n = svc.db.retract_download_media_kind([URL], reason="classification_conflict")
        assert n == 1
        assert svc.db.get_release_identity([URL])[URL]["media_kind"] is None

    def test_retraction_is_not_reachable_through_add_to_history(self, svc):
        """The reason this needed its own operation.

        add_to_history(media_kind=None) COALESCEs on purpose -- there None means
        "no observation this time". Routing a retraction through it would
        silently do nothing, which is worse than not trying.
        """
        _scanned_as(svc.db, "4k")
        self._persist(svc.db, "movie")
        self._persist(svc.db, None)          # a status-only update
        assert svc.db.get_release_identity([URL])[URL]["media_kind"] == "movie", (
            "add_to_history must still PRESERVE on a no-observation write")

        svc.db.retract_download_media_kind([URL], reason="classification_conflict")
        assert svc.db.get_release_identity([URL])[URL]["media_kind"] is None

    def test_retracting_an_unrecorded_kind_is_a_no_op(self, svc):
        _scanned_as(svc.db, "4k")
        self._persist(svc.db, None)
        assert svc.db.retract_download_media_kind([URL], reason="x") == 0

    def test_a_retracted_row_yields_no_semantic_identity(self, svc):
        """The consumer end. An unknown kind must not authorise anything."""
        _scanned_as(svc.db, "4k")
        self._persist(svc.db, "movie")
        svc.db.retract_download_media_kind([URL], reason="classification_conflict")
        ident = svc.db.get_release_identity([URL])[URL]
        assert ident["media_kind"] is None
        # title+year survive; only the KIND is withdrawn, so the row still
        # groups for display and simply cannot authorise.
        assert ident["year"] == 2026


# ── M1b: absence of a conflict flag is not proof of no conflict ───────────


class TestLegacyRowsAreNotTreatedAsAttested:
    def _legacy(self, db):
        """A row exactly as the old first-source-wins crawler wrote it."""
        db.upsert_background_cache([{
            "url": URL, "title": "Some Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k"}),
        }])

    def test_a_row_that_was_never_conflict_checked_answers_nothing(self, svc):
        """The state Round 10 identified could otherwise survive the fix: a
        release that appeared in BOTH listings before conflict detection existed
        still looks unconflicted, because the key is simply absent."""
        self._legacy(svc.db)
        assert svc.db.get_scan_category(URL) is None
        assert svc.verified_media_kind(URL, "4k") is None

    def test_observing_it_cleanly_attests_it(self, svc):
        """A conflict-aware crawl seeing the release IS the check."""
        self._legacy(svc.db)
        assert svc.db.attest_scan_categories([URL]) == 1
        assert svc.db.get_scan_category(URL) == "4k"
        assert svc.verified_media_kind(URL, "4k") == "movie"

    def test_attestation_never_overrides_a_recorded_conflict(self, svc):
        svc.db.upsert_background_cache([{
            "url": URL, "title": "x", "year": 2026, "status": "missing",
            "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k",
                                "category_conflict": True}),
        }])
        assert svc.db.attest_scan_categories([URL]) == 0
        assert svc.db.get_scan_category(URL) is None

    def test_attesting_twice_writes_once(self, svc):
        self._legacy(svc.db)
        assert svc.db.attest_scan_categories([URL]) == 1
        assert svc.db.attest_scan_categories([URL]) == 0


class TestAConflictIsSeenEvenWhenTheReleaseIsSkipped:
    """The half M1b was really about: the DEPLOYED corpus.

    The conflict check used to consult `post_index`, which is populated only for
    posts that survive the cached-skip. So an already-cached release -- every
    row on the live instance -- was added to `seen_post_urls`, skipped, and
    never indexed. A later listing that classified it differently found nothing
    to mark and discarded the conflict.

    The original conflict tests all passed `previously_scanned=set()`, so they
    exercised only the fresh-row path and could never have caught this.
    """

    def _crawl_with_skip(self, sources, scraper, monkeypatch, skipped):
        async def no_sleep(_s):
            return None
        monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)
        shell = _shell()

        async def run():
            loop = asyncio.get_running_loop()
            posts = await shell._crawl_pages(
                sources, pages=1, base_url="https://hdencode.org",
                scraper=scraper, loop=loop, previously_scanned=set(skipped),
                early_stop=False, policy_excluded=None, skip_full_disc=False)
            return posts, shell

        return asyncio.run(run())

    def test_an_already_cached_release_still_records_a_conflict(self, monkeypatch):
        """The exact case: URL is in previously_scanned, so no detail fetch is
        scheduled -- but two listings still disagree about what it is, and that
        is listing-membership evidence which needs no detail page."""
        scraper = _Scraper([_page([(SHARED, "Some Show S02 2160p")])])
        posts, shell = self._crawl_with_skip(
            [_source("4K Movies", "movie", "4k"),
             _source("TV Packs", "tv", "tv")],
            scraper, monkeypatch, skipped={SHARED})

        assert posts == [], "a cached release must still be skipped for detail"
        assert SHARED in shell._last_crawl_conflicted_urls, (
            "the conflict was observed and then discarded because the release "
            "was skipped -- which is every row on the deployed instance")

    def test_a_skipped_release_with_no_disagreement_is_not_flagged(self, monkeypatch):
        """Positive control. Without it the assertion above would pass even if
        every skipped release were marked conflicted."""
        scraper = _Scraper([_page([(SHARED, "A Film 2026 2160p")])])
        posts, shell = self._crawl_with_skip(
            [_source("4K Movies", "movie", "4k"),
             _source("Remux Movies", "movie", "remux")],
            scraper, monkeypatch, skipped={SHARED})
        assert posts == []
        assert SHARED not in shell._last_crawl_conflicted_urls

    def test_a_fresh_release_still_works(self, monkeypatch):
        """The path the original tests covered, kept so the restructure cannot
        regress it."""
        scraper = _Scraper([_page([(SHARED, "Some Show S02 2160p")])])
        posts, shell = self._crawl_with_skip(
            [_source("4K Movies", "movie", "4k"),
             _source("TV Packs", "tv", "tv")],
            scraper, monkeypatch, skipped=set())
        assert len(posts) == 1
        assert posts[0]["category_conflict"] is True
        assert SHARED in shell._last_crawl_conflicted_urls

    def test_a_fresh_post_is_NOT_marked_attested_by_the_crawler(self, monkeypatch):
        """DELIBERATELY FLIPPED in round 12 (M12-1). This test used to assert
        True, on the reasoning quoted in its old docstring: "anything this
        crawler produces HAS been conflict-checked".

        That reasoning conflates two different claims. The crawler does check
        every sighting it MAKES -- but a crawl of the 4K arm alone, as set up
        here, never fetched the TV listing, so there was no opportunity for a
        contradiction to appear. Stamping "attested" on its output turned "we
        did not look" into "we looked and it is clean", and that flag is what
        later authorises a destructive Keep-best.

        Attestation is now decided once, after the crawl, by whether its
        coverage could actually rule a contradiction out. See
        tests/test_round12_attestation_authority.py for that gate, including the
        positive controls proving a qualified crawl still attests."""
        scraper = _Scraper([_page([(SHARED, "A Film 2026 2160p")])])
        posts, _shell = self._crawl_with_skip(
            [_source("4K Movies", "movie", "4k")], scraper, monkeypatch, skipped=set())
        assert posts[0]["category_attested"] is False
