"""Schema + behavior tests for the coverage-canary evidence layer:

  * hdencode_listing_membership -- raw per-source listing sightings
  * hdencode_shadow_cycles.mode -- 'rss_shadow' | 'rss_primary_canary' only
  * hdencode_request_ledger -- request-cost accounting, kept OUT of
    hdencode_shadow_cycles on purpose (see database.py's mode-migration
    comment for the three reasons: NOT NULL comparison columns, an
    unfiltered latest-row read, and a details_json scan that treats every
    row as an observation cycle).

Uses the same fixtures/construction style as tests/test_database.py (the
`db_manager`/`tmp_db` fixtures from tests/conftest.py, plus a raw
sqlite3.connect for the pre-migration test) -- tests/test_hde4_reveal_accounting.py,
named in this lane's spec as the pattern to follow, does not exist in this
worktree (this branch is based on #108, before the PR that added it), so
test_database.py is the closest existing database test file and is used
here instead. See this file's companion report for the full note.
"""

import datetime
import sqlite3

import pytest

from backend.database import DatabaseManager


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# 1. Schema objects exist; init_db is idempotent.
# ---------------------------------------------------------------------------

class TestSchemaObjectsExist:
    def test_listing_membership_table_and_index_exist(self, db_manager):
        conn = db_manager.get_connection()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "hdencode_listing_membership" in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_hdencode_listing_membership_lookup" in indexes

    def test_request_ledger_table_exists(self, db_manager):
        conn = db_manager.get_connection()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "hdencode_request_ledger" in tables

    def test_shadow_cycles_has_mode_column(self, db_manager):
        conn = db_manager.get_connection()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(hdencode_shadow_cycles)")}
        assert "mode" in cols

    def test_reinit_is_harmless(self, db_manager):
        """init_db a second time must not raise and must not disturb the
        three new schema objects."""
        db_manager.init_db()
        db_manager.init_db()
        conn = db_manager.get_connection()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "hdencode_listing_membership" in tables
        assert "hdencode_request_ledger" in tables
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(hdencode_shadow_cycles)")}
        assert "mode" in cols


# ---------------------------------------------------------------------------
# 2. mode default + migration of a pre-existing database.
# ---------------------------------------------------------------------------

# Verbatim copy of the CREATE TABLE currently in database.py's init_db
# (minus the `mode` column, which is added only via the additive ALTER --
# it was never part of the base CREATE). Used to seed a database that
# predates this lane's change, the same way test_database.py's
# TestDownloadResultsSchemaMigration seeds a pre-migration download_results
# table with a raw sqlite3.connect before handing the file to
# DatabaseManager.
_PRE_CANARY_SHADOW_CYCLES_DDL = """
    CREATE TABLE hdencode_shadow_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_uuid TEXT NOT NULL UNIQUE,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        normal_feeds_complete INTEGER NOT NULL,
        rss_requests INTEGER NOT NULL,
        listing_requests INTEGER NOT NULL,
        rss_count INTEGER NOT NULL,
        listing_count INTEGER NOT NULL,
        duplicate_count INTEGER NOT NULL,
        feed_only_count INTEGER NOT NULL,
        listing_only_count INTEGER NOT NULL,
        relevant_miss_count INTEGER NOT NULL,
        request_reduction_pct REAL NOT NULL,
        catchup_used INTEGER NOT NULL DEFAULT 0,
        restart_recovery INTEGER NOT NULL DEFAULT 0,
        outcome TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        normal_feed_outcomes TEXT,
        listing_complete INTEGER
    )
"""

_PRE_CANARY_ROW = (
    "pre-existing-cycle", "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00",
    1, 10, 20, 5, 6, 1, 0, 1, 0, 50.0, 0, 0, "success", "{}",
)


