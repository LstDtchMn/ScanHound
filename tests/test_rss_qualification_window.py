"""The qualification window: scoping, fail-closed defaults, and the boundary lock.

Found 2026-08-01 while checking the Part 9 prerequisites: the readiness summary
aggregated EVERY shadow cycle ever recorded, with no window boundary anywhere in
the schema. The live table held 206 rows going back to 07-22, so a freshly
deployed corrected build would have reported observed_days=10.67 and
successful_cycles=148 earned by pre-fix evidence, while 101 pre-fix relevant
misses blocked the gate permanently. Wrong in both directions at once.

"Old evidence is void, no reuse" was a policy with no mechanism. This is the
mechanism, in three layers:

  * SCOPING       — evidence is owned by the window it was recorded in;
  * FAIL-CLOSED   — no window, or an unusable one, blocks; it never falls back
                    to counting everything;
  * IMMUTABILITY  — once evidence accumulates the boundary locks, because
                    sliding it past an observed miss would defeat the zero-miss
                    requirement outright.

All timestamps are RELATIVE to the current time. Hardcoded dates were a latent
bug here: a future boundary is refused by design, so a literal that was past
when written silently becomes an invalid input later.
"""

import datetime as dt
import uuid

import pytest

from backend.database import DatabaseManager

_NOW = dt.datetime.now(dt.timezone.utc)


def _iso(**delta):
    return (_NOW - dt.timedelta(**delta)).isoformat()


OLD = _iso(days=10)        # the void window
NEW = _iso(hours=3)        # the fresh boundary
AFTER = _iso(hours=2)      # a cycle inside the fresh window
LATER = _iso(hours=1)      # a second cycle inside it
MOVED = _iso(minutes=30)   # a later boundary — still in the past


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


def started(db, boundary=NEW):
    """Persist a boundary — readiness requires durable state, not config."""
    return db.start_qualification_window(boundary)["window_start_at"]


class TestWindowScoping:
    def test_the_raw_summary_helper_still_aggregates_when_unscoped(self, db):
        """That is its job as a query helper. The gate view is what must not."""
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
        """THE PERMANENT BLOCK. Void misses would otherwise keep
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
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        cycle(db, completed_at=LATER)
        boundary = started(db)
        scoped = db.get_hdencode_rss_readiness(window_start_at=boundary)
        assert scoped["observed_days"] == pytest.approx(1 / 24, abs=0.01)

    def test_old_rows_are_retained_not_deleted(self, db):
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(db, completed_at=AFTER)
        assert db.get_hdencode_shadow_summary()["relevant_misses"] == 101


class TestNoWindowMustNotLeakHistoricalEvidence:
    """REGRESSION (review round 2, blocking defect).

    Returning the unscoped totals alongside a blocking reason was not untidy,
    it was dangerous. The collector reads `relevant_misses` independently and
    turns any nonzero value into a MANDATORY STOP with a priority-8 push and a
    "stop and roll back" instruction:

        if misses:
            stop.append(f"RELEVANT RSS MISS x{misses}")

    With 102 void misses live, that fired before the new window existed.
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
        misses = db.get_hdencode_rss_readiness().get("relevant_misses", 0)
        assert ([f"RELEVANT RSS MISS x{misses}"] if misses else []) == []

    def test_the_only_reason_is_that_no_window_has_started(self, db):
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        assert db.get_hdencode_rss_readiness()["reasons"] == [
            "qualification_window_not_started"]

    def test_history_is_preserved_under_an_explicitly_named_key(self, db):
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")
        h = db.get_hdencode_rss_readiness()["historical_evidence_not_counted"]
        assert h["relevant_misses"] == 101

    def test_a_real_miss_inside_a_STARTED_window_still_stops_the_collector(self, db):
        cycle(db, completed_at=AFTER, misses=1, outcome="relevant_miss")
        boundary = started(db)
        r = db.get_hdencode_rss_readiness(window_start_at=boundary)
        assert r["relevant_misses"] == 1


