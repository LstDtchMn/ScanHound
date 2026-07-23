"""Regression coverage for the persistent browser and durable download queue."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
import pytest

from backend.api.routes.downloads import remove_download_retry
from backend.api.ws import ConnectionManager
from backend.browser_adapter import (
    browser_plan,
    clear_stale_profile_locks,
    launch_browser,
    profile_lock_paths,
)
from backend.database import DatabaseManager
from backend.download_queue import (
    DownloadQueueItemClaimed,
    DownloadQueueService,
)


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


def test_actual_persistent_profile_stale_locks_are_removed_before_launch(
    tmp_path,
    monkeypatch,
):
    profile = tmp_path / "configured-profile"
    config = {
        "hdencode_browser_profile_mode": "persistent",
        "hdencode_browser_profile_dir": str(profile),
    }
    locks = profile_lock_paths(config)
    assert {path.name for path in locks} == {
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
    }
    for path in locks:
        path.write_text("dead-container-lock", encoding="utf-8")

    removed = clear_stale_profile_locks(config)
    assert set(removed) == {str(path) for path in locks}
    assert all(not path.exists() for path in locks)

    class FakeDriver:
        capabilities = {
            "browserName": "chrome",
            "browserVersion": "1.0",
            "chrome": {"chromedriverVersion": "1.0"},
        }

    import selenium.webdriver

    def fake_chrome(*, service, options):
        assert all(not path.exists() for path in locks)
        return FakeDriver()

    monkeypatch.setattr(selenium.webdriver, "Chrome", fake_chrome)
    driver, status = launch_browser(
        config,
        chrome_ver=None,
        chrome_bin=None,
        system_driver=None,
    )
    assert isinstance(driver, FakeDriver)
    assert status["profile_mode"] == "persistent"


def test_claimed_retry_cancel_is_rejected_with_typed_409(tmp_path):
    db = DatabaseManager(str(tmp_path / "claimed-cancel.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock())
        service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        claimed = service._claim_due()
        assert claimed is not None

        with pytest.raises(DownloadQueueItemClaimed):
            service.cancel_item(claimed["item_uuid"])

        reg = SimpleNamespace(download_queue=service)
        with pytest.raises(HTTPException) as captured:
            remove_download_retry(claimed["item_uuid"], reg=reg)
        assert captured.value.status_code == 409
        assert captured.value.detail["code"] == "download_queue_item_claimed"
        assert service.get_item(claimed["item_uuid"])["state"] == "claimed"
    finally:
        db.close()


def test_expired_owned_claim_fail_stops_without_automatic_redelivery(tmp_path):
    db = DatabaseManager(str(tmp_path / "expired-claim.db"))
    call_order = []
    fake = MagicMock()

    def flush(message):
        call_order.append(("flush", message))
        return True

    def fatal_exit(code):
        call_order.append(("exit", code))

    try:
        service = DownloadQueueService(
            {},
            db,
            fake,
            broadcast_flush=flush,
            fatal_exit=fatal_exit,
        )
        service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        claimed = service._claim_due()
        assert claimed is not None
        db._mutate(
            """
            UPDATE download_queue_items
            SET claim_expires_at = '2000-01-01T00:00:00+00:00'
            WHERE item_uuid = ?
            """,
            (claimed["item_uuid"],),
            label="expire_test_claim",
        )

        assert service._watchdog_tick() is True
        assert call_order[0][0] == "flush"
        assert call_order[0][1]["type"] == "notification"
        assert call_order[1] == ("exit", 70)
        current = service.get_item(claimed["item_uuid"])
        assert current["state"] == "failed"
        assert current["last_reason_code"] == "operation_timeout_unknown"
        assert current["queue_reason"] == "manual_retry"

        stale_success = {
            "success": True,
            "method": "jdownloader",
            "message": "Late completion",
        }
        assert service._complete(claimed, stale_success) is False
        assert service.get_item(claimed["item_uuid"])["state"] == "failed"
        assert fake.download_item.call_count == 0
    finally:
        db.close()


def test_mixed_batch_source_pause_only_defers_matching_source(tmp_path):
    db = DatabaseManager(str(tmp_path / "mixed-source.db"))
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
    items = [
        _item(1),
        _item(2),
        {**_item(3), "url": "https://ddlbase.com/release/3"},
        {**_item(4), "url": "https://adit-hd.com/threads/4"},
    ]
    try:
        service = DownloadQueueService({}, db, fake)
        batch = service.schedule_batch(
            items,
            interval_minutes=0,
            mode="immediate",
        )
        claimed = service._claim_due()
        assert claimed is not None
        assert claimed["source"] == "hdencode"
        service._execute(claimed)

        current = service.get_batch(batch["batch_uuid"])
        by_url = {row["canonical_url"]: row for row in current["items"]}
        assert by_url[items[1]["url"]]["state"] == "waiting_source"
        assert by_url[items[2]["url"]]["state"] == "scheduled"
        assert by_url[items[3]["url"]]["state"] == "scheduled"

        next_claim = service._claim_due()
        assert next_claim is not None
        assert next_claim["source"] == "ddlbase"
        assert fake.download_item.call_count == 1
    finally:
        db.close()


def test_auto_resume_scopes_source_and_preserves_unknown_outcome(tmp_path):
    db = DatabaseManager(str(tmp_path / "auto-resume-poison.db"))
    fake = MagicMock()
    items = [
        _item(1),
        _item(2),
        {**_item(3), "url": "https://ddlbase.com/release/3"},
    ]
    pause_stamp = "2026-07-23T12:00:00+00:00"
    expired_cooldown = "2000-01-01T00:00:00+00:00"
    try:
        service = DownloadQueueService({}, db, fake)
        service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
        batch = service.schedule_batch(
            items,
            interval_minutes=0,
            mode="immediate",
            auto_resume_after_cooldown=True,
        )
        current = service.get_batch(batch["batch_uuid"])
        hdencode_rows = [row for row in current["items"] if row["source"] == "hdencode"]
        ddlbase_row = next(row for row in current["items"] if row["source"] == "ddlbase")

        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'verification_required',
                    queue_reason = 'interactive_challenge',
                    cooldown_until = ?,
                    last_reason_code = 'interactive_challenge',
                    updated_at = ?
                WHERE item_uuid = ?
                """,
                (expired_cooldown, pause_stamp, hdencode_rows[0]["item_uuid"]),
            )
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'waiting_source',
                    queue_reason = 'source_deferred',
                    cooldown_until = ?,
                    last_reason_code = 'source_temporarily_blocked',
                    updated_at = ?
                WHERE item_uuid = ?
                """,
                (expired_cooldown, pause_stamp, hdencode_rows[1]["item_uuid"]),
            )
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'failed',
                    queue_reason = 'manual_retry',
                    last_reason_code = 'operation_timeout_unknown',
                    transport_attempted = 1,
                    updated_at = ?
                WHERE item_uuid = ?
                """,
                (pause_stamp, ddlbase_row["item_uuid"]),
            )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'paused_source',
                    paused_at = ?,
                    cooldown_until = ?,
                    last_reason_code = 'interactive_challenge',
                    auto_resume_after_cooldown = 1,
                    auto_resume_used = 0
                WHERE batch_uuid = ?
                """,
                (pause_stamp, expired_cooldown, batch["batch_uuid"]),
            )
            service._refresh_batch_locked(conn, batch["batch_uuid"], pause_stamp)

        service._maybe_auto_resume()

        current = service.get_batch(batch["batch_uuid"])
        by_uuid = {row["item_uuid"]: row for row in current["items"]}
        assert by_uuid[hdencode_rows[0]["item_uuid"]]["state"] == "ready"
        assert by_uuid[hdencode_rows[1]["item_uuid"]]["state"] == "ready"
        poison = by_uuid[ddlbase_row["item_uuid"]]
        assert poison["state"] == "failed"
        assert poison["last_reason_code"] == "operation_timeout_unknown"
        assert current["auto_resume_used"] == 1
        fake.download_item.assert_not_called()
    finally:
        db.close()


def test_null_cooldown_batch_does_not_auto_resume(tmp_path):
    db = DatabaseManager(str(tmp_path / "null-cooldown.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock())
        service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
        batch = service.schedule_batch(
            [_item(1)],
            interval_minutes=0,
            mode="immediate",
            auto_resume_after_cooldown=True,
        )
        item_uuid = batch["items"][0]["item_uuid"]
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'waiting_source',
                    queue_reason = 'source_deferred',
                    cooldown_until = NULL,
                    last_reason_code = 'source_temporarily_blocked'
                WHERE item_uuid = ?
                """,
                (item_uuid,),
            )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'paused_source', cooldown_until = NULL,
                    auto_resume_after_cooldown = 1, auto_resume_used = 0
                WHERE batch_uuid = ?
                """,
                (batch["batch_uuid"],),
            )

        service._maybe_auto_resume()

        assert service.get_item(item_uuid)["state"] == "waiting_source"
        assert service.get_batch(batch["batch_uuid"])["auto_resume_used"] == 0
    finally:
        db.close()


def test_claim_lease_config_override_is_written_to_claim(tmp_path):
    db = DatabaseManager(str(tmp_path / "lease-config.db"))
    try:
        service = DownloadQueueService(
            {"download_queue_claim_lease_seconds": 123},
            db,
            MagicMock(),
        )
        service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        claimed = service._claim_due()
        assert claimed is not None
        current = service.get_item(claimed["item_uuid"])
        attempted = datetime.fromisoformat(current["last_attempt_at"])
        expires = datetime.fromisoformat(current["claim_expires_at"])
        assert 122 <= (expires - attempted).total_seconds() <= 124
    finally:
        db.close()


def test_claim_due_rejects_poison_until_manual_retry(tmp_path):
    db = DatabaseManager(str(tmp_path / "claim-poison.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock())
        service._assert_hdencode_available = MagicMock()
        batch = service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        item_uuid = batch["items"][0]["item_uuid"]
        db._mutate(
            """
            UPDATE download_queue_items
            SET state = 'ready',
                last_reason_code = 'operation_timeout_unknown'
            WHERE item_uuid = ?
            """,
            (item_uuid,),
            label="poison_claim_guard",
        )

        assert service._claim_due() is None
        retried = service.retry_item(item_uuid)
        assert retried["last_reason_code"] is None
        assert service._claim_due() is not None
    finally:
        db.close()


def test_manual_retry_refreshes_batch_counters(tmp_path):
    db = DatabaseManager(str(tmp_path / "retry-counters.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock())
        service._assert_hdencode_available = MagicMock()
        batch = service.schedule_batch([_item(1)], interval_minutes=0, mode="immediate")
        item_uuid = batch["items"][0]["item_uuid"]
        now = "2026-07-23T12:00:00+00:00"
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'failed', last_reason_code = 'download_failed'
                WHERE item_uuid = ?
                """,
                (item_uuid,),
            )
            service._refresh_batch_locked(conn, batch["batch_uuid"], now)
        assert service.get_batch(batch["batch_uuid"])["failed_items"] == 1

        service.retry_item(item_uuid)

        refreshed = service.get_batch(batch["batch_uuid"])
        assert refreshed["failed_items"] == 0
        assert refreshed["items"][0]["state"] == "ready"
    finally:
        db.close()


def test_websocket_sync_wait_flushes_before_return():
    manager = ConnectionManager()
    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    messages = []

    class FakeWebSocket:
        async def accept(self):
            return None

        async def send_json(self, message):
            messages.append(message)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop_ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=2)
    ws = FakeWebSocket()
    try:
        manager.set_loop(loop)
        asyncio.run_coroutine_threadsafe(manager.connect(ws), loop).result(timeout=2)
        payload = {"type": "notification", "data": {"title": "Timed out"}}
        assert manager.broadcast_sync_wait(payload, timeout=1.0) is True
        assert messages[-1] == payload
    finally:
        try:
            asyncio.run_coroutine_threadsafe(manager.disconnect(ws), loop).result(timeout=2)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
