"""D-2: a sidecar-preservation FAILURE must not proceed to a fresh database.

The quarantine renames the corrupt main DB to <db>.corrupt.<ts>, then moves the
-wal/-shm sidecars next to it. Before this fix a failed sidecar move was logged
and execution continued: the corruption flag was written and a fresh DB was
created at the original basename. When the failed sidecar is a -wal that the
pre-quarantine checkpoint could not fully fold, that fresh DB consumes/resets
the ONLY copy of those transactions while the flag advertises a backup missing
them — an unrecoverable, silent data loss.

The fix reads the checkpoint result instead of discarding it:
``PRAGMA wal_checkpoint(TRUNCATE)`` returns (busy, log, checkpointed), and only
busy == 0 with checkpointed == log proves the main file complete. When it is not
proven complete and a -wal exists, moving that -wal is MANDATORY; a failure
rolls the moves back and raises QuarantineAbortedError instead of creating a
fresh DB.

Layout of this file:
  * TestCheckpointResultIsInspected — unit-level truth table for the
    complete/not-complete decision, including rows where a plausible weaker
    implementation disagrees.
  * TestMandatoryWalMoveFailureAborts — real SQLite, real uncheckpointed WAL,
    injected rename failures: abort, rollback, and rollback-failure.
  * TestQuarantineStillCompletes — POSITIVE CONTROLS. A "fix" that simply
    aborted more often, or always required the WAL, fails these.

Nothing here mocks SQLite for the integration cases: the WAL is made genuinely
hot with a pinned stale reader, the same technique the pass-2 audit used to
reproduce a real (1, 54, 4) partial checkpoint.
"""
import glob
import logging
import os
import sqlite3

import pytest

from backend.database import DatabaseManager, QuarantineAbortedError


# ── shared SQLite helpers ────────────────────────────────────────────────

def _open_wal(path):
    """A writer connection whose commits STAY in the -wal until told otherwise."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _attach(path):
    """A second connection that has touched the wal-index and holds no snapshot.

    Keeping one open is what stops the manager's close() from being the LAST
    close — SQLite deletes the -wal/-shm on last close, and then the quarantine
    would have no sidecar to try to move, so a rename-failure injection could
    never fire and the test would pass vacuously. fetchall() (not fetchone) so
    the statement is reset and no read snapshot is pinned, which keeps a
    TRUNCATE checkpoint able to complete.
    """
    conn = sqlite3.connect(path)
    conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    return conn


def _pin_stale_snapshot(path):
    """A reader pinned at the CURRENT state inside an open transaction.

    Rows committed after this returns are invisible to it and SQLite may not
    fold those frames during a checkpoint — this is what makes the -wal
    genuinely load-bearing rather than notionally so.
    """
    conn = sqlite3.connect(path)
    conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
    return conn


def _rows(path, sql="SELECT COUNT(*) FROM audit_marker"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _corrupt_once(monkeypatch):
    """Make the NEXT PRAGMA integrity_check report corruption, exactly once.

    Once only: on the paths that DO rebuild, the rebuild's own init_db() would
    otherwise quarantine the fresh DB too and (same-second timestamp) rename it
    over the backup under test. The file itself stays physically healthy, so
    "the data survived" is checkable by simply reading it back — which is the
    property this finding is about.
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


def _break_rename(monkeypatch, predicate):
    """Fail os.rename for the (src, dst) pairs matching ``predicate``.

    backend.database calls the module attribute os.rename, so patching it here
    reaches the quarantine and the rollback both. Returns the REAL os.rename so
    a test can still stage or un-stage files itself without tripping its own
    injection.
    """
    real_rename = os.rename

    def _fake(src, dst, *a, **kw):
        if predicate(str(src), str(dst)):
            raise OSError(13, "injected rename failure")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", _fake)
    return real_rename


def _backups(db_path):
    return sorted(f for f in glob.glob(db_path + ".corrupt.*")
                  if not f.endswith((".corrupt_flag.json",)))


def _main_backup(db_path):
    found = [f for f in _backups(db_path) if not f.endswith(("-wal", "-shm"))]
    assert len(found) == 1, f"expected exactly one backup, got {found}"
    return found[0]


# ── the checkpoint result must be read, not discarded ────────────────────

class _StubCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _StubConn:
    """Returns a fixed wal_checkpoint result row, or raises."""

    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.executed = []

    def execute(self, sql, *a, **kw):
        self.executed.append(sql)
        if self.error is not None:
            raise self.error
        return _StubCursor(self.row)


