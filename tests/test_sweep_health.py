"""Interval health — staleness must survive activity.

The design's rule (rev 2.1 §6) is that a running sweep does NOT clear `overdue`.
Most of these tests exist to make that rule fail loudly if someone later
"simplifies" the state machine by letting activity win.
"""

import datetime as dt
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.sweep.health import (
    IntervalState,
    evaluate_source_health,
)
from backend.sweep.session import SweepSessionStore

NOW = dt.datetime(2026, 8, 1, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "health.db")
    DatabaseManager(path)
    c = sqlite3.connect(path)
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return SweepSessionStore(conn, owner="worker-a")


def cov(hours_old, failures=0):
    """A coverage row whose watermark is `hours_old` hours behind NOW."""
    return {"source_key": "4k_movies",
            "coverage_through": (NOW - dt.timedelta(hours=hours_old)).isoformat(),
            "consecutive_failures": failures}


def running(expires_in_minutes=20):
    return {"source_key": "4k_movies",
            "lease_expires_at": (NOW + dt.timedelta(minutes=expires_in_minutes)).isoformat()}


def stalled(expired_minutes_ago=30):
    """A non-terminal session whose lease has lapsed — abandoned, not running."""
    return {"source_key": "4k_movies",
            "lease_expires_at": (NOW - dt.timedelta(minutes=expired_minutes_ago)).isoformat()}


def ev(row, session=None):
    return evaluate_source_health(row, now=NOW, live_session=session)


class TestBoundaries:
    def test_fresh_coverage_is_current(self):
        assert ev(cov(1)).state is IntervalState.CURRENT

    def test_due_at_six_hours(self):
        assert ev(cov(5.9)).state is IntervalState.CURRENT
        assert ev(cov(6.1)).state is IntervalState.DUE

    def test_overdue_after_the_one_hour_grace(self):
        assert ev(cov(6.9)).state is IntervalState.DUE
        assert ev(cov(7.1)).state is IntervalState.OVERDUE

    def test_due_and_overdue_timestamps_are_exposed(self):
        h = ev(cov(2))
        assert h.due_at == h.coverage_through + dt.timedelta(hours=6)
        assert h.overdue_at == h.due_at + dt.timedelta(hours=1)


class TestNonSuppression:
    """§6: 'Do not suppress overdue because a sweep or continuation is running.'"""

    def test_running_does_NOT_clear_overdue(self):
        h = ev(cov(20), running())
        assert h.state is IntervalState.RUNNING_OVERDUE
        assert h.is_overdue and h.is_running

    def test_unfinished_sweep_does_NOT_clear_overdue(self):
        h = ev(cov(20), stalled())
        assert h.state is IntervalState.INCOMPLETE_OVERDUE
        assert h.is_overdue and h.is_incomplete

    def test_perpetually_restarting_source_never_reports_healthy(self):
        """The failure this rule exists to prevent: sweeps that keep starting and
        failing would otherwise show `running` forever while coverage rots."""
        h = ev(cov(72, failures=9), running())
        assert h.state is IntervalState.RUNNING_OVERDUE
        assert h.blocks_promotion
        assert h.is_degraded          # still reported, just not the headline

    def test_running_is_a_valid_state_when_NOT_stale(self):
        """The rule is about not hiding staleness, not about ignoring activity."""
        h = ev(cov(2), running())
        assert h.state is IntervalState.RUNNING

    def test_degraded_does_not_mask_overdue_either(self):
        h = ev(cov(30, failures=5))
        assert h.state is IntervalState.OVERDUE
        assert h.is_degraded

    def test_degraded_surfaces_when_coverage_is_still_good(self):
        h = ev(cov(2, failures=3))
        assert h.state is IntervalState.DEGRADED


class TestUnknown:
    def test_no_row_at_all_is_unknown(self):
        assert ev(None).state is IntervalState.UNKNOWN

    def test_null_watermark_is_unknown_not_current(self):
        """A source row created by a failed first attempt has no coverage. It must
        never default to healthy."""
        h = ev({"source_key": "x", "coverage_through": None})
        assert h.state is IntervalState.UNKNOWN
        assert h.blocks_promotion

    def test_bootstrap_in_progress_is_still_unknown(self):
        """Activity does not manufacture coverage that was never proven."""
        h = ev({"source_key": "x", "coverage_through": None}, running())
        assert h.state is IntervalState.UNKNOWN
        assert h.is_running


