"""Round 15 review: M15-2 (raw aliases) and M15-3 (safety before enrichment).

Both are fail-OPEN cases in code I wrote last round, and both are cases my own
tests could not reach:

M15-2  listing_claims is keyed (canonical_url, arm_key), so a second raw href in
       the SAME arm overwrites the first. Revocation keys on the RAW href, so the
       forgotten variant is a download row that keeps its media kind after the
       release has been contradicted. My cosmetic-URL test missed this because its
       two variants lived under DIFFERENT arms, which is the easy case.

M15-3  record / enrich / consume shared one try block, so a failure in the date
       enrichment -- optional work for a coverage model that does not exist yet --
       skipped the revocation entirely. The consumer also sat under `if _claims:`,
       so a cycle with nothing new never retried an older contradiction.
"""
import json
import pytest

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner
from backend.download_links import annotate_source_links
from tests.test_background_scanner import _FakeScanner, _FakeRegistry

CANON = "https://hdencode.example/the-release-2026-2160p"
RAW_A = CANON + "/"
RAW_B = CANON + "/?utm_source=rss"
RAW_C = CANON + "?ref=tv"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r16.db"))
    yield dm
    dm.close()


def _claim(url, ltype, category, source="hdencode"):
    return {"url": url, "source": source,
            "listing_type": ltype, "listing_category": category}


def _identity(db, url):
    rows = [{"id": 1, "provenance_url": url, "provenance_observed": True}]
    annotate_source_links(db, rows)
    return rows[0].get("identity_kind")


def _kind(db, url):
    row = db._query("SELECT media_kind FROM downloads WHERE url = ?",
                    (url,), one=True, default=None)
    return dict(row).get("media_kind") if row else None


class TestEveryRawVariantIsRevoked:
    """M15-2. The grab happened under raw B, which the claim row no longer
    remembers because raw A and raw B share one (canonical, arm) key."""

    def test_a_same_arm_alias_is_still_revoked(self, db):
        # Two sightings, SAME movie arm, different raw hrefs.
        db.record_listing_claims([_claim(RAW_A, "movie", "4k")])
        db.record_listing_claims([_claim(RAW_B, "movie", "4k")])

        # The download was grabbed under the SECOND variant.
        db.add_to_history(RAW_B, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        assert _identity(db, RAW_B) == "movie", "precondition: authority is live"

        # A later TV sighting contradicts it, under yet another variant.
        db.record_listing_claims([_claim(RAW_C, "tv", "tv")])
        assert db.consume_cross_crawl_conflicts() == 1

        assert _kind(db, RAW_B) is None, (
            "the grab under raw B survived revocation -- the ledger had "
            "forgotten that variant, so the destructive permission is still live")
        assert _identity(db, RAW_B) == "unknown"

    def test_the_alias_history_is_actually_kept(self, db):
        db.record_listing_claims([_claim(RAW_A, "movie", "4k")])
        db.record_listing_claims([_claim(RAW_B, "movie", "4k")])
        rows = db._query_dicts(
            "SELECT raw_url FROM listing_claim_aliases ORDER BY raw_url",
            (), default=[])
        assert len(rows) == 2, (
            "the second same-arm variant was not retained, so revocation "
            "cannot enumerate it")

    def test_it_still_collapses_to_one_release(self, db):
        """POSITIVE CONTROL: aliases must not fragment the canonical identity,
        or the contradiction would stop being visible at all."""
        db.record_listing_claims([_claim(RAW_A, "movie", "4k")])
        db.record_listing_claims([_claim(RAW_B, "movie", "4k")])
        claims = db.get_listing_claims(CANON)
        assert len(claims) == 1, "one arm, one claim row"
        assert claims[0]["sightings"] == 2


class TestSafetyRunsEvenWhenEnrichmentFails:
    """M15-3. Date enrichment is optional work for a model that does not exist
    yet. It must never be able to postpone a revocation."""

    def _cycle(self, db, scanner_claims, monkeypatch=None):
        scanner = _FakeScanner()
        scanner._last_crawl_seen_urls = set()
        scanner._last_crawl_conflicted_urls = set()
        scanner._last_crawl_early_stopped = True
        scanner._last_crawl_termination = "early_stopped"
        scanner._last_crawl_attests_coverage = False
        scanner._last_crawl_types_covered = set()
        scanner._last_crawl_listing_claims = scanner_claims
        BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 3},
            scanner, db)).scan_once()

    def test_a_failing_date_backfill_does_not_block_revocation(self, db, monkeypatch):
        # Run A: the movie claim, and the grab.
        self._cycle(db, [_claim(RAW_A, "movie", "4k")])
        db.add_to_history(RAW_A, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        assert _identity(db, RAW_A) == "movie"

        def boom(*a, **k):
            raise RuntimeError("injected: enrichment blew up")
        monkeypatch.setattr(db, "backfill_listing_claim_posted_dates", boom)

        # Run B: the contradicting TV claim, in a cycle where enrichment fails.
        self._cycle(db, [_claim(RAW_C, "tv", "tv")])

        assert _identity(db, RAW_A) == "unknown", (
            "the contradiction was persisted and then not consumed, because "
            "optional date enrichment failed first in the same try block")

    def test_an_older_contradiction_is_retried_on_an_empty_cycle(self, db):
        """The consumer used to sit under `if _claims:`. Durable evidence must
        not need a fresh sighting to be acted on."""
        db.record_listing_claims([_claim(RAW_A, "movie", "4k"),
                                  _claim(RAW_C, "tv", "tv")])
        db.add_to_history(RAW_A, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        assert _identity(db, RAW_A) == "movie"

        self._cycle(db, [])          # a cycle that records nothing new

        assert _identity(db, RAW_A) == "unknown", (
            "an already-durable contradiction was never consumed because this "
            "cycle happened to produce no new claims")

    def test_a_clean_cycle_revokes_nothing(self, db):
        """POSITIVE CONTROL: running the consumer unconditionally must not start
        withdrawing authority from releases that were never contradicted."""
        self._cycle(db, [_claim(RAW_A, "movie", "4k")])
        db.add_to_history(RAW_A, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        self._cycle(db, [])
        assert _identity(db, RAW_A) == "movie"
