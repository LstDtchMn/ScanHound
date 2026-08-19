"""The screen and the log must name the SAME stuck downloads.

`_warn_manual_recovery_batches` writes "a human must decide" to the log; the
retry list now carries `manual_recovery_required` for the UI. Those are two
consumers of one rule (`manual_recovery_groups`), and the reason they share it
is that two implementations would drift -- eventually the badge flags something
the log does not, or misses something it does, and the operator has no way to
tell which is lying.

So the load-bearing test here is not "the flag is set". It is that the flag and
the warning agree, on the same fixtures, including the source-scoped cases that
took three review rounds to get right.
"""
import logging
import re

from unittest.mock import MagicMock

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService

EXPIRED = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
STAMP = "2026-08-12T04:00:00+00:00"


def _items(n, prefix="release"):
    return [{"url": "https://hdencode.org/%s-%d-2160p/" % (prefix, i),
             "title": "%s %d" % (prefix.title(), i), "media_type": "movie"}
            for i in range(n)]


def _rig(db, *, auto_resume, count=2, prefix="release"):
    fake = MagicMock()
    service = DownloadQueueService({}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(_items(count, prefix), interval_minutes=0,
                                   mode="immediate",
                                   auto_resume_after_cooldown=auto_resume)
    return service, batch["batch_uuid"]


def _park(db, service, batch_uuid, *, cooldown=EXPIRED, hold=None):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE download_queue_items "
            "SET state='waiting_source', queue_reason='source_deferred', "
            "    cooldown_until=?, last_reason_code='source_temporarily_blocked', "
            "    updated_at=? WHERE batch_uuid=? AND state NOT IN "
            "    ('completed','cancelled')",
            (cooldown, STAMP, batch_uuid))
        conn.execute(
            "UPDATE download_queue_batches SET state='paused_source', "
            "  cooldown_until=?, verification_hold_source=?, "
            "  last_reason_code='source_temporarily_blocked' "
            "WHERE batch_uuid=?", (cooldown, hold, batch_uuid))


def _set_sources(db, batch_uuid, sources):
    """Assign one source per item, ordered by canonical_url so the assignment
    is deterministic (item_uuid is random -- ordering by it made an earlier
    test a coin flip)."""
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
            "ORDER BY canonical_url", (batch_uuid,)).fetchall()
        for row, source in zip(rows, sources):
            conn.execute("UPDATE download_queue_items SET source=? WHERE item_uuid=?",
                         (source, row[0]))


def _flagged(service):
    return {(i["batch_uuid"], i["source"])
            for i in service.list_retries() if i.get("manual_recovery_required")}


def _warned(caplog):
    """(batch, source) pairs the operator log actually named."""
    out = set()
    for record in caplog.records:
        if record.levelno < logging.WARNING:
            continue
        m = re.match(r"Batch (\S+) source (\S+) is parked with automatic resume "
                     r"DISABLED", record.getMessage())
        if m:
            out.add((m.group(1), m.group(2)))
    return out


class TestScreenAgreesWithLog:
    def test_a_manual_only_parked_group_is_both_flagged_and_warned(self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0)
        _park(db, service, batch)

        with caplog.at_level(logging.WARNING):
            service._maybe_auto_resume()

        assert _flagged(service) == _warned(caplog) != set()

    def test_a_held_source_is_neither_flagged_nor_warned(self, tmp_path, caplog):
        """Source-scoped: the held group belongs to the hold diagnostic, and the
        UI must not contradict that by badging it as needing manual recovery."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0, count=2, prefix="held")
        _set_sources(db, batch, ["hdencode", "othersite"])
        _park(db, service, batch, hold="hdencode")

        with caplog.at_level(logging.WARNING):
            service._maybe_auto_resume()

        flagged, warned = _flagged(service), _warned(caplog)
        assert flagged == warned
        assert (batch, "othersite") in flagged
        assert (batch, "hdencode") not in flagged

    def test_an_auto_resuming_batch_is_neither(self, tmp_path, caplog):
        """auto_resume_after_cooldown=1 has a recovery path; flagging it would
        send the operator to intervene where nothing is wrong."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=1)
        _park(db, service, batch)

        with caplog.at_level(logging.WARNING):
            service._maybe_auto_resume()

        assert _flagged(service) == set()
        assert _warned(caplog) == set()

    def test_a_cooldown_not_yet_due_is_neither(self, tmp_path, caplog):
        """Still inside its cooldown: parked, but not yet stranded."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0)
        _park(db, service, batch, cooldown=FUTURE)

        with caplog.at_level(logging.WARNING):
            service._maybe_auto_resume()

        assert _flagged(service) == set()
        assert _warned(caplog) == set()


class TestDisposition:
    def test_every_returned_item_carries_the_key(self, tmp_path):
        """Always present, never absent-when-false: a consumer that has to
        distinguish missing from false will eventually guess wrong."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0)
        _park(db, service, batch)

        items = service.list_retries()

        assert items
        assert all("manual_recovery_required" in i for i in items)

    def test_the_flag_survives_a_disposition_failure(self, tmp_path):
        """The retry list's job is showing the queue. If the disposition lookup
        breaks, the list must still render -- degraded, not empty."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0)
        _park(db, service, batch)
        service.manual_recovery_groups = MagicMock(side_effect=RuntimeError("boom"))

        items = service.list_retries()

        assert items
        assert all(i["manual_recovery_required"] is False for i in items)

    def test_reporting_does_not_authorise_a_retry(self, tmp_path):
        """The rule is a read. It must not promote anything -- the whole point
        is that a human decides."""
        db = DatabaseManager(str(tmp_path / "q.sqlite"))
        service, batch = _rig(db, auto_resume=0)
        _park(db, service, batch)

        service.manual_recovery_groups()

        states = {r["s"] for r in db._query_dicts(
            "SELECT DISTINCT state AS s FROM download_queue_items "
            "WHERE batch_uuid = ?", (batch,), default=[])}
        assert states == {"waiting_source"}