class TestWindowBoundaryNormalisation:
    """The boundary is compared as TEXT against `completed_at`, stored ISO with
    a +00:00 offset. Any other shape would compare against a different format
    and silently select the wrong rows."""

    def test_a_Z_suffixed_timestamp_is_normalised(self, db):
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        z = NEW.replace("+00:00", "Z")
        assert db.get_hdencode_shadow_summary(
            window_start_at=z)["successful_cycles"] == 1

    def test_a_naive_timestamp_is_treated_as_utc(self, db):
        cycle(db, completed_at=OLD)
        cycle(db, completed_at=AFTER)
        naive = NEW.replace("+00:00", "")
        assert db.get_hdencode_shadow_summary(
            window_start_at=naive)["successful_cycles"] == 1

    def test_a_malformed_value_fails_CLOSED(self, db):
        """Not 'ignore the filter and count everything' — that is the failure
        this whole change exists to prevent."""
        cycle(db, completed_at=OLD, misses=50, outcome="relevant_miss")
        r = db.get_hdencode_rss_readiness(window_start_at="not-a-timestamp")
        assert "qualification_window_not_started" in r["reasons"]
        assert r["relevant_misses"] == 0

    def test_a_FUTURE_boundary_fails_closed(self, db):
        """A future boundary excludes every cycle forever, so the window could
        never accumulate evidence — indistinguishable from a stalled collector
        until someone read the timestamp."""
        future = (_NOW + dt.timedelta(days=365)).isoformat()
        assert "qualification_window_not_started" in db.get_hdencode_rss_readiness(
            window_start_at=future)["reasons"]

    def test_the_normalised_value_is_reported_back(self, db):
        cycle(db, completed_at=AFTER)
        boundary = started(db)
        assert db.get_hdencode_rss_readiness(
            window_start_at=NEW.replace("+00:00", "Z"))["window_start_at"] == boundary


class TestBoundaryIsLockedOnceEvidenceExists:
    """Review round 3: the boundary is safety state, not configuration.

    Sliding it forward past an observed miss defeats the zero-miss requirement
    outright; sliding it backward imports evidence from an earlier build. A
    runbook rule cannot prevent either.
    """

    def test_a_boundary_can_be_corrected_before_any_evidence(self, db):
        """Fixing a typo during setup is not rewriting history."""
        db.start_qualification_window(NEW)
        assert db.start_qualification_window(MOVED)["window_start_at"] == MOVED

    def test_it_LOCKS_once_a_cycle_has_accumulated(self, db):
        db.start_qualification_window(NEW)
        cycle(db, completed_at=AFTER)
        with pytest.raises(ValueError) as e:
            db.start_qualification_window(MOVED)
        assert "LOCKED" in str(e.value)

    def test_the_lock_names_how_much_evidence_would_be_rewritten(self, db):
        db.start_qualification_window(NEW)
        cycle(db, completed_at=AFTER)
        cycle(db, completed_at=LATER)
        with pytest.raises(ValueError) as e:
            db.start_qualification_window(MOVED)
        assert "2 cycle(s)" in str(e.value)

    def test_moving_the_boundary_past_a_MISS_is_what_this_prevents(self, db):
        """The concrete attack: a relevant miss is a mandatory stop, so sliding
        the boundary past it turns a failed window into a passing one."""
        boundary = started(db)
        cycle(db, completed_at=AFTER, misses=1, outcome="relevant_miss")
        assert db.get_hdencode_rss_readiness(
            window_start_at=boundary)["relevant_misses"] == 1
        with pytest.raises(ValueError):
            db.start_qualification_window(MOVED)

    def test_an_explicit_new_window_is_allowed_and_records_the_previous(self, db):
        """Superseding is legitimate; silently overwriting is not."""
        db.start_qualification_window(NEW)
        cycle(db, completed_at=AFTER)
        row = db.start_qualification_window(MOVED, supersede=True)
        assert row["window_start_at"] == MOVED
        assert row["previous_window_start_at"] == NEW

    def test_restating_the_same_boundary_is_a_no_op(self, db):
        db.start_qualification_window(NEW)
        cycle(db, completed_at=AFTER)
        assert db.start_qualification_window(NEW)["window_start_at"] == NEW

    def test_an_unusable_boundary_is_refused_outright(self, db):
        with pytest.raises(ValueError):
            db.start_qualification_window("not-a-timestamp")
        with pytest.raises(ValueError):
            db.start_qualification_window((_NOW + dt.timedelta(days=1)).isoformat())


