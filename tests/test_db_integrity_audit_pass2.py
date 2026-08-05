"""Regression tests for the 2026-08-04 audit pass-2 db-integrity findings.

Covers, in order:

* #8   backend/config.py  — the one-time DB relocation must never adopt a
       partially-checkpointed (stale/empty) copy while reporting success.
* #10  backend/database.py — corruption quarantine must move the -wal/-shm
       sidecars WITH the backup, or the "backup" is missing every
       uncheckpointed transaction and the stranded WAL is destroyed.
* #30  _dismissed_urls_set() must not cache a failed read as an empty set.
* #28/#32  the plex_cache full_replace prune must report what it deleted.
* #31  reset_applying_rename_jobs() must return jobs actually recovered.

Every finding gets a POSITIVE CONTROL alongside the failure case: a fix that
made migration always fail, or quarantine always delete, would otherwise pass
a failure-only test. Nothing here mocks SQLite — the WAL cases build real
databases with real uncheckpointed WAL content on tmp_path.
"""
import logging
import os
import shutil
import sqlite3
import threading

import pytest

import backend.config as cfg
from backend.database import DatabaseManager


# ── shared SQLite helpers ────────────────────────────────────────────────

def _open_wal(path):
    """A writer connection whose commits STAY in the -wal until told otherwise."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _attach(path):
    """A second connection that has actually touched the wal-index.

    Holding one open is what stops the writer's close() from being the LAST
    close, which is what SQLite's implicit checkpoint-and-delete-sidecars
    hangs off. fetchall() (not fetchone) so the statement is reset and no read
    snapshot is left pinned.
    """
    conn = sqlite3.connect(path)
    conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    return conn


def _pin_stale_snapshot(path):
    """A reader pinned at the CURRENT state, inside an open transaction.

    Rows committed after this returns are invisible to it, and SQLite may not
    fold those frames during a checkpoint — the exact condition that produced
    the reported (1, 54, 4) partial checkpoint.
    """
    conn = sqlite3.connect(path)
    conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    return conn


def _rows(path, sql="SELECT COUNT(*) FROM downloads"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ── #8  config.py migration ──────────────────────────────────────────────

class TestMigrationCheckpointAndVerify:
    """backend/config.py:325 — PRAGMA wal_checkpoint(TRUNCATE)'s result was
    discarded and the copy was never verified."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        # SQLite burns the FULL busy timeout on every attempt whenever a reader
        # still holds the WAL, so the production defaults would make the
        # blocked cases take ~6s each.
        monkeypatch.setattr(cfg, "_MIGRATION_CHECKPOINT_BUSY_MS", 50)
        monkeypatch.setattr(cfg, "_MIGRATION_CHECKPOINT_ATTEMPTS", 2)

    def _seed(self, path, n=501):
        w = _open_wal(path)
        w.execute("CREATE TABLE downloads (url TEXT PRIMARY KEY)")
        w.executemany("INSERT INTO downloads VALUES (?)",
                      [(f"http://x/{i}",) for i in range(n)])
        return w

    def test_healthy_migration_still_copies_and_returns_true(self, tmp_path):
        """POSITIVE CONTROL: nothing contended, migration must still happen."""
        legacy = str(tmp_path / "legacy.db")
        new = str(tmp_path / "vol" / "crawler.db")
        os.makedirs(os.path.dirname(new))
        self._seed(legacy).close()

        assert cfg._checkpoint_and_copy(legacy, new) is True
        assert _rows(new) == 501
        # The verification read must not strand sidecars at the destination.
        assert not os.path.exists(new + "-wal")
        assert not os.path.exists(new + "-shm")
        assert not os.path.exists(new + ".migrating")

    def test_uncheckpointed_wal_rows_survive_the_migration(self, tmp_path):
        """POSITIVE CONTROL on the WAL itself: rows that exist ONLY in the
        -wal must be present in the migrated copy."""
        legacy = str(tmp_path / "legacy.db")
        new = str(tmp_path / "vol" / "crawler.db")
        os.makedirs(os.path.dirname(new))

        keeper = _attach_after_seed = None
        w = self._seed(legacy)
        keeper = _attach(legacy)      # keeps w.close() from checkpointing
        w.close()
        try:
            assert os.path.getsize(legacy + "-wal") > 0, "setup: WAL must be hot"

            # Counterfactual: the main file ALONE (what the pre-fix code could
            # end up copying) does not hold these rows.
            bare = str(tmp_path / "bare.db")
            shutil.copy2(legacy, bare)
            with pytest.raises(sqlite3.DatabaseError):
                _rows(bare)

            assert cfg._checkpoint_and_copy(legacy, new) is True
            assert _rows(new) == 501
        finally:
            keeper.close()

    def test_partial_checkpoint_raises_instead_of_copying(self, tmp_path):
        """The reported defect: a stale reader means only some frames fold, so
        the main file is stale — migration must refuse, not report success."""
        legacy = str(tmp_path / "legacy.db")
        new = str(tmp_path / "vol" / "crawler.db")
        os.makedirs(os.path.dirname(new))

        w = _open_wal(legacy)
        w.execute("CREATE TABLE downloads (url TEXT PRIMARY KEY)")
        w.execute("INSERT INTO downloads VALUES ('http://x/before')")
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # 1 row is in the main file
        stale = _pin_stale_snapshot(legacy)
        try:
            w.executemany("INSERT INTO downloads VALUES (?)",
                          [(f"http://x/{i}",) for i in range(500)])
            with pytest.raises(RuntimeError, match="incomplete WAL checkpoint"):
                cfg._checkpoint_and_copy(legacy, new)
            assert not os.path.exists(new), "no stale DB may be left at the destination"
        finally:
            stale.close()
            w.close()
        # The legacy DB still has everything — that is why falling back is safe.
        assert _rows(legacy) == 501

    def test_busy_but_fully_checkpointed_still_migrates(self, tmp_path):
        """DISAGREEING CASE. A reader holding a CURRENT snapshot makes the
        pragma report busy=1 with every frame folded — measured (1, 53, 53).
        An implementation that keyed off `busy` would refuse to migrate
        whenever anything else has the DB open (the desktop UI, a script),
        i.e. approximately always. Only `checkpointed != log` means loss.
        """
        legacy = str(tmp_path / "legacy.db")
        new = str(tmp_path / "vol" / "crawler.db")
        os.makedirs(os.path.dirname(new))

        w = self._seed(legacy)
        current = _pin_stale_snapshot(legacy)   # pinned AFTER the writes
        try:
            assert cfg._checkpoint_and_copy(legacy, new) is True
            assert _rows(new) == 501
        finally:
            current.close()
            w.close()

    # REMOVED: test_stale_copy_is_rejected_even_when_the_checkpoint_looks_clean
    #
    # It injected staleness by patching shutil.copy2, a seam the migration no
    # longer uses -- D-1 replaced the file copy with sqlite3's online backup API
    # over a pinned read transaction. The patch therefore stopped firing and the
    # test failed with "DID NOT RAISE".
    #
    # Its property is covered better, and by the real mechanism rather than a
    # simulated one, in tests/test_db_relocation_snapshot_consistency.py:
    #   test_update_after_checkpoint_is_not_certified_stale
    #   test_paired_delete_insert_after_checkpoint_is_not_certified_stale
    #   test_mismatched_destination_bytes_are_still_rejected

    def test_resolve_db_path_falls_back_to_legacy_on_partial_checkpoint(
            self, tmp_path, monkeypatch):
        """CONSUMER-level check: the caller must keep using the legacy DB, not
        adopt an empty one (adopting it is permanent — the exists() guard in
        _resolve_db_path never retries)."""
        data_dir = tmp_path / "appdata"
        db_dir = tmp_path / "vol"
        data_dir.mkdir()
        db_dir.mkdir()
        legacy = str(data_dir / "crawler.db")

        w = _open_wal(legacy)
        w.execute("CREATE TABLE downloads (url TEXT PRIMARY KEY)")
        w.execute("INSERT INTO downloads VALUES ('http://x/before')")
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stale = _pin_stale_snapshot(legacy)
        try:
            w.executemany("INSERT INTO downloads VALUES (?)",
                          [(f"http://x/{i}",) for i in range(500)])
            monkeypatch.setattr(cfg, "_DATA_DIR", str(data_dir))
            monkeypatch.setattr(cfg, "_DB_DIR", str(db_dir))

            assert cfg._resolve_db_path("crawler.db") == legacy
            assert not os.path.exists(str(db_dir / "crawler.db"))
        finally:
            stale.close()
            w.close()
        assert _rows(legacy) == 501

    def test_resolve_db_path_still_migrates_a_healthy_db(self, tmp_path, monkeypatch):
        """POSITIVE CONTROL at the consumer: the happy path must still relocate."""
        data_dir = tmp_path / "appdata"
        db_dir = tmp_path / "vol"
        data_dir.mkdir()
        db_dir.mkdir()
        legacy = str(data_dir / "crawler.db")
        self._seed(legacy).close()

        monkeypatch.setattr(cfg, "_DATA_DIR", str(data_dir))
        monkeypatch.setattr(cfg, "_DB_DIR", str(db_dir))

        resolved = cfg._resolve_db_path("crawler.db")
        assert resolved == str(db_dir / "crawler.db")
        assert _rows(resolved) == 501