class TestModeMigration:
    def test_mode_defaults_for_row_written_without_it(self, db_manager):
        conn = db_manager.get_connection()
        conn.execute(
            "INSERT INTO hdencode_shadow_cycles ("
            "cycle_uuid, started_at, completed_at, normal_feeds_complete, "
            "rss_requests, listing_requests, rss_count, listing_count, "
            "duplicate_count, feed_only_count, listing_only_count, "
            "relevant_miss_count, request_reduction_pct, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("no-mode-cycle", "2026-02-01T00:00:00+00:00",
             "2026-02-01T00:10:00+00:00", 1, 1, 1, 1, 1, 0, 0, 0, 0, 0.0,
             "success"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT mode FROM hdencode_shadow_cycles WHERE cycle_uuid=?",
            ("no-mode-cycle",),
        ).fetchone()
        assert row[0] == "rss_shadow"

    def test_existing_database_migrates_without_losing_rows(self, tmp_path):
        db_path = str(tmp_path / "pre_canary.db")
        seed = sqlite3.connect(db_path)
        seed.execute(_PRE_CANARY_SHADOW_CYCLES_DDL)
        seed.execute(
            "INSERT INTO hdencode_shadow_cycles ("
            "cycle_uuid, started_at, completed_at, normal_feeds_complete, "
            "rss_requests, listing_requests, rss_count, listing_count, "
            "duplicate_count, feed_only_count, listing_only_count, "
            "relevant_miss_count, request_reduction_pct, catchup_used, "
            "restart_recovery, outcome, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _PRE_CANARY_ROW,
        )
        seed.commit()
        seed.close()

        db = DatabaseManager(db_path=db_path)
        try:
            row = db._query(
                "SELECT cycle_uuid, mode, rss_requests FROM "
                "hdencode_shadow_cycles WHERE cycle_uuid=?",
                ("pre-existing-cycle",), one=True,
            )
            assert row is not None, "pre-existing row was lost on migration"
            assert row["cycle_uuid"] == "pre-existing-cycle"
            assert row["rss_requests"] == 10
            assert row["mode"] == "rss_shadow"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 3. Nearest-page semantics.
# ---------------------------------------------------------------------------

class TestNearestPageSemantics:
    def test_nearest_page_sequence(self, db_manager):
        cycle, source, url = "cycle-seq", "movies", "http://example/seq-title"

        def write(page_index, rank_on_page, rss_present=0):
            db_manager.record_listing_membership(cycle, source, [{
                "canonical_url": url,
                "page_index": page_index,
                "rank_on_page": rank_on_page,
                "rss_present": rss_present,
            }])

        def stored():
            rows = db_manager.list_listing_membership(cycle_uuid=cycle)
            assert len(rows) == 1
            return rows[0]

        # Initial sighting: page 2, rank 5.
        write(2, 5)
        row = stored()
        assert (row["page_index"], row["rank_on_page"]) == (2, 5)

        # Closer sighting (page 1, rank 9) must win.
        write(1, 9, rss_present=1)
        row = stored()
        assert (row["page_index"], row["rank_on_page"]) == (1, 9)
        assert row["rss_present"] == 1

        # A farther sighting (page 3, rank 0) must NOT overwrite the closer one.
        write(3, 0, rss_present=0)
        row = stored()
        assert (row["page_index"], row["rank_on_page"]) == (1, 9)
        assert row["rss_present"] == 1

        # Same page, worse (larger) rank must not move the stored rank.
        write(1, 20, rss_present=0)
        row = stored()
        assert (row["page_index"], row["rank_on_page"]) == (1, 9)
        assert row["rss_present"] == 1

    def test_upsert_is_not_read_then_write(self, db_manager):
        """A single call carrying both a near and a far sighting for the
        same key in one batch must still resolve to the nearest -- this
        exercises the SQL-level conditional upsert directly, independent of
        Python call ordering."""
        db_manager.record_listing_membership("cycle-batch", "movies", [
            {"canonical_url": "http://batch", "page_index": 5,
             "rank_on_page": 5, "rss_present": 0},
        ])
        db_manager.record_listing_membership("cycle-batch", "movies", [
            {"canonical_url": "http://batch", "page_index": 1,
             "rank_on_page": 0, "rss_present": 1},
        ])
        rows = db_manager.list_listing_membership(cycle_uuid="cycle-batch")
        assert len(rows) == 1
        assert (rows[0]["page_index"], rows[0]["rank_on_page"]) == (1, 0)


# ---------------------------------------------------------------------------
# 4. Same URL under two source_keys.
# ---------------------------------------------------------------------------

class TestSourceKeyIsPartOfIdentity:
    def test_same_url_two_sources_produces_two_rows(self, db_manager):
        db_manager.record_listing_membership("cycle-multi", "movies", [
            {"canonical_url": "http://shared", "page_index": 1,
             "rank_on_page": 0, "rss_present": 0},
        ])
        db_manager.record_listing_membership("cycle-multi", "tv", [
            {"canonical_url": "http://shared", "page_index": 1,
             "rank_on_page": 0, "rss_present": 1},
        ])
        rows = db_manager.list_listing_membership(cycle_uuid="cycle-multi")
        assert len(rows) == 2
        by_source = {r["source_key"]: r for r in rows}
        assert set(by_source) == {"movies", "tv"}
        assert by_source["movies"]["rss_present"] == 0
        assert by_source["tv"]["rss_present"] == 1


# ---------------------------------------------------------------------------
# 5. purge_listing_membership.
# ---------------------------------------------------------------------------

