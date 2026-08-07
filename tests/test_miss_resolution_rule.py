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
`test_a_very_late_acquisition_still_counts_as_acquired` pins that consequence
explicitly rather than leaving it implied, so nobody later reads the behaviour as
a bug.

WHAT IS DELIBERATELY NOT WIDENED. "Acquired" needs evidence and so does "never
acquired". A row that can be shown neither way is UNDETERMINED and still blocks.
Calling unprovable rows healthy is the fail-open shape that produced two HIGH
findings in this same subsystem, and the qualification grader already gates on
"0 RED, 0 PENDING and 0 AMBIGUOUS".
"""
from datetime import datetime, timedelta, timezone

from backend.hdencode_shadow import (
    classify_miss_resolution,
    summarise_miss_resolutions,
)

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
URL = "https://hdencode.org/some-release-2160p/"
OTHER = "https://hdencode.org/unrelated-1080p/"


def cycle(hours, *, listing_only=(), feed_only=()):
    return {"at": T0 + timedelta(hours=hours),
            "listing_only": set(listing_only),
            "feed_only": set(feed_only)}


class TestAcquisitionIsProvenByTheFeedTransition:

    def test_the_feed_picking_it_up_is_acquisition(self):
        state, hours, _ = classify_miss_resolution(
            URL, T0, [cycle(1, feed_only=[URL])])
        assert state == "acquired"
        assert hours == 1.0

    def test_a_very_late_acquisition_still_counts_as_acquired(self):
        """THE CONSEQUENCE OF THE CHOSEN RULE, pinned deliberately.

        72 hours late is still a success under "never acquired at all". This is
        the trade Jesse accepted over a 6-hour budget. If this ever needs to
        change, change the RULE here -- do not let it drift by accident.
        """
        state, hours, _ = classify_miss_resolution(
            URL, T0, [cycle(6, listing_only=[URL]), cycle(72, feed_only=[URL])])
        assert state == "acquired", (
            "under the chosen rule there is no deadline; only 'never' fails")
        assert hours == 72.0, "the lag is still measured so it can be reported"

    def test_earlier_cycles_are_ignored(self):
        """A sighting BEFORE the row was recorded says nothing about it."""
        state, _, _ = classify_miss_resolution(
            URL, T0, [cycle(-5, feed_only=[URL])])
        assert state != "acquired", (
            "a feed sighting predating the miss cannot resolve it; accepting it "
            "would let any historical presence excuse a later disappearance")

    def test_another_release_being_acquired_proves_nothing(self):
        state, _, _ = classify_miss_resolution(
            URL, T0, [cycle(2, feed_only=[OTHER], listing_only=[URL])])
        assert state == "never_acquired"


class TestNeverAcquiredRequiresEvidence:

    def test_still_missing_later_and_never_acquired_is_a_failure(self):
        state, hours, detail = classify_miss_resolution(
            URL, T0, [cycle(3, listing_only=[URL]),
                      cycle(9, listing_only=[URL])])
        assert state == "never_acquired"
        assert hours == 9.0
        assert "never acquired" in detail

    def test_vanishing_from_both_sides_is_undetermined_not_healthy(self):
        """THE FAIL-OPEN THIS REFUSES. The listing pages away over time, so a URL
        absent from both sides is not evidence of acquisition. Counting it as
        acquired would let ordinary listing churn manufacture a passing gate."""
        state, _, detail = classify_miss_resolution(
            URL, T0, [cycle(4, listing_only=[OTHER])])
        assert state == "undetermined", (
            "absence from both sides proves nothing in either direction")
        assert "neither" in detail

    def test_no_cycles_at_all_is_not_yet_assessable(self):
        """CHANGED DELIBERATELY 2026-08-07, not quietly relaxed.

        This originally asserted "undetermined" — which blocks. Measuring against
        live data showed that rule made the gate unpassable by construction: rows
        from the newest cycle can never have a later observation, and every poll
        can record one. See TestNotYetAssessableIsNotAFailure for the reasoning and
        for the assertions keeping the exclusion narrow.
        """
        state, hours, _ = classify_miss_resolution(URL, T0, [])
        assert state == "not_yet_assessable"
        assert hours == 0.0

    def test_acquisition_wins_over_an_earlier_still_missing_sighting(self):
        """Observed missing, then acquired: the acquisition is what matters."""
        state, hours, _ = classify_miss_resolution(
            URL, T0, [cycle(2, listing_only=[URL]),
                      cycle(5, feed_only=[URL]),
                      cycle(8, listing_only=[OTHER])])
        assert state == "acquired"
        assert hours == 5.0


class TestTheSummaryThatReadinessGatesOn:

    def test_only_unacquired_and_undetermined_block(self):
        cycles = [cycle(2, listing_only=["b", "c"]),
                  cycle(4, feed_only=["a"], listing_only=["c"]),
                  cycle(9, listing_only=["c"])]
        summary = summarise_miss_resolutions(
            [{"url": "a", "at": T0},    # acquired at 4h
             {"url": "c", "at": T0},    # still missing at 9h -> never acquired
             {"url": "d", "at": T0}],   # never seen again -> undetermined
            cycles)
        assert summary["acquired"] == 1
        assert summary["never_acquired"] == 1
        assert summary["undetermined"] == 1
        assert summary["blocking"] == 2, (
            "acquired must not block at any lag; the other two must")

    def test_an_all_acquired_population_is_clean(self):
        """POSITIVE CONTROL. This is the shape the live data is expected to have,
        and the whole reason for the rule change. If it cannot pass, the new rule
        is no better than the old one."""
        cycles = [cycle(1, feed_only=["a", "b"]), cycle(2, feed_only=["c"])]
        summary = summarise_miss_resolutions(
            [{"url": u, "at": T0} for u in ("a", "b", "c")], cycles)
        assert summary["acquired"] == 3
        assert summary["blocking"] == 0
        assert summary["worst_acquisition_lag_hours"] == 2.0

    def test_the_worst_lag_is_reported_even_though_it_does_not_gate(self):
        """The reservation is kept visible: the number survives even though the
        rule ignores it, so latency regression stays observable."""
        cycles = [cycle(1, feed_only=["a"]), cycle(100, feed_only=["b"])]
        summary = summarise_miss_resolutions(
            [{"url": "a", "at": T0}, {"url": "b", "at": T0}], cycles)
        assert summary["blocking"] == 0
        assert summary["worst_acquisition_lag_hours"] == 100.0, (
            "a 100-hour acquisition passes the gate by design, but it must not "
            "become invisible")

    def test_a_malformed_row_counts_as_undetermined_not_ignored(self):
        """Dropping unreadable rows is how a gate silently reports zero."""
        summary = summarise_miss_resolutions(
            [{"url": None, "at": T0}, {"url": "a"}, {}], [cycle(1)])
        assert summary["undetermined"] == 3
        assert summary["blocking"] == 3
        assert summary["acquired"] == 0

    def test_no_misses_at_all_is_clean(self):
        summary = summarise_miss_resolutions([], [cycle(1)])
        assert summary["blocking"] == 0
        assert summary["acquired"] == 0
        assert summary["rows"] == []


class TestReadinessActuallyUsesTheRule:
    """The wiring, not just the logic.

    Five times in this project I have proved a function correct and never checked
    that anything calls it. The old blocker was one line -- `relevant_misses > 0`
    -- so if that line survived, or if the new one were never reached, the rule
    would be decorative and every test above would still pass.
    """

    def test_the_old_unconditional_blocker_is_gone(self):
        import inspect

        from backend.database import DatabaseManager
        src = inspect.getsource(DatabaseManager.get_hdencode_rss_readiness)
        assert "relevant_misses_detected" not in src, (
            "the old rule still blocks on any miss at all, so the new "
            "classification cannot change the outcome")
        assert "unacquired_misses_detected" in src
        assert "miss_resolution_undetermined" in src

    def test_readiness_consumes_the_summary(self, tmp_path, monkeypatch):
        from backend import database as database_module
        from backend.database import DatabaseManager

        db = DatabaseManager(str(tmp_path / "miss-rule-consumer.db"))
        try:
            monkeypatch.setattr(
                database_module, "summarise_miss_resolutions", None,
                raising=False)
            monkeypatch.setattr(
                DatabaseManager, "get_hdencode_miss_resolution",
                lambda self: {"acquired": 7, "never_acquired": 2,
                              "undetermined": 3, "blocking": 5,
                              "worst_acquisition_lag_hours": 41.5, "rows": []})
            out = db.get_hdencode_rss_readiness()
            assert "unacquired_misses_detected" in out["reasons"], (
                "two never-acquired releases must block")
            assert "miss_resolution_undetermined" in out["reasons"], (
                "three undetermined releases must block; treating unprovable as "
                "healthy is the fail-open this rule refuses")
            assert out["misses_acquired"] == 7
            assert out["misses_never_acquired"] == 2
            assert out["misses_undetermined"] == 3
            assert out["worst_acquisition_lag_hours"] == 41.5, (
                "the lag must stay visible even though it no longer gates")
        finally:
            db.close()

    def test_an_all_acquired_population_does_not_block_on_misses(
            self, tmp_path, monkeypatch):
        """THE POINT OF THE CHANGE. Under the old rule this could never happen."""
        from backend.database import DatabaseManager

        db = DatabaseManager(str(tmp_path / "miss-rule-clean.db"))
        try:
            monkeypatch.setattr(
                DatabaseManager, "get_hdencode_miss_resolution",
                lambda self: {"acquired": 60, "never_acquired": 0,
                              "undetermined": 0, "blocking": 0,
                              "worst_acquisition_lag_hours": 3.2, "rows": []})
            out = db.get_hdencode_rss_readiness()
            assert "unacquired_misses_detected" not in out["reasons"]
            assert "miss_resolution_undetermined" not in out["reasons"]
            assert out["misses_acquired"] == 60
            # Other blockers (cycle counts, feed health) still apply on an empty
            # database -- this asserts only that MISSES stopped blocking.
        finally:
            db.close()


class TestNotYetAssessableIsNotAFailure:
    """The refinement measuring forced, and the line it must not cross.

    My first implementation returned "undetermined" whenever acquisition could
    not be proven -- including for rows recorded during the newest cycle, which by
    definition have no later observation. Live data: 62 acquired, 0 never
    acquired, 4 undetermined, and ALL FOUR were from the newest cycle, one hour
    old. Since any poll can record a miss, that would have left the gate
    permanently unpassable for a new reason instead of the old one.

    The split is between "unproven because no observation has happened yet" and
    "unproven because we observed and still cannot tell". The first is excluded;
    the second still blocks. Everything below exists to keep that line where it
    is, because widening it is how this subsystem produced two HIGH findings.
    """

    def test_a_row_with_no_later_cycle_is_not_yet_assessable(self):
        state, hours, detail = classify_miss_resolution(
            URL, T0, [cycle(-2, feed_only=[OTHER])])
        assert state == "not_yet_assessable", (
            "no completed observation exists after this row, so no resolution "
            "could have been seen; calling it a failure makes the gate "
            "unpassable by construction")
        assert hours == 0.0
        assert "yet" in detail

    def test_the_newest_cycle_case_from_the_live_data(self):
        """The exact shape of all four live rows: recorded AT the newest cycle."""
        newest = cycle(0)          # same instant as first_seen
        state, _, _ = classify_miss_resolution(URL, T0, [cycle(-5), newest])
        assert state == "not_yet_assessable"

    def test_one_later_cycle_makes_it_assessable_again(self):
        """The exclusion must be narrow: a SINGLE later observation is enough to
        put the row back under judgement."""
        state, _, _ = classify_miss_resolution(
            URL, T0, [cycle(1, listing_only=[OTHER])])
        assert state == "undetermined", (
            "once any later observation exists, an unprovable row must block "
            "again -- otherwise 'not yet assessable' becomes a permanent excuse")

    def test_a_later_cycle_that_still_shows_it_missing_is_a_real_failure(self):
        state, _, _ = classify_miss_resolution(
            URL, T0, [cycle(1, listing_only=[URL]), cycle(5, listing_only=[URL])])
        assert state == "never_acquired"

    def test_not_yet_assessable_does_not_count_toward_blocking(self):
        summary = summarise_miss_resolutions(
            [{"url": URL, "at": T0}], [cycle(-1)])
        assert summary["not_yet_assessable"] == 1
        assert summary["blocking"] == 0
        assert summary["undetermined"] == 0

    def test_it_is_still_reported_so_the_exclusion_is_visible(self):
        """A silent exclusion is indistinguishable from a bug."""
        summary = summarise_miss_resolutions(
            [{"url": URL, "at": T0}, {"url": OTHER, "at": T0}], [cycle(-1)])
        assert summary["not_yet_assessable"] == 2, (
            "the count must appear in the summary; if excluded rows vanish, a "
            "reader cannot tell a clean gate from a gate that dropped its "
            "evidence")

    def test_malformed_rows_are_still_undetermined_not_excused(self):
        """The new state must not become a dumping ground for bad data."""
        summary = summarise_miss_resolutions([{"url": None, "at": T0}], [cycle(-1)])
        assert summary["undetermined"] == 1
        assert summary["not_yet_assessable"] == 0
        assert summary["blocking"] == 1