def _manager_with(conn):
    # __new__ deliberately: this decision needs no database on disk, and
    # constructing one would run a real init_db() and muddy the unit.
    dm = DatabaseManager.__new__(DatabaseManager)
    dm.db_path = "/definitely/not/used/by/this/unit.db"
    dm.conn = conn
    return dm


class TestCheckpointResultIsInspected:
    def test_full_fold_is_complete_and_actually_checkpoints(self):
        """POSITIVE CONTROL for the reader: busy=0 and checkpointed == log."""
        conn = _StubConn(row=(0, 12, 12))
        assert _manager_with(conn)._checkpoint_for_quarantine() is True
        # It must still be a TRUNCATE checkpoint — reading the result is not a
        # licence to stop folding the WAL.
        assert any("wal_checkpoint(TRUNCATE)" in s for s in conn.executed), \
            conn.executed

    def test_partial_backfill_is_not_complete(self):
        """(1, 54, 4) is the real partial result the audit reproduced; the
        not-busy variant of it must be judged the same way."""
        assert _manager_with(
            _StubConn(row=(0, 54, 4)))._checkpoint_for_quarantine() is False

    def test_busy_with_full_backfill_is_not_complete(self):
        """DISAGREEING CASE. An implementation that compares only
        checkpointed == log calls this complete and would then permit the WAL to
        be dropped. busy=1 means another connection blocked the reset, so the
        frame counts describe a checkpoint that did not finish cleanly — not
        knowing is treated as not complete, because the cost of being wrong is
        asymmetric (a stale sidecar vs. destroyed transactions)."""
        assert _manager_with(
            _StubConn(row=(1, 12, 12)))._checkpoint_for_quarantine() is False

    def test_checkpoint_error_is_not_complete(self):
        assert _manager_with(_StubConn(
            error=sqlite3.OperationalError("disk I/O error"),
        ))._checkpoint_for_quarantine() is False

    def test_absent_result_row_is_not_complete(self):
        assert _manager_with(
            _StubConn(row=None))._checkpoint_for_quarantine() is False

    def test_short_result_row_is_not_complete(self):
        assert _manager_with(
            _StubConn(row=(0,)))._checkpoint_for_quarantine() is False

    def test_non_wal_minus_one_result_is_not_complete(self):
        """DISAGREEING CASE. SQLite answers (0, -1, -1) when the DB is not in
        WAL mode; -1 == -1 makes a bare `checkpointed == log` test report
        'complete'. It proves nothing about a -wal file that exists on disk
        anyway, so it must not license dropping one."""
        assert _manager_with(
            _StubConn(row=(0, -1, -1)))._checkpoint_for_quarantine() is False

    def test_non_integer_result_is_not_complete(self):
        """None == None would otherwise read as checkpointed == log."""
        assert _manager_with(_StubConn(
            row=(0, None, None)))._checkpoint_for_quarantine() is False


# ── the mandatory -wal move ──────────────────────────────────────────────

