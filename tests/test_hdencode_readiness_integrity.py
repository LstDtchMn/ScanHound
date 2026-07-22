"""Adversarial readiness/recovery contract tests."""
from __future__ import annotations

import datetime as dt

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner


def _insert_cycle(db, *, uuid, completed_at, normal=1, rss=2, listing=10,
                  misses=0, restart=0, catchup=0, outcome="success"):
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO hdencode_shadow_cycles (
               cycle_uuid, started_at, completed_at, normal_feeds_complete,
               rss_requests, listing_requests, rss_count, listing_count,
               duplicate_count, feed_only_count, listing_only_count,
               relevant_miss_count, request_reduction_pct, catchup_used,
               restart_recovery, outcome, details_json
           ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, 0, ?, ?, ?, '{}')""",
        (
            uuid, completed_at, completed_at, normal, rss, listing,
            misses, catchup, restart, outcome,
        ),
    )
    conn.commit()


def test_incomplete_and_degenerate_cycles_do_not_advance_readiness(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="incomplete-early",
        completed_at="2026-07-01T00:00:00+00:00",
        normal=0, rss=0, listing=100,
    )
    _insert_cycle(
        db, uuid="eligible",
        completed_at="2026-07-21T00:00:00+00:00",
        normal=1, rss=2, listing=10,
    )
    _insert_cycle(
        db, uuid="degenerate-late",
        completed_at="2026-08-15T00:00:00+00:00",
        normal=1, rss=0, listing=100,
    )

    summary = db.get_hdencode_shadow_summary()
    assert summary["successful_cycles"] == 1
    assert summary["first_completed_at"] == "2026-07-21T00:00:00+00:00"
    assert summary["last_completed_at"] == "2026-07-21T00:00:00+00:00"
    assert summary["rss_requests"] == 2
    assert summary["listing_requests"] == 10
    assert summary["request_reduction_pct"] == 80.0


def test_relevant_miss_blocks_even_when_cycle_is_incomplete(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="incomplete-miss",
        completed_at="2026-07-21T00:00:00+00:00",
        normal=0, rss=1, listing=1, misses=1, outcome="relevant_miss",
    )
    summary = db.get_hdencode_shadow_summary()
    assert summary["successful_cycles"] == 0
    assert summary["relevant_misses"] == 1


class _Registry:
    lifespan_generation = 1
    config = {}
    scanner = None
    db = None

    def owns_lifespan(self, _generation):
        return True


class _FeedDb:
    def __init__(self, present=True):
        self.present = present

    def get_hdencode_feed_state(self, key):
        if not self.present:
            return {}
        return {"feed_key": key, "last_checked_at": "2026-07-21T00:00:00+00:00"}


def test_restart_marker_is_process_lifetime_not_service_lifetime():
    scanner = BackgroundScanner(_Registry())
    assert scanner._rss_first_cycle_after_startup is True
    scanner._rss_first_cycle_after_startup = False
    assert scanner._rss_first_cycle_after_startup is False
