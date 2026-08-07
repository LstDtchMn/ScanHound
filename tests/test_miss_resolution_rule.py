"""A listing-only release counts against RSS only if it was never acquired.

THE DECISION (Jesse, 2026-08-07). Readiness blocked on `relevant_misses > 0`: any
release ever seen on the listing page before the feed blocked the gate
permanently. The measurement showed those are not losses — 99 of 100 were acquired
anyway, median about an hour, all via the normal feeds, with exactly one never
acquired. So the old rule could never pass and RSS would stay in shadow mode
however well it worked.

Offered a 6-hour tolerance or "never acquired at all", Jesse chose the latter.
Recorded reservation: with no deadline, a release acquired three days late counts
as a success, so the rule stops measuring the latency RSS exists to improve. The
lag is still computed per row so it can be REPORTED — it just no longer GATES.

## TWO THINGS PEER REVIEW REVERSED, 2026-08-07 — read before editing

**1. `not_yet_assessable` BLOCKS.** I had made it non-blocking so the gate could
pass, and defended that as "otherwise the gate is unpassable by construction". The
review showed the composition that makes it unsafe rather than merely optimistic:
the shadow comparison is recorded only while `discovery_mode == "rss_shadow"`
(`background_scanner.py:449`), so promoting to `rss_primary` STOPS producing the
observations a pending row needs. The gate could open on evidence its own promoted
mode then destroys. The honest way to pass is a frozen cohort — fix an admission
cutoff, collect an observation tail, require every admitted row to resolve — not to
make a live unresolved row vanish because it is newest.

**2. Per-feed authority, not cycle completeness.** See
`test_miss_resolution_per_feed.py`, which holds those cases. `classify_miss_resolution`
now takes a `media_type` and each cycle carries its per-feed `outcomes`.

This file keeps what is unique to it: the no-deadline consequence, malformed-input
handling, lag reporting, and the WIRING — that readiness actually consumes this and
that the old unconditional blocker is gone.
"""
from datetime import datetime, timedelta, timezone

from backend import database as database_module
from backend.database import DatabaseManager
from backend.hdencode_shadow import (
    classify_miss_resolution,
    summarise_miss_resolutions,
)

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
URL = "https://hdencode.org/some-release-2160p/"
OTHER = "https://hdencode.org/unrelated-1080p/"

#: Both normal feeds observed — so these cycles are valid evidence for any row.
BOTH = {"movies_all": "changed", "tv_all": "changed"}


def cycle(hours, *, listing_only=(), feed_only=(), outcomes=None):
    return {"at": T0 + timedelta(hours=hours),
            "outcomes": dict(BOTH if outcomes is None else outcomes),
            "listing_only": set(listing_only),
            "feed_only": set(feed_only)}


def classify(url=URL, media_type="movie", first_seen=T0, cycles=()):
    return classify_miss_resolution(url, media_type, first_seen, list(cycles))


class TestTheNoDeadlineConsequence:

    def test_the_feed_picking_it_up_is_acquisition(self):
        state, hours, _ = classify(cycles=[cycle(1, feed_only=[URL])])
        assert state == "acquired"
        assert hours == 1.0

    def test_a_very_late_acquisition_still_counts_as_acquired(self):
        """THE CONSEQUENCE OF THE CHOSEN RULE, pinned deliberately.

        72 hours late is still a success under "never acquired at all". This is
        the trade Jesse accepted over a 6-hour budget. If it should change, change
        the RULE — do not let it drift by accident.
        """
        state, hours, _ = classify(cycles=[cycle(6, listing_only=[URL]),
                                           cycle(72, feed_only=[URL])])
        assert state == "acquired", "there is no deadline; only 'never' fails"
        assert hours == 72.0, "the lag is still measured so it can be reported"

    def test_earlier_cycles_are_ignored(self):
        """A feed sighting predating the row cannot resolve it; accepting it would
        let any historical presence excuse a later disappearance."""
        assert classify(cycles=[cycle(-5, feed_only=[URL])])[0] != "acquired"

    def test_another_release_being_acquired_proves_nothing(self):
        assert classify(cycles=[cycle(2, feed_only=[OTHER],
                                      listing_only=[URL])])[0] == "never_acquired"

    def test_acquisition_wins_over_an_earlier_still_missing_sighting(self):
        state, hours, _ = classify(cycles=[cycle(2, listing_only=[URL]),
                                           cycle(5, feed_only=[URL]),
                                           cycle(8, listing_only=[OTHER])])
        assert state == "acquired"
        assert hours == 5.0

    def test_vanishing_from_both_sides_is_undetermined_not_healthy(self):
        """The listing pages away over time, so absence from both sides is not
        evidence of acquisition. Counting it as acquired would let ordinary
        listing churn manufacture a passing gate."""
        state, _, detail = classify(cycles=[cycle(4, listing_only=[OTHER])])
        assert state == "undetermined"
        assert "neither" in detail


class TestTheSummaryReadinessGatesOn:

    def test_the_worst_lag_is_reported_even_though_it_does_not_gate(self):
        """The reservation stays visible: the number survives even though the rule
        ignores it, so a latency regression remains observable."""
        summary = summarise_miss_resolutions(
            [{"url": "a", "media_type": "movie", "at": T0},
             {"url": "b", "media_type": "movie", "at": T0}],
            [cycle(1, feed_only=["a"]), cycle(100, feed_only=["b"])])
        assert summary["blocking"] == 0
        assert summary["worst_acquisition_lag_hours"] == 100.0, (
            "a 100-hour acquisition passes by design, but must not be invisible")

    def test_a_malformed_row_counts_as_undetermined_not_ignored(self):
        """Dropping unreadable rows is how a gate silently reports zero."""
        summary = summarise_miss_resolutions(
            [{"url": None, "media_type": "movie", "at": T0},
             {"url": "a", "media_type": "movie"}, {}],
            [cycle(1)])
        assert summary["undetermined"] == 3
        assert summary["blocking"] == 3
        assert summary["acquired"] == 0

    def test_no_misses_at_all_is_clean(self):
        summary = summarise_miss_resolutions([], [cycle(1)])
        assert summary["blocking"] == 0
        assert summary["rows"] == []


