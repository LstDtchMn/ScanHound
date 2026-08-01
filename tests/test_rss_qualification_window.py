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
        """Otherwise a fresh build inherits ten days it did not earn.

        Note the two layers: the raw SUMMARY still aggregates everything when
        unscoped — that is its documented job as a query helper — but READINESS
        never surfaces those totals in fields the gate consumes."""
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        cycle(db, completed_at="2026-08-04T06:00:00+00:00")

        raw = db.get_hdencode_shadow_summary()                   # unscoped helper
        assert raw["successful_cycles"] == 3

        scoped = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert scoped["observed_days"] == pytest.approx(2.0, abs=0.1)

        unscoped = db.get_hdencode_rss_readiness()               # gate view
        assert unscoped["observed_days"] == 0.0
        assert unscoped["historical_evidence_not_counted"]["successful_cycles"] == 3

    def test_old_rows_are_retained_not_deleted(self, db):
        """The previous window stays available for forensics; it simply stops
        counting toward the current one."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary()["relevant_misses"] == 101


class TestNoWindowMustNotLeakHistoricalEvidence:
    """REGRESSION (review round 2, blocking defect).

    Returning the unscoped historical totals alongside a blocking reason was
    not merely untidy. The qualification collector reads `relevant_misses`
    independently and turns any nonzero value into a MANDATORY STOP with a
    priority-8 push alert and a "stop and roll back" instruction:

        if misses:
            stop.append(f"RELEVANT RSS MISS x{misses}")

    With 102 void misses in the live table, that alert fired from the previous
    window's evidence before the new window had started.
    """

    def test_gate_consumed_fields_are_ZERO_with_no_window(self, db):
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness()
        assert r["relevant_misses"] == 0
        assert r["successful_cycles"] == 0
        assert r["observed_days"] == 0.0
        assert r["request_reduction_pct"] == 0.0
        assert r["recovery_cycles"] == 0

    def test_the_collector_raises_NO_mandatory_stop(self, db):
        """The exact consumer logic, quoted from collect_shadow_evidence.py."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        r = db.get_hdencode_rss_readiness()
        misses = r.get("relevant_misses", 0)
        stop = [f"RELEVANT RSS MISS x{misses}"] if misses else []
        assert stop == []

    def test_the_only_reason_is_that_no_window_has_started(self, db):
        """Not 'relevant_misses_detected' — that would describe void evidence
        as a current-window finding."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        assert db.get_hdencode_rss_readiness()["reasons"] == [
            "qualification_window_not_started"]

    def test_history_is_preserved_under_an_explicitly_named_key(self, db):
        """Available for diagnosis, but in a field no gate consumes."""
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        h = db.get_hdencode_rss_readiness()["historical_evidence_not_counted"]
        assert h["relevant_misses"] == 101

    def test_a_real_miss_inside_a_STARTED_window_still_stops_the_collector(self, db):
        """The suppression must apply only to the no-window case."""
        cycle(db, completed_at=AFTER, misses=1, outcome="relevant_miss")
        r = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert r["relevant_misses"] == 1
        assert [f"RELEVANT RSS MISS x{r['relevant_misses']}"] != []


class TestWindowBoundaryNormalisation:
    """The boundary is compared as TEXT against `completed_at`, which is stored
    ISO-8601 with a +00:00 offset. Any other shape would compare against a
    different format and silently select the wrong rows."""

    def test_a_Z_suffixed_timestamp_is_normalised(self, db):
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary(
            window_start_at="2026-08-01T18:00:00Z")["successful_cycles"] == 1

    def test_a_naive_timestamp_is_treated_as_utc(self, db):
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_rss_readiness(
            window_start_at="2026-08-01T18:00:00")["successful_cycles"] == 1

    def test_a_malformed_value_fails_CLOSED(self, db):
        """Not 'ignore the filter and count everything' — that is the failure
        this whole change exists to prevent."""
        cycle(db, completed_at=OLD, misses=50, outcome="relevant_miss")
        r = db.get_hdencode_rss_readiness(window_start_at="not-a-timestamp")
        assert "qualification_window_not_started" in r["reasons"]
        assert r["relevant_misses"] == 0

    def test_a_FUTURE_boundary_fails_closed(self, db):
        """A future boundary would exclude every cycle forever, so the window
        could never accumulate evidence — indistinguishable from a stalled
        collector until someone read the timestamp."""
        r = db.get_hdencode_rss_readiness(window_start_at="2099-01-01T00:00:00+00:00")
        assert "qualification_window_not_started" in r["reasons"]

    def test_the_normalised_value_is_reported_back(self, db):
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_rss_readiness(
            window_start_at="2026-08-01T18:00:00Z")["window_start_at"] == NEW


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
        deploys. The raw history is real and preserved; neither the gate nor the
        collector may see it as current-window evidence."""
        for i in range(25):
            cycle(db, completed_at=f"2026-07-{22 + i // 6:02d}T{i % 24:02d}:00:00+00:00")
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")

        raw = db.get_hdencode_shadow_summary()
        assert raw["successful_cycles"] >= 20 and raw["relevant_misses"] == 101

        # No window: gate fields empty, history under the diagnostic key, and
        # crucially NO collector mandatory stop.
        unscoped = db.get_hdencode_rss_readiness()
        assert unscoped["successful_cycles"] == 0
        assert unscoped["relevant_misses"] == 0
        assert unscoped["reasons"] == ["qualification_window_not_started"]
        assert unscoped["historical_evidence_not_counted"]["relevant_misses"] == 101
        assert not (lambda m: [f"RELEVANT RSS MISS x{m}"] if m else [])(
            unscoped["relevant_misses"])

        # Window started: an honest empty window, earned from scratch.
        scoped = db.get_hdencode_rss_readiness(window_start_at=NEW)
        assert scoped["successful_cycles"] == 0
        assert scoped["relevant_misses"] == 0
        assert not scoped["ready"]
        assert "insufficient_comparison_cycles" in scoped["reasons"]
