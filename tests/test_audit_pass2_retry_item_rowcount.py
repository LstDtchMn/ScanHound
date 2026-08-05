"""Audit pass 2 finding 21: retry_item must honour the item UPDATE's rowcount.

Retrying an item whose state the UPDATE's WHERE clause excludes used to return
HTTP 200 with the row untouched while still forcing the whole batch back to
'scheduled' with cooldown_until NULL -- which destroys both preconditions
_maybe_auto_resume() selects on and strands every sibling left in
waiting_source.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import (
    DownloadQueueError,
    DownloadQueueItemClaimed,
    DownloadQueueService,
)


# A cooldown already in the past, so _maybe_auto_resume() is eligible to fire
# the moment it is called -- the stranding this finding is about is only
# observable once the cooldown has expired.
PAST_COOLDOWN = "2020-01-01T00:00:00+00:00"


def _item(index: int) -> dict:
    return {
        "url": f"https://hdencode.org/release/{index}",
        "title": f"Title {index}",
        "year": 2026,
        "season": None,
        "resolution": "2160p",
        "size": "20 GB",
        "hdr": "HDR",
        "dovi": True,
        "service_type": "Rapidgator",
    }


def _challenge_outcome() -> dict:
    return {
        "success": False,
        "method": "",
        "link_count": 0,
        "message": "Verification required.",
        "reason_code": "interactive_challenge",
        "cause_code": "interactive_challenge",
        "stage": "verification",
        "retryable": False,
        "retry_mode": "manual_verification",
        "cooldown_until": PAST_COOLDOWN,
        "transport_attempted": True,
        "affected_scope": "source",
        "action_code": "verification_required",
        "signals": [],
    }


class _Harness:
    """A real 2-item batch parked in paused_source by a source-wide denial."""

    def __init__(self, tmp_path, name: str):
        self.db = DatabaseManager(str(tmp_path / f"{name}.db"))
        self.events = []
        fake = MagicMock()
        fake.download_item.return_value = _challenge_outcome()
        self.service = DownloadQueueService(
            {},
            self.db,
            fake,
            broadcast=self.events.append,
            poll_seconds=0.01,
        )
        # The hdencode coordinator is process-global; pin it to "not blocked"
        # so a neighbouring test cannot decide this one's outcome.
        self.service._coordinator_snapshot = lambda: {}
        batch = self.service.schedule_batch(
            [_item(1), _item(2)],
            interval_minutes=0,
            mode="immediate",
            auto_resume_after_cooldown=True,
        )
        self.batch_uuid = batch["batch_uuid"]
        claimed = self.service._claim_due()
        assert claimed is not None
        self.service._execute(claimed)
        current = self.service.get_batch(self.batch_uuid)
        assert current["state"] == "paused_source"
        assert current["cooldown_until"] == PAST_COOLDOWN
        self.first_uuid = current["items"][0]["item_uuid"]
        self.second_uuid = current["items"][1]["item_uuid"]
        assert current["items"][0]["state"] == "verification_required"
        assert current["items"][1]["state"] == "waiting_source"
        self.events.clear()

    def state(self, item_uuid: str) -> str:
        return self.service.get_item(item_uuid)["state"]

    def batch(self) -> dict:
        return self.service.get_batch(self.batch_uuid)

    def close(self) -> None:
        self.db.close()


def test_retry_of_a_deferred_item_still_works(tmp_path):
    """POSITIVE CONTROL: the healthy retry path must keep working.

    A "fix" that rejects every retry, or that stops touching the batch at all,
    fails here.
    """
    h = _Harness(tmp_path, "positive")
    try:
        returned = h.service.retry_item(h.second_uuid)

        assert returned["state"] == "ready"
        assert returned["queue_reason"] == "manual_retry"
        assert returned["cooldown_until"] is None
        assert h.state(h.second_uuid) == "ready"

        batch = h.batch()
        assert batch["state"] == "scheduled"
        assert batch["cooldown_until"] is None

        assert [event["type"] for event in h.events] == ["download:queue_updated"]
        assert h.events[0]["data"]["item_uuid"] == h.second_uuid
    finally:
        h.close()


def test_retry_of_a_cancelled_item_is_rejected(tmp_path):
    h = _Harness(tmp_path, "cancelled")
    try:
        assert h.service.cancel_item(h.first_uuid) is True
        assert h.state(h.first_uuid) == "cancelled"
        h.events.clear()  # cancel_item legitimately emits its own event

        with pytest.raises(DownloadQueueError) as excinfo:
            h.service.retry_item(h.first_uuid)
        assert "cancelled" in str(excinfo.value)

        # The lying HTTP 200 this finding is about: the row must not have moved.
        assert h.state(h.first_uuid) == "cancelled"
        # No event, or the UI is told a retry happened that did not.
        assert h.events == []
    finally:
        h.close()


def test_rejected_retry_leaves_the_paused_batch_intact(tmp_path):
    h = _Harness(tmp_path, "batch-intact")
    try:
        assert h.service.cancel_item(h.first_uuid) is True
        before = h.batch()

        with pytest.raises(DownloadQueueError):
            h.service.retry_item(h.first_uuid)

        after = h.batch()
        assert after["state"] == "paused_source"
        assert after["cooldown_until"] == PAST_COOLDOWN
        assert after["auto_resume_used"] == 0
        assert after["auto_resume_after_cooldown"] == 1
        # The rollback must cover the whole transaction, not just the batch
        # state column -- _refresh_batch_locked also stamps updated_at.
        assert after["updated_at"] == before["updated_at"]
        assert h.state(h.second_uuid) == "waiting_source"
    finally:
        h.close()


def test_sibling_still_auto_resumes_after_a_rejected_retry(tmp_path):
    """The consumer of the batch row, not just the batch row itself.

    _maybe_auto_resume() selects on state='paused_source' AND a non-NULL
    cooldown_until that has expired. Clobbering either one strands the sibling
    permanently, so assert the sibling actually gets picked back up.
    """
    h = _Harness(tmp_path, "sibling")
    try:
        assert h.service.cancel_item(h.first_uuid) is True

        with pytest.raises(DownloadQueueError):
            h.service.retry_item(h.first_uuid)

        h.service._maybe_auto_resume()

        assert h.state(h.second_uuid) == "ready"
        assert h.batch()["state"] == "scheduled"
    finally:
        h.close()


def test_positive_control_sibling_auto_resumes_with_no_retry_at_all(tmp_path):
    """POSITIVE CONTROL for the test above.

    Proves the auto-resume assertion is discriminating: if auto-resume could
    never fire in this fixture, the sibling test would pass for the wrong
    reason.
    """
    h = _Harness(tmp_path, "sibling-control")
    try:
        assert h.service.cancel_item(h.first_uuid) is True
        h.service._maybe_auto_resume()
        assert h.state(h.second_uuid) == "ready"
    finally:
        h.close()


def test_retry_of_a_completed_item_is_rejected(tmp_path):
    h = _Harness(tmp_path, "completed")
    try:
        h.db._mutate(
            "UPDATE download_queue_items SET state = 'completed' WHERE item_uuid = ?",
            (h.first_uuid,),
            label="test_completed_state",
        )

        with pytest.raises(DownloadQueueError):
            h.service.retry_item(h.first_uuid)

        assert h.state(h.first_uuid) == "completed"
        assert h.batch()["state"] == "paused_source"
        assert h.batch()["cooldown_until"] == PAST_COOLDOWN
    finally:
        h.close()


def test_retry_of_a_claimed_item_reports_the_claim_conflict(tmp_path):
    """A claimed item is in-flight; retrying it must not reset it underneath
    the worker. cancel_item raises DownloadQueueItemClaimed here, and the
    route maps its detail() to a structured 409 -- so retry matches it.
    """
    h = _Harness(tmp_path, "claimed")
    try:
        h.db._mutate(
            """
            UPDATE download_queue_items
            SET state = 'claimed', claimed_by = 'other-worker'
            WHERE item_uuid = ?
            """,
            (h.first_uuid,),
            label="test_claimed_state",
        )

        with pytest.raises(DownloadQueueItemClaimed) as excinfo:
            h.service.retry_item(h.first_uuid)
        assert excinfo.value.detail()["code"] == "download_queue_item_claimed"

        assert h.state(h.first_uuid) == "claimed"
        assert h.batch()["state"] == "paused_source"
        assert h.batch()["cooldown_until"] == PAST_COOLDOWN
    finally:
        h.close()


def test_rejection_is_decided_by_the_update_not_by_the_pre_read(tmp_path):
    """DISAGREEING CASE.

    An implementation that gates on the state read BEFORE the transaction
    opens (rather than on the UPDATE's rowcount) passes every test above, but
    loses the race this asserts: the pre-read says 'failed' (retryable) while
    the committed row is 'cancelled'. Only the rowcount check rejects it.
    """
    h = _Harness(tmp_path, "stale-read")
    try:
        assert h.service.cancel_item(h.first_uuid) is True
        stale = {
            "item_uuid": h.first_uuid,
            "batch_uuid": h.batch_uuid,
            "source": "hdencode",
            "state": "failed",
        }
        h.service.get_item = lambda _uuid: dict(stale)

        with pytest.raises(DownloadQueueError):
            h.service.retry_item(h.first_uuid)

        row = h.db._query(
            "SELECT state FROM download_queue_items WHERE item_uuid = ?",
            (h.first_uuid,),
            one=True,
            default=None,
        )
        assert row["state"] == "cancelled"
        assert h.batch()["state"] == "paused_source"
        assert h.batch()["cooldown_until"] == PAST_COOLDOWN
    finally:
        h.close()


def test_a_stale_pre_read_does_not_block_a_retryable_item(tmp_path):
    """The other half of the disagreeing pair.

    Same stale-read setup, opposite direction: the pre-read says 'cancelled'
    while the committed row is 'waiting_source'. A Python-level check on the
    pre-read would refuse a retry the database would have accepted, so this
    fails any gate that is not the UPDATE itself.
    """
    h = _Harness(tmp_path, "stale-read-inverse")
    try:
        stale = {
            "item_uuid": h.second_uuid,
            "batch_uuid": h.batch_uuid,
            "source": "hdencode",
            "state": "cancelled",
        }
        h.service.get_item = lambda _uuid: dict(stale)

        h.service.retry_item(h.second_uuid)

        row = h.db._query(
            "SELECT state, queue_reason, cooldown_until "
            "FROM download_queue_items WHERE item_uuid = ?",
            (h.second_uuid,),
            one=True,
            default=None,
        )
        assert row["state"] == "ready"
        assert row["queue_reason"] == "manual_retry"
        assert row["cooldown_until"] is None
        assert h.batch()["state"] == "scheduled"
    finally:
        h.close()


def test_retry_of_a_missing_item_is_rejected(tmp_path):
    h = _Harness(tmp_path, "missing")
    try:
        with pytest.raises(DownloadQueueError):
            h.service.retry_item("no-such-item-uuid")
        assert h.batch()["state"] == "paused_source"
        assert h.batch()["cooldown_until"] == PAST_COOLDOWN
    finally:
        h.close()