class TestConfigurationCannotOverrideThePersistedBoundary:
    def test_a_mismatch_BLOCKS_rather_than_silently_rescoping(self, db):
        boundary = started(db)
        cycle(db, completed_at=AFTER, misses=1, outcome="relevant_miss")
        r = db.get_hdencode_rss_readiness(window_start_at=MOVED)
        assert not r["ready"]
        assert r["reasons"] == ["qualification_window_boundary_changed"]
        assert r["window_start_at"] == boundary      # persisted value wins

    def test_clearing_the_configuration_is_also_a_mismatch(self, db):
        """Not 'fall back to the persisted value' — an operator who blanked it
        has changed something, and that must surface."""
        started(db)
        assert db.get_hdencode_rss_readiness(window_start_at=None)["reasons"] == [
            "qualification_window_boundary_changed"]

    def test_matching_configuration_proceeds_normally(self, db):
        boundary = started(db)
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness(window_start_at=boundary)
        assert "qualification_window_boundary_changed" not in r["reasons"]
        assert r["successful_cycles"] == 1

    def test_an_equivalent_but_differently_formatted_value_matches(self, db):
        """Normalisation runs before comparison, so '...Z' is not a mismatch."""
        started(db)
        r = db.get_hdencode_rss_readiness(window_start_at=NEW.replace("+00:00", "Z"))
        assert "qualification_window_boundary_changed" not in r["reasons"]


class TestReadinessFailsClosed:
    def test_no_window_started_is_a_BLOCKING_reason(self, db):
        cycle(db, completed_at=AFTER)
        r = db.get_hdencode_rss_readiness()
        assert not r["ready"]
        assert "qualification_window_not_started" in r["reasons"]

    def test_starting_a_window_removes_that_reason(self, db):
        cycle(db, completed_at=AFTER)
        boundary = started(db)
        assert "qualification_window_not_started" not in db.get_hdencode_rss_readiness(
            window_start_at=boundary)["reasons"]

    def test_a_scoped_window_still_enforces_every_other_criterion(self, db):
        """Scoping removes inherited evidence; it must not remove the bar."""
        cycle(db, completed_at=AFTER)
        boundary = started(db)
        r = db.get_hdencode_rss_readiness(window_start_at=boundary)
        assert not r["ready"]
        assert "insufficient_comparison_cycles" in r["reasons"]
        assert "insufficient_observation_days" in r["reasons"]

    def test_the_window_start_is_reported_back(self, db):
        """So an operator can see WHICH window a verdict describes."""
        cycle(db, completed_at=AFTER)
        boundary = started(db)
        assert db.get_hdencode_rss_readiness(
            window_start_at=boundary)["window_start_at"] == boundary


class TestAgainstTheRealProductionShape:
    def test_the_live_situation_reproduced(self, db):
        """206 cycles with 101 misses, then a corrected build deploys. The raw
        history is real and preserved; neither the gate nor the collector may
        see it as current-window evidence."""
        for i in range(25):
            cycle(db, completed_at=_iso(days=9, hours=i))
        cycle(db, completed_at=OLD, misses=101, outcome="relevant_miss")

        raw = db.get_hdencode_shadow_summary()
        assert raw["successful_cycles"] >= 20 and raw["relevant_misses"] == 101

        unscoped = db.get_hdencode_rss_readiness()
        assert unscoped["successful_cycles"] == 0
        assert unscoped["relevant_misses"] == 0
        assert unscoped["reasons"] == ["qualification_window_not_started"]
        assert unscoped["historical_evidence_not_counted"]["relevant_misses"] == 101

        boundary = started(db)
        scoped = db.get_hdencode_rss_readiness(window_start_at=boundary)
        assert scoped["successful_cycles"] == 0
        assert scoped["relevant_misses"] == 0
        assert not scoped["ready"]
        assert "insufficient_comparison_cycles" in scoped["reasons"]
