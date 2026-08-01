"""The qualification window must be restartable.

Found 2026-08-01 while checking the Part 9 prerequisites: the readiness summary
aggregated EVERY shadow cycle ever recorded, with no window boundary anywhere in
the schema. The live table held 206 rows going back to 07-22, so a freshly
deployed corrected build would have reported observed_days=10.67 and
successful_cycles=148 earned by pre-fix evidence, while 101 pre-fix relevant
misses blocked the gate permanently. Wrong in both directions at once.

"Old evidence is void, no reuse" was a policy with no mechanism. These tests are
the mechanism.
"""

import datetime as dt
import uuid

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "window.db"))


def cycle(db, *, completed_at, misses=0, outcome="success", rss=2, listing=10,
          recovery=0):
    db.record_hdencode_shadow_comparison(
        cycle_uuid=str(uuid.uuid4()),
        started_at=completed_at, completed_at=completed_at,
        metrics={"normal_feeds_complete": True, "rss_requests": rss,
                 "listing_requests": listing, "rss_count": 10,
                 "listing_count": 10, "duplicate_count": 10,
                 "feed_only_count": 0, "listing_only_count": 0,
                 "relevant_miss_count": misses,
                 "request_reduction_pct": 80.0, "outcome": outcome},
        catchup_used=bool(recovery), restart_recovery=False)


OLD = "2026-07-22T00:00:00+00:00"
NEW = "2026-08-01T18:00:00+00:00"
AFTER = "2026-08-02T06:00:00+00:00"


class TestWindowScoping:
    def test_without_a_window_every_cycle_ever_is_counted(self, db):
        """The behaviour that made a fresh window impossible."""
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary()["successful_cycles"] == 2

    def test_a_window_start_excludes_earlier_cycles(self, db):
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        s = db.get_hdencode_shadow_summary(window_start_at=NEW)
        assert s["successful_cycles"] == 1
        assert s["window_start_at"] == NEW

    def test_old_relevant_misses_do_not_poison_a_new_window(self, db):
        """THE PERMANENT BLOCK. 101 pre-fix misses would otherwise keep
        relevant_misses > 0 forever, and that is a hard gate condition."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary()["relevant_misses"] == 101
        assert db.get_hdencode_shadow_summary(
            window_start_at=NEW)["relevant_misses"] == 0

    def test_a_miss_INSIDE_the_window_still_counts(self, db):
        """Scoping must not become a way to launder misses."""
        cycle(db, completed_at=AFTER, misses=1, outcome="relevant_miss")
        assert db.get_hdencode_shadow_summary(
            window_start_at=NEW)["relevant_misses"] == 1

    def test_observed_days_measures_only_the_new_window(self, db):
        """Otherwise a fresh build inherits ten days it did not earn."""
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        cycle(db, completed_at="2026-08-04T06:00:00+00:00")
        unscoped = db.get_hdencode_rss_readiness()
        scoped = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert unscoped["observed_days"] > 10          # inherited
        assert scoped["observed_days"] == pytest.approx(2.0, abs=0.1)

    def test_old_rows_are_retained_not_deleted(self, db):
        """The previous window stays available for forensics; it simply stops
        counting toward the current one."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary()["relevant_misses"] == 101


class TestReadinessFailsClosed:
    def test_no_window_started_is_a_BLOCKING_reason(self, db):
        """FAIL-CLOSED. Absent scoping is not a licence to accept unscoped
        evidence — that is exactly how a fresh build would have inherited a
        satisfied 7-day criterion."""
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness()
        assert not r["ready"]
        assert "qualification_window_not_started" in r["reasons"]

    def test_starting_a_window_removes_that_reason(self, db):
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert "qualification_window_not_started" not in r["reasons"]

    def test_a_scoped_window_still_enforces_every_other_criterion(self, db):
        """Scoping removes inherited evidence; it must not remove the bar."""
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert not r["ready"]
        assert "insufficient_comparison_cycles" in r["reasons"]
        assert "insufficient_observation_days" in r["reasons"]

    def test_the_window_start_is_reported_back(self, db):
        """So an operator can see WHICH window a verdict describes."""
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_rss_readiness(
            window_start_at=NEW)["window_start_at"] == NEW


class TestAgainstTheRealProductionShape:
    def test_the_live_situation_reproduced(self, db):
        """206 cycles from 07-22 with 101 misses, then a corrected build
        deploys. Unscoped: days and cycles look earned, misses block forever.
        Scoped: an honest empty window that must be earned from scratch."""
        for i in range(25):
            cycle(db, completed_at=f"2026-07-{22 + i // 6:02d}T{i % 24:02d}:00:00+00:00")
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")

        unscoped = db.get_hdencode_rss_readiness()
        assert unscoped["successful_cycles"] >= 20      # inherited
        assert unscoped["relevant_misses"] == 101       # blocks forever

        scoped = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert scoped["successful_cycles"] == 0
        assert scoped["relevant_misses"] == 0
        assert not scoped["ready"]
        assert "insufficient_comparison_cycles" in scoped["reasons"]