class TestLeaseInterpretation:
    def test_expired_lease_is_incomplete_not_running(self):
        """An abandoned session is work waiting to resume, not work happening."""
        h = ev(cov(2), stalled())
        assert h.state is IntervalState.INCOMPLETE
        assert not h.is_running

    def test_no_session_means_neither_running_nor_incomplete(self):
        h = ev(cov(2))
        assert not h.is_running and not h.is_incomplete


class TestAgainstTheRealSchema:
    """The pure function above is fed by real rows; these prove the wiring."""

    def test_source_never_swept_reports_unknown(self, store):
        assert store.health("4k_movies").state is IntervalState.UNKNOWN

    def test_live_lease_from_begin_reports_running(self, store):
        store.begin("4k_movies", now=NOW)
        assert store.health("4k_movies", now=NOW).is_running

    def test_after_success_source_is_current_then_goes_overdue(self, store):
        s = store.begin("4k_movies", now=NOW)
        store.commit_success(s, now=NOW)
        assert store.health("4k_movies", now=NOW).state is IntervalState.CURRENT
        later = NOW + dt.timedelta(hours=8)
        assert store.health("4k_movies", now=later).state is IntervalState.OVERDUE

    def test_incomplete_sweep_left_stale_reports_the_compound_state(self, store):
        """End-to-end version of the non-suppression rule: a real sweep that hit
        the page cap and was never resumed."""
        s = store.begin("4k_movies", now=NOW)
        store.commit_success(s, now=NOW)
        s2 = store.begin("4k_movies", now=NOW + dt.timedelta(hours=6))
        store.mark_incomplete(s2, reason="page cap", pages_crawled=15)
        h = store.health("4k_movies", now=NOW + dt.timedelta(hours=9))
        assert h.state is IntervalState.INCOMPLETE_OVERDUE

    def test_sources_with_no_row_still_appear_in_the_report(self, store):
        """A source missing from the table is `unknown`, never omitted — an
        omitted source is one nobody notices has stopped."""
        report = store.all_health(["4k_movies", "tv_packs"], now=NOW)
        assert set(report) == {"4k_movies", "tv_packs"}
        assert all(h.state is IntervalState.UNKNOWN for h in report.values())

    def test_cached_label_is_written_from_the_live_computation(self, store, conn):
        s = store.begin("4k_movies", now=NOW)
        store.commit_success(s, now=NOW)
        store.refresh_interval_states(["4k_movies"], now=NOW + dt.timedelta(hours=8))
        cached = conn.execute(
            "SELECT interval_state FROM hdencode_source_coverage WHERE source_key=?",
            ("4k_movies",)).fetchone()[0]
        assert cached == "overdue"

    def test_refresh_does_not_clobber_the_watermark(self, store):
        """The cache write must be an upsert of the label alone."""
        s = store.begin("4k_movies", now=NOW)
        store.commit_success(s, now=NOW)
        store.refresh_interval_states(["4k_movies"], now=NOW + dt.timedelta(hours=8))
        assert store.coverage_through("4k_movies") == NOW


class TestPromotionGate:
    """§10 requires every source interval to be `current`."""

    @pytest.mark.parametrize("state_row,session", [
        (cov(20), None),                 # overdue
        (cov(20), running()),            # running_overdue
        (cov(20), stalled()),            # incomplete_overdue
        (cov(6.5), None),                # due
        (cov(2), running()),             # running
        (cov(2), stalled()),             # incomplete
        (cov(2, failures=4), None),      # degraded
        (None, None),                    # unknown
    ])
    def test_only_current_clears_promotion(self, state_row, session):
        assert ev(state_row, session).blocks_promotion

    def test_current_clears(self):
        assert not ev(cov(1)).blocks_promotion
