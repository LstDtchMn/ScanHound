"""Durable download queue, CAPTCHA retry list, and staggered scheduler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
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


def _source(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    if host == "ddlbase.com" or host.endswith(".ddlbase.com"):
        return "ddlbase"
    if host == "adit-hd.com" or host.endswith(".adit-hd.com"):
        return "adithd"
    return "hdencode"


class DownloadQueueError(RuntimeError):
    pass


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


class DownloadQueueService:
    """One restart-safe worker for scheduled link retrieval and verification retries."""

    def __init__(
        self,
        config: Dict[str, Any],
        db,
        download_service,
        *,
        broadcast: Optional[Callable[[dict], None]] = None,
        on_delivery: Optional[Callable[[], None]] = None,
        poll_seconds: float = 2.0,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.db = db
        self.download = download_service
        self.broadcast = broadcast or (lambda _event: None)
        self.on_delivery = on_delivery or (lambda: None)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.worker_id = str(uuid.uuid4())
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.recover_interrupted()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="download-queue",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _emit(self, event_type: str, data: dict) -> None:
        try:
            self.broadcast({"type": event_type, "data": data})
        except Exception:
            logger.debug("download queue broadcast failed", exc_info=True)

    def recover_interrupted(self) -> None:
        if self.db is None:
            return
        now = _utcnow()
        grace = _iso(now + timedelta(seconds=30))
        with self.db.transaction() as conn:
            if not conn:
                return
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'scheduled',
                    scheduled_for = COALESCE(scheduled_for, ?),
                    claimed_by = NULL,
                    claim_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'claimed'
                """,
                (grace, _iso(now)),
            )
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
            raise DownloadQueueError("The download queue is unavailable.")
        interval = max(0, min(120, int(interval_minutes))) * 60
        mode = "immediate" if interval == 0 or mode == "immediate" else "staggered"
        batch_uuid = str(uuid.uuid4())
        now = _utcnow()
        unique: list[dict] = []
        seen = set()
        for raw in items:
            item = self._request_dict(dict(raw))
            source = _source(item["url"])
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
                raise DownloadQueueError("The database is unavailable.")
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
                    unique[0]["source"] if len({i["source"] for i in unique}) == 1 else "mixed",
                    len(unique),
                    1 if auto_resume_after_cooldown else 0,
                    _iso(now),
                    _iso(now),
                ),
            )
            for index, item in enumerate(unique):
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
                raise DownloadQueueError(
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
        return payload

    def enqueue_retry(self, request: Any, outcome: dict) -> dict:
        data = (
            request.model_dump()
            if hasattr(request, "model_dump")
            else dict(request)
        )
        item = self._request_dict(data)
        source = _source(item["url"])
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
                    last_cause_code
                ) VALUES (
                    ?, 'verification_retry', 0, 'paused_source', ?, 1, 1,
                    ?, ?, ?, ?, ?, ?
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

    def _claim_due(self) -> Optional[dict]:
        if self.db is None:
            return None
        now = _iso()
        lease = _iso(_utcnow() + timedelta(minutes=10))
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

        self._emit("download:result", outcome)

        if outcome.get("success"):
            self._complete(item, outcome)
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
            self._pause_for_source(item, outcome)
            self._emit(
                "notification",
                notification_for_result(outcome, title=item["title"]),
            )
            return

        self._fail(item, outcome)
        self._emit(
            "notification",
            notification_for_result(outcome, title=item["title"]),
        )

    def _complete(self, item: dict, outcome: dict) -> None:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return
            conn.execute(
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
                """,
                (now, now, outcome.get("message"), item["item_uuid"]),
            )
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        self._emit(
            "download:queue_updated",
            {**item, **outcome, "state": "completed"},
        )

    def _fail(self, item: dict, outcome: dict) -> None:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return
            conn.execute(
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
                """,
                (
                    now,
                    outcome.get("reason_code"),
                    outcome.get("cause_code"),
                    outcome.get("message"),
                    1 if outcome.get("transport_attempted") else 0,
                    item["item_uuid"],
                ),
            )
            self._refresh_batch_locked(conn, item["batch_uuid"], now)
        self._emit(
            "download:queue_updated",
            {**item, **outcome, "state": "failed"},
        )

    def _pause_for_source(self, item: dict, outcome: dict) -> None:
        now = _iso()
        direct = outcome.get("reason_code") == "interactive_challenge"
        item_state = "verification_required" if direct else "waiting_source"
        item_reason = "interactive_challenge" if direct else "source_deferred"
        with self.db.transaction() as conn:
            if not conn:
                return
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = ?, queue_reason = ?, cooldown_until = ?,
                    last_reason_code = ?, last_cause_code = ?,
                    last_message = ?, transport_attempted = ?,
                    claimed_by = NULL, claim_expires_at = NULL,
                    updated_at = ?
                WHERE item_uuid = ?
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
                ),
            )
            deferred = conn.execute(
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
                  AND state IN ('scheduled', 'ready')
                """,
                (
                    outcome.get("cooldown_until"),
                    outcome.get("cause_code") or outcome.get("reason_code"),
                    "No request was made because the source was paused.",
                    now,
                    item["batch_uuid"],
                ),
            ).rowcount
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'paused_source',
                    paused_at = ?,
                    cooldown_until = ?,
                    last_reason_code = ?,
                    last_cause_code = ?,
                    deferred_items = deferred_items + ?,
                    updated_at = ?
                WHERE batch_uuid = ?
                """,
                (
                    now,
                    outcome.get("cooldown_until"),
                    outcome.get("reason_code"),
                    outcome.get("cause_code"),
                    max(0, int(deferred or 0)),
                    now,
                    item["batch_uuid"],
                ),
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

    def _maybe_auto_resume(self) -> None:
        if self.db is None:
            return
        snapshot = self._coordinator_snapshot()
        if snapshot.get("blocked"):
            return
        now = _utcnow()
        batches = self.db._query_dicts(
            """
            SELECT *
            FROM download_queue_batches
            WHERE state = 'paused_source'
              AND auto_resume_after_cooldown = 1
              AND auto_resume_used = 0
            ORDER BY created_at
            """,
            default=[],
        )
        for batch in batches:
            until = _parse(batch.get("cooldown_until"))
            if until and until > now:
                continue
            self._resume_batch(
                batch["batch_uuid"],
                interval_minutes=max(
                    0,
                    int(batch.get("interval_seconds") or 0) // 60,
                ),
                automated=True,
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
                    queue_reason = 'manual_retry', updated_at = ?
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
            rows = conn.execute(
                """
                SELECT item_uuid, batch_uuid
                FROM download_queue_items
                WHERE source = 'hdencode'
                  AND state IN (
                      'verification_required', 'waiting_source', 'failed'
                  )
                ORDER BY created_at, sequence_number
                """
            ).fetchall()
            cursor = now
            batches = set()
            for row in rows:
                conn.execute(
                    """
                    UPDATE download_queue_items
                    SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                        queue_reason = 'manual_retry', updated_at = ?
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
        self._wake.set()
        return {"scheduled": len(rows), "interval_minutes": interval}

    def _resume_batch(
        self,
        batch_uuid: str,
        *,
        interval_minutes: int,
        automated: bool,
    ) -> dict:
        if not automated:
            self._assert_hdencode_available()
        interval = max(0, min(120, int(interval_minutes)))
        now = _utcnow()
        with self.db.transaction() as conn:
            if not conn:
                raise DownloadQueueError("The database is unavailable.")
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
                conn.execute(
                    """
                    UPDATE download_queue_items
                    SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                        queue_reason = ?, automated_retry_count =
                            automated_retry_count + ?,
                        updated_at = ?
                    WHERE item_uuid = ?
                    """,
                    (
                        _iso(cursor),
                        "source_deferred" if automated else "manual_retry",
                        1 if automated else 0,
                        _iso(now),
                        row["item_uuid"],
                    ),
                )
                cursor += timedelta(minutes=interval)
            conn.execute(
                """
                UPDATE download_queue_batches
                SET state = 'scheduled', interval_seconds = ?,
                    cooldown_until = NULL,
                    auto_resume_used = auto_resume_used + ?,
                    updated_at = ?
                WHERE batch_uuid = ?
                """,
                (
                    interval * 60,
                    1 if automated else 0,
                    _iso(now),
                    batch_uuid,
                ),
            )
        self._wake.set()
        batch = self.get_batch(batch_uuid) or {"batch_uuid": batch_uuid}
        self._emit("download:batch_schedule", batch)
        return batch

    def resume_batch(self, batch_uuid: str, interval_minutes: int = 10) -> dict:
        return self._resume_batch(
            batch_uuid,
            interval_minutes=interval_minutes,
            automated=False,
        )

    def cancel_item(self, item_uuid: str) -> bool:
        now = _iso()
        ok = self.db._mutate(
            """
            UPDATE download_queue_items
            SET state = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE item_uuid = ?
              AND state NOT IN ('completed', 'cancelled')
            """,
            (now, now, item_uuid),
            label="cancel_download_queue_item",
        )
        if ok:
            self._emit(
                "download:queue_updated",
                {"item_uuid": item_uuid, "state": "cancelled"},
            )
        return bool(ok)

    def cancel_batch(self, batch_uuid: str) -> bool:
        now = _iso()
        with self.db.transaction() as conn:
            if not conn:
                return False
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE batch_uuid = ?
                  AND state NOT IN ('completed', 'cancelled')
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