class TestPurgeListingMembership:
    def test_purge_deletes_only_rows_older_than_cutoff(self, db_manager):
        old_at = _iso(_now() - datetime.timedelta(days=40))
        new_at = _iso(_now() - datetime.timedelta(days=1))
        db_manager.record_listing_membership("cycle-purge", "movies", [
            {"canonical_url": "http://old", "page_index": 1,
             "rank_on_page": 0, "rss_present": 0, "observed_at": old_at},
        ])
        db_manager.record_listing_membership("cycle-purge", "movies", [
            {"canonical_url": "http://new", "page_index": 1,
             "rank_on_page": 0, "rss_present": 0, "observed_at": new_at},
        ])

        deleted = db_manager.purge_listing_membership(30)

        assert deleted == 1
        remaining = db_manager.list_listing_membership(cycle_uuid="cycle-purge")
        assert len(remaining) == 1
        assert remaining[0]["canonical_url"] == "http://new"

    def test_purge_returns_zero_when_nothing_is_old_enough(self, db_manager):
        recent = _iso(_now() - datetime.timedelta(hours=1))
        db_manager.record_listing_membership("cycle-purge2", "movies", [
            {"canonical_url": "http://recent", "page_index": 1,
             "rank_on_page": 0, "rss_present": 0, "observed_at": recent},
        ])
        assert db_manager.purge_listing_membership(30) == 0


# ---------------------------------------------------------------------------
# 5b. R3: list_listing_membership is a tri-state read, not the
#     _query_dicts(default=[]) two-state collapse used elsewhere in this
#     file. "Cannot read" (no connection, a query error, or a
#     row-conversion error) must come back as None, DISTINCT from a
#     healthy empty table ([]) -- conflating the two is fail-open for
#     canary evidence, whose approved design is that unevaluable evidence
#     SUSPENDS rss_primary. Each failure case below monkeypatches
#     get_connection() on the instance (matching the pattern used in
#     tests/test_pipeline_service.py) so this exercises
#     list_listing_membership's own error handling directly, without
#     needing to actually corrupt a database file.
#
#     Reverting the reader to `_query_dicts(..., default=[])` makes the
#     three None-returning tests below fail (they would get [] instead).
# ---------------------------------------------------------------------------

class TestListingMembershipIsTriState:
    def test_no_connection_returns_none(self, db_manager, monkeypatch):
        monkeypatch.setattr(db_manager, "get_connection", lambda: None)
        assert db_manager.list_listing_membership(cycle_uuid="cycle-x") is None

    def test_query_failure_returns_none(self, db_manager, monkeypatch):
        class _BoomCursor:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("simulated query failure")

        class _BoomConn:
            def cursor(self):
                return _BoomCursor()

        monkeypatch.setattr(db_manager, "get_connection", lambda: _BoomConn())
        assert db_manager.list_listing_membership(cycle_uuid="cycle-x") is None

    def test_row_conversion_failure_returns_none(self, db_manager, monkeypatch):
        class _BadRowCursor:
            def execute(self, *a, **kw):
                pass

            def fetchall(self):
                # Plain objects have no keys()/__iter__ of key-value pairs,
                # so dict(row) raises TypeError for each -- simulates a
                # driver/row-factory mismatch that makes reachable rows
                # unconvertible, distinct from a query error.
                return [object()]

        class _FakeConn:
            def cursor(self):
                return _BadRowCursor()

        monkeypatch.setattr(db_manager, "get_connection", lambda: _FakeConn())
        assert db_manager.list_listing_membership(cycle_uuid="cycle-x") is None

    def test_healthy_empty_table_returns_empty_list(self, db_manager):
        result = db_manager.list_listing_membership(cycle_uuid="cycle-never-written")
        assert result == []
        assert result is not None

    def test_healthy_populated_table_returns_rows(self, db_manager):
        db_manager.record_listing_membership("cycle-tristate", "movies", [
            {"canonical_url": "http://tristate", "page_index": 1,
             "rank_on_page": 0, "rss_present": 0},
        ])
        rows = db_manager.list_listing_membership(cycle_uuid="cycle-tristate")
        assert rows is not None
        assert len(rows) == 1
        assert rows[0]["canonical_url"] == "http://tristate"


# ---------------------------------------------------------------------------
# 6. record_request_batch + sum_requests.
# ---------------------------------------------------------------------------

