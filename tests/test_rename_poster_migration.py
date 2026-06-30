"""Migration tests: rename_jobs.poster_path exists and ALTER is idempotent."""
import sqlite3
import tempfile
import os

import pytest

from backend.database import DatabaseManager


def _make_dm():
    """Create a DatabaseManager backed by an isolated temp file."""
    tmp = tempfile.mktemp(suffix=".db", prefix="scanhound_migration_test_")
    return DatabaseManager(db_path=tmp), tmp


def _rename_columns(dm):
    conn = dm.get_connection()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(rename_jobs)")}
    return cols


def test_fresh_db_has_poster_path_column():
    dm, tmp = _make_dm()
    try:
        assert "poster_path" in _rename_columns(dm)
    finally:
        dm.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass


def test_rerunning_migrations_is_a_noop():
    # First init already ran migrations via __init__; re-running init_db()
    # twice more against the same file must not raise on the duplicate ADD COLUMN.
    dm, tmp = _make_dm()
    try:
        dm.init_db()
        dm.init_db()
        cols = _rename_columns(dm)
        assert "poster_path" in cols
    finally:
        dm.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass
