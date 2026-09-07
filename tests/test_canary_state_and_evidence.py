"""Tests for the canary scheduler's durable state table
(hdencode_canary_state) and the shadow-cycle URL-set evidence reader
(get_shadow_cycle_url_sets).

Construction style follows tests/test_canary_evidence_schema.py: the same
db_manager/tmp_db fixtures from tests/conftest.py, plus a raw connection
(via db_manager.get_connection()) to write hdencode_shadow_cycles rows
directly -- a full compare_shadow()/record_hdencode_shadow_comparison()
cycle belongs to another lane, and get_shadow_cycle_url_sets only cares
about the columns it reads, not how they got there.

See backend/database.py's list_listing_membership and sum_requests
docstrings for the tri-state read convention this file's tests exist to
pin: None means unavailable/unreadable, [] (or an empty-shaped default)
means a healthy read that found nothing. Reverting any reader added in
this lane to `_query_dicts(..., default=[])` collapses that distinction --
exactly what the "no connection" / "query failure" tests below are written
to catch.
"""

import datetime
import json
import sqlite3

import pytest


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _insert_shadow_cycle(conn, *, cycle_uuid, completed_at, details_json="{}",
                          normal_feeds_complete=1, listing_complete=1,
                          mode="rss_primary_canary", outcome="success"):
    """Write a minimally-valid hdencode_shadow_cycles row directly.

    Only the columns get_shadow_cycle_url_sets actually reads are
    parameterised; the table's other NOT NULL comparison columns
    (rss_requests, listing_count, etc.) are filled with harmless literals
    because that table's schema requires them to be present, not because
    this reader cares about their values.
    """
    conn.execute(
        "INSERT INTO hdencode_shadow_cycles ("
        "cycle_uuid, started_at, completed_at, normal_feeds_complete, "
        "rss_requests, listing_requests, rss_count, listing_count, "
        "duplicate_count, feed_only_count, listing_only_count, "
        "relevant_miss_count, request_reduction_pct, outcome, "
        "details_json, listing_complete, mode) "
        "VALUES (?, ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 0.0, ?, ?, ?, ?)",
        (cycle_uuid, completed_at, completed_at, normal_feeds_complete,
         outcome, details_json, listing_complete, mode),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Schema exists; init_db is idempotent.
# ---------------------------------------------------------------------------

class TestSchemaObjectsExist:
    def test_canary_state_table_exists(self, db_manager):
        conn = db_manager.get_connection()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "hdencode_canary_state" in tables

    def test_canary_state_columns(self, db_manager):
        conn = db_manager.get_connection()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(hdencode_canary_state)")}
        assert cols == {
            "source_key", "last_attempt_at", "next_attempt_at",
            "last_success_at", "last_outcome", "last_reason",
            "consecutive_failures", "consecutive_overlap_losses",
        }

    def test_reinit_is_harmless(self, db_manager):
        """init_db a second time must not raise and must not disturb an
        already-recorded row."""
        db_manager.record_canary_attempt(
            "movies_all", at="2026-01-01T00:00:00+00:00",
            next_attempt_at="2026-01-01T01:00:00+00:00", outcome="success")
        db_manager.init_db()
        db_manager.init_db()
        conn = db_manager.get_connection()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "hdencode_canary_state" in tables
        state = db_manager.get_canary_state("movies_all")
        assert state is not None
        assert state["last_outcome"] == "success"


# ---------------------------------------------------------------------------
# 2. Guards enforced at the SQL level, not just by the Python wrapper.
#    Each test asserts on the REJECTION -- if the corresponding constraint
#    is ever removed from the CREATE TABLE, the insert would succeed and
#    the test would fail.
# ---------------------------------------------------------------------------

class TestGuardsAreEnforcedNotJustDocumented:
    def test_bad_last_outcome_rejected_at_sql_level(self, db_manager):
        """Direct SQL insert, bypassing record_canary_attempt's ValueError
        entirely -- pins the table's own CHECK (last_outcome IN (...))
        constraint."""
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_canary_state (source_key, last_outcome) "
                "VALUES (?, ?)",
                ("bad-outcome-source", "not_a_real_outcome"),
            )
        conn.rollback()

    def test_null_last_outcome_is_allowed(self, db_manager):
        """A source with no attempt yet must be insertable with a NULL
        last_outcome -- the CHECK constrains non-null values only (SQLite
        does not evaluate CHECK (col IN (...)) against a NULL column)."""
        conn = db_manager.get_connection()
        conn.execute(
            "INSERT INTO hdencode_canary_state (source_key) VALUES (?)",
            ("never-attempted-source",),
        )
        conn.commit()
        row = conn.execute(
            "SELECT last_outcome FROM hdencode_canary_state WHERE source_key=?",
            ("never-attempted-source",),
        ).fetchone()
        assert row[0] is None

    def test_negative_consecutive_failures_rejected_at_sql_level(self, db_manager):
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_canary_state "
                "(source_key, consecutive_failures) VALUES (?, ?)",
                ("negative-failures-source", -1),
            )
        conn.rollback()

    def test_negative_consecutive_overlap_losses_rejected_at_sql_level(self, db_manager):
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_canary_state "
                "(source_key, consecutive_overlap_losses) VALUES (?, ?)",
                ("negative-overlap-source", -1),
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# 3. record_canary_attempt: last_success_at moves on success only;
#    consecutive_failures increments on non-success and resets on success.
# ---------------------------------------------------------------------------