class TestRequestLedger:
    def test_record_request_batch_rejects_unknown_kind(self, db_manager):
        with pytest.raises(ValueError):
            db_manager.record_request_batch("rss_shadow", "not_a_real_kind", 1)

    def test_sum_requests_totals_per_kind_within_window(self, db_manager):
        base = _now()
        in_window = _iso(base - datetime.timedelta(hours=1))
        also_in_window = _iso(base - datetime.timedelta(minutes=30))
        before_window = _iso(base - datetime.timedelta(days=2))
        after_window = _iso(base + datetime.timedelta(days=2))

        db_manager.record_request_batch(
            "rss_shadow", "rss_poll", 3, at=in_window)
        db_manager.record_request_batch(
            "rss_primary", "canary", 2, source_key="movies_all",
            at=also_in_window)
        db_manager.record_request_batch(
            "rss_primary", "canary_retry", 1, source_key="movies_all",
            at=also_in_window)
        # Outside the window on both ends -- must not be counted.
        db_manager.record_request_batch(
            "rss_shadow", "rss_poll", 100, at=before_window)
        db_manager.record_request_batch(
            "rss_primary", "fallback", 100, at=after_window)

        since = _iso(base - datetime.timedelta(hours=2))
        until = _iso(base)
        totals = db_manager.sum_requests(since, until)

        assert totals is not None
        assert totals["rss_poll"] == 3
        assert totals["canary"] == 2
        assert totals["canary_retry"] == 1
        assert totals["fallback"] == 0
        assert totals["total"] == 6

    def test_kinds_are_recorded_exclusively_not_doubly(self, db_manager):
        """A retry is recorded ONCE, as canary_retry, never also as canary --
        this is a caller discipline the ledger can't enforce structurally
        (both are valid independent kinds), so this pins the accounting
        contract: two events in, two rows out, each counted under its own
        kind only."""
        db_manager.record_request_batch("rss_primary", "canary", 1,
                                         source_key="movies_all")
        db_manager.record_request_batch("rss_primary", "canary_retry", 1,
                                         source_key="movies_all")
        since = _iso(_now() - datetime.timedelta(minutes=5))
        totals = db_manager.sum_requests(since)
        assert totals["canary"] == 1
        assert totals["canary_retry"] == 1
        assert totals["total"] == 2


# ---------------------------------------------------------------------------
# 7. Guards shown to fail: removing either constraint flips these tests to
#    passing-when-it-shouldn't, which is why they assert on the REJECTION,
#    not on the absence of a crash.
# ---------------------------------------------------------------------------

class TestGuardsAreEnforcedNotJustDocumented:
    def test_mode_column_rejects_a_third_value(self, db_manager):
        """If the CHECK constraint on `mode` is ever removed, this INSERT
        would succeed and this test would fail."""
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_shadow_cycles ("
                "cycle_uuid, started_at, completed_at, normal_feeds_complete, "
                "rss_requests, listing_requests, rss_count, listing_count, "
                "duplicate_count, feed_only_count, listing_only_count, "
                "relevant_miss_count, request_reduction_pct, outcome, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad-mode-cycle", "2026-03-01T00:00:00+00:00",
                 "2026-03-01T00:10:00+00:00", 1, 1, 1, 1, 1, 0, 0, 0, 0, 0.0,
                 "success", "poll_only"),
            )
        conn.rollback()

    def test_a_bare_ledger_style_row_cannot_enter_shadow_cycles(self, db_manager):
        """A request-cost event (mode/kind/source_key/requests/at) has none of
        hdencode_shadow_cycles's eleven required comparison columns. If those
        NOT NULL constraints were ever relaxed -- the exact change this
        table split was designed to prevent -- this minimal insert would
        succeed and this test would fail."""
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_shadow_cycles (cycle_uuid, mode) "
                "VALUES (?, ?)",
                ("ledger-shaped-row", "rss_shadow"),
            )
        conn.rollback()

    def test_request_ledger_rejects_unknown_kind_at_sql_level(self, db_manager):
        """Direct SQL insert, bypassing record_request_batch's ValueError
        guard entirely -- pins the table's own CHECK (kind IN (...))
        constraint. record_request_batch() rejecting an unknown kind is not
        enough on its own: any other writer of this table (or a future
        change to that method) could still insert one, and sum_requests()
        would fold such a row into its per-kind GROUP BY result while
        leaving it out of `total` (only _REQUEST_LEDGER_KINDS is summed),
        letting malformed evidence vanish from the safety total. If the
        CHECK constraint is ever removed from the CREATE TABLE, this insert
        would succeed and this test would fail."""
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_request_ledger "
                "(at, mode, kind, source_key, requests) VALUES (?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00+00:00", "rss_shadow", "not_a_real_kind",
                 None, 1),
            )
        conn.rollback()

    def test_request_ledger_rejects_negative_requests_at_sql_level(self, db_manager):
        """Same direct-SQL approach for the `requests >= 0` CHECK, which
        guards the same safety total against a negative row silently
        reducing it. hdencode_request_ledger is a NEW table added in this
        lane -- no release has ever written to it -- so both this CHECK and
        the kind CHECK above are part of the ORIGINAL CREATE TABLE, not an
        ALTER onto rows that might already violate them; no migration of
        existing rows is involved. If this CHECK is ever removed, this
        insert would succeed and this test would fail."""
        conn = db_manager.get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO hdencode_request_ledger "
                "(at, mode, kind, source_key, requests) VALUES (?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00+00:00", "rss_shadow", "rss_poll",
                 None, -1),
            )
        conn.rollback()