# ── #10  quarantine sidecars ─────────────────────────────────────────────

def _corrupt_once(monkeypatch):
    """Make the NEXT PRAGMA integrity_check report corruption, exactly once.

    Once only, because the rebuild's own init_db() would otherwise quarantine
    the fresh DB too and (same-second timestamp) rename it over the backup this
    test is about. The DB file itself stays physically healthy so the backup is
    genuinely recoverable — which is the property under test.
    """
    state = {"fired": False}

    class _Cursor:
        def __init__(self, real):
            self._real = real
            self._faking = False

        def execute(self, sql, *a, **kw):
            if "integrity_check" in sql and not state["fired"]:
                state["fired"] = True
                self._faking = True
                return self
            self._faking = False
            return self._real.execute(sql, *a, **kw)

        def fetchone(self):
            if self._faking:
                return ("database disk image is malformed",)
            return self._real.fetchone()

        def __getattr__(self, item):
            return getattr(self._real, item)

    class _Conn:
        def __init__(self, real):
            self._real = real

        def cursor(self, *a, **kw):
            return _Cursor(self._real.cursor(*a, **kw))

        def __getattr__(self, item):
            return getattr(self._real, item)

    real_get_connection = DatabaseManager.get_connection

    def _wrapped(self):
        conn = real_get_connection(self)
        if conn is not None and not isinstance(conn, _Conn):
            self.conn = _Conn(conn)
            return self.conn
        return conn

    monkeypatch.setattr(DatabaseManager, "get_connection", _wrapped)
    return state


