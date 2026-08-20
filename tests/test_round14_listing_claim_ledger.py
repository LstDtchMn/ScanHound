"""Round 14: keep what each listing arm SAID, before the releases age off.

The reviewer's coverage model needs per-release listing claims. Until now
`url_type_claim` was a function-local dict rebuilt on every crawl: only CONFLICTS
survived, as a boolean, and the sightings themselves were discarded. Releases age
off the listings continuously, and a claim not captured cannot be reconstructed
by any later crawl of any depth -- so the recording half is built now, while the
coverage model itself is still undecided.

THE PROPERTY THAT MATTERS MOST is that the ledger is INERT. Adding evidence must
not be able to widen permission on its own; if recording a claim could make a
release answerable, this would be the attestation bug again wearing a new hat.
`test_the_ledger_authorises_nothing` is that control.
"""
import json
import pytest

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner
from tests.test_background_scanner import _FakeScanner, _FakeRegistry

URL = "https://hdencode.example/the-release-2026-2160p/"
OTHER = "https://hdencode.example/another-2026-2160p/"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r14.db"))
    yield dm
    dm.close()


def _claim(url, ltype, category, source="hdencode"):
    return {"url": url, "source": source,
            "listing_type": ltype, "listing_category": category}


class TestClaimsArePersisted:

    def test_every_arm_is_recorded_not_only_the_first(self, db):
        """The whole point. `url_type_claim` kept only the winner-so-far because
        it exists to DETECT disagreement; a coverage proof needs the sightings."""
        assert db.record_listing_claims([
            _claim(URL, "movie", "4k"),
            _claim(URL, "tv", "tv"),
        ]) == 2
        arms = {(c["listing_type"], c["listing_category"])
                for c in db.get_listing_claims(URL)}
        assert arms == {("movie", "4k"), ("tv", "tv")}

    def test_re_observation_counts_and_keeps_the_first_sighting(self, db):
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        first = db.get_listing_claims(URL)[0]["first_seen_at"]
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        row = db.get_listing_claims(URL)[0]
        assert row["sightings"] == 2
        assert row["first_seen_at"] == first, (
            "first_seen_at is when the claim was first observed; overwriting it "
            "would destroy the age information the coverage model needs")

    def test_a_claim_without_an_arm_is_not_a_claim(self, db):
        assert db.record_listing_claims([{"url": URL, "source": "hdencode"}]) == 0
        assert db.get_listing_claims(URL) == []

    def test_the_summary_counts_releases_claimed_by_both_types(self, db):
        """The population a movie-vs-TV conflict could ever be found in."""
        db.record_listing_claims([
            _claim(URL, "movie", "4k"), _claim(URL, "tv", "tv"),
            _claim(OTHER, "movie", "4k"),
        ])
        s = db.listing_claim_summary()
        assert s["claimed_by_multiple_types"] == 1


class TestTheLedgerIsInert:
    """THE SAFETY CONTROL.

    Evidence accumulating must never widen permission by itself. The attestation
    bug was exactly this shape -- an observation being read as a certification."""

    def test_the_ledger_authorises_nothing(self, db):
        db.upsert_background_cache([{
            "url": URL, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k"}),
        }])
        assert db.get_scan_category(URL) is None, "precondition: unattested"

        db.record_listing_claims([
            _claim(URL, "movie", "4k"), _claim(URL, "movie", "remux"),
        ])
        db.backfill_listing_claim_order_keys()

        assert db.get_scan_category(URL) is None, (
            "recording claims made an unattested release answerable -- the "
            "ledger has become an authority, which is the bug it exists to "
            "provide evidence against")

    def test_claims_do_not_mark_a_conflict_on_their_own(self, db):
        """Two disagreeing claims are the RAW MATERIAL of a conflict, not a
        conflict. The crawl decides that, with its own logic."""
        db.upsert_background_cache([{
            "url": URL, "title": "x", "year": 2026, "status": "missing",
            "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k",
                                "category_attested": True}),
        }])
        assert db.get_scan_category(URL) == "4k", "precondition: attested clean"
        db.record_listing_claims([
            _claim(URL, "movie", "4k"), _claim(URL, "tv", "tv"),
        ])
        assert db.get_scan_category(URL) == "4k", (
            "the ledger must not mutate classification state; only the crawl's "
            "own conflict path may do that")


