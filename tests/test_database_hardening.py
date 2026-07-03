"""Tests for SQLite hardening: busy_timeout/synchronous pragmas, the
checkpoint() method, and the loud (not silent) corruption-quarantine path.
"""
import json
import logging
import os
import sqlite3

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "hardening.db")


class TestPragmas:
    def test_busy_timeout_is_set(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            conn = dm.get_connection()
            val = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert val == 5000
        finally:
            dm.close()

    def test_synchronous_is_normal(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            conn = dm.get_connection()
            # SQLite reports synchronous as an integer: 0=OFF, 1=NORMAL, 2=FULL
            val = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert val == 1
        finally:
            dm.close()

    def test_journal_mode_is_wal(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            conn = dm.get_connection()
            val = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert val.lower() == "wal"
        finally:
            dm.close()


class TestCheckpoint:
    def test_checkpoint_returns_true_on_healthy_db(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            assert dm.checkpoint() is True
        finally:
            dm.close()

    def test_checkpoint_truncates_wal(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            dm.add_dismissed_item("http://x/a", "A")
            # WAL file should exist (mode is WAL) after a write.
            wal_path = db_path + "-wal"
            dm.checkpoint()
            # After TRUNCATE checkpoint, the -wal file is truncated to 0 bytes
            # (it may still exist as an empty file — that's expected WAL
            # behavior; the key property is it's not left growing).
            if os.path.exists(wal_path):
                assert os.path.getsize(wal_path) == 0
        finally:
            dm.close()

    def test_checkpoint_called_once_after_startup_init(self, db_path, monkeypatch):
        """init_db() should trigger one checkpoint call on a healthy DB."""
        calls = []
        original = DatabaseManager.checkpoint

        def _spy(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(DatabaseManager, "checkpoint", _spy)
        dm = DatabaseManager(db_path=db_path)
        try:
            assert len(calls) >= 1
        finally:
            dm.close()


class TestLoudCorruptionQuarantine:
    def _make_corrupt_db(self, path):
        with open(path, "wb") as f:
            f.write(b"this is not a valid sqlite database file, just garbage bytes")

    def test_corrupt_db_triggers_quarantine_and_loud_log(self, db_path, caplog):
        self._make_corrupt_db(db_path)
        with caplog.at_level(logging.ERROR, logger="backend.database"):
            dm = DatabaseManager(db_path=db_path)
        try:
            # Quarantine: original file renamed to a .corrupt.<ts> backup, and
            # a fresh, usable DB now lives at db_path.
            backups = [f for f in os.listdir(os.path.dirname(db_path))
                       if ".corrupt." in f]
            assert backups, "expected a .corrupt.<ts> backup file"
            assert os.path.exists(db_path)

            # Loud: an ERROR-level log record was emitted, not a silent rebuild.
            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert any("CORRUPTION" in r.message.upper() for r in error_records), (
                "expected a loud ERROR-level corruption log record")

            # The new DB actually works.
            conn = dm.get_connection()
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dm.close()

    def test_corrupt_db_writes_persisted_flag_file(self, db_path):
        self._make_corrupt_db(db_path)
        dm = DatabaseManager(db_path=db_path)
        try:
            flag_path = f"{db_path}.corrupt_flag.json"
            assert os.path.exists(flag_path), "expected a persisted corruption flag file"
            with open(flag_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data["db_path"] == db_path
            assert "detected_at" in data
            assert "backup_path" in data
        finally:
            dm.close()

    def test_healthy_db_is_untouched_no_flag_no_backup(self, db_path):
        dm = DatabaseManager(db_path=db_path)
        try:
            dm.add_dismissed_item("http://x/a", "A")
        finally:
            dm.close()

        # Reopen — a healthy DB must not be quarantined or flagged.
        dm2 = DatabaseManager(db_path=db_path)
        try:
            flag_path = f"{db_path}.corrupt_flag.json"
            assert not os.path.exists(flag_path)
            backups = [f for f in os.listdir(os.path.dirname(db_path))
                       if ".corrupt." in f]
            assert not backups
            assert dm2.get_dismissed_urls() == {"http://x/a"}
        finally:
            dm2.close()

    def test_integrity_check_failure_without_exception_also_quarantines(self, db_path, caplog, monkeypatch):
        """A DB that opens fine but fails PRAGMA integrity_check (returns a
        non-'ok' string rather than raising) must still be quarantined loudly,
        not silently accepted."""
        dm_probe = DatabaseManager(db_path=db_path)
        dm_probe.close()

        class _FakeCursor:
            def __init__(self, real_cursor):
                self._real = real_cursor
                self._next_is_integrity = False

            def execute(self, sql, *a, **kw):
                if "integrity_check" in sql:
                    self._next_is_integrity = True
                    return self
                self._next_is_integrity = False
                return self._real.execute(sql, *a, **kw)

            def fetchone(self):
                if self._next_is_integrity:
                    return ("corruption detected: freelist",)
                return self._real.fetchone()

            def __getattr__(self, item):
                return getattr(self._real, item)

        class _FakeConn:
            def __init__(self, real_conn):
                self._real = real_conn

            def cursor(self, *a, **kw):
                return _FakeCursor(self._real.cursor(*a, **kw))

            def __getattr__(self, item):
                return getattr(self._real, item)

        dm = DatabaseManager.__new__(DatabaseManager)
        dm.db_path = db_path
        dm.conn = None
        import threading as _threading
        dm._lock = _threading.RLock()
        dm._init_depth = 0
        dm._dismissed_cache = None

        real_get_connection = DatabaseManager.get_connection

        def _wrapped_get_connection(self):
            conn = real_get_connection(self)
            if conn is not None and not isinstance(conn, _FakeConn):
                self.conn = _FakeConn(conn)
                return self.conn
            return conn

        monkeypatch.setattr(DatabaseManager, "get_connection", _wrapped_get_connection)

        with caplog.at_level(logging.ERROR, logger="backend.database"):
            dm.init_db()
        try:
            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert any("CORRUPTION" in r.message.upper() for r in error_records)
        finally:
            dm.close()