class TestQuarantinePreservesSidecars:
    """backend/database.py:1246 — os.rename moved only the main DB file."""

    def _backup_of(self, db_path):
        d = os.path.dirname(db_path)
        base = os.path.basename(db_path)
        found = [f for f in os.listdir(d)
                 if f.startswith(base + ".corrupt.") and not f.endswith(("-wal", "-shm"))]
        assert len(found) == 1, f"expected exactly one backup, got {found}"
        return os.path.join(d, found[0])

    def test_backup_keeps_the_uncheckpointed_wal(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "crawler.db")

        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.execute("INSERT INTO audit_marker VALUES (0)")
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # row 0 reaches the main file
        stale = _pin_stale_snapshot(db_path)           # blocks folding of what follows
        try:
            w.executemany("INSERT INTO audit_marker VALUES (?)",
                          [(i,) for i in range(1, 301)])
            w.close()   # `stale` is still open, so no close-time checkpoint
            assert os.path.getsize(db_path + "-wal") > 0, "setup: WAL must be hot"

            _corrupt_once(monkeypatch)
            dm = DatabaseManager(db_path=db_path)
            try:
                backup = self._backup_of(db_path)

                # 1. The fresh DB did not ADOPT the old WAL.
                #
                # Deliberately not `assert not os.path.exists(db_path + "-wal")`:
                # by this point the rebuilt database has already opened in WAL
                # mode and created its OWN sidecar under that name, so that
                # assertion fails on correct behaviour. What must not happen is
                # the OLD wal being left for the new database to consume --
                # which would reset it, destroying the 300 rows and leaving the
                # operator pointed at a backup missing them.
                #
                # The observable difference is the data: an adopted WAL brings
                # audit_marker with it, a fresh one cannot.
                assert _rows(db_path,
                             "SELECT COUNT(*) FROM sqlite_master "
                             "WHERE type = 'table' AND name = 'audit_marker'"
                             ) == 0, (
                    "the rebuilt database adopted the corrupt DB's WAL")

                # 2. The WAL travelled with the backup.
                assert os.path.exists(backup + "-wal")

                # 3. The assertion that a "just delete the sidecars" fix cannot
                #    pass: all 301 rows are still recoverable from the backup,
                #    and 300 of them exist only in that -wal.
                assert _rows(backup, "SELECT COUNT(*) FROM audit_marker") == 301

                # POSITIVE CONTROL: the rebuilt DB at db_path is fresh and usable.
                assert dm.add_dismissed_item("http://x/a", "A") is True
                assert dm.get_dismissed_urls() == {"http://x/a"}
            finally:
                dm.close()
        finally:
            stale.close()

    def test_quarantine_logs_the_preserved_sidecars(self, tmp_path, monkeypatch, caplog):
        db_path = str(tmp_path / "crawler.db")
        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.execute("INSERT INTO audit_marker VALUES (0)")
        keeper = _attach(db_path)
        w.close()
        try:
            _corrupt_once(monkeypatch)
            with caplog.at_level(logging.WARNING, logger="backend.database"):
                dm = DatabaseManager(db_path=db_path)
            try:
                renamed = [r.getMessage() for r in caplog.records
                           if "Renamed corrupt DB" in r.getMessage()]
                assert renamed, "quarantine must still say where the backup went"
                assert "sidecar" in renamed[0], renamed[0]
            finally:
                dm.close()
        finally:
            keeper.close()

    def test_healthy_db_is_never_quarantined_and_keeps_its_sidecars(self, tmp_path):
        """POSITIVE CONTROL: normal open/close must not move or delete anything."""
        db_path = str(tmp_path / "crawler.db")
        dm = DatabaseManager(db_path=db_path)
        try:
            dm.add_dismissed_item("http://x/a", "A")
        finally:
            dm.close()

        dm2 = DatabaseManager(db_path=db_path)
        try:
            assert not [f for f in os.listdir(str(tmp_path)) if ".corrupt." in f]
            assert not os.path.exists(f"{db_path}.corrupt_flag.json")
            assert dm2.get_dismissed_urls() == {"http://x/a"}
        finally:
            dm2.close()

    def test_notify_corruption_probe_is_gone(self):
        """#29: the in-init alert looked up a module attribute that does not
        exist, so it was unreachable. It is removed, not repointed — the
        NotificationBridge is built strictly after init_db() runs."""
        assert not hasattr(DatabaseManager, "_notify_corruption")
        import backend.app_service as app_service
        assert not hasattr(app_service, "notification_bridge"), (
            "if this name ever appears, the deleted probe could have worked "
            "and this decision needs revisiting")


