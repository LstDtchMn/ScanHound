"""A write that failed must not report success.

upsert_media_inventory, upsert_dv_scan and upsert_media_probe each ended with
`self._mutate(...) is not None`. _mutate returns True or False and never None,
so `False is not None` is True: all three returned success on a failed write.

That matters because callers DO check. plex_metadata_scan has
`if not self._db.upsert_dv_scan(...)` and two `if not ... upsert_media_inventory(...)`
guards, and dv_import assigns `ok = db.upsert_dv_scan(...)`. Every one of those
branches was unreachable, so a DV scan that failed to persist was counted as
scanned -- and the inventory silently stops growing while the log says it is.
"""

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    manager = DatabaseManager(path)
    yield manager
    try:
        manager.close()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def a_dv_scan(db):
    return db.upsert_dv_scan(
        "/media/4k/Dune (2021)/Dune.2021.2160p.mkv", "FEL",
        title="Dune", sig_mtime=1.0, sig_size=1, source="host_detector",
    )


def a_probe(db):
    return db.upsert_media_probe(
        "/media/4k/Dune (2021)/Dune.2021.2160p.mkv", "{}",
        sig_mtime=1.0, sig_size=1)


def an_inventory_row(db):
    return db.upsert_media_inventory({
        "path": "/media/4k/Dune (2021)/Dune.2021.2160p.mkv",
        "title": "Dune", "scan_state": "current",
    })


WRITES = pytest.mark.parametrize("write", [
    pytest.param(a_dv_scan, id="upsert_dv_scan"),
    pytest.param(a_probe, id="upsert_media_probe"),
    pytest.param(an_inventory_row, id="upsert_media_inventory"),
])


@WRITES
def test_a_successful_write_reports_success(db, write):
    """POSITIVE CONTROL: without it, a function hardwired to return False
    would pass the test below."""
    assert write(db) is True


@WRITES
def test_a_failed_write_reports_failure(db, write):
    """The disagreeing case. Under `is not None` this returned True."""
    with patch.object(DatabaseManager, "_mutate", return_value=False):
        assert write(db) is False, (
            "a failed write reported success, so every caller's `if not ...` "
            "guard is dead code")


@WRITES
def test_a_real_database_error_reports_failure(db, write):
    """Not just a patched return -- an actual failing statement.

    _mutate catches the exception and returns False; the caller must see it.
    """
    with patch.object(DatabaseManager, "get_connection", return_value=None):
        assert write(db) is False
