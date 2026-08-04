"""Scan metrics survive the round trip, and say when they cannot be trusted.

These run against a real SQLite file, not a mock. A mock would confirm the
method was called; the question worth answering is whether the numbers that
come back out are the numbers that went in, and whether a scan whose books did
not balance is still readable as untrustworthy afterwards.
"""

import json
import os
import tempfile

import pytest

from backend.database import DatabaseManager
from backend.scan_metrics import (
    DiscardCode,
    PostOutcome,
    ScanStage,
    ScanStageCounters,
    TerminalKind,
    conservation_errors_of,
    media_items_emitted_of,
)


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


def a_balanced_scan():
    """Three posts: one shipped, one lost its page, one stopped by the operator."""
    counters = ScanStageCounters()
    counters.note_scheduled(3)

    shipped = PostOutcome(counters, url="https://x/1", source="hdencode")
    shipped.note_started()
    shipped.data_returned()
    shipped.item_created()

    lost = PostOutcome(counters, url="https://x/2", source="hdencode")
    lost.note_started()
    lost.discard(
        DiscardCode.DETAIL_NO_FILENAME,
        stage=ScanStage.DETAIL_PARSE,
        terminal_kind=TerminalKind.RETURNED_NONE,
    )

    stopped = PostOutcome(counters, url="https://x/3", source="hdencode")
    stopped.note_started()
    stopped.discard(
        DiscardCode.DETAIL_CANCELLED_AFTER_START,
        stage=ScanStage.DETAIL_FETCH,
        terminal_kind=TerminalKind.CANCELLED_AFTER_START,
    )
    return counters


def test_a_balanced_scan_round_trips_with_its_counts_intact(db):
    counters = a_balanced_scan()
    snap = counters.snapshot_counts()
    assert conservation_errors_of(snap) == [], "fixture itself must balance"

    row_id = db.record_scan_metrics(snap)
    assert row_id is not None

    rows = db.get_recent_scan_metrics(limit=5)
    assert len(rows) == 1
    row = rows[0]

    assert row["posts_scheduled"] == 3
    assert row["items_emitted"] == media_items_emitted_of(snap) == 1
    assert row["conservation_ok"] is True
    assert row["conservation_errors"] == []
    # Not just "some dict came back" -- the specific counters, so a writer that
    # persisted an empty or partial payload cannot pass.
    assert row["counters"]["detail_scheduled"] == 3
    assert row["counters"]["detail_started"] == 3
    assert row["counters"]["media_item_created"] == 1
    assert row["counters"]["detail_returned_none"] == 1
    assert row["counters"]["detail_cancelled_after_start"] == 1


def test_the_reason_for_every_loss_survives_the_round_trip(db):
    """A count without a reason cannot be acted on."""
    db.record_scan_metrics(a_balanced_scan().snapshot_counts())
    row = db.get_recent_scan_metrics(limit=1)[0]

    reasons = row["counters"]["reasons"]
    assert reasons.get(DiscardCode.DETAIL_NO_FILENAME.value) == 1
    assert reasons.get(DiscardCode.DETAIL_CANCELLED_AFTER_START.value) == 1

    urls = {s["canonical_url"]: s for s in row["samples"]}
    assert urls["https://x/2"]["reason_code"] == DiscardCode.DETAIL_NO_FILENAME.value
    assert urls["https://x/2"]["stage"] == ScanStage.DETAIL_PARSE.value
    # No response body is ever stored, only a classification.
    assert set(urls["https://x/2"]) == {
        "canonical_url", "stage", "reason_code", "terminal_kind", "source",
        "category", "exception_type", "taxonomy_version", "parser_version",
        "content_fingerprint",
    }


def test_a_scan_whose_books_did_not_balance_is_stored_as_untrustworthy(db):
    """NEGATIVE CONTROL for conservation_ok.

    Without this, every row reads conservation_ok=True and the column proves
    nothing. Here 5 posts are scheduled but only 2 accounted for, so the
    equations must fail AND that failure must survive the round trip -- a
    stored `True` on a broken scan is worse than no column, because it
    certifies numbers that are wrong.
    """
    counters = ScanStageCounters()
    counters.note_scheduled(5)
    for url in ("https://x/1", "https://x/2"):
        t = PostOutcome(counters, url=url)
        t.note_started()
        t.data_returned()
        t.item_created()

    snap = counters.snapshot_counts()
    errors = conservation_errors_of(snap)
    assert errors, "5 scheduled vs 2 accounted must not balance"

    db.record_scan_metrics(snap)
    row = db.get_recent_scan_metrics(limit=1)[0]

    assert row["conservation_ok"] is False
    assert row["conservation_errors"] == errors
    assert row["posts_scheduled"] == 5
    assert row["items_emitted"] == 2


def test_rows_come_back_newest_first(db):
    for n in (1, 2, 3):
        c = ScanStageCounters()
        c.note_scheduled(n)
        db.record_scan_metrics(c.snapshot_counts())

    rows = db.get_recent_scan_metrics(limit=10)
    assert [r["posts_scheduled"] for r in rows] == [3, 2, 1], (
        "an operator looking at scan health wants the last scan first; and "
        "these are written within the same second, so ordering cannot rely "
        "on the timestamp alone")


def test_an_undecodable_row_reports_as_undecodable_not_as_an_empty_scan(db):
    """A corrupt blob must not read as a clean scan of nothing."""
    db.record_scan_metrics(a_balanced_scan().snapshot_counts())
    with db.transaction() as conn:
        conn.execute("UPDATE scan_metrics SET counters_json = ?", ("{not json",))

    row = db.get_recent_scan_metrics(limit=1)[0]
    assert row["counters"] is None, (
        "returning {} here would render as a scan where nothing happened")
    # The columns stored outside the blob still stand on their own.
    assert row["posts_scheduled"] == 3


def test_taxonomy_version_is_recorded_so_old_rows_are_not_misread(db):
    from backend import scan_metrics as sm

    db.record_scan_metrics(a_balanced_scan().snapshot_counts())
    row = db.get_recent_scan_metrics(limit=1)[0]
    assert row["taxonomy_version"] == sm.TAXONOMY_VERSION
    assert row["counters"]["taxonomy_version"] == sm.TAXONOMY_VERSION