class TestMandatoryWalMoveFailureAborts:
    def _seed_hot_wal(self, db_path):
        """301 rows, of which 300 exist ONLY in an unfoldable -wal.

        Returns the pinned reader; the caller must close it.
        """
        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.execute("INSERT INTO audit_marker VALUES (0)")
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # row 0 reaches the main file
        stale = _pin_stale_snapshot(db_path)           # blocks folding of what follows
        w.executemany("INSERT INTO audit_marker VALUES (?)",
                      [(i,) for i in range(1, 301)])
        w.close()   # `stale` is still open, so no close-time checkpoint
        assert os.path.getsize(db_path + "-wal") > 0, "setup: WAL must be hot"
        return stale

    def test_wal_rename_failure_aborts_and_rolls_back(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "crawler.db")
        stale = self._seed_hot_wal(db_path)
        try:
            _break_rename(monkeypatch, lambda src, dst: src.endswith("-wal"))
            _corrupt_once(monkeypatch)

            with pytest.raises(QuarantineAbortedError):
                DatabaseManager(db_path=db_path)

            # Rolled back: the corrupt main file is back under its own name...
            assert os.path.exists(db_path), "the main DB was not rolled back"
            # ...the WAL was never moved...
            assert os.path.getsize(db_path + "-wal") > 0
            # ...no backup was left behind...
            assert _backups(db_path) == []
            # ...and nothing was advertised. A flag here would tell the operator
            # (and, since A-1, the recovery lock) that a complete backup exists.
            assert not os.path.exists(db_path + ".corrupt_flag.json")

            # The whole point: all 301 rows are still readable, 300 of them only
            # because that -wal was left paired with its main file.
            assert _rows(db_path) == 301
        finally:
            stale.close()

    def test_abort_creates_no_fresh_database(self, tmp_path, monkeypatch):
        """The pre-fix behaviour was 'log and carry on', so the specific thing
        to pin is that no NEW, empty DB was created at the original basename —
        that creation is what consumes and resets the stranded WAL."""
        db_path = str(tmp_path / "crawler.db")
        stale = self._seed_hot_wal(db_path)
        try:
            _break_rename(monkeypatch, lambda src, dst: src.endswith("-wal"))
            _corrupt_once(monkeypatch)
            with pytest.raises(QuarantineAbortedError):
                DatabaseManager(db_path=db_path)

            # A fresh DB would have the app's schema and an empty audit_marker;
            # the rolled-back original has audit_marker with 301 rows.
            assert _rows(db_path,
                         "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                         "AND name='downloads'") == 0, (
                "a fresh schema was created over the quarantine's own failure")
            assert _rows(db_path) == 301
        finally:
            stale.close()

    def test_data_survives_the_next_consumer_after_an_abort(
            self, tmp_path, monkeypatch):
        """CONSUMER-level check, not component-level.

        Aborting is only worth anything if nothing downstream then creates a DB
        at the original basename anyway. AppService catches the abort
        (app_service.py:461-464 `except Exception` → self.db = None) and keeps
        going, after which WatchlistManager(db_manager=None) falls back to
        `sqlite3.connect(self.db_path or DB_PATH)` (watchlist.py:118/140) — a
        bare connect against the same path. This test walks that far: after a
        rolled-back abort the main file is back under its own name, so that
        connect opens the EXISTING pair instead of creating a fresh DB over the
        stranded WAL, and all 301 rows are still there afterwards.
        """
        db_path = str(tmp_path / "crawler.db")
        stale = self._seed_hot_wal(db_path)
        try:
            _break_rename(monkeypatch, lambda src, dst: src.endswith("-wal"))
            _corrupt_once(monkeypatch)
            with pytest.raises(QuarantineAbortedError):
                DatabaseManager(db_path=db_path)
            stale.close()   # the app moves on / restarts; the pin is gone

            # Exactly what the watchlist/analytics fallback does.
            consumer = sqlite3.connect(db_path)
            consumer.execute("PRAGMA journal_mode=WAL")
            consumer.close()

            assert _rows(db_path) == 301
            assert _rows(db_path,
                         "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                         "AND name='downloads'") == 0, (
                "a fresh application schema appeared at the original basename")
        finally:
            stale.close()

    def test_rollback_failure_still_refuses_to_create_a_fresh_db(
            self, tmp_path, monkeypatch, caplog):
        """Later-step/rollback failure: the -wal move fails AND putting the main
        file back fails. Aborting is still mandatory — a fresh DB at db_path
        would consume the stranded -wal that is now the only copy of 300 rows.
        """
        db_path = str(tmp_path / "crawler.db")
        stale = self._seed_hot_wal(db_path)
        try:
            real_rename = _break_rename(
                monkeypatch,
                # Fail the mandatory -wal move, and also the rollback that
                # tries to move the backup back to the original name.
                lambda src, dst: (src.endswith("-wal")
                                  or (".corrupt." in src and dst == db_path)),
            )
            _corrupt_once(monkeypatch)

            with caplog.at_level(logging.CRITICAL, logger="backend.database"):
                with pytest.raises(QuarantineAbortedError, match="rollback"):
                    DatabaseManager(db_path=db_path)

            assert any("rollback FAILED" in r.getMessage()
                       for r in caplog.records), (
                "a failed rollback must be reported at CRITICAL, not swallowed")

            # No fresh DB at the original basename, so the stranded -wal is
            # untouched and the pair is still reunitable by hand.
            assert not os.path.exists(db_path)
            assert os.path.getsize(db_path + "-wal") > 0
            assert not os.path.exists(db_path + ".corrupt_flag.json")

            backup = _main_backup(db_path)
            real_rename(backup, db_path)   # the operator's manual recovery
            assert _rows(db_path) == 301
        finally:
            stale.close()


# ── positive controls: the quarantine must still work ────────────────────