def _seed_one_valid_cycle(db):
    """One cycle with per-feed provenance and one attributable miss row."""
    import json
    cyc = "cycle-round6"
    stamp = "2026-08-06T12:00:00+00:00"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hdencode_shadow_cycles "
            "(cycle_uuid, started_at, completed_at, normal_feed_outcomes, "
            " normal_feeds_complete, rss_requests, listing_requests, rss_count, "
            " listing_count, duplicate_count, feed_only_count, "
            " listing_only_count, relevant_miss_count, request_reduction_pct, "
            " outcome, details_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cyc, stamp, stamp, json.dumps(BOTH), 1, 2, 1, 20, 4, 3, 1, 1, 1,
             85.0, "relevant_miss",
             json.dumps({"listing_only": ["https://hdencode.org/x/"],
                         "feed_only": []})))
        conn.execute(
            "INSERT INTO hdencode_shadow_misses "
            "(cycle_uuid, canonical_url, media_type) VALUES (?,?,?)",
            (cyc, "https://hdencode.org/x/", "movie"))
    return cyc


class TestTheProductionPathActuallyUsesIt:
    """Five times in this project I have proved a function correct and never
    checked that anything calls it. So prove the call."""

    def test_the_old_unconditional_blocker_is_gone(self):
        import inspect
        src = inspect.getsource(DatabaseManager.get_hdencode_rss_readiness)
        assert "relevant_misses_detected" not in src, (
            "the old rule still blocks on any miss at all, so the new "
            "classification cannot change the outcome")
        for reason in ("unacquired_misses_detected", "miss_resolution_undetermined",
                       "miss_resolution_pending",
                       "miss_resolution_evidence_unreadable"):
            assert reason in src, reason

    def test_readiness_consumes_the_summary(self, tmp_path, monkeypatch):
        db = DatabaseManager(str(tmp_path / "miss-rule-consumer.db"))
        try:
            monkeypatch.setattr(
                DatabaseManager, "get_hdencode_miss_resolution",
                lambda self: {"acquired": 7, "never_acquired": 2,
                              "undetermined": 3, "not_yet_assessable": 4,
                              "blocking": 9, "worst_acquisition_lag_hours": 41.5,
                              "rows": [], "evidence_problems": ["bad:c1"]})
            out = db.get_hdencode_rss_readiness()
            assert "unacquired_misses_detected" in out["reasons"]
            assert "miss_resolution_undetermined" in out["reasons"]
            assert "miss_resolution_pending" in out["reasons"], (
                "pending must block; see the module docstring for why")
            assert "miss_resolution_evidence_unreadable" in out["reasons"]
            assert out["misses_acquired"] == 7
            assert out["misses_not_yet_assessable"] == 4
            assert out["worst_acquisition_lag_hours"] == 41.5
            assert out["miss_evidence_problems"] == ["bad:c1"]
        finally:
            db.close()

    def test_a_healthy_resolved_population_does_not_block_on_misses(
            self, tmp_path, monkeypatch):
        """POSITIVE CONTROL. Making pending block must not make everything block."""
        db = DatabaseManager(str(tmp_path / "miss-rule-clean.db"))
        try:
            monkeypatch.setattr(
                DatabaseManager, "get_hdencode_miss_resolution",
                lambda self: {"acquired": 60, "never_acquired": 0,
                              "undetermined": 0, "not_yet_assessable": 0,
                              "blocking": 0, "worst_acquisition_lag_hours": 3.2,
                              "rows": [], "evidence_problems": []})
            out = db.get_hdencode_rss_readiness()
            for reason in ("unacquired_misses_detected",
                           "miss_resolution_undetermined",
                           "miss_resolution_pending",
                           "miss_resolution_evidence_unreadable"):
                assert reason not in out["reasons"], reason
            assert out["misses_acquired"] == 60
        finally:
            db.close()

    def test_unreadable_cycle_evidence_is_reported_not_absorbed(self, tmp_path):
        """Skipping a malformed cycle can remove the only observation after a
        miss, which would quietly turn a decidable row into an unresolved one."""
        db = DatabaseManager(str(tmp_path / "miss-rule-malformed.db"))
        try:
            cyc = _seed_one_valid_cycle(db)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE hdencode_shadow_cycles SET details_json='{not json' "
                    "WHERE cycle_uuid=?", (cyc,))
            res = db.get_hdencode_miss_resolution()
            assert any("details_json_unparseable" in p
                       for p in res["evidence_problems"]), res["evidence_problems"]
        finally:
            db.close()

    def test_a_non_list_url_container_does_not_raise(self, tmp_path):
        """`set(5)` raises TypeError, which would take the whole readiness call
        down rather than degrade. Peer review found this path."""
        db = DatabaseManager(str(tmp_path / "miss-rule-badtype.db"))
        try:
            cyc = _seed_one_valid_cycle(db)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE hdencode_shadow_cycles "
                    "SET details_json='{\"listing_only\": 5, \"feed_only\": []}' "
                    "WHERE cycle_uuid=?", (cyc,))
            res = db.get_hdencode_miss_resolution()
            assert any("listing_only_not_a_list" in p
                       for p in res["evidence_problems"]), res["evidence_problems"]
        finally:
            db.close()