class TestRecordCanaryAttempt:
    def test_rejects_unknown_outcome(self, db_manager):
        """As record_request_batch rejects an unknown kind."""
        with pytest.raises(ValueError):
            db_manager.record_canary_attempt(
                "movies_all", at=_iso(_now()), next_attempt_at=_iso(_now()),
                outcome="not_a_real_outcome")

    def test_success_sets_last_success_at_and_resets_failures(self, db_manager):
        t1 = _iso(_now() - datetime.timedelta(hours=2))
        t2 = _iso(_now() - datetime.timedelta(hours=1))
        db_manager.record_canary_attempt(
            "movies_all", at=t1, next_attempt_at=t2, outcome="error",
            reason="boom")
        db_manager.record_canary_attempt(
            "movies_all", at=t2, next_attempt_at=_iso(_now()),
            outcome="success")
        state = db_manager.get_canary_state("movies_all")
        assert state["last_success_at"] == t2
        assert state["consecutive_failures"] == 0
        assert state["last_outcome"] == "success"

    def test_non_success_outcome_does_not_move_last_success_at(self, db_manager):
        """Pins the freshness contract: last_success_at must move ONLY on a
        genuine success. If a failure or a retry were ever allowed to
        advance it, this test would fail."""
        t1 = _iso(_now() - datetime.timedelta(hours=3))
        t2 = _iso(_now() - datetime.timedelta(hours=2))
        t3 = _iso(_now() - datetime.timedelta(hours=1))
        db_manager.record_canary_attempt(
            "movies_all", at=t1, next_attempt_at=t2, outcome="success")
        db_manager.record_canary_attempt(
            "movies_all", at=t2, next_attempt_at=t3, outcome="incomplete")
        db_manager.record_canary_attempt(
            "movies_all", at=t3, next_attempt_at=_iso(_now()), outcome="error")
        state = db_manager.get_canary_state("movies_all")
        assert state["last_success_at"] == t1
        assert state["last_outcome"] == "error"

    def test_consecutive_failures_increment_across_non_success_outcomes(self, db_manager):
        base = _now()
        for i, outcome in enumerate(
                ["incomplete", "overlap_lost", "blocked", "error"]):
            at = _iso(base - datetime.timedelta(hours=4 - i))
            nxt = _iso(base - datetime.timedelta(hours=3 - i))
            db_manager.record_canary_attempt(
                "tv_all", at=at, next_attempt_at=nxt, outcome=outcome)
        state = db_manager.get_canary_state("tv_all")
        assert state["consecutive_failures"] == 4
        assert state["last_outcome"] == "error"

    def test_last_attempt_next_attempt_and_reason_are_stored(self, db_manager):
        at = _iso(_now())
        nxt = _iso(_now() + datetime.timedelta(hours=1))
        db_manager.record_canary_attempt(
            "movies_all", at=at, next_attempt_at=nxt, outcome="blocked",
            reason="turnstile challenge")
        state = db_manager.get_canary_state("movies_all")
        assert state["last_attempt_at"] == at
        assert state["next_attempt_at"] == nxt
        assert state["last_reason"] == "turnstile challenge"


# ---------------------------------------------------------------------------
# 4. record_overlap_loss: increments on a loss, resets to zero on a win,
#    and never touches the attempt/failure bookkeeping.
# ---------------------------------------------------------------------------

class TestRecordOverlapLoss:
    def test_overlap_loss_increments(self, db_manager):
        db_manager.record_overlap_loss("movies_all", lost=True)
        db_manager.record_overlap_loss("movies_all", lost=True)
        state = db_manager.get_canary_state("movies_all")
        assert state["consecutive_overlap_losses"] == 2

    def test_overlap_loss_resets_on_false(self, db_manager):
        db_manager.record_overlap_loss("movies_all", lost=True)
        db_manager.record_overlap_loss("movies_all", lost=True)
        db_manager.record_overlap_loss("movies_all", lost=False)
        state = db_manager.get_canary_state("movies_all")
        assert state["consecutive_overlap_losses"] == 0

    def test_overlap_loss_is_independent_of_attempt_bookkeeping(self, db_manager):
        """record_overlap_loss must never touch last_outcome or
        consecutive_failures -- those belong to record_canary_attempt."""
        db_manager.record_canary_attempt(
            "movies_all", at=_iso(_now()), next_attempt_at=_iso(_now()),
            outcome="success")
        db_manager.record_overlap_loss("movies_all", lost=True)
        state = db_manager.get_canary_state("movies_all")
        assert state["last_outcome"] == "success"
        assert state["consecutive_failures"] == 0
        assert state["consecutive_overlap_losses"] == 1