class TestQuarantineStillCompletes:
    def test_mandatory_wal_move_that_succeeds_completes_the_quarantine(
            self, tmp_path, monkeypatch):
        """POSITIVE CONTROL for the mandatory path: same hot-WAL setup, no
        injected failure. A fix that turned 'mandatory' into 'always abort'
        fails here."""
        db_path = str(tmp_path / "crawler.db")
        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.execute("INSERT INTO audit_marker VALUES (0)")
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stale = _pin_stale_snapshot(db_path)
        try:
            w.executemany("INSERT INTO audit_marker VALUES (?)",
                          [(i,) for i in range(1, 301)])
            w.close()
            _corrupt_once(monkeypatch)
            dm = DatabaseManager(db_path=db_path)
            try:
                backup = _main_backup(db_path)
                assert os.path.exists(backup + "-wal")
                # Every row is recoverable from the backup pair, 300 of them
                # only from that -wal.
                assert _rows(backup) == 301
                assert os.path.exists(db_path + ".corrupt_flag.json")
                # The rebuilt DB is fresh and usable.
                assert dm.add_dismissed_item("http://x/a", "A") is True
                assert dm.get_dismissed_urls() == {"http://x/a"}
            finally:
                dm.close()
        finally:
            stale.close()

    def test_fully_checkpointed_wal_move_failure_is_tolerated(
            self, tmp_path, monkeypatch, caplog):
        """DISAGREEING CASE against 'always require the -wal'.

        Here the checkpoint folds everything, so the -wal is genuinely
        disposable and a failure to move it must NOT block recovery: the backup
        is already complete. An implementation that ignored the checkpoint
        result and made every -wal mandatory would abort and fail this.
        """
        db_path = str(tmp_path / "crawler.db")
        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.executemany("INSERT INTO audit_marker VALUES (?)",
                      [(i,) for i in range(301)])
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # all 301 in the main file
        keeper = _attach(db_path)                      # keeps the sidecars on disk
        w.close()
        try:
            assert os.path.exists(db_path + "-wal"), "setup: a -wal must exist"
            _break_rename(monkeypatch, lambda src, dst: src.endswith("-wal"))
            _corrupt_once(monkeypatch)

            with caplog.at_level(logging.WARNING, logger="backend.database"):
                dm = DatabaseManager(db_path=db_path)
            try:
                # Premise of this test, asserted rather than assumed: the
                # quarantine judged the WAL fully folded.
                assert not any("NOT fully folded" in r.getMessage()
                               for r in caplog.records), (
                    "premise broken: SQLite did not fully fold the WAL here, so "
                    "this test is no longer exercising the disposable-WAL case")
                # Recovery proceeded despite the failed -wal move.
                backup = _main_backup(db_path)
                assert _rows(backup) == 301, "the backup must be complete"
                assert os.path.exists(db_path + ".corrupt_flag.json")
                assert dm.add_dismissed_item("http://x/a", "A") is True
                assert dm.get_dismissed_urls() == {"http://x/a"}
            finally:
                dm.close()
        finally:
            keeper.close()

    def test_shm_move_failure_never_aborts(self, tmp_path, monkeypatch, caplog):
        """A later-step failure on a NON-mandatory file must not abort.

        The -shm is a rebuildable shared-memory index, not data, so losing it
        costs nothing — treating every sidecar failure as fatal would strand
        installs that could have recovered.
        """
        db_path = str(tmp_path / "crawler.db")
        w = _open_wal(db_path)
        w.execute("CREATE TABLE audit_marker (n INTEGER)")
        w.execute("INSERT INTO audit_marker VALUES (0)")
        keeper = _attach(db_path)
        w.close()
        try:
            assert os.path.exists(db_path + "-shm"), "setup: a -shm must exist"
            _break_rename(monkeypatch, lambda src, dst: src.endswith("-shm"))
            _corrupt_once(monkeypatch)

            with caplog.at_level(logging.ERROR, logger="backend.database"):
                dm = DatabaseManager(db_path=db_path)
            try:
                assert any("Could not preserve" in r.getMessage()
                           and "-shm" in r.getMessage()
                           for r in caplog.records), (
                    "the tolerated failure must still be logged")
                backup = _main_backup(db_path)
                assert _rows(backup) == 1
                assert os.path.exists(db_path + ".corrupt_flag.json")
                assert dm.add_dismissed_item("http://x/a", "A") is True
                assert dm.get_dismissed_urls() == {"http://x/a"}
            finally:
                dm.close()
        finally:
            keeper.close()

    def test_healthy_db_is_never_quarantined_or_aborted(self, tmp_path):
        """POSITIVE CONTROL at the top level: ordinary startup on a healthy DB
        must not move anything, raise anything, or write a flag (which since
        A-1 would put the install into RECOVERY_LOCKED)."""
        db_path = str(tmp_path / "crawler.db")
        dm = DatabaseManager(db_path=db_path)
        try:
            dm.add_dismissed_item("http://x/a", "A")
        finally:
            dm.close()

        dm2 = DatabaseManager(db_path=db_path)
        try:
            assert _backups(db_path) == []
            assert not os.path.exists(db_path + ".corrupt_flag.json")
            assert dm2.get_dismissed_urls() == {"http://x/a"}
        finally:
            dm2.close()
