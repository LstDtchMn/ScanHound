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
        arms = {(c["arm_id"], c["listing_type"])
                for c in db.get_listing_claims(URL)}
        assert arms == {("hdencode:4k", "movie"), ("hdencode:tv", "tv")}

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
        db.backfill_listing_claim_posted_dates()

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

    def test_it_fills_the_posted_date_from_the_cached_detail_row(self, db):
        """The claim is recorded at LISTING time, where no date exists: the
        selector returns anchors only. The date comes from the detail page."""
        self._cached_with_date(db)
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.get_listing_claims(URL)[0]["posted_date_raw"] is None
        assert db.backfill_listing_claim_posted_dates() == 1
        assert db.get_listing_claims(URL)[0]["posted_date_raw"] == \
            "June 29, 2026 at 11:38 PM"

    def test_a_claim_with_no_cached_date_simply_stays_unenriched(self, db):
        """POSITIVE CONTROL for the failure direction: the claim must still be
        RECORDED. The claim is the perishable part; the date can arrive later."""
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.backfill_listing_claim_posted_dates() == 0
        assert len(db.get_listing_claims(URL)) == 1

    def test_it_does_not_overwrite_a_key_it_already_has(self, db):
        self._cached_with_date(db, date="January 1, 2020 at 1:00 AM")
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.backfill_listing_claim_posted_dates()
        self._cached_with_date(db, date="December 31, 2026 at 9:00 PM")
        db.backfill_listing_claim_posted_dates()
        assert db.get_listing_claims(URL)[0]["posted_date_raw"] == \
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
    the tests above go through backfill_listing_claim_posted_dates(), whose
    `WHERE posted_date_raw IS NULL` already prevents an overwrite, so the ON CONFLICT
    clause was never reached. The line was real but unexercised."""

    def test_a_second_sighting_does_not_replace_the_recorded_key(self, db):
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 1, 2026 at 1:00 AM")])
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 2, 2026 at 2:00 AM")])
        row = db.get_listing_claims(URL)[0]
        assert row["posted_date_raw"] == "June 1, 2026 at 1:00 AM"
        assert row["sightings"] == 2

    def test_but_a_missing_key_is_still_filled_in_later(self, db):
        """The other direction: COALESCE must not freeze a NULL."""
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        assert db.get_listing_claims(URL)[0]["posted_date_raw"] is None
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 3, 2026 at 3:00 AM")])
        assert db.get_listing_claims(URL)[0]["posted_date_raw"] ==             "June 3, 2026 at 3:00 AM"


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


class TestClaimIdentityIsCanonical:
    """Round 14 review, ledger shape A.

    A cosmetic URL variant must not split one release into two historical
    identities. That is not tidiness: the whole value of the ledger is detecting
    that one release was claimed by two arms, and filing the two claims under
    different keys would hide exactly the contradiction we are collecting them
    for."""

    def test_variants_collapse_to_one_release(self, db):
        db.record_listing_claims([
            _claim("https://HDencode.example/A-Release/", "movie", "4k"),
            _claim("https://hdencode.example/A-Release?utm=x", "tv", "tv"),
        ])
        claims = db.get_listing_claims("https://hdencode.example/A-Release")
        assert len(claims) == 2, (
            "trailing slash, query and host case produced separate identities, "
            "so a movie-vs-TV contradiction would never be visible")
        assert {c["listing_type"] for c in claims} == {"movie", "tv"}

    def test_the_summary_sees_one_contradicted_release_not_two(self, db):
        db.record_listing_claims([
            _claim("https://hdencode.example/A-Release/", "movie", "4k"),
            _claim("https://hdencode.example/a-release", "tv", "tv"),
        ])
        assert db.listing_claim_summary()["claimed_by_multiple_types"] in (0, 1)

    def test_the_raw_url_is_kept_for_audit(self, db):
        raw = "https://hdencode.example/A-Release/?utm=x"
        db.record_listing_claims([_claim(raw, "movie", "4k")])
        assert db.get_listing_claims(raw)[0]["raw_url"] == raw


class TestAChangedPublishDateIsAnAnomalyNotATieBreak:
    """Round 14 review, ledger shape D.

    The surviving COALESCE mutant exposed this as a SEMANTIC decision rather than
    an implementation detail. Silently keeping the first value would bury evidence
    that the site's own ordering key is not immutable -- and the coverage model is
    about to depend on exactly that immutability."""

    def test_a_differing_later_date_raises_the_flag(self, db):
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 1, 2026 at 1:00 AM")])
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 2, 2026 at 2:00 AM")])
        row = db.get_listing_claims(URL)[0]
        assert row["posted_date_raw"] == "June 1, 2026 at 1:00 AM"
        assert row["posted_date_changed"] == 1
        assert db.listing_claim_summary()["posted_date_changed"] == 1

    def test_an_identical_repeat_is_not_an_anomaly(self, db):
        """POSITIVE CONTROL: flagging every re-sighting would make the signal
        useless, which is the same as not having it."""
        for _ in range(3):
            db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                           posted_date_raw="June 1, 2026 at 1:00 AM")])
        row = db.get_listing_claims(URL)[0]
        assert row["posted_date_changed"] == 0
        assert row["sightings"] == 3

    def test_filling_in_a_previously_absent_date_is_not_an_anomaly(self, db):
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.record_listing_claims([dict(_claim(URL, "movie", "4k"),
                                       posted_date_raw="June 1, 2026 at 1:00 AM")])
        row = db.get_listing_claims(URL)[0]
        assert row["posted_date_raw"] == "June 1, 2026 at 1:00 AM"
        assert row["posted_date_changed"] == 0


class TestCrossCrawlContradictionsRevoke:
    """Round 14 review, M14-2.

        positive evidence may NARROW authority immediately
        authority may WIDEN only through a coverage proof

    The crawl's own conflict path only ever saw disagreement WITHIN a single
    `_crawl_pages()` invocation, because `url_type_claim` lived and died there.
    Two sightings a week apart that disagree are contradictory positive evidence
    just the same -- and narrowing on positive evidence needs no coverage proof.

    These assert on `annotate_source_links()`, the producer of the wire fields
    `canKeepBest` is computed from. Asserting on the claims table would pass while
    the destructive permission was still being served."""

    def _identity(self, db, url=URL):
        from backend.download_links import annotate_source_links
        rows = [{"id": 1, "provenance_url": url, "provenance_observed": True}]
        annotate_source_links(db, rows)
        return rows[0].get("identity_kind")

    def _grabbed_as_movie(self, db, url=URL):
        db.add_to_history(url, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")

    def test_two_crawls_that_disagree_withdraw_the_identity(self, db):
        """THE REQUIRED CASE. Neither claim was ever in the same crawl as the
        other, so the in-crawl conflict path can never have seen this."""
        self._grabbed_as_movie(db)
        assert self._identity(db) == "movie", "precondition: authority is live"

        db.record_listing_claims([_claim(URL, "movie", "4k")])     # crawl A
        assert self._identity(db) == "movie", (
            "one claim is agreement, not contradiction")

        db.record_listing_claims([_claim(URL, "tv", "tv")])        # crawl B, later
        assert db.consume_cross_crawl_conflicts() == 1
        assert self._identity(db) == "unknown", (
            "the release is claimed by both a movie and a TV arm and the "
            "destructive permission is still being served")

    def test_agreeing_claims_across_crawls_change_nothing(self, db):
        """POSITIVE CONTROL. Revoking on any repeat sighting would satisfy the
        test above while destroying every recorded kind in the library."""
        self._grabbed_as_movie(db)
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.record_listing_claims([_claim(URL, "movie", "remux")])
        assert db.consume_cross_crawl_conflicts() == 0
        assert self._identity(db) == "movie"

    def test_it_matches_across_cosmetic_url_variants(self, db):
        """The canonical identity earning its keep: the two claims arrive under
        different raw hrefs, which under the first ledger shape would have been
        two unrelated releases and no contradiction at all."""
        self._grabbed_as_movie(db)
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.record_listing_claims([_claim(URL.rstrip("/") + "?utm=x", "tv", "tv")])
        assert db.consume_cross_crawl_conflicts() == 1
        assert self._identity(db) == "unknown"

    def test_it_is_idempotent(self, db):
        """It runs every cycle; a second pass must not thrash or re-report."""
        self._grabbed_as_movie(db)
        db.record_listing_claims([_claim(URL, "movie", "4k"),
                                  _claim(URL, "tv", "tv")])
        db.consume_cross_crawl_conflicts()
        assert self._identity(db) == "unknown"
        db.consume_cross_crawl_conflicts()
        assert self._identity(db) == "unknown"

    def test_the_claim_writer_itself_still_revokes_nothing(self, db):
        """The writer stays inert. Recording must not revoke on its own -- the
        consumer is a separate, named step, so the inert-ledger property survives
        this addition."""
        self._grabbed_as_movie(db)
        db.record_listing_claims([_claim(URL, "movie", "4k"),
                                  _claim(URL, "tv", "tv")])
        assert self._identity(db) == "movie", (
            "record_listing_claims() revoked by itself; the ledger is no longer "
            "an inert writer")