# ---------------------------------------------------------------------------
# 5. get_canary_state / list_canary_states: "no row yet" is distinguishable
#    from "cannot read". Each failure case monkeypatches get_connection() on
#    the instance (matching tests/test_canary_evidence_schema.py's pattern
#    for list_listing_membership) so this exercises the readers' own error
#    handling directly, without corrupting a real database file.
# ---------------------------------------------------------------------------

class TestCanaryStateIsTriState:
    def test_no_row_yet_returns_empty_shaped_default(self, db_manager):
        state = db_manager.get_canary_state("never-run-source")
        assert state is not None
        assert state["source_key"] == "never-run-source"
        assert state["last_outcome"] is None
        assert state["last_attempt_at"] is None
        assert state["consecutive_failures"] == 0
        assert state["consecutive_overlap_losses"] == 0

    def test_no_connection_returns_none(self, db_manager, monkeypatch):
        monkeypatch.setattr(db_manager, "get_connection", lambda: None)
        assert db_manager.get_canary_state("movies_all") is None

    def test_query_failure_returns_none(self, db_manager, monkeypatch):
        class _BoomCursor:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("simulated query failure")

        class _BoomConn:
            def cursor(self):
                return _BoomCursor()

        monkeypatch.setattr(db_manager, "get_connection", lambda: _BoomConn())
        assert db_manager.get_canary_state("movies_all") is None

    def test_healthy_populated_row_is_returned(self, db_manager):
        db_manager.record_canary_attempt(
            "movies_all", at=_iso(_now()), next_attempt_at=_iso(_now()),
            outcome="success")
        state = db_manager.get_canary_state("movies_all")
        assert state["source_key"] == "movies_all"
        assert state["last_outcome"] == "success"

    def test_list_canary_states_no_connection_returns_none(self, db_manager, monkeypatch):
        monkeypatch.setattr(db_manager, "get_connection", lambda: None)
        assert db_manager.list_canary_states() is None

    def test_list_canary_states_healthy_empty_returns_empty_list(self, db_manager):
        result = db_manager.list_canary_states()
        assert result == []
        assert result is not None

    def test_list_canary_states_returns_all_sources_ordered(self, db_manager):
        db_manager.record_canary_attempt(
            "tv_all", at=_iso(_now()), next_attempt_at=_iso(_now()),
            outcome="success")
        db_manager.record_canary_attempt(
            "movies_all", at=_iso(_now()), next_attempt_at=_iso(_now()),
            outcome="error")
        result = db_manager.list_canary_states()
        assert [r["source_key"] for r in result] == ["movies_all", "tv_all"]


# ---------------------------------------------------------------------------
# 6. get_shadow_cycle_url_sets.
# ---------------------------------------------------------------------------

