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


def test_misses_from_zero_fetch_cycles_do_not_fail_the_gate(tmp_path):
    """A cycle that never fetched cannot condemn the window.

    rss_urls comes from list_hdencode_current_feed_urls(), which reads the last
    persisted feed snapshot from the database -- not that cycle's fetch. With
    rss_requests=0 the comparison is listing-vs-stale-snapshot, listing_only is
    inflated by everything the feed had merely not collected yet, and every
    relevant row in it was booked as a miss. Over 2026-07-22..2026-08-05, 41
    such cycles produced 89 of 150 recorded misses and none was a real loss
    (median catch-up 1.10h, worst 4.06h).

    compare_shadow no longer records these; this filter stops the 89 already on
    disk from failing the gate forever.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="eligible-clean",
                  completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=0)
    # 89 of the 90 real contested records looked exactly like this.
    _insert_cycle(db, uuid="feed-never-fetched",
                  completed_at="2026-07-22T00:00:00+00:00",
                  normal=0, rss=0, listing=4, misses=8,
                  outcome="relevant_miss")
    _insert_cycle(db, uuid="feed-never-fetched-2",
                  completed_at="2026-07-23T00:00:00+00:00",
                  normal=0, rss=0, listing=5, misses=81,
                  outcome="relevant_miss")

    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 0
    assert summary["successful_cycles"] == 1


def test_a_degraded_cycle_that_fetched_still_fails_the_gate(tmp_path):
    """The 2026-07-21 audit rule (f5e3c6e), preserved rather than reversed.

    test_relevant_miss_blocks_even_when_cycle_is_incomplete above is the
    original. This is its sharper form: the exclusion must key on rss_requests,
    NOT on normal_feeds_complete, so a degraded cycle that DID fetch still
    reports. Widening the exclusion to all incomplete cycles would silence the
    real 2026-07-28 record (rss_requests=2), which grades green at 1.25h -- so
    keeping it costs nothing and dropping it would cost the protection.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="degraded-but-fetched",
                  completed_at="2026-07-28T01:32:02+00:00",
                  normal=0, rss=2, listing=27, misses=1,
                  outcome="relevant_miss")
    _insert_cycle(db, uuid="feed-never-fetched",
                  completed_at="2026-07-30T02:26:28+00:00",
                  normal=0, rss=0, listing=4, misses=97,
                  outcome="relevant_miss")

    summary = db.get_hdencode_shadow_summary()
    # The 1 that fetched, not the 97 that did not, and not 0.
    assert summary["relevant_misses"] == 1


def test_a_real_miss_on_an_eligible_cycle_still_fails_the_gate(tmp_path):
    """The protection this change must not weaken at all."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="eligible-with-a-real-gap",
                  completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=3,
                  outcome="relevant_miss")
    _insert_cycle(db, uuid="feed-never-fetched",
                  completed_at="2026-07-22T00:00:00+00:00",
                  normal=0, rss=0, listing=4, misses=97,
                  outcome="relevant_miss")

    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 3