# ── #30  dismissed-URL cache ─────────────────────────────────────────────

class _CountingConn:
    """Passes everything through, counts (and optionally fails) the
    dismissed_items SELECT."""

    def __init__(self, real, state):
        self._real = real
        self._state = state

    def cursor(self, *a, **kw):
        return _CountingCursor(self._real.cursor(*a, **kw), self._state)

    def __getattr__(self, item):
        return getattr(self._real, item)


class _CountingCursor:
    def __init__(self, real, state):
        self._real = real
        self._state = state

    def execute(self, sql, *a, **kw):
        if "FROM dismissed_items" in sql:
            self._state["reads"] += 1
            if self._state["fail"]:
                raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, item):
        return getattr(self._real, item)


class TestDismissedCacheDoesNotCacheFailures:
    """backend/database.py:3417 — one transient read blanked the set forever."""

    def _instrument(self, dm):
        state = {"fail": False, "reads": 0}
        dm.conn = _CountingConn(dm.get_connection(), state)
        return state

    def test_transient_read_failure_is_not_cached(self, tmp_db):
        dm = DatabaseManager(db_path=tmp_db)
        try:
            dm.add_dismissed_items([("http://x/a", "A"), ("http://x/b", "B")])
            dm._dismissed_cache = None          # as if this were a fresh process
            state = self._instrument(dm)

            state["fail"] = True
            assert dm.get_dismissed_urls() == set()
            # The failure must leave the cache unset so the next call retries.
            assert dm._dismissed_cache is None

            state["fail"] = False
            assert dm.get_dismissed_urls() == {"http://x/a", "http://x/b"}
        finally:
            dm.conn = getattr(dm.conn, "_real", dm.conn)
            dm.close()

    def test_genuinely_empty_table_is_still_cached(self, tmp_db):
        """POSITIVE CONTROL + DISAGREEING CASE. An implementation that fixed
        the bug by never caching would pass the test above; this pins that an
        empty table is loaded ONCE and served from memory afterwards.
        fetchall() returns [] (never None) for an empty table, which is what
        makes the two distinguishable at all."""
        dm = DatabaseManager(db_path=tmp_db)
        try:
            state = self._instrument(dm)
            assert dm.get_dismissed_urls() == set()
            assert dm._dismissed_cache == set()
            assert dm._dismissed_cache is not None
            reads_after_first = state["reads"]
            assert reads_after_first == 1

            assert dm.get_dismissed_urls() == set()
            assert state["reads"] == reads_after_first, "second call must hit the cache"
        finally:
            dm.conn = getattr(dm.conn, "_real", dm.conn)
            dm.close()

    def test_healthy_load_and_mutators_still_work(self, tmp_db):
        """POSITIVE CONTROL: the normal path is unchanged."""
        dm = DatabaseManager(db_path=tmp_db)
        try:
            dm.add_dismissed_items([("http://x/a", "A"), ("http://x/b", "B")])
            dm._dismissed_cache = None
            assert dm.get_dismissed_urls() == {"http://x/a", "http://x/b"}
            dm.remove_dismissed_item("http://x/a")
            assert dm.get_dismissed_urls() == {"http://x/b"}
            dm.clear_dismissed_items()
            assert dm.get_dismissed_urls() == set()
        finally:
            dm.close()


