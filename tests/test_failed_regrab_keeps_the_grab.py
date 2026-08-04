"""A failed re-grab must not erase the record of a successful one.

add_to_history upserts on url with `status = excluded.status`, unconditionally.
The Regrab button passes force=True, which skips the dedup gate, so a retry of
an ALREADY-DELIVERED release reached that statement; when JDownloader was
unreachable the handler wrote status='failed' and the row flipped
completed -> failed.

Every "do we already have this?" reader excludes failed rows, so the release
stopped counting as downloaded and lost its duplicate protection -- and the
next auto-grab would re-fetch a 60 GB file already sitting on disk. The retry
failure is notified; the demotion is not.
"""

import os
import tempfile

import pytest

from backend.database import DatabaseManager


URL = "https://hdencode.org/dune-part-two-2160p/"


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


def a_delivered_grab(db):
    db.add_to_history(
        URL, "Dune Part Two", normalized_title="dune part two", season=None,
        resolution="2160p", size="60 GB", status="completed",
        hdr="HDR10", dovi=True, year=2024, package_name="Dune.Part.Two",
        service_type="movie")


def a_failed_regrab(db):
    """What download_item writes when the grab fails after force=True."""
    db.add_to_history(
        URL, "Dune Part Two", normalized_title="dune part two", season=None,
        resolution="2160p", size="60 GB", status="failed",
        hdr="HDR10", dovi=True, year=2024)


def test_a_delivered_grab_survives_a_failed_regrab(db):
    a_delivered_grab(db)
    assert db.is_downloaded(URL) is True, "fixture must start as downloaded"

    a_failed_regrab(db)

    assert db.is_downloaded(URL) is True, (
        "the release stopped counting as downloaded, so nothing blocks a "
        "re-download of a file already on disk")
    assert URL in db.get_downloaded_urls()
    assert db.get_downloaded_title_quality(), (
        "title-level duplicate protection was lost")


def test_the_recorded_quality_of_a_delivered_grab_is_not_rewritten(db):
    """A failed attempt must not restate what we have, either."""
    a_delivered_grab(db)

    db.add_to_history(URL, "Dune Part Two", resolution="1080p",
                      size="8 GB", status="failed", year=2024)

    with db.transaction() as conn:
        row = conn.execute(
            "SELECT resolution, size, status FROM downloads WHERE url = ?",
            (URL,)).fetchone()
    assert row["status"] == "completed"
    assert row["resolution"] == "2160p", (
        f"a failed 1080p attempt rewrote the recorded quality: "
        f"{dict(row)}")
    assert row["size"] == "60 GB"


def test_a_genuinely_new_failed_grab_still_records_as_failed(db):
    """NEGATIVE CONTROL. A guard that swallowed every failed write would pass
    the tests above while breaking failure tracking entirely."""
    other = "https://hdencode.org/never-grabbed/"
    db.add_to_history(other, "Never Grabbed", resolution="2160p",
                      size="50 GB", status="failed", year=2024)

    assert db.is_downloaded(other) is False, (
        "a URL that only ever failed must NOT count as downloaded")
    assert other not in db.get_downloaded_urls()


def test_a_retry_after_a_failure_can_still_succeed(db):
    """The guard is one-directional: failure cannot demote success, but
    success must always be able to overwrite a failure."""
    db.add_to_history(URL, "Dune Part Two", resolution="2160p",
                      size="60 GB", status="failed", year=2024)
    assert db.is_downloaded(URL) is False

    a_delivered_grab(db)

    assert db.is_downloaded(URL) is True
    assert URL in db.get_downloaded_urls()


def test_a_failed_row_can_still_be_updated_by_another_failure(db):
    """Two failed attempts in a row must not be frozen by the guard."""
    db.add_to_history(URL, "Old Title", resolution="1080p", size="8 GB",
                      status="failed", year=2024)
    db.add_to_history(URL, "Corrected Title", resolution="2160p",
                      size="60 GB", status="failed", year=2024)

    with db.transaction() as conn:
        row = conn.execute(
            "SELECT title, resolution FROM downloads WHERE url = ?",
            (URL,)).fetchone()
    assert row["title"] == "Corrected Title"
    assert row["resolution"] == "2160p"
