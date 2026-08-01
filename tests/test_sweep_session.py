"""Sweep session lifecycle — leases, continuation and atomic commit.

Each test targets one invariant that an earlier design version violated.
"""

import datetime as dt
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.sweep.session import SweepLeaseError, SweepSessionStore

SOURCE = "4k_movies"
T0 = dt.datetime(2026, 8, 1, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "sweep.db")
    DatabaseManager(path)          # builds v9 schema
    c = sqlite3.connect(path)
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return SweepSessionStore(conn, owner="worker-a")


class TestBootstrap:
    def test_first_ever_sweep_targets_30_hours_back(self, store):
        """24 h RED band + 6 h overlap, derived rather than chosen."""
        s = store.begin(SOURCE, now=T0)
        assert s.stop_target == T0 - dt.timedelta(hours=30)
        assert s.attempt_count == 1

    def test_bootstrap_not_marked_complete_until_success(self, store):
        store.begin(SOURCE, now=T0)
        assert store.bootstrap_complete(SOURCE) is False


class TestCoverageAdvance:
    def test_success_commits_coverage_to_SESSION_START(self, store):
        """Not to the newest item seen, and not to completion time — to S."""
        s = store.begin(SOURCE, now=T0)
        store.commit_success(s, now=T0 + dt.timedelta(minutes=20))
        assert store.coverage_through(SOURCE) == T0
        assert store.bootstrap_complete(SOURCE) is True

    def test_second_sweep_targets_prior_coverage_minus_overlap(self, store):
        s1 = store.begin(SOURCE, now=T0)
        store.commit_success(s1, now=T0)
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(hours=6), overlap_hours=6.0)
        assert s2.stop_target == T0 - dt.timedelta(hours=6)

    def test_incomplete_does_NOT_advance_coverage(self, store):
        """The whole point: partial work never claims coverage."""
        s1 = store.begin(SOURCE, now=T0)
        store.commit_success(s1, now=T0)
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(hours=6))
        store.mark_incomplete(s2, reason="page cap", pages_crawled=15)
        assert store.coverage_through(SOURCE) == T0     # unchanged


class TestContinuation:
    def test_resume_preserves_original_session_start(self, store):
        """THE INVARIANT. A restart must continue the same logical session, or a
        long crawl would commit a later boundary than it actually proved."""
        s1 = store.begin(SOURCE, now=T0)
        store.mark_incomplete(s1, reason="cap",
                              frontier_at=T0 - dt.timedelta(hours=10),
                              anchor_url="https://hdencode.org/x", pages_crawled=15)
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(hours=1))
        assert s2.uuid == s1.uuid
        assert s2.started_at == T0                  # NOT T0+1h
        assert s2.attempt_count == 2
        assert s2.is_continuation

    def test_continuation_carries_timestamp_and_anchor_not_page_number(self, store):
        """Pages shift as posts publish, so a page number is not a valid anchor."""
        s1 = store.begin(SOURCE, now=T0)
        store.mark_incomplete(s1, reason="cap",
                              frontier_at=T0 - dt.timedelta(hours=10),
                              anchor_url="https://hdencode.org/anchor", pages_crawled=15)
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(hours=1))
        assert s2.continuation_frontier_at == T0 - dt.timedelta(hours=10)
        assert s2.continuation_anchor_url == "https://hdencode.org/anchor"

    def test_continued_session_commits_the_ORIGINAL_start(self, store):
        """A backlog cleared over three attempts still only claims coverage
        through S, never through the final attempt's clock."""
        s1 = store.begin(SOURCE, now=T0)
        store.mark_incomplete(s1, reason="cap")
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(hours=2))
        store.commit_success(s2, now=T0 + dt.timedelta(hours=3))
        assert store.coverage_through(SOURCE) == T0


class TestLease:
    def test_live_lease_blocks_a_second_worker(self, conn):
        a = SweepSessionStore(conn, owner="worker-a")
        b = SweepSessionStore(conn, owner="worker-b")
        a.begin(SOURCE, now=T0)
        with pytest.raises(SweepLeaseError):
            b.begin(SOURCE, now=T0 + dt.timedelta(minutes=5))

    def test_expired_lease_may_be_taken_over(self, conn):
        a = SweepSessionStore(conn, owner="worker-a")
        b = SweepSessionStore(conn, owner="worker-b")
        s1 = a.begin(SOURCE, now=T0, lease_seconds=60)
        s2 = b.begin(SOURCE, now=T0 + dt.timedelta(hours=1))
        assert s2.uuid == s1.uuid          # takeover, not a new session
        assert s2.started_at == T0

    def test_same_owner_resuming_is_not_blocked(self, store):
        s1 = store.begin(SOURCE, now=T0)
        s2 = store.begin(SOURCE, now=T0 + dt.timedelta(minutes=1))
        assert s2.uuid == s1.uuid

    def test_schema_forbids_two_active_sessions_for_one_source(self, conn):
        """Enforced by a partial unique index, not by application convention."""
        conn.execute("INSERT INTO hdencode_sweep_sessions "
                     "(sweep_session_uuid, source_key, started_at, overlap_hours) "
                     "VALUES ('a', ?, 't', 6)", (SOURCE,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO hdencode_sweep_sessions "
                         "(sweep_session_uuid, source_key, started_at, overlap_hours) "
                         "VALUES ('b', ?, 't', 6)", (SOURCE,))


class TestLedger:
    def _posts(self, *urls, page=1):
        return [{"canonical_url": u, "raw_url": u + "/", "title": "T",
                 "page_index": page} for u in urls]

    def test_new_identities_counted_once(self, store):
        s = store.begin(SOURCE, now=T0)
        assert store.record_observations(s, self._posts("a", "b", "c")) == 3

    def test_replay_is_idempotent(self, store):
        """Re-running a page after a restart must not inflate the new count —
        that count is a completion condition, so inflating it would keep a
        finished sweep running forever."""
        s = store.begin(SOURCE, now=T0)
        store.record_observations(s, self._posts("a", "b"))
        assert store.record_observations(s, self._posts("a", "b")) == 0

    def test_ledger_is_per_source(self, conn):
        """A URL known on one source proves nothing about traversal on another."""
        st = SweepSessionStore(conn, owner="w")
        s1 = st.begin("4k_movies", now=T0)
        st.record_observations(s1, self._posts("shared"))
        s2 = st.begin("tv_packs", now=T0)
        assert st.record_observations(s2, self._posts("shared")) == 1
        assert "shared" in st.known_urls("4k_movies")
        assert "shared" in st.known_urls("tv_packs")