class TestShadowCycleUrlSets:
    def test_returns_parsed_url_sets(self, db_manager):
        conn = db_manager.get_connection()
        details = json.dumps({
            "feed_only": ["http://feed-only"],
            "listing_only": ["http://listing-only"],
            "duplicate_urls": ["http://dup"],
        })
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-1",
            completed_at="2026-01-01T00:10:00+00:00", details_json=details,
            normal_feeds_complete=1, listing_complete=1,
            mode="rss_primary_canary")

        result = db_manager.get_shadow_cycle_url_sets()

        assert result is not None
        assert result["evidence_problems"] == []
        assert len(result["cycles"]) == 1
        cycle = result["cycles"][0]
        assert cycle["cycle_uuid"] == "cycle-1"
        assert cycle["at"] == "2026-01-01T00:10:00+00:00"
        assert cycle["feed_only"] == {"http://feed-only"}
        assert cycle["listing_only"] == {"http://listing-only"}
        assert cycle["duplicate_urls"] == {"http://dup"}
        assert cycle["normal_feeds_complete"] is True
        assert cycle["listing_complete"] is True
        assert cycle["mode"] == "rss_primary_canary"

    def test_missing_keys_default_to_empty_sets(self, db_manager):
        """A cycle recorded before this evidence existed has no
        feed_only/listing_only/duplicate_urls keys at all -- absent
        evidence must read as empty, not as a parse problem."""
        conn = db_manager.get_connection()
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-legacy",
            completed_at="2026-01-01T00:10:00+00:00", details_json="{}")
        result = db_manager.get_shadow_cycle_url_sets()
        assert result["evidence_problems"] == []
        cycle = result["cycles"][0]
        assert cycle["feed_only"] == set()
        assert cycle["listing_only"] == set()
        assert cycle["duplicate_urls"] == set()

    def test_excludes_and_reports_unparseable_row(self, db_manager):
        """A row whose details_json will not parse is an EVIDENCE PROBLEM,
        not an empty cycle: it must be excluded from `cycles` and named in
        `evidence_problems`, so a caller can fail closed rather than
        silently trusting a cycle that never actually got parsed. Removing
        the try/except around json.loads (or the `continue` after it) would
        make this test fail -- either by raising or by admitting the bad
        row into `cycles`."""
        conn = db_manager.get_connection()
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-good",
            completed_at="2026-01-01T00:10:00+00:00",
            details_json=json.dumps({"feed_only": ["http://ok"]}))
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-bad",
            completed_at="2026-01-01T00:20:00+00:00",
            details_json="{not valid json")

        result = db_manager.get_shadow_cycle_url_sets()

        assert result is not None
        cycle_uuids = {c["cycle_uuid"] for c in result["cycles"]}
        assert cycle_uuids == {"cycle-good"}
        assert any("cycle-bad" in p and "unparseable" in p
                   for p in result["evidence_problems"])

    def test_malformed_url_field_is_excluded_and_reported(self, db_manager):
        """feed_only stored as a non-list (a writer bug) must be an
        evidence problem, not silently treated as an empty or single-item
        set. Removing the isinstance guard in the local _urlset helper
        (letting `{str(v) for v in value}` run on a string) would make
        this test fail by iterating the string's characters instead of
        reporting the problem."""
        conn = db_manager.get_connection()
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-malformed",
            completed_at="2026-01-01T00:10:00+00:00",
            details_json=json.dumps({"feed_only": "not-a-list"}))
        result = db_manager.get_shadow_cycle_url_sets()
        assert result["cycles"] == []
        assert any("feed_only_not_a_list" in p and "cycle-malformed" in p
                   for p in result["evidence_problems"])

    def test_invalid_listing_complete_is_excluded_and_reported(self, db_manager):
        """listing_complete is an unconstrained INTEGER column; a value
        outside {NULL, 0, 1} is corrupt evidence, not a truthy 'complete'.
        Removing the strict `in (0, 1, True, False)` check (falling back to
        bool()) would make this test fail by admitting the cycle instead of
        reporting it."""
        conn = db_manager.get_connection()
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-badlisting",
            completed_at="2026-01-01T00:10:00+00:00", details_json="{}",
            listing_complete=2)
        result = db_manager.get_shadow_cycle_url_sets()
        assert result["cycles"] == []
        assert any("listing_complete_invalid" in p and "cycle-badlisting" in p
                   for p in result["evidence_problems"])

    def test_since_filters_out_older_cycles(self, db_manager):
        conn = db_manager.get_connection()
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-old",
            completed_at="2026-01-01T00:00:00+00:00", details_json="{}")
        _insert_shadow_cycle(
            conn, cycle_uuid="cycle-new",
            completed_at="2026-06-01T00:00:00+00:00", details_json="{}")
        result = db_manager.get_shadow_cycle_url_sets(
            since="2026-03-01T00:00:00+00:00")
        assert {c["cycle_uuid"] for c in result["cycles"]} == {"cycle-new"}

    def test_limit_returns_the_most_recent_cycle(self, db_manager):
        conn = db_manager.get_connection()
        for i, completed_at in enumerate([
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
                "2026-03-01T00:00:00+00:00"]):
            _insert_shadow_cycle(
                conn, cycle_uuid=f"cycle-{i}", completed_at=completed_at,
                details_json="{}")
        result = db_manager.get_shadow_cycle_url_sets(limit=1)
        assert len(result["cycles"]) == 1
        assert result["cycles"][0]["cycle_uuid"] == "cycle-2"

    def test_no_connection_returns_none(self, db_manager, monkeypatch):
        monkeypatch.setattr(db_manager, "get_connection", lambda: None)
        assert db_manager.get_shadow_cycle_url_sets() is None

    def test_query_failure_returns_none(self, db_manager, monkeypatch):
        class _BoomCursor:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("simulated query failure")

        class _BoomConn:
            def cursor(self):
                return _BoomCursor()

        monkeypatch.setattr(db_manager, "get_connection", lambda: _BoomConn())
        assert db_manager.get_shadow_cycle_url_sets() is None

    def test_healthy_empty_table_returns_empty_cycles_and_no_problems(self, db_manager):
        result = db_manager.get_shadow_cycle_url_sets()
        assert result == {"cycles": [], "evidence_problems": []}
