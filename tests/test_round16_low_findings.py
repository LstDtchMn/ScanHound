"""Round 15 L15-1 and L15-2.

L15-1  posted_date_changed could only ever read zero in production. The backfill
       selected `WHERE posted_date_raw IS NULL`, so once a date was attached
       nothing compared a later one against it. My unit tests passed a date
       straight into record_listing_claims(), which the real listing crawler
       never does -- so the decisive path was never exercised. A flag that cannot
       fire is worse than no flag, because the coverage model was about to treat
       its zero as evidence of timestamp stability.

       These tests go through backfill_listing_claim_posted_dates(), the route
       production actually uses.

L15-2  the consumer reselected every contradiction each cycle and re-issued the
       revocation whether or not anything remained to do. Safe but noisy, and
       journal I/O failure trips the global interlock -- so pointless journal
       traffic is pointless risk.
"""
import io
import json
import pytest

from backend.database import DatabaseManager

URL = "https://hdencode.example/the-release-2026-2160p/"
OTHER = "https://hdencode.example/another-2026-2160p/"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r16low.db"))
    yield dm
    dm.close()


def _claim(url, ltype, category, source="hdencode"):
    return {"url": url, "source": source,
            "listing_type": ltype, "listing_category": category}


def _cache(db, url, date=None):
    payload = {"url": url, "category": "4k"}
    if date:
        payload["posted_date"] = date
    db.upsert_background_cache([{
        "url": url, "title": "The Release", "year": 2026, "status": "missing",
        "source_category": "HDEncode", "data": json.dumps(payload)}])


def _claim_row(db, url=URL):
    return db.get_listing_claims(url)[0]


class TestAChangedSiteDateIsNoticedByTheRealRoute:

    def test_a_later_different_date_raises_the_flag(self, db):
        """THE GAP. Everything here goes through the production enrichment path;
        nothing injects a date into record_listing_claims()."""
        _cache(db, URL, "June 1, 2026 at 1:00 AM")
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.backfill_listing_claim_posted_dates()
        assert _claim_row(db)["posted_date_raw"] == "June 1, 2026 at 1:00 AM"
        assert _claim_row(db)["posted_date_changed"] == 0

        # The site now reports a different publication date for the same release.
        _cache(db, URL, "June 2, 2026 at 2:00 AM")
        db.backfill_listing_claim_posted_dates()

        row = _claim_row(db)
        assert row["posted_date_changed"] == 1, (
            "the enrichment route never re-compared, so the flag could only ever "
            "read zero and its zero would have been mistaken for stability")
        assert row["posted_date_raw"] == "June 1, 2026 at 1:00 AM", (
            "the first value is kept; the point is to record that it MOVED, "
            "not to pick a winner")

    def test_an_unchanged_date_is_not_flagged(self, db):
        """POSITIVE CONTROL. Flagging every re-check would make the signal
        useless, which is the same as not having it."""
        _cache(db, URL, "June 1, 2026 at 1:00 AM")
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        for _ in range(3):
            db.backfill_listing_claim_posted_dates()
        assert _claim_row(db)["posted_date_changed"] == 0

    def test_a_claim_with_no_cached_date_is_untouched(self, db):
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.backfill_listing_claim_posted_dates()
        row = _claim_row(db)
        assert row["posted_date_raw"] is None
        assert row["posted_date_changed"] == 0

    def test_the_summary_surfaces_it(self, db):
        _cache(db, URL, "June 1, 2026 at 1:00 AM")
        db.record_listing_claims([_claim(URL, "movie", "4k")])
        db.backfill_listing_claim_posted_dates()
        _cache(db, URL, "June 9, 2026 at 9:00 PM")
        db.backfill_listing_claim_posted_dates()
        assert db.listing_claim_summary()["posted_date_changed"] == 1


def _journal_ops(db):
    """How many revocation operations the journal has recorded."""
    path = db._revocation_journal_path()
    try:
        with io.open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh
                       if line.strip() and '"kind": "PENDING"' in line)
    except OSError:
        return 0


class TestTheConsumerDoesNoWorkTwice:

    def _contradicted(self, db):
        db.add_to_history(URL, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        _cache(db, URL)
        db.record_listing_claims([_claim(URL, "movie", "4k"),
                                  _claim(URL, "tv", "tv")])

    def test_a_second_pass_does_nothing(self, db):
        self._contradicted(db)
        assert db.consume_cross_crawl_conflicts() == 1
        after_first = _journal_ops(db)

        assert db.consume_cross_crawl_conflicts() == 0, (
            "an already-consumed contradiction was processed again")
        assert _journal_ops(db) == after_first, (
            "a second pass wrote another journal operation; journal I/O failure "
            "trips the global interlock, so this is risk for no benefit")

    def test_it_still_acts_when_work_remains(self, db):
        """POSITIVE CONTROL. Skipping on the mere PRESENCE of a contradiction
        would make the consumer a no-op after the first cycle forever."""
        self._contradicted(db)
        db.consume_cross_crawl_conflicts()

        # A later grab re-records a kind under the same release.
        db.add_to_history(URL, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        assert db.consume_cross_crawl_conflicts() == 1, (
            "authority came back and the consumer ignored it")
        row = db._query("SELECT media_kind FROM downloads WHERE url = ?",
                        (URL,), one=True, default=None)
        assert dict(row).get("media_kind") is None

    def test_an_unreadable_cache_row_counts_as_outstanding(self, db):
        """Unreadable evidence is not proof the work was already done."""
        db.add_to_history(URL, "The Release", None, None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        db.upsert_background_cache([{
            "url": URL, "title": "x", "year": 2026, "status": "missing",
            "source_category": "HDEncode", "data": "{not json"}])
        db.record_listing_claims([_claim(URL, "movie", "4k"),
                                  _claim(URL, "tv", "tv")])
        assert db.consume_cross_crawl_conflicts() == 1
