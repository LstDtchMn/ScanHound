"""Migration tests: rename_jobs.poster_path exists and ALTER is idempotent."""
import sqlite3
import tempfile
import os

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def tmp_db_path():
    """Yield a path to an isolated temp SQLite file; clean up after the test."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="scanhound_migration_test_")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _rename_columns(dm):
    conn = dm.get_connection()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(rename_jobs)")}
    return cols


def test_fresh_db_has_poster_path_column(tmp_db_path):
    dm = DatabaseManager(db_path=tmp_db_path)
    try:
        assert "poster_path" in _rename_columns(dm)
    finally:
        dm.close()


def test_rerunning_migrations_is_a_noop(tmp_db_path):
    # First init already ran migrations via __init__; re-running init_db()
    # twice more against the same file must not raise on the duplicate ADD COLUMN.
    dm = DatabaseManager(db_path=tmp_db_path)
    try:
        dm.init_db()
        dm.init_db()
        cols = _rename_columns(dm)
        assert "poster_path" in cols
    finally:
        dm.close()