# ── #28 / #32  plex_cache prune counter ──────────────────────────────────

def _movie(i):
    return {"clean_title": f"m{i}", "original_title": f"M{i}", "year": 2000,
            "res": "1080p", "size": 1.0, "imdb_id": f"tt{i}",
            "rating_key": str(i), "media_id": f"mid{i}",
            "dovi": False, "hdr": False}


def _pruned_counts(caplog):
    return [int(r.getMessage().split()[1]) for r in caplog.records
            if r.getMessage().startswith("Pruned ")]


class TestPlexCachePruneCount:
    """backend/database.py:2616 — cursor.rowcount was read after the loop."""

    def test_multi_batch_prune_reports_every_deleted_row(self, db_manager, caplog):
        """DISAGREEING CASE: 1200 stale keys span three DELETE batches
        (500/500/200). Under the old code the counter reported the LAST batch
        only, so a <=500-row test would pass either way."""
        db_manager.save_plex_cache([_movie(i) for i in range(1201)], "Movies")
        with caplog.at_level(logging.INFO, logger="backend.database"):
            db_manager.save_plex_cache([_movie(0)], "Movies", full_replace=True)

        assert len(db_manager.load_plex_cache("Movies")) == 1   # really deleted
        assert _pruned_counts(caplog) == [1200]

    def test_nothing_stale_logs_no_prune_line(self, db_manager, caplog):
        """The other half: with an empty stale set the DELETE never ran, so
        rowcount still held the preceding SELECT's -1 — truthy — and every
        healthy full refresh logged 'Pruned -1 stale rows'."""
        items = [_movie(i) for i in range(3)]
        db_manager.save_plex_cache(items, "Movies", full_replace=True)
        with caplog.at_level(logging.INFO, logger="backend.database"):
            db_manager.save_plex_cache(items, "Movies", full_replace=True)

        assert _pruned_counts(caplog) == []
        assert len(db_manager.load_plex_cache("Movies")) == 3

    def test_small_prune_is_still_reported(self, db_manager, caplog):
        """POSITIVE CONTROL: suppressing the -1 must not suppress real prunes."""
        db_manager.save_plex_cache([_movie(i) for i in range(3)], "Movies")
        with caplog.at_level(logging.INFO, logger="backend.database"):
            db_manager.save_plex_cache([_movie(0)], "Movies", full_replace=True)

        assert _pruned_counts(caplog) == [2]
        assert len(db_manager.load_plex_cache("Movies")) == 1


