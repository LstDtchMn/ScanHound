"""Regression coverage for the persistent browser and durable download queue."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from backend.browser_adapter import browser_plan
from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService


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


def test_default_browser_is_standard_selenium_with_persistent_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANHOUND_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    plan = browser_plan({})
    assert plan.adapter == "selenium_chromium"
    assert plan.profile_mode == "persistent"
    assert plan.profile_dir == str(tmp_path / "profile")
    assert Path(plan.profile_dir).is_dir()


def test_uc_adapter_remains_an_explicit_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANHOUND_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    plan = browser_plan({"hdencode_browser_adapter": "uc_chromium"})
    assert plan.adapter == "uc_chromium"


def test_staggered_batch_is_durable_and_spaced(tmp_path):
    db = DatabaseManager(str(tmp_path / "queue.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock(), poll_seconds=0.01)
        batch = service.schedule_batch(
            [_item(1), _item(2), _item(3)],
            interval_minutes=10,
            mode="staggered",
        )
        rows = batch["items"]
        assert len(rows) == 3
        parsed = [
            datetime.fromisoformat(row["scheduled_for"]).astimezone(timezone.utc)
            for row in rows
        ]
        assert int((parsed[1] - parsed[0]).total_seconds()) == 600
        assert int((parsed[2] - parsed[1]).total_seconds()) == 600

        db.close()
        reopened = DatabaseManager(str(tmp_path / "queue.db"))
        try:
            persisted = reopened._query_dicts(
                "SELECT * FROM download_queue_items ORDER BY sequence_number"
            )
            assert len(persisted) == 3
        finally:
            reopened.close()
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_challenge_pauses_batch_and_retains_unattempted_items(tmp_path):
    db = DatabaseManager(str(tmp_path / "challenge.db"))
    fake = MagicMock()
    fake.download_item.return_value = {
        "success": False,
        "method": "",
        "link_count": 0,
        "message": "Verification required.",
        "reason_code": "interactive_challenge",
        "cause_code": "interactive_challenge",
        "stage": "verification",
        "retryable": False,
        "retry_mode": "manual_verification",
        "cooldown_until": "2099-01-01T00:00:00+00:00",
        "transport_attempted": True,
        "affected_scope": "source",
        "action_code": "verification_required",
        "signals": [],
    }
    try:
        service = DownloadQueueService({}, db, fake, poll_seconds=0.01)
        batch = service.schedule_batch(
            [_item(1), _item(2), _item(3)],
            interval_minutes=0,
            mode="immediate",
        )
        claimed = service._claim_due()
        assert claimed is not None
        service._execute(claimed)

        current = service.get_batch(batch["batch_uuid"])
        assert current["state"] == "paused_source"
        states = [row["state"] for row in current["items"]]
        assert states[0] == "verification_required"
        assert states[1:] == ["waiting_source", "waiting_source"]
        assert current["items"][1]["transport_attempted"] == 0
        assert current["items"][2]["transport_attempted"] == 0
        assert fake.download_item.call_count == 1
    finally:
        db.close()


def test_single_challenge_can_be_saved_for_manual_retry(tmp_path):
    db = DatabaseManager(str(tmp_path / "single.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock(), poll_seconds=0.01)
        saved = service.enqueue_retry(
            _item(1),
            {
                "reason_code": "interactive_challenge",
                "cause_code": "interactive_challenge",
                "message": "Verification required.",
                "cooldown_until": "2099-01-01T00:00:00+00:00",
                "transport_attempted": True,
            },
        )
        assert saved["state"] == "verification_required"
        assert saved["queue_reason"] == "interactive_challenge"
        assert service.list_retries()[0]["title"] == "Title 1"
    finally:
        db.close()


def test_raised_execution_error_does_not_leave_item_claimed(tmp_path):
    db = DatabaseManager(str(tmp_path / "raised.db"))
    fake = MagicMock()
    fake.download_item.side_effect = RuntimeError("private failure detail")
    try:
        service = DownloadQueueService({}, db, fake, poll_seconds=0.01)
        batch = service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        claimed = service._claim_due()
        assert claimed is not None

        service._execute(claimed)

        current = service.get_batch(batch["batch_uuid"])
        assert current["items"][0]["state"] == "failed"
        assert current["items"][0]["last_reason_code"] == "download_failed"
        assert current["items"][0]["claimed_by"] is None
        assert current["items"][0]["claim_expires_at"] is None
        assert "private failure detail" not in (
            current["items"][0]["last_message"] or ""
        )
    finally:
        db.close()


def test_jdownloader_success_preserves_post_delivery_callback(tmp_path):
    db = DatabaseManager(str(tmp_path / "delivery.db"))
    fake = MagicMock()
    fake.download_item.return_value = {
        "success": True,
        "method": "jdownloader",
        "link_count": 2,
        "message": "Sent to JDownloader.",
    }
    delivered = MagicMock()
    try:
        service = DownloadQueueService(
            {},
            db,
            fake,
            on_delivery=delivered,
            poll_seconds=0.01,
        )
        service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        claimed = service._claim_due()
        assert claimed is not None

        service._execute(claimed)

        delivered.assert_called_once_with()
        assert service.get_item(claimed["item_uuid"])["state"] == "completed"
    finally:
        db.close()
