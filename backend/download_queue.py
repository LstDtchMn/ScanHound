"""Durable download queue, CAPTCHA retry list, and staggered scheduler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlparse
import uuid

from backend.download_outcome import (
    is_source_wide_denial,
    notification_for_result,
    public_download_result,
)
from backend.hdencode_coordinator import get_hdencode_coordinator


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _utcnow()).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


from backend.source_identity import source_kind as _source_kind


#: Queue-row source values, mapped from the shared identity kinds. The queue stores
#: `filehost` where the shared module says `direct_file`, because that string is
#: already in `download_queue_items.source` on this branch and the active unique
#: index `(source, canonical_url, service_type)` is built on it.
_KIND_TO_QUEUE_SOURCE = {
    "hdencode": "hdencode",
    "ddlbase": "ddlbase",
    "adithd": "adithd",
    "direct_file": "filehost",
    "other": "other",
}


def _source(url: str, hdencode_host: str = "hdencode.org") -> str:
    """The durable queue row's source, from the ONE shared classifier.

    UNIFIED 2026-08-07 on peer review. This function and
    `download_service._source_page_kind()` were deciding the same question
    independently, and had already drifted: both originally defaulted everything
    that was not DDLBase or Adit-HD to "hdencode", round 4 fixed only this one, and
    their host lists differed as well. Two registries answering one question is how
    that happened, so both now call `backend.source_identity.source_kind`.

    What the old default cost: Rapidgator, 1fichier, Nitroflare, ddownload and any
    future host were stored as `source="hdencode"`, so a direct-host row could be
    grouped under an HDEncode pause and -- once the retry refund worked -- could
    refund HDEncode's budget. My own mixed-batch test could not catch it because it
    used DDLBase, one of the only two hosts the old function actually recognised.
    """
    return _KIND_TO_QUEUE_SOURCE.get(
        _source_kind(url, hdencode_host), "other")


class DownloadQueueError(RuntimeError):
    pass


class DownloadQueueConflict(DownloadQueueError):
    """The request conflicts with an already-active durable queue item."""


class DownloadQueueUnavailable(DownloadQueueError):
    """The durable queue cannot accept work because a dependency is absent."""


class DownloadQueueSourceHeld(DownloadQueueError):
    def __init__(self, *, reason_code: str, cooldown_until: Optional[str]):
        super().__init__("The source is temporarily paused.")
        self.reason_code = reason_code
        self.cooldown_until = cooldown_until

    def detail(self) -> dict:
        return {
            "code": "source_temporarily_blocked",
            "cause_code": self.reason_code,
            "cooldown_until": self.cooldown_until,
            "transport_attempted": False,
            "message": "The source is temporarily paused; no request was made.",
        }


class DownloadQueueItemClaimed(DownloadQueueError):
    """Cancellation was rejected because transport may already be active."""

    def __init__(self, *, item_uuid: Optional[str] = None, batch_uuid: Optional[str] = None):
        super().__init__("An active queue operation cannot be removed safely.")
        self.item_uuid = item_uuid
        self.batch_uuid = batch_uuid

    def detail(self) -> dict:
        return {
            "code": "download_queue_item_claimed",
            "item_uuid": self.item_uuid,
            "batch_uuid": self.batch_uuid,
            "retryable": True,
            "message": (
                "This item is already being processed. Wait for it to finish "
                "before removing or retrying it."
            ),
        }


class DownloadQueueService:
    """One restart-safe worker for scheduled link retrieval and verification retries."""

    def __init__(
        self,
        config: Dict[str, Any],
        db,
        download_service,
        *,
        broadcast: Optional[Callable[[dict], None]] = None,
        broadcast_flush: Optional[Callable[[dict], bool]] = None,
        on_delivery: Optional[Callable[[], None]] = None,
        poll_seconds: float = 2.0,
        claim_lease_seconds: Optional[float] = None,
        watchdog_poll_seconds: float = 2.0,
        fatal_exit: Optional[Callable[[int], None]] = None,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.db = db
        self.download = download_service
        self.broadcast = broadcast or (lambda _event: None)
        self.broadcast_flush = broadcast_flush
        self.on_delivery = on_delivery or (lambda: None)
        self.poll_seconds = max(0.2, float(poll_seconds))
        configured_lease = (
            claim_lease_seconds
            if claim_lease_seconds is not None
            else self.config.get("download_queue_claim_lease_seconds", 600)
        )
        try:
            lease_seconds = float(configured_lease)
        except (TypeError, ValueError):
            lease_seconds = 600.0
        self.claim_lease_seconds = max(60.0, min(7200.0, lease_seconds))
        self.watchdog_poll_seconds = max(0.05, float(watchdog_poll_seconds))
        self._fatal_exit = fatal_exit or os._exit
        self.worker_id = str(uuid.uuid4())
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._fatal_recovery_started = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self.recover_interrupted()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._fatal_recovery_started.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="download-queue",
            daemon=True,
        )
        self._watchdog_thread = threading.Thread(
            target=self._watchdog,
            name="download-queue-watchdog",
            daemon=True,
        )
        self._thread.start()
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in (self._thread, self._watchdog_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5)

    def _emit(self, event_type: str, data: dict) -> None:
        try:
            self.broadcast({"type": event_type, "data": data})
        except Exception:
            logger.debug("download queue broadcast failed", exc_info=True)

    def _emit_flush(self, event_type: str, data: dict) -> bool:
        message = {"type": event_type, "data": data}
        if self.broadcast_flush is None:
            self._emit(event_type, data)
            return False
        try:
            return bool(self.broadcast_flush(message))
        except Exception:
            logger.warning("download queue synchronous broadcast failed", exc_info=True)
            return False

    def _emit_batch_progress(
        self,
        batch_uuid: str,
        *,
        current_title: str = "",
    ) -> None:
        """Broadcast aggregate durable-batch progress for the existing UI bar."""
        batch = self.get_batch(batch_uuid)
        if not batch:
            return
        items = batch.get("items") or []
        total = len(items)
        if total <= 0:
            return
        terminal = {"completed", "failed", "cancelled"}
        completed = sum(1 for item in items if item.get("state") in terminal)
        self._emit(
            "download:batch_progress",
            {
                "batch_uuid": batch_uuid,
                "completed": completed,
                "total": total,
                "current_title": current_title,
                "state": batch.get("state"),
            },
        )

    def recover_interrupted(self) -> None:
        if self.db is None:
            return
        now = _utcnow()
        with self.db.transaction() as conn:
            if not conn:
                return
            interrupted_batches = conn.execute(
                """
                SELECT DISTINCT batch_uuid
                FROM download_queue_items
                WHERE state = 'claimed'
                """
            ).fetchall()
            # A process exit can occur after an external delivery but before
            # the queue row commits. Never auto-redeliver that unknown outcome.
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'failed',
                    queue_reason = 'manual_retry',
                    last_reason_code = 'interrupted_unknown_outcome',
                    last_message = ?,
                    transport_attempted = 1,
                    claimed_by = NULL,
                    claim_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'claimed'
                """,
                (
                    "The previous process stopped during this operation. "
                    "Review JDownloader before retrying to avoid a duplicate.",
                    _iso(now),
                ),
            )
            for row in interrupted_batches:
                self._refresh_batch_locked(conn, row["batch_uuid"], _iso(now))
            # Re-space overdue scheduled items by batch. This prevents a burst
            # after a long container outage.
            rows = conn.execute(
                """
                SELECT batch_uuid, interval_seconds
                FROM download_queue_batches
                WHERE state IN ('scheduled', 'running')
                """
            ).fetchall()
            for row in rows:
                interval = max(0, int(row["interval_seconds"] or 0))
                due = conn.execute(
                    """
                    SELECT item_uuid
                    FROM download_queue_items
                    WHERE batch_uuid = ?
                      AND state = 'scheduled'
                      AND (scheduled_for IS NULL OR scheduled_for <= ?)
                    ORDER BY sequence_number
                    """,
                    (row["batch_uuid"], _iso(now)),
                ).fetchall()
                cursor = now + timedelta(seconds=30)
                for item in due:
                    conn.execute(
                        """
                        UPDATE download_queue_items
                        SET scheduled_for = ?, updated_at = ?
                        WHERE item_uuid = ?
                        """,
                        (_iso(cursor), _iso(now), item["item_uuid"]),
                    )
                    cursor += timedelta(seconds=interval)

    def _coordinator_snapshot(self) -> dict:
        return get_hdencode_coordinator().snapshot()

    def _assert_hdencode_available(self) -> None:
        snapshot = self._coordinator_snapshot()
        if snapshot.get("blocked"):
            raise DownloadQueueSourceHeld(
                reason_code=str(snapshot.get("reason_code") or "cooldown"),
                cooldown_until=snapshot.get("cooldown_until"),
            )

    @staticmethod
    def _request_dict(item: dict) -> dict:
        return {
            "url": item.get("url") or item.get("canonical_url") or "",
            "title": item.get("title") or "Untitled",
            "year": item.get("year"),
            "season": item.get("season"),
            "resolution": item.get("resolution") or "",
            "size": item.get("size") or item.get("size_text") or "",
            "hdr": item.get("hdr") or "",
            "dovi": bool(item.get("dovi")),
            "service_type": item.get("service_type") or "Rapidgator",
        }

    def schedule_batch(
        self,
        items: Iterable[dict],
        *,
        interval_minutes: int,
        mode: str = "staggered",
        auto_resume_after_cooldown: bool = False,
    ) -> dict:
        if self.db is None or self.download is None:
            raise DownloadQueueUnavailable("The download queue is unavailable.")
        interval = max(0, min(120, int(interval_minutes))) * 60
        mode = "immediate" if interval == 0 or mode == "immediate" else "staggered"
        batch_uuid = str(uuid.uuid4())
        now = _utcnow()
        unique: list[dict] = []
        seen = set()
        for raw in items:
            item = self._request_dict(dict(raw))
            source = _source(item["url"], self._hdencode_host())
            key = (source, item["url"], item["service_type"])
            if not item["url"] or key in seen:
                continue
            seen.add(key)
            item["source"] = source
            unique.append(item)
        if not unique:
            raise DownloadQueueError("No unique download items were provided.")

        inserted = 0
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueUnavailable("The database is unavailable.")
            active_rows = conn.execute(
                """
                SELECT source, canonical_url, service_type
                FROM download_queue_items
                WHERE state IN (
                    'scheduled', 'waiting_source', 'verification_required',
                    'ready', 'claimed'
                )
                """
            ).fetchall()
            active_keys = {
                (
                    row["source"],
                    row["canonical_url"],
                    row["service_type"],
                )
                for row in active_rows
            }
            pending = [
                item
                for item in unique
                if (
                    item["source"],
                    item["url"],
                    item["service_type"],
                ) not in active_keys
            ]
            if not pending:
                raise DownloadQueueConflict(
                    "Every selected item is already active in the download queue."
                )
            conn.execute(
                """
                INSERT INTO download_queue_batches (
                    batch_uuid, mode, interval_seconds, state, source,
                    total_items, auto_resume_after_cooldown,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)
                """,
                (
                    batch_uuid,
                    mode,
                    interval,
                    pending[0]["source"] if len({i["source"] for i in pending}) == 1 else "mixed",
                    len(pending),
                    1 if auto_resume_after_cooldown else 0,
                    _iso(now),
                    _iso(now),
                ),
            )
            for index, item in enumerate(pending):
                scheduled = now + timedelta(seconds=interval * index)
                item_uuid = str(uuid.uuid4())
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO download_queue_items (
                        item_uuid, batch_uuid, sequence_number, source,
                        canonical_url, title, year, season, resolution,
                        size_text, hdr, dovi, service_type, queue_reason,
                        state, scheduled_for, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'user_batch', 'scheduled', ?, ?, ?
                    )
                    """,
                    (
                        item_uuid,
                        batch_uuid,
                        index,
                        item["source"],
                        item["url"],
                        item["title"],
                        item["year"],
                        item["season"],
                        item["resolution"],
                        item["size"],
                        item["hdr"],
                        1 if item["dovi"] else 0,
                        item["service_type"],
                        _iso(scheduled),
                        _iso(now),
                        _iso(now),
                    ),
                )
                inserted += max(0, int(cursor.rowcount or 0))
            if inserted == 0:
                conn.execute(
                    "DELETE FROM download_queue_batches WHERE batch_uuid = ?",
                    (batch_uuid,),
                )
                raise DownloadQueueConflict(
                    "Every selected item is already active in the download queue."
                )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET total_items = ?, updated_at = ?
                WHERE batch_uuid = ?
                """,
                (inserted, _iso(now), batch_uuid),
            )

        self._wake.set()
        payload = self.get_batch(batch_uuid) or {
            "batch_uuid": batch_uuid,
            "count": inserted,
        }
        self._emit("download:batch_schedule", payload)
        self._emit_batch_progress(batch_uuid)
        return payload

    def enqueue_retry(self, request: Any, outcome: dict) -> dict:
        data = (
            request.model_dump()
            if hasattr(request, "model_dump")
            else dict(request)
        )
        item = self._request_dict(data)
        source = _source(item["url"], self._hdencode_host())
        reason = str(outcome.get("reason_code") or "")
        direct = reason == "interactive_challenge" or bool(outcome.get("transport_attempted"))
        state = "verification_required" if direct else "waiting_source"
        queue_reason = "interactive_challenge" if direct else "source_deferred"
        now = _iso()
        batch_uuid = str(uuid.uuid4())
        item_uuid = str(uuid.uuid4())
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            existing = conn.execute(
                """
                SELECT *
                FROM download_queue_items
                WHERE source = ? AND canonical_url = ? AND service_type = ?
                  AND state IN (
                      'scheduled', 'waiting_source', 'verification_required',
                      'ready', 'claimed'
                  )
                """,
                (source, item["url"], item["service_type"]),
            ).fetchone()
            if existing:
                return dict(existing)
            conn.execute(
                """
                INSERT INTO download_queue_batches (
                    batch_uuid, mode, interval_seconds, state, source,
                    total_items, deferred_items, created_at, updated_at,
                    paused_at, cooldown_until, last_reason_code,
                    last_cause_code, verification_hold_source
                ) VALUES (
                    ?, 'verification_retry', 0, 'paused_source', ?, 1, 1,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    batch_uuid,
                    source,
                    now,
                    now,
                    now,
                    outcome.get("cooldown_until"),
                    reason,
                    outcome.get("cause_code"),
                    # A challenge row is born under the hold: a timer must not
                    # release it (see queue_recovery_policy.VERIFICATION_HOLD).
                    source if queue_reason == "interactive_challenge" else None,
                ),
            )
            conn.execute(
                """
                INSERT INTO download_queue_items (
                    item_uuid, batch_uuid, sequence_number, source,
                    canonical_url, title, year, season, resolution,
                    size_text, hdr, dovi, service_type, queue_reason,
                    state, cooldown_until, attempt_count, last_attempt_at,
                    last_reason_code, last_cause_code, last_message,
                    transport_attempted, created_at, updated_at
                ) VALUES (
                    ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    item_uuid,
                    batch_uuid,
                    source,
                    item["url"],
                    item["title"],
                    item["year"],
                    item["season"],
                    item["resolution"],
                    item["size"],
                    item["hdr"],
                    1 if item["dovi"] else 0,
                    item["service_type"],
                    queue_reason,
                    state,
                    outcome.get("cooldown_until"),
                    now,
                    reason,
                    outcome.get("cause_code"),
                    outcome.get("message"),
                    1 if outcome.get("transport_attempted") else 0,
                    now,
                    now,
                ),
            )
        row = self.get_item(item_uuid) or {"item_uuid": item_uuid}
        self._emit("download:retry_required", row)
        return row

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_auto_resume()
                item = self._claim_due()
                if item is not None:
                    self._execute(item)
                    continue
            except Exception:
                logger.exception("download queue worker iteration failed")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _watchdog(self) -> None:
        while not self._stop.wait(self.watchdog_poll_seconds):
            try:
                if self._watchdog_tick():
                    return
            except Exception:
                logger.exception("download queue watchdog iteration failed")

    def _watchdog_tick(self) -> bool:
        """Fail-stop one expired owned claim; never start a second claimant.

        ScanHound has one queue worker in one process. If that worker exceeds
        its lease, the outcome of any external handoff is unknowable. The safe
        recovery is to persist a manual-review failure, then terminate the
        stuck process so Docker's restart policy can rebuild the worker. The
        row is not rescheduled automatically.
        """
        if self._fatal_recovery_started.is_set():
            return False
        recovered = self._recover_expired_claim()
        if recovered is None:
            return False
        self._fatal_recovery_started.set()
        self._emit(
            "download:queue_updated",
            {**recovered, "state": "failed"},
        )
        self._emit_flush(
            "notification",
            {
                "title": "Download queue operation timed out",
                "body": recovered["last_message"],
                "priority": "high",
                "reason_code": recovered["last_reason_code"],
                "item_uuid": recovered["item_uuid"],
            },
        )
        logger.critical(
            "download queue claim %s exceeded its lease; exiting for supervised restart",
            recovered["item_uuid"],
        )
        self._fatal_exit(70)
        return True

    def _recover_expired_claim(self) -> Optional[dict]:
        if self.db is None:
            return None
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return None
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM download_queue_items
                WHERE state = 'claimed'
                  AND claimed_by = ?
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at <= ?
                ORDER BY claim_expires_at
                LIMIT 1
                """,
                (self.worker_id, now),
            ).fetchone()
            if row is None:
                return None
            message = (
                "The queue operation exceeded its safety lease. Its delivery "
                "outcome is unknown; review JDownloader before retrying."
            )
            updated = conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'failed',
                    queue_reason = 'manual_retry',
                    last_reason_code = 'operation_timeout_unknown',
                    last_message = ?,
                    transport_attempted = 1,
                    claimed_by = NULL,
                    claim_expires_at = NULL,
                    updated_at = ?
                WHERE item_uuid = ?
                  AND state = 'claimed'
                  AND claimed_by = ?
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at <= ?
                """,
                (
                    message,
                    now,
                    row["item_uuid"],
                    self.worker_id,
                    now,
                ),
            ).rowcount
            if updated != 1:
                return None
            self._refresh_batch_locked(conn, row["batch_uuid"], now)
            recovered = dict(row)
            recovered.update(
                {
                    "state": "failed",
                    "queue_reason": "manual_retry",
                    "last_reason_code": "operation_timeout_unknown",
                    "last_message": message,
                    "transport_attempted": 1,
                    "claimed_by": None,
                    "claim_expires_at": None,
                    "updated_at": now,
                }
            )
            return recovered

    def _claim_due(self) -> Optional[dict]:
        if self.db is None:
            return None
        now = _iso()
        lease = _iso(
            _utcnow() + timedelta(seconds=self.claim_lease_seconds)
        )
        with self.db.transaction() as conn:
            if not conn:
                return None
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM download_queue_items
                WHERE state IN ('scheduled', 'ready')
                  AND scheduled_for IS NOT NULL
                  AND scheduled_for <= ?
                  AND COALESCE(last_reason_code, '') NOT IN (
                      'operation_timeout_unknown',
                      'interrupted_unknown_outcome'
                  )
                ORDER BY scheduled_for, sequence_number
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'claimed',
                    claimed_by = ?,
                    claim_expires_at = ?,
                    last_attempt_at = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE item_uuid = ?
                  AND state IN ('scheduled', 'ready')
                  AND COALESCE(last_reason_code, '') NOT IN (
                      'operation_timeout_unknown',
                      'interrupted_unknown_outcome'
                  )
                """,
                (
                    self.worker_id,
                    lease,
                    now,
                    now,
                    row["item_uuid"],
                ),
            ).rowcount
            if updated != 1:
                return None
            claimed = dict(row)
            claimed["state"] = "claimed"
            claimed["attempt_count"] = int(row["attempt_count"] or 0) + 1
            return claimed

    def _execute(self, item: dict) -> None:
        self._emit("download:queue_updated", {**item, "state": "claimed"})
        self._emit_batch_progress(
            item["batch_uuid"],
            current_title=item.get("title") or "",
        )

        def progress(event: str, data: dict) -> None:
            self._emit(event, data)

        try:
            result = self.download.download_item(
                url=item["canonical_url"],
                title=item["title"],
                year=item.get("year"),
                season=item.get("season"),
                resolution=item.get("resolution") or "",
                size=item.get("size_text") or "",
                hdr=item.get("hdr") or "",
                dovi=bool(item.get("dovi")),
                service_type=item.get("service_type") or "Rapidgator",
                progress_callback=progress,
            )
            outcome = public_download_result(
                result,
                title=item["title"],
                url=item["canonical_url"],
            )
        except Exception:
            # A raised execution error must never strand the durable row in
            # 'claimed' until the next container restart.
            logger.exception(
                "queued download execution failed for item %s",
                item.get("item_uuid"),
            )
            outcome = public_download_result(
                {
                    "success": False,
                    "method": "",
                    "link_count": 0,
                    "message": "The queued download could not be completed.",
                    "reason_code": "download_failed",
                    "stage": "download",
                    "retryable": True,
                    "retry_mode": "manual",
                    "transport_attempted": True,
                    "affected_scope": "item",
                    "action_code": "retry",
                    "signals": [],
                },
                title=item["title"],
                url=item["canonical_url"],
            )

        if outcome.get("success"):
            if not self._complete(item, outcome):
                return
            self._emit("download:result", outcome)
            method = outcome.get("method")
            message = outcome.get("message") or f"Sent: {item['title']}"
            if method in ("duplicate", "duplicate_similar"):
                notification = {
                    "title": "Already grabbed",
                    "body": message,
                    "priority": "normal",
                }
            elif method == "jdownloader":
                try:
                    self.on_delivery()
                except Exception:
                    logger.debug(
                        "queued post-delivery callback failed",
                        exc_info=True,
                    )
                notification = {
                    "title": "Download",
                    "body": message,
                    "priority": "normal",
                }
            else:
                notification = {
                    "title": "Download",
                    "body": (
                        f"{message} (not sent to JDownloader — method: {method})"
                    ),
                    "priority": "warning",
                }
            self._emit("notification", notification)
            return

        if is_source_wide_denial(outcome):
            if not self._pause_for_source(item, outcome):
                return
            self._emit("download:result", outcome)
            self._emit(
                "notification",
                notification_for_result(outcome, title=item["title"]),
            )
            return

        if not self._fail(item, outcome):
            return
        self._emit("download:result", outcome)
        self._emit(
            "notification",
            notification_for_result(outcome, title=item["title"]),
        )

    def _complete(self, item: dict, outcome: dict) -> bool:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return False
            updated = conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'completed',
                    completed_at = ?,
                    updated_at = ?,
                    last_reason_code = NULL,
                    last_cause_code = NULL,
                    last_message = ?,
                    transport_attempted = 1,
                    claimed_by = NULL,
                    claim_expires_at = NULL
                WHERE item_uuid = ?
                  AND state = 'claimed'
                  AND claimed_by = ?
                """,
                (
                    now,
                    now,
                    outcome.get("message"),
                    item["item_uuid"],
                    self.worker_id,
                ),
            ).rowcount
            if updated != 1:
                logger.warning(
                    "ignored stale completion for queue item %s",
                    item.get("item_uuid"),
                )
                return False
            # REAL SOURCE PROGRESS, recorded separately from 'completed'.
            # A pre-scrape dedup returns success without contacting the source, so
            # counting completions would refund retry budget the source never
            # earned. See is_source_delivery().
            self._release_verification_hold(conn, item, outcome)
            # SOURCE OWNERSHIP, added 2026-08-07 on peer review. schedule_batch
            # permits mixed-source batches (it labels them "mixed"), and
            # _claim_due does not require the parent batch to be scheduled, so a
            # DDLBase or Adit-HD item can complete while the batch is paused for
            # HDEncode. A batch-global counter therefore let another source refund
            # HDEncode's retry budget. Only work from the owning source counts.
            if (self.is_source_delivery(outcome)
                    and str(item.get("source") or "") == self.AUTO_RESUME_SOURCE):
                conn.execute(
                    "UPDATE download_queue_batches "
                    "SET source_delivery_count = source_delivery_count + 1 "
                    "WHERE batch_uuid = ?",
                    (item["batch_uuid"],),
                )
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        self._emit(
            "download:queue_updated",
            {**item, **outcome, "state": "completed"},
        )
        self._emit_batch_progress(item["batch_uuid"])
        return True

    def _release_verification_hold(self, conn, item: dict, outcome: dict) -> None:
        """Clear the verification hold for a source that just served its reveal.

        THE ONLY RELEASE OF A VERIFICATION HOLD. Keyed on
        `source_reveal_succeeded` (round-2 review, finding 6): the hold owns
        SOURCE accessibility, not downstream delivery. Once HDEncode serves the
        file-host links, its verification has demonstrably cleared for OUR
        session — a human passing it in a different browser proves nothing, and
        a later JDownloader failure is an item/delivery problem recorded
        separately. So this fires from BOTH _complete (delivery also succeeded)
        and _fail (reveal succeeded, delivery did not). A pre-scrape dedup and a
        pasted direct link both leave the flag False, so neither can release it.

        SOURCE-WIDE and source-matched in SQL: one affirmative reveal clears the
        hold for every batch of that source, and a DDLBase reveal never releases
        an HDEncode hold.
        """
        if outcome.get("source_reveal_succeeded"):
            conn.execute(
                "UPDATE download_queue_batches "
                "SET verification_hold_source = NULL "
                "WHERE verification_hold_source = ?",
                (str(item.get("source") or ""),),
            )

    def _fail(self, item: dict, outcome: dict) -> bool:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return False
            updated = conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'failed',
                    updated_at = ?,
                    last_reason_code = ?,
                    last_cause_code = ?,
                    last_message = ?,
                    transport_attempted = ?,
                    claimed_by = NULL,
                    claim_expires_at = NULL
                WHERE item_uuid = ?
                  AND state = 'claimed'
                  AND claimed_by = ?
                """,
                (
                    now,
                    outcome.get("reason_code"),
                    outcome.get("cause_code"),
                    outcome.get("message"),
                    1 if outcome.get("transport_attempted") else 0,
                    item["item_uuid"],
                    self.worker_id,
                ),
            ).rowcount
            if updated != 1:
                logger.warning(
                    "ignored stale failure for queue item %s",
                    item.get("item_uuid"),
                )
                return False
            # A JDownloader hand-off can fail AFTER the reveal served links; that
            # reveal success still clears the source's verification hold.
            self._release_verification_hold(conn, item, outcome)
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        self._emit(
            "download:queue_updated",
            {**item, **outcome, "state": "failed"},
        )
        self._emit_batch_progress(item["batch_uuid"])
        return True

    def _pause_for_source(self, item: dict, outcome: dict) -> bool:
        now = _iso()
        direct = outcome.get("reason_code") == "interactive_challenge"
        item_state = "verification_required" if direct else "waiting_source"
        item_reason = "interactive_challenge" if direct else "source_deferred"
        with self.db.transaction() as conn:
            if not conn:
                return False
            transitioned = conn.execute(
                """
                UPDATE download_queue_items
                SET state = ?, queue_reason = ?, cooldown_until = ?,
                    last_reason_code = ?, last_cause_code = ?,
                    last_message = ?, transport_attempted = ?,
                    claimed_by = NULL, claim_expires_at = NULL,
                    updated_at = ?
                WHERE item_uuid = ?
                  AND state = 'claimed'
                  AND claimed_by = ?
                """,
                (
                    item_state,
                    item_reason,
                    outcome.get("cooldown_until"),
                    outcome.get("reason_code"),
                    outcome.get("cause_code"),
                    outcome.get("message"),
                    1 if outcome.get("transport_attempted") else 0,
                    now,
                    item["item_uuid"],
                    self.worker_id,
                ),
            ).rowcount
            if transitioned != 1:
                logger.warning(
                    "ignored stale source pause for queue item %s",
                    item.get("item_uuid"),
                )
                return False
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'waiting_source',
                    queue_reason = 'source_deferred',
                    cooldown_until = ?,
                    last_reason_code = 'source_temporarily_blocked',
                    last_cause_code = ?,
                    last_message = ?,
                    transport_attempted = 0,
                    updated_at = ?
                WHERE batch_uuid = ?
                  AND source = ?
                  AND state IN ('scheduled', 'ready')
                """,
                (
                    outcome.get("cooldown_until"),
                    outcome.get("cause_code") or outcome.get("reason_code"),
                    "No request was made because the source was paused.",
                    now,
                    item["batch_uuid"],
                    item["source"],
                ),
            )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'paused_source',
                    paused_at = ?,
                    cooldown_until = ?,
                    last_reason_code = ?,
                    last_cause_code = ?,
                    updated_at = ?
                WHERE batch_uuid = ?
                """,
                (
                    now,
                    outcome.get("cooldown_until"),
                    outcome.get("reason_code"),
                    outcome.get("cause_code"),
                    now,
                    item["batch_uuid"],
                ),
            )
            if direct:
                # ARM THE VERIFICATION HOLD, in the same transaction that parks
                # the episode. The siblings above were just deferred as ordinary
                # `source_deferred` rows, and without this marker the automatic
                # resume would promote them one by one into the same failing
                # challenge once their cooldowns expire — the exact churn that
                # kept 22 items cycling. decide() holds every row of this
                # (batch, source) group while it is set; _complete clears it
                # only when a probe genuinely delivers from this source. A
                # non-challenge pause deliberately leaves an existing hold in
                # place: a throttle arriving mid-episode does not mean the
                # challenge went away.
                conn.execute(
                    "UPDATE download_queue_batches "
                    "SET verification_hold_source = ? WHERE batch_uuid = ?",
                    (item["source"], item["batch_uuid"]),
                )
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        updated = self.get_item(item["item_uuid"]) or item
        self._emit("download:retry_required", updated)
        batch = self.get_batch(item["batch_uuid"]) or {}
        self._emit(
            "download:batch_paused",
            {
                **batch,
                "triggering_item_uuid": item["item_uuid"],
                "deferred_count": batch.get("deferred_items", 0),
            },
        )
        self._emit_batch_progress(item["batch_uuid"])
        return True

    def _refresh_batch_locked(self, conn, batch_uuid: str, now: str) -> None:
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN state IN (
                    'waiting_source', 'verification_required'
                ) THEN 1 ELSE 0 END) AS deferred,
                SUM(CASE WHEN state IN (
                    'scheduled', 'ready', 'claimed', 'waiting_source',
                    'verification_required'
                ) THEN 1 ELSE 0 END) AS active
            FROM download_queue_items
            WHERE batch_uuid = ?
            """,
            (batch_uuid,),
        ).fetchone()
        if counts is None:
            return
        state = None
        if int(counts["active"] or 0) == 0:
            state = "completed"
        conn.execute(
            """
            UPDATE download_queue_batches
            SET completed_items = ?,
                failed_items = ?,
                deferred_items = ?,
                state = COALESCE(?, state),
                updated_at = ?
            WHERE batch_uuid = ?
            """,
            (
                int(counts["completed"] or 0),
                int(counts["failed"] or 0),
                int(counts["deferred"] or 0),
                state,
                now,
                batch_uuid,
            ),
        )

    def _hdencode_host(self) -> str:
        """The configured HDEncode host, so identity follows configuration.

        `base_url` is the operator-set source base (default https://hdencode.org),
        so a mirror or changed domain classifies correctly instead of relying on a
        hard-coded literal.
        """
        return str((self.config or {}).get("base_url") or "https://hdencode.org")

    def _auto_resume_max_attempts(self) -> int:
        """How many CONSECUTIVE fruitless automatic resumes a batch may make.

        Only fruitless ones count: _resume_batch refunds the budget whenever the
        previous resume delivered anything, so a batch making progress is never
        cut off. Clamped to 1..10 -- 1 restores the old single-shot behaviour, and
        the ceiling exists because an unbounded retry loop against a source that
        is rate-limiting us is how this incident started.
        """
        # DELEGATED, round 14. This kept its own parse+clamp while
        # queue_recovery_policy owned the same logic, so round 10's "one parser" item
        # was not literally closed. They agreed, which is precisely how the earlier
        # copies survived long enough to drift.
        from backend.queue_recovery_policy import parse_max_attempts
        return parse_max_attempts(self.config)

    #: The source whose retry budget the auto-resume machinery manages. The
    #: coordinator, the reveal cooldown and the batch pause are all HDEncode
    #: concepts, so only HDEncode work may refund an HDEncode retry.
    AUTO_RESUME_SOURCE = "hdencode"

    @staticmethod
    def is_source_delivery(outcome: dict) -> bool:
        """Did this completion actually cross the source boundary?

        Keyed on ONE affirmative signal the producer sets where the delivery
        happens (`source_progress`), for a reason worth recording.

        THE PREVIOUS VERSION DID NOTHING. It required
        `transport_attempted` to be truthy on top of a method check, and peer
        review found that no real success path sets that field --
        `download_item()` initialises it to None and the jdownloader, clipboard
        and browser paths never touch it. The only writers are failure
        diagnostics. So the counter never incremented in production and the
        refund could never fire, while the tests passed because they fabricated
        the flag. The extra condition I added as "belt and braces" is precisely
        what made the whole mechanism inert.

        The lesson applied here: infer nothing. One field, set by the code that
        performs the delivery, checked by the code that rewards it -- and an
        integration test that runs the real producer rather than a hand-built
        dict.
        """
        return bool((outcome or {}).get("source_progress"))

    def _source_delivery_count(self, conn, batch_uuid: str) -> int:
        row = conn.execute(
            "SELECT source_delivery_count AS n FROM download_queue_batches "
            "WHERE batch_uuid = ?",
            (batch_uuid,),
        ).fetchone()
        return int((row["n"] if row is not None else 0) or 0)

    def _warn_exhausted_batches(self, now) -> None:
        """A batch that has spent its automatic budget must SAY so, once.

        THE DEFECT THIS CLOSES, raised by peer review as a combined-set blocker.
        #47 exists because a due batch could stay parked forever without saying
        why. #51 then created a NEW terminal automatic state -- paused, resume
        enabled, budget spent, no fresh source progress -- and that state is
        filtered out by the eligibility query itself, so it never reaches #47's
        diagnostic. The tests said the batch should "stop and wait for a human";
        nothing told the human. That recreates the invisibility #47 removes, just
        after three attempts instead of one.

        Warned once per batch per process. A persisted marker would survive
        restarts, but this deliberately avoids another schema column and another
        write on the polling path; the tradeoff is that a restart can re-warn.
        """
        if not hasattr(self, "_exhausted_warned"):
            self._exhausted_warned = set()
        maximum = self._auto_resume_max_attempts()
        for row in self.db._query_dicts(
            """
            SELECT batch_uuid, auto_resume_used, cooldown_until,
                   last_reason_code, source_delivery_count,
                   auto_resume_progress_mark
            FROM download_queue_batches
            WHERE state = 'paused_source'
              AND auto_resume_after_cooldown = 1
              AND auto_resume_used >= ?
              AND source_delivery_count <= auto_resume_progress_mark
            """,
            (maximum,), default=[],
        ):
            batch_uuid = str(row.get("batch_uuid") or "")
            # PER EPISODE, not once per process. Peer review noted that keying on
            # the batch alone means a batch that recovers and later exhausts again
            # is silent the second time. The attempt count and progress mark both
            # change when a refund or resume happens, so keying on the triple makes
            # a genuinely new exhaustion a new key -- without another schema column
            # or a write on the polling path.
            episode = (batch_uuid,
                       int(row.get("auto_resume_used") or 0),
                       int(row.get("auto_resume_progress_mark") or 0))
            if episode in self._exhausted_warned:
                continue
            until = _parse(row.get("cooldown_until"))
            if until is not None and until > now:
                continue  # not yet due; nothing to report
            deferred = self.db._query(
                "SELECT COUNT(*) AS n FROM download_queue_items "
                "WHERE batch_uuid = ? AND state IN "
                "      ('waiting_source','verification_required')",
                (batch_uuid,), one=True, default=None)
            self._exhausted_warned.add(episode)
            logger.warning(
                "Batch %s has spent all %d automatic resume attempts without any "
                "further source delivery and will NOT retry on its own. "
                "%s item(s) remain deferred; last source reason %r. "
                "Manual action is required: reason auto_resume_budget_exhausted.",
                batch_uuid, maximum,
                int((deferred["n"] if deferred else 0) or 0),
                row.get("last_reason_code"),
            )

    def _log_unresumable_batch(self, batch: dict) -> None:
        """Say WHY a due batch could not be resumed, instead of failing silently.

        The eligibility query above requires an item whose `cooldown_until`
        matches the BATCH's `cooldown_until` exactly, as strings. That coupling
        is easy to break -- an operator raising one and not the other, or any
        future code path that updates one side -- and when it breaks the batch is
        parked permanently with no error, no warning, and a database that looks
        entirely correct.

        That happened on 2026-08-06: the batch cooldown was moved to 03:25Z to
        wait out a source throttle while its 69 items kept 00:02Z, so the resume
        would have skipped all five batches. Nothing would have said so.

        This runs only on the skip path, which is rare, so the extra query costs
        nothing in the normal case. It never changes behaviour -- it only makes
        the existing behaviour visible.
        """
        batch_uuid = batch.get("batch_uuid")
        counts = self.db._query(
            """
            SELECT
                COUNT(*) AS deferred,
                -- TEMPORAL STATE, not timestamp equality. Peer review round 12:
                -- this used to count how many item cooldowns EQUALLED the batch's
                -- and, when none did, told the operator the batch "will NEVER
                -- RESUME until they match". That was true of the architecture the
                -- item-first rewrite deleted -- equality is no longer a
                -- prerequisite for anything -- so the diagnostic was reporting a
                -- cause that can no longer block, and a test was protecting the
                -- claim. Timestamps are still evidence; DIFFERING from the batch is
                -- no longer itself a fault.
                SUM(CASE WHEN cooldown_until IS NOT NULL
                          AND cooldown_until <= ? THEN 1 ELSE 0 END) AS due_items,
                SUM(CASE WHEN cooldown_until IS NOT NULL
                          AND cooldown_until >  ? THEN 1 ELSE 0 END) AS future_items,
                SUM(CASE WHEN cooldown_until IS NULL THEN 1 ELSE 0 END)
                    AS no_retry_time_items,
                SUM(CASE WHEN queue_reason IN (
                        'interactive_challenge', 'source_deferred'
                    ) THEN 1 ELSE 0 END) AS reason_recognised,
                SUM(CASE WHEN queue_reason = 'interactive_challenge'
                    THEN 1 ELSE 0 END) AS challenge_held,
                SUM(CASE WHEN COALESCE(last_reason_code, '') IN (
                        'operation_timeout_unknown',
                        'interrupted_unknown_outcome'
                    ) THEN 1 ELSE 0 END) AS unknown_outcome
            FROM download_queue_items
            WHERE batch_uuid = ?
              AND state IN ('verification_required', 'waiting_source')
            """,
            (_iso(_utcnow()), _iso(_utcnow()), batch_uuid),
            one=True,
            default=None,
        )
        if counts is None:
            logger.warning(
                "Batch %s is due to auto-resume but the diagnostic query "
                "returned nothing; the batch stays paused.", batch_uuid,
            )
            return
        # _query(one=True) yields a sqlite3.Row, which supports indexing but not
        # .get(). Every column below is in the SELECT above, so indexing is safe.
        deferred = int(counts["deferred"] or 0)
        if deferred == 0:
            # Benign: nothing is waiting. Items were retried, cancelled or
            # completed by other means, so there is genuinely nothing to resume.
            logger.debug(
                "Batch %s is past its cooldown but holds no deferred items; "
                "nothing to resume.", batch_uuid,
            )
            return

        due = int(counts["due_items"] or 0)
        future = int(counts["future_items"] or 0)
        no_time = int(counts["no_retry_time_items"] or 0)
        recognised = int(counts["reason_recognised"] or 0)
        unknown = int(counts["unknown_outcome"] or 0)

        # REPORT EVERY FAILED PREDICATE, not one cause chosen by precedence.
        #
        # THE DEFECT THIS FIXES, raised by peer review. This was an if/elif chain,
        # so a batch whose rows had BOTH a cooldown mismatch AND
        # operation_timeout_unknown was reported as a cooldown problem only. But
        # matching the timestamps would not make those rows safe to retry -- they
        # are excluded deliberately because a retry could double-submit a delivery
        # that already happened. The diagnostic hid the safety-critical reason and
        # sent the reader to fix the wrong thing, which is the exact failure class
        # this method exists to prevent.
        causes = []
        if due == 0 and future:
            # TRANSIENT and correct, so say so plainly. The old text asserted
            # "THE BATCH WILL NEVER RESUME ON ITS OWN until they match", which was
            # about timestamp equality and is now simply false: these items have a
            # retry time and will recover when it arrives. A permanent-sounding
            # message invites a needless manual rescue.
            causes.append(
                f"all {future} deferred item(s) have their OWN retry time still in "
                "the future, so nothing is due yet. This is transient and will "
                "clear on its own; no action is needed")
        if no_time and due == 0 and future == 0:
            causes.append(
                f"{no_time} deferred item(s) have no retry time at all, and the "
                f"batch has no shared cooldown either ({batch.get('cooldown_until')!r}). "
                "Nothing authorises an automatic retry, so this needs an explicit "
                "operator resume -- it will NOT clear on its own")
        if recognised == 0:
            causes.append(
                "no deferred item carries a queue_reason the resume path accepts "
                "(only 'interactive_challenge' and 'source_deferred')")
        if unknown:
            # Reported whenever ANY row is affected, not only when all are, and
            # never suppressed by another cause.
            causes.append(
                f"{unknown} of {deferred} deferred item(s) ended in an unknown "
                "execution state (operation_timeout_unknown / "
                "interrupted_unknown_outcome). Those are excluded deliberately: a "
                "retry could double-submit a delivery that already happened. They "
                "need adjudicating by hand, and fixing anything else will not make "
                "them resumable")
        challenge = int(counts["challenge_held"] or 0)
        if challenge:
            causes.append(
                f"{challenge} of {deferred} deferred item(s) are under a "
                "verification hold (queue_reason=interactive_challenge). A timer "
                "will not release those; 'Retry now' on one of them sends a "
                "single probe")
        if not causes:
            causes.append(
                "each condition is satisfied by some row, but no single row "
                "satisfies all of them at once")

        logger.warning(
            "Batch %s did not auto-resume. Predicates: deferred=%d due=%d "
            "future=%d no_retry_time=%d recognised_reason=%d unknown_outcome=%d. "
            "Cause(s): %s",
            batch_uuid, deferred, due, future, no_time, recognised, unknown,
            "; ".join(causes),
        )

    def _maybe_auto_resume(self) -> None:
        if self.db is None:
            return
        snapshot = self._coordinator_snapshot()
        if snapshot.get("blocked"):
            return
        now = _utcnow()
        # A RETRY BUDGET, not a single shot. Changed 2026-08-07 after the
        # single shot was observed to be the binding constraint in production.
        #
        # WHAT HAPPENED. At 03:25Z the one automatic resume fired and WORKED: 24
        # of 69 stranded grabs completed over the next 50 minutes -- the first
        # fully automatic recovery from a source throttle this system has done.
        # Then at 04:07Z HDEncode throttled again, four batches re-paused, and
        # because `auto_resume_used` had reached 1 they could never resume
        # themselves again. 44 items sat parked behind a spent retry.
        #
        # A resume that delivered 24 items is not a failed attempt. So the budget
        # is REFUNDED whenever a resume made progress -- see _resume_batch, which
        # resets the counter if the batch completed anything since the last
        # automatic resume. The budget therefore only runs down on resumes that
        # achieved NOTHING, which is the case where giving up is correct.
        #
        # NOTE ON PACING, corrected 2026-08-07. An earlier version of this comment
        # claimed the coordinator's 1h -> 2h -> 4h escalation "composes with" this
        # budget so repeated retries spread out. That composition is NOT proven
        # here and the claim is withdrawn: no test in this branch exercises the
        # real coordinator alongside the queue. What IS true is that fruitless
        # retries are capped and each one waits for whatever cooldown the
        # coordinator set.
        #
        # UPDATED 2026-08-07: the half of that finding about
        # observe_reveal_success() having no production call site is now FIXED --
        # download_service calls it when HDEncode actually delivers file-host
        # links, so the streak does reset on evidence of health. The composition
        # claim stays withdrawn regardless, because it is still untested here, and
        # "the mechanism now exists" is not the same as "the behaviour is proven".
        # DISCOVERY STARTS FROM THE DEFERRED ITEMS, not from the batch.
        #
        # WHAT WAS WRONG. This query used to require `state = 'paused_source'`, and
        # the per-item lookup below additionally required
        # `item.cooldown_until = batch.cooldown_until` -- an EQUALITY between two
        # copies of one recovery fact. Liveness therefore depended on two separate
        # cross-table synchronisations holding forever, and every mutation path
        # silently became responsible for maintaining them.
        #
        # retry_item() does not maintain them. It sets ONE item ready and then sets
        # the batch to `scheduled` with `cooldown_until = NULL` regardless of
        # deferred siblings, and _refresh_batch_locked only ever writes `completed`
        # -- it never restores `paused_source`. So retrying a single item made every
        # other deferred item in that batch permanently unreachable: the sweep
        # would not select the batch, and the scheduler cannot claim
        # `waiting_source`. That is how 34 of Jesse's downloads sat idle for seven
        # hours with a healthy source and nothing in their way. A batch is an
        # aggregate; it can legitimately hold completed, ready, claimed and deferred
        # children at once, so no single `batch.state` can be the liveness authority
        # for all of them.
        #
        # NOW: find eligible deferred ITEMS, group them by (batch, source), and let
        # the batch supply only policy -- the retry budget and the shared cooldown.
        # The safety filters are unchanged and deliberately still here: an unknown
        # outcome is never auto-retried, because retrying something that may already
        # have happened is worse than leaving it parked.
        groups = self.db._query_dicts(
            """
            SELECT i.batch_uuid            AS batch_uuid,
                   i.source                AS source,
                   MIN(i.cooldown_until)   AS earliest_item_cooldown,
                   b.cooldown_until        AS batch_cooldown,
                   b.interval_seconds      AS interval_seconds,
                   b.state                 AS batch_state,
                   COUNT(*)                AS deferred_items
            FROM download_queue_items i
            JOIN download_queue_batches b ON b.batch_uuid = i.batch_uuid
            WHERE i.state IN ('verification_required', 'waiting_source')
              AND i.queue_reason IN (
                  'interactive_challenge', 'source_deferred'
              )
              AND COALESCE(i.last_reason_code, '') NOT IN (
                  'operation_timeout_unknown',
                  'interrupted_unknown_outcome'
              )
              AND b.auto_resume_after_cooldown = 1
              AND (
                    b.auto_resume_used < ?
                 OR b.source_delivery_count > b.auto_resume_progress_mark
              )
            GROUP BY i.batch_uuid, i.source
            ORDER BY i.batch_uuid
            """,
            (self._auto_resume_max_attempts(),),
            default=[],
        )
        # PROGRESS IS AN INDEPENDENT PATH TO ELIGIBILITY, and it has to be.
        #
        # My first version refunded the budget inside _resume_batch and gated
        # eligibility on `auto_resume_used < max` alone. A test caught that the
        # refund was then unreachable exactly when it mattered: a batch at its
        # limit never got selected, so _resume_batch never ran, so the refund it
        # contained could never fire. The batch stayed stuck despite delivering.
        #
        # So the second clause above lets a batch that completed something since
        # its last automatic resume through regardless of the counter, and
        # _resume_batch then resets the counter and re-marks the baseline.
        self._last_auto_resumed_batches = set()
        self._warn_exhausted_batches(now)
        # DISCOVERY IS DELIBERATELY DUMB NOW. It answers only "which (batch, source)
        # groups hold a deferred row worth looking at?" -- never whether any row may
        # run. Rounds 12 and 13 were both caused by this gate deciding authorisation
        # too, with logic that differed from the transactional gate:
        #
        #   round 12  MIN(cooldown) said the group was due, then every sibling was
        #             promoted -- including one due in 2030 and one with no retry time.
        #   round 13  MIN() ignores NULLs, so an item with NO cooldown plus a sibling
        #             due in 2030 gave MIN = 2030 and the whole group was SKIPPED.
        #             An ineligible sibling vetoed an eligible one -- the exact mirror.
        #
        # I answered round 12 by adding a per-item predicate to the transaction and
        # LEAVING this gate in place, so two places decided the same thing differently.
        # Round 13 was the direct consequence. The gate is gone: _resume_batch's
        # transaction is the single authority, and it re-reads every fact itself.
        for group in groups:
            self._resume_batch(
                group["batch_uuid"],
                interval_minutes=max(
                    0,
                    int(group.get("interval_seconds") or 0) // 60,
                ),
                automated=True,
                blocked_source=str(group["source"]),
            )

        # THE DIAGNOSTIC. Reachability corrected on round 13: this set used to be
        # built from `groups`, i.e. "held a candidate row", which is not the same as
        # "actually resumed something". A NULL/NULL batch was therefore listed as
        # resumable and the diagnostic never ran for it -- the exact case its own
        # comment said it would explain. It now records what really resumed.
        resumable = self._last_auto_resumed_batches
        stuck = self.db._query_dicts(
            """
            SELECT *
            FROM download_queue_batches
            WHERE state = 'paused_source'
              AND auto_resume_after_cooldown = 1
              AND (
                    auto_resume_used < ?
                 OR source_delivery_count > auto_resume_progress_mark
              )
            ORDER BY created_at
            """,
            (self._auto_resume_max_attempts(),),
            default=[],
        )
        # Sources currently under a verification hold, read once. A batch is
        # held if it recorded the hold itself OR (source-scoped, round-2 review)
        # any of its deferred items belongs to a held source — the transitively
        # held sibling batch, which must not be re-diagnosed as stuck.
        held_sources = {
            str(r.get("verification_hold_source"))
            for r in self.db._query_dicts(
                "SELECT DISTINCT verification_hold_source "
                "FROM download_queue_batches "
                "WHERE verification_hold_source IS NOT NULL", default=[])
        }
        for batch in stuck:
            if str(batch.get("batch_uuid")) in resumable:
                continue
            until = _parse(batch.get("cooldown_until"))
            if until is not None and until > now:
                continue          # still deliberately quiet; nothing to explain yet
            hold_source = str(batch.get("verification_hold_source") or "")
            if not hold_source and held_sources:
                match = self.db._query(
                    "SELECT source FROM download_queue_items "
                    "WHERE batch_uuid = ? AND state IN "
                    "('verification_required', 'waiting_source') "
                    "AND source IN (%s) LIMIT 1" % ",".join(
                        "?" * len(held_sources)),
                    (str(batch.get("batch_uuid")), *sorted(held_sources)),
                    one=True, default=None)
                if match is not None:
                    hold_source = str(match["source"])
            if hold_source:
                # DELIBERATELY held, not stuck. This is the steady state of a
                # verification hold — the cooldowns are long expired and nothing
                # automatic will ever promote it, BY DESIGN — so it must not be
                # re-diagnosed as a fault on every worker pass. Said once per
                # batch per process, like _warn_exhausted_batches.
                if not hasattr(self, "_hold_reported"):
                    self._hold_reported = set()
                if str(batch.get("batch_uuid")) not in self._hold_reported:
                    self._hold_reported.add(str(batch.get("batch_uuid")))
                    logger.warning(
                        "Batch %s is under a verification hold for source %s: "
                        "automated verification did not complete, and a timer "
                        "will not release it. Use 'Retry now' on ONE item as a "
                        "probe; the hold clears only when a probe actually "
                        "delivers.",
                        batch.get("batch_uuid"), hold_source,
                    )
                continue
            self._log_unresumable_batch(batch)

        self._warn_manual_recovery_batches(now, held_sources)

    #: Grace after a cooldown lapses before a manual-only batch is reported.
    #: Exists ONLY to avoid churn around the worker poll; it authorises no retry.
    _MANUAL_RECOVERY_GRACE_SECONDS = 60

    def _warn_manual_recovery_batches(self, now, held_sources) -> None:
        """Report a batch that will NEVER auto-resume, because nothing else does.

        THE SILENT FOURTH STATE (peer review 2026-08-12). Three separate queries
        gate recovery observability on ``auto_resume_after_cooldown = 1``: the
        resume eligibility query, ``_warn_exhausted_batches``, and the ``stuck``
        query feeding ``_log_unresumable_batch``. So the dispositions are:

            enabled + budget available -> automatic recovery
            enabled + budget spent     -> exhaustion warning
            enabled + due but blocked  -> unresumable diagnostic
            verification hold          -> explicit hold diagnostic
            DISABLED + cooldown due    -> no path, no warning, no diagnostic

        That last row is a STRICTLY WORSE parked state than the one already
        warned about: automatic recovery was never permitted, so there is no
        budget to spend and nothing will ever promote it. Observed in production
        2026-08-10..12 -- a batch sat with an expired cooldown and one deferred
        item for two days, its retry budget UNTOUCHED, reported by nothing, until
        a manual retry completed it immediately.

        The invariant this restores: every nonterminal ``paused_source`` group is
        automatically recoverable, deliberately safety-held, or REPORTED THROUGH
        OPERATOR DIAGNOSTICS as needing a human. Configuration may choose WHICH
        branch applies; it may not create a silent fourth state.

        "Reported through operator diagnostics" is deliberately weaker than
        "durably reported" (peer review round 2): this is a log warning with a
        per-process warn-once set, exactly like the exhausted-budget and
        verification-hold diagnostics beside it. Nothing is persisted on the batch
        and no API/UI disposition exists, so a restart re-warns rather than
        suppressing forever. A persisted or API-visible disposition is a
        worthwhile follow-up, not a claim this code should make.

        Evaluated PER (batch, source): a verification hold is source-scoped, so a
        held source must not erase an unrelated source group's disposition.

        This ONLY reports. It changes no state and authorises no retry -- the
        flag is a deliberate retry-policy choice, unknown outcomes still require
        adjudication, and a held source still gets the one-probe rule. It is also
        deliberately NOT labelled ``auto_resume_budget_exhausted``: "never
        permitted" and "permitted and spent" are different causes needing
        different operator responses.
        """
        from backend.queue_recovery_policy import UNKNOWN_OUTCOMES

        if not hasattr(self, "_manual_recovery_warned"):
            self._manual_recovery_warned = set()
        for row in self.db._query_dicts(
            """
            SELECT batch_uuid, cooldown_until, verification_hold_source
            FROM download_queue_batches
            WHERE state = 'paused_source'
              AND auto_resume_after_cooldown = 0
            """,
            default=[],
        ):
            batch_uuid = str(row.get("batch_uuid") or "")
            until = _parse(row.get("cooldown_until"))
            if until is not None and (
                until + timedelta(
                    seconds=self._MANUAL_RECOVERY_GRACE_SECONDS) > now
            ):
                continue  # not yet due (plus grace); nothing to report
            # A batch that recorded the hold itself is owned by the hold
            # diagnostic and its one-probe release rule.
            if row.get("verification_hold_source"):
                continue
            # PER SOURCE, NOT PER BATCH (peer review round 2). The first version
            # skipped the WHOLE batch when ANY deferred source was held:
            #     if held_sources and any(r.source in held_sources ...): continue
            # A verification hold is SOURCE-scoped, so in a mixed batch one held
            # source erased an unrelated, unheld, overdue source group's
            # disposition — recreating the very silent branch this diagnostic
            # exists to remove. Exclude only the held groups; report the rest.
            for group in self.db._query_dicts(
                """
                SELECT source,
                       COUNT(*) AS n,
                       SUM(CASE WHEN last_reason_code IN (%s)
                                THEN 1 ELSE 0 END) AS unknown_n,
                       MAX(last_reason_code) AS a_reason
                FROM download_queue_items
                WHERE batch_uuid = ?
                  AND state IN ('waiting_source','verification_required')
                GROUP BY source
                """ % ",".join("?" * len(UNKNOWN_OUTCOMES)),
                (*sorted(UNKNOWN_OUTCOMES), batch_uuid), default=[],
            ):
                source = str(group.get("source") or "")
                count = int(group.get("n") or 0)
                if count == 0:
                    continue
                if source in held_sources:
                    continue  # this group is held; the hold diagnostic owns it
                unknown_n = int(group.get("unknown_n") or 0)
                # Keyed on the parking episode so a group that recovers and later
                # parks again is reported again.
                episode = (batch_uuid, source,
                           str(row.get("cooldown_until") or ""), count)
                if episode in self._manual_recovery_warned:
                    continue
                self._manual_recovery_warned.add(episode)
                # AFFIRM THE SAFETY CONDITION when it is really present (peer
                # review round 2). Generic "unknown outcomes need adjudication"
                # boilerplate is not enough: the manual _resume_batch path selects
                # deferred rows WITHOUT decide(), clears exactly these reason
                # codes and makes the rows ready — so the natural operator
                # sequence (see this warning -> press Resume batch) can retry a
                # row whose delivery outcome is unknown. Say so only when a child
                # actually carries one.
                adjudicate = (
                    " %d of these have an UNKNOWN delivery outcome "
                    "(reason adjudicate_before_retry): check whether the release "
                    "already downloaded before retrying, and do NOT use plain "
                    "batch resume, which clears that state without adjudicating."
                    % unknown_n
                ) if unknown_n else ""
                logger.warning(
                    "Batch %s source %s is parked with automatic resume DISABLED "
                    "(auto_resume_after_cooldown=0), so its cooldown expiring will "
                    "never promote anything. %d item(s) remain deferred; a last "
                    "reason is %r. A human must decide: reason "
                    "manual_recovery_required. This is NOT budget exhaustion — no "
                    "automatic attempt was ever permitted.%s",
                    batch_uuid, source, count, group.get("a_reason"), adjudicate,
                )

    def retry_item(self, item_uuid: str) -> dict:
        item = self.get_item(item_uuid)
        if item is None:
            raise DownloadQueueError("The retry item was not found.")
        if item.get("source") == "hdencode":
            self._assert_hdencode_available()
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                    queue_reason = 'manual_retry',
                    last_reason_code = CASE
                        WHEN last_reason_code IN (
                            'operation_timeout_unknown',
                            'interrupted_unknown_outcome'
                        ) THEN NULL
                        ELSE last_reason_code
                    END,
                    updated_at = ?
                WHERE item_uuid = ?
                  AND state IN (
                    'verification_required', 'waiting_source', 'failed',
                    'scheduled', 'ready'
                  )
                """,
                (now, now, item_uuid),
            )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'scheduled', cooldown_until = NULL, updated_at = ?
                WHERE batch_uuid = ?
                """,
                (now, item["batch_uuid"]),
            )
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        self._wake.set()
        updated = self.get_item(item_uuid) or item
        self._emit("download:queue_updated", updated)
        return updated

    def retry_ready(self, interval_minutes: int = 10) -> dict:
        self._assert_hdencode_available()
        interval = max(0, min(120, int(interval_minutes)))
        now = _utcnow()
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            # EXCLUDE verification-held rows (round-2 review, finding 2). This
            # bulk path is an operator convenience, but the UI describes a retry
            # as a single probe; scheduling every held row at once would fan the
            # source out into the failing challenge — one transport attempt per
            # batch — while the operator believes they sent one probe. A held
            # source must be probed one item at a time via retry_item. Held rows
            # are counted and returned so the caller/UI can say what was skipped.
            held = int((conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM download_queue_items i
                WHERE i.source = 'hdencode'
                  AND i.state IN (
                      'verification_required', 'waiting_source', 'failed'
                  )
                  AND EXISTS (
                      SELECT 1 FROM download_queue_batches h
                      WHERE h.verification_hold_source = i.source
                  )
                """
            ).fetchone()["n"]) or 0)
            rows = conn.execute(
                """
                SELECT i.item_uuid AS item_uuid, i.batch_uuid AS batch_uuid
                FROM download_queue_items i
                WHERE i.source = 'hdencode'
                  AND i.state IN (
                      'verification_required', 'waiting_source', 'failed'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM download_queue_batches h
                      WHERE h.verification_hold_source = i.source
                  )
                ORDER BY i.created_at, i.sequence_number
                """
            ).fetchall()
            cursor = now
            batches = set()
            for row in rows:
                conn.execute(
                    """
                    UPDATE download_queue_items
                    SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                        queue_reason = 'manual_retry',
                        last_reason_code = CASE
                            WHEN last_reason_code IN (
                                'operation_timeout_unknown',
                                'interrupted_unknown_outcome'
                            ) THEN NULL
                            ELSE last_reason_code
                        END,
                        updated_at = ?
                    WHERE item_uuid = ?
                    """,
                    (_iso(cursor), _iso(now), row["item_uuid"]),
                )
                batches.add(row["batch_uuid"])
                cursor += timedelta(minutes=interval)
            for batch_uuid in batches:
                conn.execute(
                    """
                    UPDATE download_queue_batches
                    SET state = 'scheduled', interval_seconds = ?,
                        cooldown_until = NULL, updated_at = ?
                    WHERE batch_uuid = ?
                    """,
                    (interval * 60, _iso(now), batch_uuid),
                )
                self._refresh_batch_locked(conn, batch_uuid, _iso(now))
        self._wake.set()
        return {"scheduled": len(rows), "interval_minutes": interval,
                "held": held}

    def _resume_batch(
        self,
        batch_uuid: str,
        *,
        interval_minutes: int,
        automated: bool,
        blocked_source: Optional[str] = None,
    ) -> dict:
        if not automated:
            self._assert_hdencode_available()
        elif not blocked_source:
            raise DownloadQueueError("Automated resume requires a blocked source.")
        interval = max(0, min(120, int(interval_minutes)))
        now = _utcnow()
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            if automated:
                # THE SINGLE AUTHORITY. Every fact is read HERE, inside the
                # transaction that will flip the rows -- the batch row, the retry
                # budget, the shared brake, and each item's own cooldown. Nothing is
                # trusted from the discovery pass.
                #
                # Round 13 found the residual hole in my previous attempt: item
                # cooldowns were re-read but `shared_brake_passed` was computed
                # OUTSIDE and passed as a stale boolean, so an operator extending the
                # batch cooldown between discovery and update could still authorise an
                # early retry. I had claimed that race was closed. It was closed for
                # item cooldowns only.
                from backend.queue_recovery_policy import (
                    AUTHORISED, ItemFacts, SharedFacts, decide, parse_max_attempts,
                )
                brow = conn.execute(
                    "SELECT cooldown_until, auto_resume_after_cooldown, "
                    "       auto_resume_used, source_delivery_count, "
                    "       auto_resume_progress_mark, verification_hold_source "
                    "FROM download_queue_batches WHERE batch_uuid = ?",
                    (batch_uuid,),
                ).fetchone()
                if brow is None:
                    raise DownloadQueueError("The batch no longer exists.")
                # SOURCE-SCOPED HOLD (round-2 review, finding 1). The challenge
                # is source-wide (affected_scope="source", and the coordinator
                # sets a source-level cooldown), so the hold must be too: a row
                # is held if ANY batch holds its source, not only if THIS batch
                # recorded it. Otherwise a second HDEncode batch — parked as an
                # ordinary source_deferred pause while the coordinator was
                # blocked — would auto-probe the challenge once the shared
                # cooldown expired. The candidates below are already filtered to
                # blocked_source, so this one verdict governs exactly them.
                held = conn.execute(
                    "SELECT 1 FROM download_queue_batches "
                    "WHERE verification_hold_source = ? LIMIT 1",
                    (blocked_source,),
                ).fetchone()
                shared = SharedFacts(
                    cooldown_until=_parse(brow["cooldown_until"]),
                    auto_resume_enabled=bool(brow["auto_resume_after_cooldown"]),
                    attempts_used=int(brow["auto_resume_used"] or 0),
                    source_delivery_count=int(brow["source_delivery_count"] or 0),
                    progress_mark=int(brow["auto_resume_progress_mark"] or 0),
                    max_attempts=parse_max_attempts(self.config),
                    verification_hold=held is not None,
                )
                candidates = conn.execute(
                    """
                    SELECT item_uuid, state, cooldown_until, queue_reason,
                           COALESCE(last_reason_code, '') AS last_reason_code
                    FROM download_queue_items
                    WHERE batch_uuid = ?
                      AND source = ?
                      AND state IN (
                          'verification_required', 'waiting_source'
                      )
                    ORDER BY sequence_number
                    """,
                    (batch_uuid, blocked_source),
                ).fetchall()
                # PER ROW, and only ever about that row. No MIN(), no group verdict,
                # so one item's future cooldown can neither authorise nor veto another.
                rows = [
                    r for r in candidates
                    if decide(
                        ItemFacts(
                            state=str(r["state"]),
                            cooldown_until=_parse(r["cooldown_until"]),
                            queue_reason=str(r["queue_reason"] or ""),
                            last_reason_code=str(r["last_reason_code"] or ""),
                        ),
                        shared,
                        now,
                    ) == AUTHORISED
                ]
                if not rows:
                    # NOTHING AUTHORISED: do not spend budget and do not promote the
                    # batch. Round 13 required this explicitly -- a fruitless visit
                    # must not look like an attempt.
                    return self.get_batch(batch_uuid) or {}
                # RECORDED so the unresumable diagnostic can tell "this batch really
                # resumed something" from "this batch merely held a candidate row".
                self._note_auto_resumed(batch_uuid)
            else:
                # AUTHORITATIVE HOLD CHECK, inside the promotion transaction
                # (ChatGPT + b087aa20 round). resume_batch()'s outer check is only
                # a fast message: a worker can arm verification_hold_source
                # between that check's commit and this one, and the manual branch
                # below promotes every deferred row without decide(). Re-checking
                # HERE, in the same transaction that flips the rows, is what
                # actually closes the bulk-fan-out door. Raising rolls the
                # transaction back, so nothing is promoted.
                held_sources = [
                    str(r["source"])
                    for r in conn.execute(
                        "SELECT DISTINCT i.source FROM download_queue_items i "
                        "WHERE i.batch_uuid = ? AND EXISTS ("
                        "  SELECT 1 FROM download_queue_batches b "
                        "  WHERE b.verification_hold_source = i.source)",
                        (batch_uuid,),
                    ).fetchall()
                ]
                if held_sources:
                    raise DownloadQueueError(
                        "This batch is held behind a verification challenge that "
                        "ScanHound could not complete "
                        f"({', '.join(sorted(held_sources))}). Resuming every "
                        "item would present the same challenge to each in turn. "
                        "Retry a single item to test whether it has lifted; the "
                        "rest are released automatically once one succeeds."
                    )
                rows = conn.execute(
                    """
                    SELECT item_uuid
                    FROM download_queue_items
                    WHERE batch_uuid = ?
                      AND state IN (
                          'verification_required', 'waiting_source', 'failed'
                      )
                    ORDER BY sequence_number
                    """,
                    (batch_uuid,),
                ).fetchall()
            cursor = now
            for row in rows:
                if automated:
                    updated = conn.execute(
                        """
                        UPDATE download_queue_items
                        SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                            queue_reason = 'source_deferred',
                            automated_retry_count = automated_retry_count + 1,
                            updated_at = ?
                        WHERE item_uuid = ?
                          AND source = ?
                          AND state IN (
                              'verification_required', 'waiting_source'
                          )
                          AND COALESCE(last_reason_code, '') NOT IN (
                              'operation_timeout_unknown',
                              'interrupted_unknown_outcome'
                          )
                        """,
                        (
                            _iso(cursor),
                            _iso(now),
                            row["item_uuid"],
                            blocked_source,
                        ),
                    ).rowcount
                else:
                    updated = conn.execute(
                        """
                        UPDATE download_queue_items
                        SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                            queue_reason = 'manual_retry',
                            last_reason_code = CASE
                                WHEN last_reason_code IN (
                                    'operation_timeout_unknown',
                                    'interrupted_unknown_outcome'
                                ) THEN NULL
                                ELSE last_reason_code
                            END,
                            updated_at = ?
                        WHERE item_uuid = ?
                        """,
                        (_iso(cursor), _iso(now), row["item_uuid"]),
                    ).rowcount
                if updated == 1:
                    cursor += timedelta(minutes=interval)
            # REFUND THE BUDGET IF THE LAST AUTOMATIC RESUME ACHIEVED SOMETHING.
            #
            # Observed in production on 2026-08-07: one resume delivered 24 items
            # and then the source throttled again. Counting that as a spent
            # attempt is wrong -- the retry worked, the source simply shut a
            # second time. So a batch that completed anything since its previous
            # automatic resume starts its budget over, and the budget only runs
            # down on resumes that delivered NOTHING, which is exactly the case
            # where giving up is the right answer.
            #
            # Consequence worth being explicit about: a batch that keeps making
            # real source progress can retry indefinitely. That is intended -- it is
            # delivering. Each retry still waits for whatever cooldown the
            # coordinator has stored, so it is not a tight loop.
            #
            # NOT CLAIMED: that those waits GROW. No test here exercises the real
            # coordinator alongside the queue, so the 1h -> 2h -> 4h composition is
            # unproven. An earlier version of this comment asserted it; that claim
            # is withdrawn and stays withdrawn.
            #
            # The other half of that finding IS fixed as of 2026-08-07:
            # observe_reveal_success() now has a production call site (see
            # download_service, where HDEncode delivers file-host links), so the
            # streak resets on evidence of health rather than only on a container
            # restart. That makes the escalation dial behave as designed; it does
            # not by itself prove the composition above.
            used_delta = 1 if automated else 0
            reset_budget = False
            if automated:
                delivered_now = self._source_delivery_count(conn, batch_uuid)
                prior = conn.execute(
                    "SELECT auto_resume_progress_mark AS mark "
                    "FROM download_queue_batches WHERE batch_uuid = ?",
                    (batch_uuid,),
                ).fetchone()
                mark = int((prior["mark"] if prior is not None else 0) or 0)
                if delivered_now > mark:
                    reset_budget = True
                    logger.info(
                        "Batch %s recorded %d real source delivery(ies) since its "
                        "last automatic resume; restoring its retry budget.",
                        batch_uuid, delivered_now - mark,
                    )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'scheduled', interval_seconds = ?,
                    cooldown_until = NULL,
                    auto_resume_used = CASE WHEN ? THEN ? ELSE
                        auto_resume_used + ? END,
                    auto_resume_progress_mark = CASE WHEN ?
                        THEN source_delivery_count
                        ELSE auto_resume_progress_mark END,
                    updated_at = ?
                WHERE batch_uuid = ?
                """,
                (
                    interval * 60,
                    1 if reset_budget else 0,
                    used_delta,
                    used_delta,
                    1 if automated else 0,
                    _iso(now),
                    batch_uuid,
                ),
            )
            self._refresh_batch_locked(conn, batch_uuid, _iso(now))
        self._wake.set()
        batch = self.get_batch(batch_uuid) or {"batch_uuid": batch_uuid}
        self._emit("download:batch_schedule", batch)
        return batch

    def _note_auto_resumed(self, batch_uuid: str) -> None:
        """Remember that THIS batch really resumed something automatically.

        The diagnostic used to skip any batch that merely held a candidate row, so a
        NULL/NULL hold -- the case its own comment promised to explain -- was never
        reported. Recording actual resumes instead of candidates fixes that.
        """
        if not hasattr(self, "_last_auto_resumed_batches"):
            self._last_auto_resumed_batches = set()
        self._last_auto_resumed_batches.add(str(batch_uuid))

    @staticmethod
    def _source_is_held(conn, source: Optional[str]) -> bool:
        """True while a verification hold is open for this SOURCE (any batch).

        The source-scoped hold predicate used by `resume_batch`'s fast pre-check
        and `_resume_batch`'s authoritative in-transaction check. NOTE: this is
        not literally the ONLY expression of the rule — `retry_ready` and the
        automated `decide()` path each phrase the equivalent membership test in
        their own single-transaction SQL (safe there, since the check and the
        write commit together). They agree by construction on the same column;
        do not let a future edit drift them.
        """
        if not source:
            return False
        return conn.execute(
            "SELECT 1 FROM download_queue_batches "
            "WHERE verification_hold_source = ? LIMIT 1",
            (source,),
        ).fetchone() is not None

    def resume_batch(self, batch_uuid: str, interval_minutes: int = 10) -> dict:
        """Operator "resume everything". REFUSED while a verification hold is open.

        FOLD, round-2 review + `agent/turnstile-classification` port. This is the
        manual counterpart to auto-resume and it releases every deferred row in
        the batch at once — correct for a merely-throttled source, wrong for one
        presenting a challenge, because it hands the whole batch to a door
        measured to be shut and spends every item's attempt to learn one fact.
        The base guarded the bulk `retry_ready` path but NOT this one: its
        non-automated branch promotes all deferred rows without calling
        `decide()`, an unguarded second door. It now refuses, rather than
        silently downgrading, and points at the single-item probe. (`retry_ready`
        keeps its exclude-and-report behaviour for the bulk-button UX.)
        """
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            sources = [
                str(row["source"])
                for row in conn.execute(
                    "SELECT DISTINCT source FROM download_queue_items "
                    "WHERE batch_uuid = ?",
                    (batch_uuid,),
                ).fetchall()
            ]
            blocked = sorted(s for s in sources if self._source_is_held(conn, s))
        if blocked:
            raise DownloadQueueError(
                "This batch is held behind a verification challenge that "
                f"ScanHound could not complete ({', '.join(blocked)}). Resuming "
                "every item would present the same challenge to each of them in "
                "turn. Retry a single item to test whether it has lifted; the "
                "rest are released automatically once one succeeds."
            )
        return self._resume_batch(
            batch_uuid,
            interval_minutes=interval_minutes,
            automated=False,
        )

    def clear_verification_hold(self, source: str = "hdencode") -> dict:
        """Operator action: abandon an open verification hold for a source.

        FOLD port of `agent/turnstile-classification`'s `clear_challenge_episode`,
        adapted to this branch's `verification_hold_source` column. THE ESCAPE
        HATCH THAT MAKES THE STRICT RELEASE RULE SAFE: because the hold clears
        only on an affirmative source reveal (never a timer), it can otherwise
        outlive everything it held — a permanently-challenged source would
        deadlock, its only exit being to cancel the items. This gives the
        operator a way to say "I have dealt with this" without a successful grab.

        Deliberately NOT wired to any timer and NOT called by
        `_maybe_auto_resume`: a hold automatic recovery can clear by itself is
        not a hold. A human passing the same challenge in their own browser is
        also not a reason to auto-clear — it proves the challenge is passable,
        not that OUR session can pass it — which is exactly why release stays
        manual here and affirmative-only in `_release_verification_hold`.
        """
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
            cleared = conn.execute(
                "UPDATE download_queue_batches SET verification_hold_source = NULL "
                "WHERE verification_hold_source = ?",
                (source,),
            ).rowcount
            # Trigger rows stay held by their OWN reason; they need a probe. Say
            # how many remain so the operator is not surprised (fold review).
            remaining_triggers = int((conn.execute(
                "SELECT COUNT(*) AS n FROM download_queue_items "
                "WHERE source = ? AND state = 'verification_required' "
                "AND queue_reason = 'interactive_challenge'",
                (source,),
            ).fetchone()["n"]) or 0)
        logger.info(
            "Operator cleared the verification hold on %d batch(es) for %s "
            "(%d trigger(s) still need a probe).",
            cleared, source, remaining_triggers,
        )
        self._wake.set()
        # SAY that the clear is self-correcting. Peer review of the fold: if the
        # challenge is still active, the first released sibling re-arms the hold
        # within a cycle. That is fail-safe, but an operator who clicks "clear"
        # and watches it re-arm 40 seconds later will read it as broken and
        # remove the re-arm. Surfacing it here (and wherever the UI wires this)
        # keeps that from happening.
        return {
            "source": source,
            "cleared": cleared,
            "remaining_triggers": remaining_triggers,
            "next_action": (
                "Retry one verification-required item as a probe."
                if remaining_triggers else "No trigger left to probe."
            ),
            "message": (
                f"Released the verification hold on {cleared} batch(es) for "
                f"{source}. If the challenge is still active, the first released "
                "item will re-arm the hold within a cycle — that is fail-safe, "
                "not a fault."
            ),
        }

    def cancel_item(self, item_uuid: str) -> bool:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return False
            row = conn.execute(
                """
                SELECT state, batch_uuid
                FROM download_queue_items
                WHERE item_uuid = ?
                """,
                (item_uuid,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] == "claimed":
                raise DownloadQueueItemClaimed(item_uuid=item_uuid)
            updated = conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE item_uuid = ?
                  AND state NOT IN ('claimed', 'completed', 'cancelled')
                """,
                (now, now, item_uuid),
            ).rowcount
            if updated != 1:
                return False
            self._refresh_batch_locked(conn, row["batch_uuid"], now)
        self._emit(
            "download:queue_updated",
            {"item_uuid": item_uuid, "state": "cancelled"},
        )
        return True

    def cancel_batch(self, batch_uuid: str) -> bool:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return False
            batch = conn.execute(
                """
                SELECT batch_uuid
                FROM download_queue_batches
                WHERE batch_uuid = ?
                """,
                (batch_uuid,),
            ).fetchone()
            if batch is None:
                return False
            claimed = conn.execute(
                """
                SELECT item_uuid
                FROM download_queue_items
                WHERE batch_uuid = ? AND state = 'claimed'
                LIMIT 1
                """,
                (batch_uuid,),
            ).fetchone()
            if claimed is not None:
                raise DownloadQueueItemClaimed(
                    item_uuid=claimed["item_uuid"],
                    batch_uuid=batch_uuid,
                )
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE batch_uuid = ?
                  AND state NOT IN ('claimed', 'completed', 'cancelled')
                """,
                (now, now, batch_uuid),
            )
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'cancelled', updated_at = ?
                WHERE batch_uuid = ?
                """,
                (now, batch_uuid),
            )
        self._emit(
            "download:batch_schedule",
            {"batch_uuid": batch_uuid, "state": "cancelled"},
        )
        return True

    def get_item(self, item_uuid: str) -> Optional[dict]:
        row = self.db._query(
            "SELECT * FROM download_queue_items WHERE item_uuid = ?",
            (item_uuid,),
            one=True,
            default=None,
        )
        return dict(row) if row is not None else None

    def get_batch(self, batch_uuid: str) -> Optional[dict]:
        row = self.db._query(
            "SELECT * FROM download_queue_batches WHERE batch_uuid = ?",
            (batch_uuid,),
            one=True,
            default=None,
        )
        if row is None:
            return None
        result = dict(row)
        result["items"] = self.db._query_dicts(
            """
            SELECT *
            FROM download_queue_items
            WHERE batch_uuid = ?
            ORDER BY sequence_number
            """,
            (batch_uuid,),
            default=[],
        )
        return result

    def list_batches(self, limit: int = 100) -> list[dict]:
        return self.db._query_dicts(
            """
            SELECT *
            FROM download_queue_batches
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(500, int(limit))),),
            default=[],
        )

    def list_retries(self, limit: int = 250) -> list[dict]:
        rows = self.db._query_dicts(
            """
            SELECT *
            FROM download_queue_items
            WHERE state IN (
                'scheduled', 'ready', 'claimed', 'waiting_source',
                'verification_required', 'failed'
            )
            ORDER BY
                CASE state
                    WHEN 'verification_required' THEN 0
                    WHEN 'waiting_source' THEN 1
                    WHEN 'ready' THEN 2
                    WHEN 'scheduled' THEN 3
                    WHEN 'claimed' THEN 4
                    ELSE 5
                END,
                COALESCE(scheduled_for, created_at),
                sequence_number
            LIMIT ?
            """,
            (max(1, min(1000, int(limit))),),
            default=[],
        )
        snapshot = self._coordinator_snapshot()
        now = _utcnow()
        for row in rows:
            scheduled = _parse(row.get("scheduled_for"))
            row["retry_available"] = (
                row.get("source") != "hdencode" or not snapshot.get("blocked")
            )
            row["due"] = bool(scheduled is None or scheduled <= now)
            row["source_state"] = snapshot.get("state") if row.get("source") == "hdencode" else None
            row["source_reason_code"] = (
                snapshot.get("reason_code") if row.get("source") == "hdencode" else None
            )
            row["source_cooldown_until"] = (
                snapshot.get("cooldown_until") if row.get("source") == "hdencode" else None
            )
        return rows

    def status(self) -> dict:
        snapshot = self._coordinator_snapshot()
        counts = self.db._query_dicts(
            """
            SELECT state, COUNT(*) AS count
            FROM download_queue_items
            GROUP BY state
            """,
            default=[],
        )
        return {
            "worker_running": bool(self._thread and self._thread.is_alive()),
            "counts": {row["state"]: row["count"] for row in counts},
            "source": snapshot,
        }
