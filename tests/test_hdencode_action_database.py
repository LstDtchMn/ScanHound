"""Real-SQLite tests for persistent RSS action transitions."""
from __future__ import annotations

import sqlite3

from backend.database import DatabaseManager


def _candidate(db):
    now = "2026-07-20T12:00:00+00:00"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO hdencode_candidates (
                canonical_url, guid, title, pub_date, media_type, raw_hash,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "https://hdencode.org/example/",
                "guid-example",
                "Example.2026.2160p",
                now,
                "movie",
                "raw",
                now,
                now,
                now,
            ),
        )


def test_action_is_persisted_before_claim_and_recovered(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    _candidate(db)
    created = db.create_hdencode_action(
        action_uuid="a1",
        idempotency_key="key",
        canonical_url="https://hdencode.org/example/",
        action_kind="grab",
        requested_by="explicit",
        service_type="Rapidgator",
        priority=100,
        package_name="Example (2026) [2160p]",
        destination="",
        lifespan_generation=1,
        authorized_evidence={"identity_state": "exact"},
    )
    assert created["state"] == "queued"
    claimed = db.claim_hdencode_action("a1")
    assert claimed["state"] == "retrieving_links"
    db.close()

    reopened = DatabaseManager(str(tmp_path / "crawler.db"))
    # Restart recovery is invoked by HDEncodeActionService.__init__ at startup;
    # this DB-layer test doesn't construct the service, so run it directly.
    reopened.recover_hdencode_actions()
    action = reopened.get_hdencode_action("a1")
    assert action["state"] == "queued"
    assert action["last_error_code"] == "recovered_after_restart"


def test_submission_interruption_requires_review(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    _candidate(db)
    db.create_hdencode_action(
        action_uuid="a2",
        idempotency_key="key2",
        canonical_url="https://hdencode.org/example/",
        action_kind="grab",
        requested_by="explicit",
        service_type="Rapidgator",
        priority=100,
        package_name="Example",
        destination="",
        lifespan_generation=1,
        authorized_evidence={},
    )
    db.claim_hdencode_action("a2")
    db.mark_hdencode_action_links_ready(
        "a2", links=["https://rapidgator.net/file/1"]
    )
    assert db.mark_hdencode_action_submitting("a2") is True
    db.close()

    reopened = DatabaseManager(str(tmp_path / "crawler.db"))
    reopened.recover_hdencode_actions()
    action = reopened.get_hdencode_action("a2")
    assert action["state"] == "needs_review"
    assert action["last_error_code"] == "submission_interrupted"
    assert reopened.retry_hdencode_action("a2")["state"] == "needs_review"


def test_idempotency_returns_existing_action(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    _candidate(db)
    kwargs = dict(
        action_uuid="a3",
        idempotency_key="same",
        canonical_url="https://hdencode.org/example/",
        action_kind="retrieve_links",
        requested_by="explicit",
        service_type="Rapidgator",
        priority=100,
        package_name="Example",
        destination="",
        lifespan_generation=1,
        authorized_evidence={},
    )
    first = db.create_hdencode_action(**kwargs)
    kwargs["action_uuid"] = "a4"
    second = db.create_hdencode_action(**kwargs)
    assert first["created"] is True
    assert second["action_uuid"] == "a3"
    assert second["idempotent"] is True