# ── #31  reset_applying_rename_jobs ──────────────────────────────────────

class _UpdateFailsConn:
    def __init__(self, real, sql_prefix):
        self._real = real
        self._sql_prefix = sql_prefix

    def execute(self, sql, *a, **kw):
        if sql.strip().upper().startswith(self._sql_prefix):
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *a, **kw)

    def cursor(self, *a, **kw):
        return self._real.cursor(*a, **kw)

    def __getattr__(self, item):
        return getattr(self._real, item)


class TestResetApplyingRenameJobs:
    """backend/database.py:4749 — the pre-UPDATE COUNT was returned regardless
    of whether the UPDATE landed, and api/main.py logs it as "Recovered N"."""

    def test_recovered_count_is_zero_when_the_update_fails(self, tmp_db, caplog):
        dm = DatabaseManager(db_path=tmp_db)
        try:
            ids = [dm.create_rename_job({"original_path": f"/x/{c}.mkv",
                                         "status": "applying"})
                   for c in "abc"]
            dm.conn = _UpdateFailsConn(dm.get_connection(), "UPDATE RENAME_JOBS")

            with caplog.at_level(logging.ERROR, logger="backend.database"):
                n = dm.reset_applying_rename_jobs()

            # Pre-fix this returned 3 — the COUNT — while nothing was recovered.
            assert n == 0
            assert any("applying" in r.getMessage() for r in caplog.records)

            dm.conn = dm.conn._real
            for jid in ids:
                assert dm.get_rename_job(jid)["status"] == "applying"
        finally:
            dm.conn = getattr(dm.conn, "_real", dm.conn)
            dm.close()

    def test_successful_reset_returns_the_number_recovered(self, db_manager):
        """POSITIVE CONTROL: the working path must be unchanged — including the
        prior_status restore, so a needs_review job is not promoted."""
        gated = db_manager.create_rename_job(
            {"original_path": "/x/gated.mkv", "status": "applying",
             "prior_status": "needs_review"})
        legacy = db_manager.create_rename_job(
            {"original_path": "/x/legacy.mkv", "status": "applying"})
        untouched = db_manager.create_rename_job(
            {"original_path": "/x/done.mkv", "status": "applied"})

        assert db_manager.reset_applying_rename_jobs() == 2
        assert db_manager.get_rename_job(gated)["status"] == "needs_review"
        assert db_manager.get_rename_job(legacy)["status"] == "matched"
        assert db_manager.get_rename_job(untouched)["status"] == "applied"
        # Idempotent: a second startup recovers nothing and must say so.
        assert db_manager.reset_applying_rename_jobs() == 0

    def test_no_applying_jobs_returns_zero(self, db_manager):
        db_manager.create_rename_job({"original_path": "/x/a.mkv", "status": "matched"})
        assert db_manager.reset_applying_rename_jobs() == 0