class TestOrderKeyBackfill:

    def _cached_with_date(self, db, url=URL, date="June 29, 2026 at 11:38 PM"):
        db.upsert_background_cache([{
            "url": url, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": url, "category": "4k",
                                "posted_date": date}),
        }])

    def test_it_fills_the_order_key_from_the_cached_posted_date(self, db):
        """The claim is recorded at LISTING time, where no date exists: the
        selector returns anchors only. The date comes from the detail page."""
        self._cached_with_date(db)
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.get_listing_claims(URL)[0]["order_key"] is None
        assert db.backfill_listing_claim_order_keys() == 1
        assert db.get_listing_claims(URL)[0]["order_key"] == \
            "June 29, 2026 at 11:38 PM"

    def test_a_claim_with_no_cached_date_simply_stays_unenriched(self, db):
        """POSITIVE CONTROL for the failure direction: the claim must still be
        RECORDED. The claim is the perishable part; the date can arrive later."""
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.backfill_listing_claim_order_keys() == 0
        assert len(db.get_listing_claims(URL)) == 1

    def test_it_does_not_overwrite_a_key_it_already_has(self, db):
        self._cached_with_date(db, date="January 1, 2020 at 1:00 AM")
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.backfill_listing_claim_order_keys()
        self._cached_with_date(db, date="December 31, 2026 at 9:00 PM")
        db.backfill_listing_claim_order_keys()
        assert db.get_listing_claims(URL)[0]["order_key"] == \
            "January 1, 2020 at 1:00 AM"


class TestTheCrawlRecordsClaims:

    def test_a_partial_crawl_still_records_what_it_saw(self, db):
        """Unlike attestation, writing down what was OBSERVED needs no coverage
        proof -- so an early-stopped crawl must still contribute to the ledger."""
        scanner = _FakeScanner()
        scanner._last_crawl_seen_urls = {URL}
        scanner._last_crawl_conflicted_urls = set()
        scanner._last_crawl_early_stopped = True
        scanner._last_crawl_termination = "early_stopped"
        scanner._last_crawl_attests_coverage = False
        scanner._last_crawl_types_covered = set()
        scanner._last_crawl_listing_claims = [_claim(URL, "movie", "4k")]

        BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 3},
            scanner, db)).scan_once()

        assert len(db.get_listing_claims(URL)) == 1
        assert db.get_scan_category(URL) is None, (
            "and it still must not have attested anything")


class TestAnExplicitlySuppliedOrderKeyIsAlsoPreserved:
    """Found by mutation, not by design.

    Removing the COALESCE in record_listing_claims() left every test passing:
    the tests above go through backfill_listing_claim_order_keys(), whose
    `WHERE order_key IS NULL` already prevents an overwrite, so the ON CONFLICT
    clause was never reached. The line was real but unexercised."""

    def test_a_second_sighting_does_not_replace_the_recorded_key(self, db):
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       order_key="June 1, 2026 at 1:00 AM")])
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       order_key="June 2, 2026 at 2:00 AM")])
        row = db.get_listing_claims(URL)[0]
        assert row["order_key"] == "June 1, 2026 at 1:00 AM"
        assert row["sightings"] == 2

    def test_but_a_missing_key_is_still_filled_in_later(self, db):
        """The other direction: COALESCE must not freeze a NULL."""
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.get_listing_claims(URL)[0]["order_key"] is None
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       order_key="June 3, 2026 at 3:00 AM")])
        assert db.get_listing_claims(URL)[0]["order_key"] ==             "June 3, 2026 at 3:00 AM"


class TestCoverageSummaryMakesTheUnknownsAMeasuredClass:
    """The reviewer asked that permanently-unknown releases be a reported class
    rather than something that merely looks like a failed backfill.

    That distinction is not academic: going into round 12 I read 82% of the
    corpus going dark as a regression, and it was the fail-closed answer working
    correctly. A number nobody prints is a number everybody misreads."""

    def _row(self, db, url, **payload):
        data = {"url": url, "category": "4k"}
        data.update(payload)
        db.upsert_background_cache([{
            "url": url, "title": "t", "year": 2026, "status": "missing",
            "source_category": "HDEncode", "data": json.dumps(data),
        }])

    def test_each_release_lands_in_exactly_one_class(self, db):
        self._row(db, "u/conflicted", category_conflict=True)
        self._row(db, "u/attested", category_attested=True)
        self._row(db, "u/claimed")
        self._row(db, "u/unclaimed")
        db.record_listing_claims([_claim("u/claimed", "movie", "4k")])

        s = db.media_kind_coverage_summary()
        assert s["total"] == 4
        assert s["conflicted"] == 1
        assert s["attested"] == 1
        assert s["unknown_claimed"] == 1
        assert s["unknown_unclaimed"] == 1
        assert (s["conflicted"] + s["attested"] + s["unknown_claimed"]
                + s["unknown_unclaimed"] + s["unreadable"]) == s["total"], (
            "the classes must partition the corpus, or the report invites the "
            "same misreading it exists to prevent")

    def test_a_conflict_outranks_an_attestation(self, db):
        """Both keys present is not a tie: a recorded conflict wins."""
        self._row(db, "u/both", category_attested=True, category_conflict=True)
        s = db.media_kind_coverage_summary()
        assert s["conflicted"] == 1 and s["attested"] == 0

    def test_unreadable_rows_are_their_own_class(self, db):
        """Not silently folded into unknown. Unreadable evidence is a different
        problem from absent evidence and needs to be visible as one."""
        db.upsert_background_cache([{
            "url": "u/bad", "title": "t", "year": 2026, "status": "missing",
            "source_category": "HDEncode", "data": "{not json",
        }])
        s = db.media_kind_coverage_summary()
        assert s["unreadable"] == 1
        assert s["unknown_unclaimed"] == 0
