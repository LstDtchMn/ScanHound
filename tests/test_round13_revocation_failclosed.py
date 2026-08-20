"""Round 13, M13-1: a pending revocation must withhold authority AT THE CONSUMER.

Round 12 made conflict-recording and media-kind retraction atomic. The reviewer's
round-13 finding is that this fixed consistency, not safety: when the transaction
could not commit, `downloads.media_kind` still held "movie", `get_release_identity()`
still returned it, and the destructive Keep-best it authorises stayed on offer --
while the system had already observed the evidence invalidating it.

My own round-12 regression asserted that stale value survived. It proved the fact
was retained for a retry; it did not prove authority was withheld. Those are
different claims and only the second is fail-closed.

So these tests assert on `get_release_identity()` -- the read path the destructive
identity actually consumes -- and never on the hold set itself. A test that checked
the set would pass while the consumer still handed out the permission.
"""
import json
import pytest

from backend.database import DatabaseManager

URL = "https://hdencode.example/the-release-2026-2160p/"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r13.db"))
    yield dm
    dm.close()


def _seed_kind(db, url=URL, kind="movie"):
    db.add_to_history(url, "The Release", None, "2160p", "20 GB",
                      hdr="HDR", dovi=False, year=2026, media_kind=kind)


def _raw_kind(db, url=URL):
    """The column itself, bypassing the hold -- so a test can distinguish
    'the row was erased' from 'the row still says movie but is masked'."""
    row = db._query("SELECT media_kind FROM downloads WHERE url = ?",
                    (url,), one=True, default=None)
    return dict(row).get("media_kind") if row else None


def _served_kind(db, url=URL):
    """What the destructive identity path actually receives."""
    return (db.get_release_identity([url]) or {}).get(url, {}).get("media_kind")


class TestAFailedRevocationWithholdsAuthorityAnyway:

    def test_the_consumer_is_masked_even_though_the_row_still_says_movie(
            self, db, monkeypatch):
        """THE FINDING. The erase cannot commit, so the durable row is unchanged --
        and the permission must disappear regardless."""
        _seed_kind(db)
        assert _served_kind(db) == "movie", "seed did not take"

        def boom(*a, **k):
            raise RuntimeError("injected: database is locked")
        monkeypatch.setattr(db, "transaction", boom)

        with pytest.raises(Exception):
            db.record_classification_conflicts_and_retract_kinds(
                [URL], reason="classification_conflict")

        monkeypatch.undo()
        # The durable row is untouched -- which is precisely why the mask matters.
        assert _raw_kind(db) == "movie"
        # But nothing is served. This is the assertion round 12 was missing.
        assert _served_kind(db) is None

    def test_the_hold_is_taken_before_the_write_is_attempted(self, db, monkeypatch):
        """Ordering, not just outcome: if the hold were taken after a successful
        write, a failure would leave the permission live."""
        _seed_kind(db)
        seen = {}

        real = db.transaction

        def watching(*a, **k):
            seen["masked_at_write_time"] = _served_kind(db) is None
            return real(*a, **k)
        monkeypatch.setattr(db, "transaction", watching)

        db.record_classification_conflicts_and_retract_kinds(
            [URL], reason="classification_conflict")
        assert seen.get("masked_at_write_time") is True

    def test_a_successful_revocation_releases_the_hold(self, db):
        """Otherwise every conflict would permanently poison the process, and the
        durable erase would be doing nothing."""
        _seed_kind(db)
        db.record_classification_conflicts_and_retract_kinds(
            [URL], reason="classification_conflict")
        assert _raw_kind(db) is None          # durably erased
        assert db.is_media_kind_held(URL) is False
        assert _served_kind(db) is None

    def test_an_unrelated_release_is_not_masked(self, db):
        """Positive control. A hold that masked everything would satisfy every
        assertion above while destroying the feature."""
        other = "https://hdencode.example/another-release/"
        _seed_kind(db)
        _seed_kind(db, url=other)
        db.hold_media_kind([URL], reason="test")
        assert _served_kind(db) is None
        assert _served_kind(db, other) == "movie"


class TestStartupReconciliationFinishesAnInterruptedRevocation:
    """The restart gap. The hold is in-process by design -- a marker in the same
    SQLite file cannot protect the case where writing to that file is what failed.
    What survives is the conflict mark, written on the failure path too."""

    def _interrupted(self, db):
        """The exact state a crashed revocation leaves: cache says conflicted,
        downloads still carries the kind."""
        _seed_kind(db)
        db.upsert_background_cache([{
            "url": URL, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k",
                                "category_conflict": True}),
        }])

    def test_it_withdraws_authority_left_live_by_a_previous_process(self, db):
        self._interrupted(db)
        assert _served_kind(db) == "movie", "precondition: the stale permission is live"
        assert db.reconcile_unrevoked_conflicts() == 1
        assert _raw_kind(db) is None
        assert _served_kind(db) is None

    def test_a_clean_row_is_left_alone(self, db):
        """Positive control: reconciliation that erased everything would pass the
        test above and silently destroy every recorded kind on every restart."""
        _seed_kind(db)
        db.upsert_background_cache([{
            "url": URL, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k"}),
        }])
        assert db.reconcile_unrevoked_conflicts() == 0
        assert _raw_kind(db) == "movie"
        assert _served_kind(db) == "movie"

    def test_an_undecodable_cache_row_fails_closed(self, db):
        """Unreadable evidence is not absent evidence, and the row carries a live
        permission right now."""
        _seed_kind(db)
        db.upsert_background_cache([{
            "url": URL, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": "{not json",
        }])
        assert db.reconcile_unrevoked_conflicts() == 1
        assert _served_kind(db) is None
